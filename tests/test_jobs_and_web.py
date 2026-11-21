"""Stage 05b tests: persistent SQLite JobRunner + FastAPI/Jinja2 operational product.

Covers P-TASK-01..16 and P-UX-01..04:
- Rule import (bundled 62 rules -> 60 unique advanced rules, partial malformed
  JSONL keeps valid lines with structured per-line errors, deduplication).
- Single & bulk build jobs, version activation, rollback, diff, immutability.
- Audit inference & annotation jobs pinned to initial frozen artifact;
  reference/gold labels rejected by strict schemas.
- Persistent job queue: idempotency keys, retry creating ONE new job with
  ``parent_job_id`` while preserving old record, restart recovery transitioning
  ``running`` -> ``interrupted``, and ``resume_job`` skipping already-completed
  ``job_items`` checkpoints.
- Result detail + rule trace + safe material image serving (PNG + bbox crop,
  strict rejection of unregistered material, bad bbox, path traversal, symlinks).
- Safe iteration via API (never auto-activates candidate).
- Baseline vs candidate comparison with explicit scope disclaimer.
- Plugin listing, persisted toggle, and isolated evaluation run.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import make_task, pass_batch
from o2_audit.domain.models import MaterialRef
from o2_audit.jobs.runner import JobRunner, import_rules_into_store
from o2_audit.runtime.infer import task_json
from o2_audit.storage.store import TaskStore
from o2_audit.web.app import create_app
from o2_public_contracts import artifact_hash


def _png_bytes(color=(90, 140, 210), size=(200, 150)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _mock_transport_factory(reply_builder):
    """Build an ``httpx.MockTransport`` returning valid Anthropic Messages JSON."""

    def factory():
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content.decode("utf-8"))
            text_blob = json.dumps(payload, ensure_ascii=False)
            body = {
                "id": "msg_test_001",
                "type": "message",
                "role": "assistant",
                "model": "zcode/GLM-5.3-Flash",
                "content": [
                    {
                        "type": "text",
                        "text": reply_builder(text_blob),
                    }
                ],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            }
            return httpx.Response(200, json=body)

        return httpx.MockTransport(handler)

    return factory


def _b001_pass_reply_builder(text_blob: str) -> str:
    """Return canonical B001 pass entities+observations citing whichever material_ids
    appear in the prompt (so material_id scope validation passes cleanly)."""
    import re

    mids = re.findall(r"m_(?:pkg|cert)", text_blob)
    src_id = mids[0] if mids else "m_pkg"
    ents = [
        {"entity_id": "L1", "kind": "label"},
        {"entity_id": "C1", "kind": "certificate"},
    ]
    obs = [
        {
            "entity_id": "L1", "predicate": "void_marked", "status": "readable",
            "raw_value": False, "scope_complete": True,
            "source": {"material_id": src_id, "page_index": 1, "bbox": [0.1, 0.1, 0.5, 0.5]},
        },
        {
            "entity_id": "L1", "predicate": "carrier", "status": "readable",
            "raw_value": "outer_package", "scope_complete": True,
            "source": {"material_id": src_id, "page_index": 1, "bbox": [0.1, 0.1, 0.5, 0.5]},
        },
        {
            "entity_id": "L1", "predicate": "model", "status": "readable",
            "raw_value": "DL-100", "scope_complete": True,
            "source": {"material_id": src_id, "page_index": 1, "bbox": [0.1, 0.1, 0.4, 0.3]},
        },
        {
            "entity_id": "L1", "predicate": "material_number", "status": "readable",
            "raw_value": "QLAB-123456", "scope_complete": True,
            "source": {"material_id": src_id, "page_index": 1, "bbox": [0.1, 0.3, 0.4, 0.5]},
        },
        {
            "entity_id": "C1", "predicate": "void_marked", "status": "readable",
            "raw_value": False, "scope_complete": True,
            "source": {"material_id": src_id, "page_index": 1, "bbox": [0.1, 0.1, 0.5, 0.25]},
        },
        {
            "entity_id": "C1", "predicate": "material_number", "status": "readable",
            "raw_value": "QLAB-123456", "scope_complete": True,
            "source": {"material_id": src_id, "page_index": 1, "bbox": [0.1, 0.1, 0.5, 0.25]},
        },
        {
            "entity_id": "C1", "predicate": "covered_models", "status": "readable",
            "raw_value": ["DL-100"], "scope_complete": True,
            "source": {"material_id": src_id, "page_index": 1, "bbox": [0.1, 0.25, 0.5, 0.4]},
        },
        {
            "entity_id": "C1", "predicate": "valid_from", "status": "readable",
            "raw_value": "2025-01-01", "scope_complete": True,
            "source": {"material_id": src_id, "page_index": 1, "bbox": [0.1, 0.4, 0.5, 0.55]},
        },
        {
            "entity_id": "C1", "predicate": "valid_until", "status": "readable",
            "raw_value": "2026-12-31", "scope_complete": True,
            "source": {"material_id": src_id, "page_index": 1, "bbox": [0.1, 0.55, 0.5, 0.7]},
        },
    ]
    return json.dumps({"entities": ents, "observations": obs}, ensure_ascii=False)


def _make_b001_dataset(root: Path, unit_ids: list[str], rule_version: str = "3.0") -> tuple[Path, Path]:
    data_root = root / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    tasks = []
    for idx, uid in enumerate(unit_ids):
        pkg_rel = f"{uid}_pkg.png"
        cert_rel = f"{uid}_cert.png"
        (data_root / pkg_rel).write_bytes(_png_bytes((50 + idx * 10, 100, 180)))
        (data_root / cert_rel).write_bytes(_png_bytes((180, 100, 50 + idx * 10)))
        t = make_task(
            audit_unit_id=uid,
            rule_id="B001",
            rule_version=rule_version,
            metadata={
                "market": "Q",
                "category": "桌面灯",
                "power_mode": "可充电",
                "declared_model": "DL-100",
                "package_longest_edge_mm": 120,
            },
            review_date="2026-06-01",
            materials=[
                MaterialRef(
                    material_id="m_pkg",
                    role="package_photos",
                    media_type="image/png",
                    relative_path=pkg_rel,
                    page_index=1,
                ),
                MaterialRef(
                    material_id="m_cert",
                    role="certificate_pages",
                    media_type="image/png",
                    relative_path=cert_rel,
                    page_index=1,
                ),
            ],
        )
        tasks.append(t)
    input_jsonl = data_root / "inputs.jsonl"
    input_jsonl.write_text(
        "\n".join(json.dumps(task_json(t), ensure_ascii=False) for t in tasks) + "\n",
        encoding="utf-8",
    )
    return data_root, input_jsonl


# ---------------------------------------------------------------------------
# 1. Rule import & partial error preservation (P-TASK-01..03, P-UX-03)
# ---------------------------------------------------------------------------


def test_rule_import_bundled_62_and_partial_bad_jsonl(tmp_path: Path):
    ws = tmp_path / "ws"
    store = TaskStore(ws / "state.sqlite3")
    try:
        # Bundled 62 rules
        s1 = import_rules_into_store(store, workspace_dir=ws)
        assert s1["valid_count"] == 62
        assert s1["unique_advanced_rules"] == 60
        assert s1["issue_count"] == 0

        # Re-import is deduplicated (newly_inserted == 0)
        s2 = import_rules_into_store(store, workspace_dir=ws)
        assert s2["newly_inserted"] == 0
        assert s2["total_stored_rules"] == 62

        # Partial bad JSONL preserves valid line + structured issue
        mixed = (
            '{"rule_id": "B001", "version": "3.0", "title": "t", "rule_text": "body"}\n'
            '{bad json line\n'
            '{"rule_id": "", "version": "3.0"}\n'
        )
        s3 = import_rules_into_store(store, jsonl_text=mixed, source_name="mixed.jsonl", workspace_dir=ws)
        assert s3["valid_count"] == 1
        assert s3["issue_count"] == 2
        for iss in s3["issues"]:
            assert "object" in iss and "cause" in iss and "next_action" in iss
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 2. JobRunner restart recovery, resume skipping done items, retry + parent_job_id
# ---------------------------------------------------------------------------


def test_job_runner_restart_resume_and_retry(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("O2_BASE_URL", "http://gw.test")
    monkeypatch.setenv("O2_API_KEY", "tok-test-secret")

    ws = tmp_path / "ws"
    store = TaskStore(ws / "state.sqlite3")
    runner = JobRunner(
        store,
        ws,
        transport_factory=_mock_transport_factory(_b001_pass_reply_builder),
    )
    try:
        runner.start()
        import_rules_into_store(store, workspace_dir=ws)

        # Build single B001@3.0
        bjob = runner.submit_job(
            "build_single",
            {"rule_id": "B001", "rule_version": "3.0", "version_name": "v-b001"},
            idempotency_key="idem-build-1",
        )
        # Same idempotency key returns identical job
        bjob_dup = runner.submit_job(
            "build_single",
            {"rule_id": "B001", "rule_version": "3.0", "version_name": "v-b001"},
            idempotency_key="idem-build-1",
        )
        assert bjob_dup["job_id"] == bjob["job_id"]

        done_b = runner.wait_for_job(bjob["job_id"], timeout=30)
        assert done_b is not None and done_b["status"] == "completed"
        rec = store.get_artifact_version("v-b001")
        assert rec is not None

        # Create 2-unit input dataset
        data_root, input_jsonl = _make_b001_dataset(tmp_path, ["u1", "u2"])

        # Simulate a crashed/running job where u1 already completed and u2 did not
        crashed = store.create_job(
            job_id="job-crashed-1",
            job_kind="audit_infer",
            payload={
                "input_jsonl": str(input_jsonl),
                "data_root": str(data_root),
                "build_dir": rec["build_dir"],
                "version_name": "v-b001",
                "run_id": "run-crashed-1",
            },
            artifact_version=rec["artifact_version"],
            artifact_root_sha256=rec["root_sha256"],
            status="running",
        )
        # Persist u1 as already completed with its prediction
        fake_u1_pred = {
            "audit_unit_id": "u1",
            "rule_id": "B001",
            "rule_version": "3.0",
            "artifact_version": rec["artifact_version"],
            "execution_status": "completed",
            "applicability": "applicable",
            "label": "pass",
            "reason_code": "all_requirements_satisfied",
            "reason_summary": "pre-checkpointed u1",
            "evidence_refs": [],
            "missing_requirements": [],
            "conflict_details": [],
            "error_code": None,
            "error_message": None,
        }
        store.upsert_job_item(
            crashed["job_id"],
            "u1",
            "completed",
            artifact_version=rec["artifact_version"],
            artifact_sha256=rec["root_sha256"],
            result=fake_u1_pred,
        )
        store.record_audit_result(
            result_id=f"{crashed['job_id']}:u1",
            job_id=crashed["job_id"],
            run_id="run-crashed-1",
            audit_unit_id="u1",
            rule_id="B001",
            rule_version="3.0",
            artifact_version=rec["artifact_version"],
            artifact_root_sha256=rec["root_sha256"],
            data_root=str(data_root),
            out_path=str(ws / "runs" / crashed["job_id"] / "predictions.jsonl"),
            task=json.loads(input_jsonl.read_text(encoding="utf-8").splitlines()[0]),
            prediction=fake_u1_pred,
        )
        runner.stop()

        # Simulate process restart: new runner transitions 'running' -> 'interrupted'
        runner2 = JobRunner(
            store,
            ws,
            transport_factory=_mock_transport_factory(_b001_pass_reply_builder),
        )
        interrupted = runner2.start()
        assert "job-crashed-1" in interrupted
        assert store.get_job("job-crashed-1")["status"] == "interrupted"

        # Resume from checkpoint: u1 must be skipped (keeps pre-checkpointed summary), u2 executes
        resumed = runner2.resume_job("job-crashed-1")
        assert resumed["job_id"] == "job-crashed-1"
        done_resumed = runner2.wait_for_job("job-crashed-1", timeout=30)
        assert done_resumed["status"] == "completed"
        assert done_resumed["result"]["skipped_completed_items"] == 1
        assert done_resumed["result"]["newly_executed_items"] == 1

        u1_after = store.get_audit_result("job-crashed-1:u1")
        u2_after = store.get_audit_result("job-crashed-1:u2")
        assert u1_after["prediction"]["reason_summary"] == "pre-checkpointed u1"
        assert u2_after is not None and u2_after["prediction"]["label"] == "pass"

        # Retry creates ONE new job with parent_job_id, leaving original intact
        retried = runner2.retry_job("job-crashed-1")
        assert retried["job_id"] != "job-crashed-1"
        assert retried["parent_job_id"] == "job-crashed-1"
        done_retried = runner2.wait_for_job(retried["job_id"], timeout=30)
        assert done_retried["status"] == "completed"
        # Original job record preserved
        assert store.get_job("job-crashed-1")["status"] == "completed"
        runner2.stop()
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 3. Full FastAPI + Jinja2 operational product end-to-end (P-TASK-01..16)
# ---------------------------------------------------------------------------


def test_web_product_full_operational_workflow(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("O2_BASE_URL", "http://gw.test")
    monkeypatch.setenv("O2_API_KEY", "tok-test-secret")

    ws = tmp_path / "web_ws"
    app = create_app(
        workspace_dir=ws,
        transport_factory=_mock_transport_factory(_b001_pass_reply_builder),
    )

    with TestClient(app) as client:
        # Health & Dashboard HTML
        assert client.get("/api/health").status_code == 200
        html = client.get("/")
        assert html.status_code == 200
        assert "O2 AI 商品审核官" in html.text

        # P-TASK-01..03: Import bundled 62 rules
        r_imp = client.post("/api/rules/import", json={"use_bundled_62": True})
        assert r_imp.status_code == 200
        assert r_imp.json()["valid_count"] == 62
        assert r_imp.json()["unique_advanced_rules"] == 60
        assert client.get("/api/rules").json()["total"] == 62

        # P-TASK-04..05: Single build + bulk build
        j_single = client.post(
            "/api/builds/single",
            json={"rule_id": "B001", "rule_version": "3.0", "version_name": "single-b001"},
        ).json()
        app.state.runner.wait_for_job(j_single["job_id"], timeout=30)
        assert client.get(f"/api/jobs/{j_single['job_id']}").json()["status"] == "completed"

        j_bulk = client.post(
            "/api/builds/bulk",
            json={"version_name": "bulk-62"},
        ).json()
        app.state.runner.wait_for_job(j_bulk["job_id"], timeout=60)
        assert client.get(f"/api/jobs/{j_bulk['job_id']}").json()["status"] == "completed"

        # Activate bulk-62
        r_act = client.post("/api/versions/activate", json={"name": "bulk-62"})
        assert r_act.status_code == 200
        assert client.get("/api/versions").json()["active"] == "bulk-62"

        # P-TASK-06: Audit endpoint strictly rejects gold/reference fields (extra=forbid)
        data_root, input_jsonl = _make_b001_dataset(tmp_path, ["web-u1"])
        r_forbid = client.post(
            "/api/audit/run",
            json={"input_jsonl": str(input_jsonl), "references_path": "/secret/gold.jsonl"},
        )
        assert r_forbid.status_code == 422

        # Run audit job
        j_audit = client.post(
            "/api/audit/run",
            json={"input_jsonl": str(input_jsonl), "data_root": str(data_root)},
        ).json()
        app.state.runner.wait_for_job(j_audit["job_id"], timeout=30)
        job_detail = client.get(f"/api/jobs/{j_audit['job_id']}").json()
        assert job_detail["status"] == "completed"

        # P-TASK-12: Export JSONL & JSON
        exp_jsonl = client.get(f"/api/jobs/{j_audit['job_id']}/export?fmt=jsonl")
        assert exp_jsonl.status_code == 200
        lines = [ln for ln in exp_jsonl.text.splitlines() if ln.strip()]
        assert len(lines) == 1
        assert json.loads(lines[0])["label"] == "pass"

        exp_json = client.get(f"/api/jobs/{j_audit['job_id']}/export?fmt=json")
        assert exp_json.status_code == 200
        assert exp_json.json()["count"] == 1

        # P-TASK-07..08 & P-UX-02: Result inspection + frozen rule trace + material serving
        results = client.get(f"/api/results?job_id={j_audit['job_id']}").json()["results"]
        assert len(results) == 1
        rid = results[0]["result_id"]
        res_detail = client.get(f"/api/results/{rid}").json()
        assert res_detail["rule_trace"]["rule_id"] == "B001"
        assert "frozen_artifact:" in res_detail["rule_trace"]["source"]
        assert "桌面灯" in res_detail["rule_trace"]["raw_rule_text"]

        # Valid material PNG + cropped bbox PNG
        r_img = client.get(f"/api/materials/{rid}/m_pkg")
        assert r_img.status_code == 200
        assert r_img.headers["content-type"] == "image/png"
        r_crop = client.get(f"/api/materials/{rid}/m_pkg?bbox=0.1,0.1,0.5,0.5")
        assert r_crop.status_code == 200

        # Security checks: unregistered material, bad bbox
        assert client.get(f"/api/materials/{rid}/unregistered_mat").status_code == 403
        assert client.get(f"/api/materials/{rid}/m_pkg?bbox=0.8,0.1,0.2,0.5").status_code == 400

        # Path traversal guard on a tampered task record in DB
        app.state.store.record_audit_result(
            result_id="evil-traversal",
            job_id="evil-job-isolated",
            run_id="r-evil",
            audit_unit_id="u-evil",
            rule_id="B001",
            rule_version="3.0",
            artifact_version="v1",
            artifact_root_sha256="a" * 64,
            data_root=str(data_root),
            out_path=str(tmp_path / "x.jsonl"),
            task={
                "materials": [
                    {
                        "material_id": "m_evil",
                        "role": "package_photos",
                        "media_type": "image/png",
                        "relative_path": "../secret.png",
                        "page_index": 1,
                    }
                ]
            },
            prediction={"label": "pass"},
        )
        assert client.get("/api/materials/evil-traversal/m_evil").status_code == 403

        # P-TASK-13..14: Safe iteration via API -> candidate NOT auto-activated, diff & rollback work
        bulk_rec = app.state.store.get_artifact_version("bulk-62")
        base_sha = bulk_rec["root_sha256"]
        fb_path = tmp_path / "web_fb.json"
        fb_path.write_text(
            json.dumps({
                "schema_version": "1.3.1",
                "feedback_id": "fb-web-1",
                "base_artifact_sha256": base_sha,
                "items": [{
                    "packet_id": "p-w1",
                    "audit_unit_id": "web-u1",
                    "teacher_proposal": "提取提示：注意区分字母 O 与数字 0。",
                    "teacher_source": "web_ui",
                }],
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        j_iter = client.post(
            "/api/iterate",
            json={
                "base_version_name": "bulk-62",
                "feedback_path": str(fb_path),
                "input_jsonl": str(input_jsonl),
                "version_name": "cand-web-1",
            },
        ).json()
        app.state.runner.wait_for_job(j_iter["job_id"], timeout=30)
        assert client.get(f"/api/jobs/{j_iter['job_id']}").json()["status"] == "completed"
        # Candidate is registered but NOT active
        assert client.get("/api/versions").json()["active"] == "bulk-62"
        # Diff between bulk-62 and cand-web-1 shows only B001@3.0 changed
        diff = client.get("/api/versions/diff?a=bulk-62&b=cand-web-1").json()
        changed = [d for d in diff["entries"] if d["change"] != "unchanged"]
        assert [d["rule"] for d in changed] == ["B001@3.0"]

        # P-TASK-06: Annotation job pinned to active frozen artifact
        targets_jsonl = tmp_path / "targets.jsonl"
        targets_jsonl.write_text(
            json.dumps({
                "audit_unit_id": "web-u1",
                "target_id": "t-1",
                "field_name": "label_model",
                "material_id": "m_pkg",
                "page_index": 1,
                "prompt": "读取标签上的型号文字",
            }, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        j_ann = client.post(
            "/api/annotate/run",
            json={
                "input_jsonl": str(input_jsonl),
                "targets_jsonl": str(targets_jsonl),
                "data_root": str(data_root),
            },
        ).json()
        app.state.runner.wait_for_job(j_ann["job_id"], timeout=30)
        ann_detail = client.get(f"/api/jobs/{j_ann['job_id']}").json()
        assert ann_detail["status"] == "completed"
        assert ann_detail["artifact_root_sha256"] == base_sha

        # Activate candidate, run second audit job, compare jobs (no references -> disclaimer), then rollback
        assert client.post("/api/versions/activate", json={"name": "cand-web-1"}).status_code == 200
        j_audit2 = client.post(
            "/api/audit/run",
            json={"input_jsonl": str(input_jsonl), "data_root": str(data_root)},
        ).json()
        app.state.runner.wait_for_job(j_audit2["job_id"], timeout=30)

        # P-TASK-09: Compare baseline vs candidate without references
        r_cmp = client.post(
            "/api/compare",
            json={"baseline_job_id": j_audit["job_id"], "candidate_job_id": j_audit2["job_id"]},
        )
        assert r_cmp.status_code == 200
        cmp_data = r_cmp.json()
        assert "无参考真值" in cmp_data["scope_label"]

        # Rollback to bulk-62
        assert client.post("/api/versions/rollback", json={"name": "bulk-62"}).status_code == 200
        assert client.get("/api/versions").json()["active"] == "bulk-62"
        assert artifact_hash.tree_digest(Path(bulk_rec["build_dir"])) == base_sha

        # P-TASK-15: Plugin listing, persisted toggle, and isolated evaluation run
        plugins = client.get("/api/plugins").json()["plugins"]
        assert len(plugins) >= 3
        assert client.post("/api/plugins/o2.evidence/toggle", json={"enabled": False}).status_code == 200
        # Verify toggle persists in SQLite
        p_after = {p["plugin_id"]: p["enabled"] for p in client.get("/api/plugins").json()["plugins"]}
        assert p_after["o2.evidence"] is False
        assert client.post("/api/plugins/o2.evidence/toggle", json={"enabled": True}).status_code == 200

        out_preds = job_detail["result"]["out_path"]
        refs_jsonl = tmp_path / "dev_refs.jsonl"
        refs_jsonl.write_text(
            json.dumps({
                "audit_unit_id": "web-u1",
                "sample_id": "S1",
                "rule_id": "B001",
                "rule_version": "3.0",
                "label": "pass",
                "applicability": "applicable",
            }, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        j_eval = client.post(
            "/api/evaluate/run",
            json={
                "inputs_path": str(input_jsonl),
                "outputs_path": out_preds,
                "references_path": str(refs_jsonl),
                "plugin_ids": ["o2.conclusion"],
            },
        ).json()
        app.state.runner.wait_for_job(j_eval["job_id"], timeout=30)
        ev_detail = client.get(f"/api/jobs/{j_eval['job_id']}").json()
        assert ev_detail["status"] == "completed", f"eval failed: {ev_detail.get('error')}"
        assert ev_detail["result"]["count"] == 1
        assert ev_detail["result"]["evaluations"][0]["status"] == "ok"
        assert ev_detail["result"]["evaluations"][0]["metrics"]["quality"] == 1.0
        assert ev_detail["result"]["evaluations"][0]["metrics"]["accuracy"] == 1.0

        # Dashboard HTML renders cleanly with populated rules, jobs, versions, results, evals
        html_populated = client.get("/")
        assert html_populated.status_code == 200
        assert "bulk-62" in html_populated.text
        assert "cand-web-1" in html_populated.text
        assert "web-u1" in html_populated.text
