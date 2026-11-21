"""Stage 03b tests: batch inference runner, target-directed annotation and the
``o2-audit infer`` / ``annotate`` CLI.

All model traffic goes through ``httpx.MockTransport`` (clearly separate from
real visual recognition per AGENTS.md). Frozen artifacts are built into
``tmp_path``; SQLite state and traces live in ``tmp_path`` workspace dirs —
never inside the frozen BUILD_DIR, whose whole-directory digest is verified
byte-identical before/after every run. No reference/gold file is read by any
production code path under test.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from pathlib import Path

import httpx
import pytest
from PIL import Image

from conftest import make_task, pass_batch
from o2_audit.build.compiler import compile_build
from o2_audit.cli import main as cli_main
from o2_audit.domain.models import AuditInput, MaterialRef
from o2_public_contracts import artifact_hash
from o2_public_contracts import contracts as public_contracts
from o2_audit.protocol.checks import annotation_semantic_errors, prediction_semantic_errors
from o2_audit.runtime.annotate import run_annotation
from o2_audit.runtime.infer import build_gateway, run_inference, task_json
from o2_audit.storage.store import TaskStore

ARTIFACT_VERSION = "stage03b-test-v1"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> TaskStore:
    s = TaskStore(tmp_path / "ws" / "state.sqlite3")
    yield s
    s.close()


@pytest.fixture()
def build_dir(tmp_path: Path) -> Path:
    """Frozen artifact with B001@3.0/3.1 only (fast; the tests use B001)."""
    out = compile_build(
        rules_jsonl=Path(__file__).resolve().parents[1] / "resources" / "rules"
        / "基础规则版本.jsonl",
        config={"artifact_version": ARTIFACT_VERSION},
        out_dir=tmp_path / "build",
        artifact_version=ARTIFACT_VERSION,
    )
    return out.build_dir


@pytest.fixture()
def gw_env(monkeypatch):
    """Model credentials present in the environment (values are test fakes)."""
    monkeypatch.setenv("O2_BASE_URL", "http://gw.test")
    monkeypatch.setenv("O2_API_KEY", "tok-test-secret")
    return monkeypatch


@pytest.fixture()
def no_model_env(monkeypatch):
    """No model config anywhere: missing-credential failures become error rows."""
    for name in ("O2_BASE_URL", "O2_API_KEY",
                 "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def make_png_bytes(size=(320, 240), color=(120, 90, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _color_for(name: str) -> tuple[int, int, int]:
    """Distinct deterministic color per asset path: every unit's material
    content is unique, so the content-addressed extraction cache never bridges
    two different units in these tests."""
    h = int(hashlib.sha256(name.encode()).hexdigest()[:6], 16)
    return (h % 256, (h >> 8) % 256, (h >> 16) % 256)


def write_task_inputs(tmp_path: Path, tasks: list[AuditInput]) -> Path:
    """Write INPUT_JSONL + real PNG assets (unique bytes per material)."""
    inputs = tmp_path / "inputs"
    (inputs / "assets").mkdir(parents=True, exist_ok=True)
    for t in tasks:
        for m in t.materials:
            path = inputs / m.relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(make_png_bytes(color=_color_for(m.relative_path)))
    rows = [task_json(t) for t in tasks]
    inp = inputs / "audit_inputs.jsonl"
    inp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                   encoding="utf-8")
    return inp


def task_with_assets(unit_id: str, **meta) -> AuditInput:
    """B001@3.0 task with unit-scoped material IDs and real files under assets/
    (distinct IDs so the mock handler can attribute observe calls per unit)."""
    mid = lambda n: f"{unit_id}-{n}"  # noqa: E731
    return make_task(
        audit_unit_id=unit_id,
        metadata=meta,
        materials=[
            MaterialRef(material_id=mid("m1"), role="package_photos",
                        relative_path=f"assets/{unit_id}/m1.png",
                        media_type="image/png", page_index=1),
            MaterialRef(material_id=mid("m2"), role="tag_photos",
                        relative_path=f"assets/{unit_id}/m2.png",
                        media_type="image/png", page_index=1),
            MaterialRef(material_id=mid("m3"), role="certificate_pages",
                        relative_path=f"assets/{unit_id}/m3.png",
                        media_type="image/png", page_index=1),
        ],
    )


def ok_response(text: str) -> httpx.Response:
    return httpx.Response(200, json={
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 42, "output_tokens": 7},
    })


def observation_reply(cited_ids: list[str]) -> str:
    """Model-shaped observation JSON echoing the fully-satisfied B001 graph."""
    ents = [{"entity_id": "L1", "kind": "label"},
            {"entity_id": "C1", "kind": "certificate"}]
    obs = [
        {"entity_id": "L1", "predicate": "void_marked", "status": "readable",
         "raw_value": False, "scope_complete": True,
         "source": {"material_id": cited_ids[0], "page_index": 1}},
        {"entity_id": "L1", "predicate": "model", "status": "readable",
         "raw_value": "LED-100", "scope_complete": True,
         "source": {"material_id": cited_ids[0], "page_index": 1}},
        {"entity_id": "L1", "predicate": "material_number", "status": "readable",
         "raw_value": "QLAB-123456", "scope_complete": True,
         "source": {"material_id": cited_ids[0], "page_index": 1}},
        {"entity_id": "L1", "predicate": "carrier", "status": "readable",
         "raw_value": "attached_tag", "scope_complete": True,
         "source": {"material_id": cited_ids[0], "page_index": 1}},
        {"entity_id": "L1", "predicate": "attached_to_package", "status": "readable",
         "raw_value": True, "scope_complete": True,
         "source": {"material_id": cited_ids[0], "page_index": 1}},
        {"entity_id": "C1", "predicate": "void_marked", "status": "readable",
         "raw_value": False, "scope_complete": True,
         "source": {"material_id": cited_ids[0], "page_index": 1}},
        {"entity_id": "C1", "predicate": "material_number", "status": "readable",
         "raw_value": "QLAB-123456", "scope_complete": True,
         "source": {"material_id": cited_ids[0], "page_index": 1}},
        {"entity_id": "C1", "predicate": "covered_models", "status": "readable",
         "raw_value": ["LED-100"], "scope_complete": True,
         "source": {"material_id": cited_ids[0], "page_index": 1}},
        {"entity_id": "C1", "predicate": "valid_from", "status": "readable",
         "raw_value": "2026-01-01", "scope_complete": True,
         "source": {"material_id": cited_ids[0], "page_index": 1}},
        {"entity_id": "C1", "predicate": "valid_until", "status": "readable",
         "raw_value": "2026-12-31", "scope_complete": True,
         "source": {"material_id": cited_ids[0], "page_index": 1}},
    ]
    return json.dumps({"entities": ents, "observations": obs}, ensure_ascii=False)


def voided_observation_reply(cited_ids: list[str]) -> str:
    reply = json.loads(observation_reply(cited_ids))
    for o in reply["observations"]:
        if o["entity_id"] == "L1" and o["predicate"] == "void_marked":
            o["raw_value"] = True
    return json.dumps(reply, ensure_ascii=False)


def annotation_reply(text: str, status: str = "value",
                     bbox=None) -> str:
    return json.dumps({"text": text, "status": status,
                       "bbox": bbox if bbox is not None else [0.1, 0.4, 0.6, 0.6]},
                      ensure_ascii=False)


def make_transport(tasks_by_unit: dict[str, AuditInput], *, calls: dict | None = None,
                   annotation_text: str = "型号：ＡＢ-１", annotation_status: str = "value",
                   annotation_by_keyword: dict[str, tuple[str, str]] | None = None,
                   fail_units: set[str] | None = None):
    """httpx.MockTransport handler: observation rounds for observe(), verbatim
    annotation rounds for annotate_target().

    ``annotation_by_keyword`` maps a prompt substring -> (text, status) so
    different targets on the same run can get different extraction results.
    """
    calls = calls if calls is not None else {}
    calls["observe"] = []
    calls["annotate"] = []
    fail_units = fail_units or set()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        prompt = body["messages"][0]["content"][0]["text"]
        if "提取目标" in prompt:
            cited = re.findall(r'"material_id":\s*"([^"]+)"', prompt)
            calls["annotate"].append(cited[0] if cited else "?")
            text, status = annotation_text, annotation_status
            if annotation_by_keyword:
                for keyword, (t, s) in annotation_by_keyword.items():
                    if keyword in prompt:
                        text, status = t, s
                        break
            return ok_response(annotation_reply(text, status))
        # observation round: find which unit's materials are cited
        cited = re.findall(r'"material_id":\s*"([^"]+)"', prompt)
        for uid, task in tasks_by_unit.items():
            unit_mids = {m.material_id for m in task.materials}
            if unit_mids & set(cited):
                calls["observe"].append(uid)
                if uid in fail_units:
                    return httpx.Response(500, text="upstream exploded")
                return ok_response(observation_reply(cited))
        return ok_response(observation_reply(cited))

    transport = httpx.MockTransport(handler)
    transport.calls = calls  # type: ignore[attr-defined]  # test accounting
    return transport


def patch_cli_gateway(monkeypatch, module, test_transport) -> None:
    """Force the test transport into a module's ``build_gateway`` regardless of
    the (CLI-provided, always-None) transport argument."""
    monkeypatch.setattr(
        module, "build_gateway",
        lambda config, data_root, cache, transport=None:
        build_gateway(config, data_root, cache, transport=test_transport))


def run_infer_fixture(tmp_path: Path, build_dir: Path, tasks: list[AuditInput],
                      *, transport: httpx.BaseTransport, workspace_name="ws",
                      run_id=None, out_name="predictions.jsonl",
                      data_root: Path | None = None, model_config=None):
    inp = write_task_inputs(tmp_path, tasks)
    return run_inference(
        input_jsonl=inp, artifacts_dir=build_dir,
        out_path=tmp_path / "out" / out_name,
        data_root=data_root if data_root is not None else inp.parent,
        model_config_path=model_config,
        workspace=tmp_path / workspace_name,
        run_id=run_id,
        transport=transport,
    )


# ---------------------------------------------------------------------------
# infer: end-to-end
# ---------------------------------------------------------------------------

class TestInferEndToEnd:
    def test_full_protocol_validation_api(self, tmp_path, build_dir, gw_env):
        tasks = [task_with_assets("u-1"), task_with_assets("u-2")]
        tasks_by_unit = {t.audit_unit_id: t for t in tasks}
        transport = make_transport(tasks_by_unit)
        result = run_infer_fixture(tmp_path, build_dir, tasks, transport=transport)

        assert result.completed == 2 and result.errors == 0
        assert [r["audit_unit_id"] for r in result.predictions] == ["u-1", "u-2"]
        for row, task in zip(result.predictions, tasks):
            assert row["execution_status"] == "completed"
            assert row["label"] == "pass"
            assert row["artifact_version"] == ARTIFACT_VERSION
            # public contract + local semantic checks both clean
            assert public_contracts.prediction_errors(row, task_json(task)) == []
            assert prediction_semantic_errors(row, task) == []
        # output file on disk matches the returned rows
        lines = result.out_path.read_text(encoding="utf-8").splitlines()
        assert [json.loads(l)["audit_unit_id"] for l in lines] == ["u-1", "u-2"]
        # trace written OUTSIDE the artifact
        assert result.trace_path.exists() and "build" not in str(result.trace_path)

    def test_infer_cli_end_to_end(self, tmp_path, build_dir, capsys, gw_env,
                                  monkeypatch):
        tasks = [task_with_assets("u-1")]
        tasks_by_unit = {t.audit_unit_id: t for t in tasks}
        transport = make_transport(tasks_by_unit)
        # inject the test transport into the CLI's gateway construction
        import o2_audit.runtime.infer as infer_mod

        patch_cli_gateway(monkeypatch, infer_mod, transport)

        inp = write_task_inputs(tmp_path, tasks)
        rc = cli_main([
            "--workspace", str(tmp_path / "ws"),
            "infer",
            "--input", str(inp),
            "--artifacts", str(build_dir),
            "--out", str(tmp_path / "out" / "pred.jsonl"),
            "--run-id", "cli-run-1",
        ])
        assert rc == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary["units"] == 1 and summary["completed"] == 1
        rows = [json.loads(l) for l in
                (tmp_path / "out" / "pred.jsonl").read_text(encoding="utf-8").splitlines()]
        assert rows[0]["execution_status"] == "completed"
        assert public_contracts.prediction_errors(rows[0], task_json(tasks[0])) == []
        assert prediction_semantic_errors(rows[0], tasks[0]) == []

    def test_missing_model_config_yields_error_rows_retained(self, tmp_path, build_dir,
                                                             no_model_env):
        tasks = [task_with_assets("u-1"), task_with_assets("u-2")]

        def no_http(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no HTTP call is possible without credentials")

        result = run_infer_fixture(tmp_path, build_dir, tasks,
                                   transport=httpx.MockTransport(no_http))
        assert len(result.predictions) == 2
        for row in result.predictions:
            assert row["execution_status"] == "error"
            assert row["label"] is None
            assert row["error_code"] == "model_not_configured"
        # error rows remain in the denominator: one row per input unit
        assert [r["audit_unit_id"] for r in result.predictions] == ["u-1", "u-2"]
        # error rows are protocol-legal
        for row, task in zip(result.predictions, tasks):
            assert public_contracts.prediction_errors(row, task_json(task)) == []

    def test_unsupported_rule_is_error_row_not_fabricated(self, tmp_path, build_dir,
                                                          gw_env):
        # R001@3.0 is NOT in the B001-only frozen artifact
        task = make_task(
            audit_unit_id="u-r001", rule_id="R001", rule_version="3.0",
            metadata={"has_outer_package": True},
            materials=[MaterialRef(material_id="m1", role="package_photos",
                                   relative_path="assets/u-r001/m1.png",
                                   media_type="image/png", page_index=1)],
        )
        result = run_infer_fixture(tmp_path, build_dir, [task],
                                   transport=make_transport({"u-r001": task}))
        assert len(result.predictions) == 1
        row = result.predictions[0]
        assert row["execution_status"] == "error" and row["label"] is None
        assert row["error_code"] == "unsupported_rule"
        assert public_contracts.prediction_errors(row, task_json(task)) == []

    def test_gateway_failure_is_error_row(self, tmp_path, build_dir, gw_env):
        tasks = [task_with_assets("u-1")]
        tasks_by_unit = {t.audit_unit_id: t for t in tasks}
        transport = make_transport(tasks_by_unit, fail_units={"u-1"})
        result = run_infer_fixture(tmp_path, build_dir, tasks, transport=transport)
        row = result.predictions[0]
        assert row["execution_status"] == "error" and row["label"] is None
        assert row["error_code"] == "gateway_http_error"

    def test_resume_no_duplicate_rows_and_no_recall(self, tmp_path, build_dir, gw_env):
        tasks = [task_with_assets("u-1"), task_with_assets("u-2")]
        tasks_by_unit = {t.audit_unit_id: t for t in tasks}
        # First run: unit u-1 completes; u-2's upstream fails persistently.
        transport = make_transport(tasks_by_unit, fail_units={"u-2"})
        first = run_infer_fixture(tmp_path, build_dir, tasks, transport=transport,
                                  run_id="resume-run")
        assert first.predictions[0]["execution_status"] == "completed"
        assert first.predictions[1]["execution_status"] == "error"

        # Second run (same run id + same inputs): u-1 is recovered from the
        # final-step checkpoint WITHOUT another gateway call; u-2 retries.
        transport2 = make_transport(tasks_by_unit)
        second = run_infer_fixture(tmp_path, build_dir, tasks, transport=transport2,
                                   run_id="resume-run")
        assert len(second.predictions) == 2
        ids = [r["audit_unit_id"] for r in second.predictions]
        assert len(ids) == len(set(ids)) == 2  # zero duplicate audit_unit_id rows
        assert second.predictions[0]["execution_status"] == "completed"
        assert second.predictions[1]["execution_status"] == "completed"
        # completed unit u-1 was NOT re-called (only u-2's materials appear)
        recalled_units = set(transport2.calls["observe"])  # type: ignore[attr-defined]
        assert recalled_units == {"u-2"}

    def test_tampered_artifacts_refused_nonzero_exit(self, tmp_path, build_dir,
                                                     capsys, gw_env):
        victim = build_dir / "engine" / "rules" / "B001_3.0.json"
        victim.write_bytes(victim.read_bytes().replace(b"B001", b"B0XX", 1))
        inp = write_task_inputs(tmp_path, [task_with_assets("u-1")])
        rc = cli_main([
            "--workspace", str(tmp_path / "ws"),
            "infer", "--input", str(inp), "--artifacts", str(build_dir),
            "--out", str(tmp_path / "out" / "pred.jsonl"),
        ])
        assert rc == 1
        assert "sha256 mismatch" in capsys.readouterr().err
        assert not (tmp_path / "out" / "pred.jsonl").exists()

    def test_artifact_digest_byte_identical_after_infer(self, tmp_path, build_dir,
                                                        gw_env):
        before = artifact_hash.tree_digest(build_dir)
        tasks = [task_with_assets("u-1"), task_with_assets("u-2")]
        tasks_by_unit = {t.audit_unit_id: t for t in tasks}
        result = run_infer_fixture(tmp_path, build_dir, tasks,
                                   transport=make_transport(tasks_by_unit))
        assert artifact_hash.tree_digest(build_dir) == before
        assert result.artifact_root_sha256 == before

    def test_duplicate_input_ids_rejected(self, tmp_path, build_dir, gw_env):
        t = task_with_assets("u-1")
        inp = write_task_inputs(tmp_path, [t])
        dup = inp.parent / "dup.jsonl"
        dup.write_text(inp.read_text(encoding="utf-8") * 2, encoding="utf-8")
        with pytest.raises(Exception, match="duplicate audit_unit_id"):
            run_inference(input_jsonl=dup, artifacts_dir=build_dir,
                          out_path=tmp_path / "o.jsonl",
                          data_root=inp.parent, workspace=tmp_path / "ws",
                          transport=httpx.MockTransport(lambda r: ok_response("{}")))

    def test_missing_data_root_files_are_error_rows(self, tmp_path, build_dir, gw_env):
        tasks = [task_with_assets("u-1")]
        inp = write_task_inputs(tmp_path, tasks)
        # no assets written anywhere under this empty data root
        empty_root = tmp_path / "empty_root"
        empty_root.mkdir()
        result = run_inference(
            input_jsonl=inp, artifacts_dir=build_dir,
            out_path=tmp_path / "o.jsonl", data_root=empty_root,
            workspace=tmp_path / "ws",
            transport=httpx.MockTransport(lambda r: ok_response("{}")))
        assert result.predictions[0]["execution_status"] == "error"
        assert result.predictions[0]["error_code"] == "material_not_found"


# ---------------------------------------------------------------------------
# annotate: end-to-end
# ---------------------------------------------------------------------------

def write_targets(tmp_path: Path, targets: list[dict]) -> Path:
    path = tmp_path / "inputs" / "targets.jsonl"
    path.write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in targets),
                    encoding="utf-8")
    return path


def target(aid: str, uid: str, mid: str, target_text: str,
           page_index: int = 1) -> dict:
    return {
        "annotation_id": aid,
        "audit_unit_id": uid,
        "material_id": mid,
        "page_index": page_index,
        "target": target_text,
        "value_type": "string_or_null",
        "normalization": "unicode_nfkc_strip",
        "bbox_format": "normalized_xyxy",
    }


MODEL_TARGET = "提取指定材料中的型号完整文本行，包含字段名。"
NUMBER_TARGET = "提取指定材料中的材料编号完整文本行，包含字段名。"


class TestAnnotateEndToEnd:
    def run_ann(self, tmp_path, build_dir, tasks, targets, *, transport,
                annotation_text="型号：ＡＢ-１", annotation_status="value",
                annotation_by_keyword=None, fail_annotation=False, errors_out=None):
        inp = write_task_inputs(tmp_path, tasks)
        targets_path = write_targets(tmp_path, targets)
        if fail_annotation:
            # Only target-extraction rounds fail; observation rounds succeed.
            base = transport

            def handler(request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content.decode("utf-8"))
                prompt = body["messages"][0]["content"][0]["text"]
                if "提取目标" in prompt:
                    return httpx.Response(500, text="downstream failure")
                return base.handle_request(request)

            transport = httpx.MockTransport(handler)
        return run_annotation(
            input_jsonl=inp, targets_jsonl=targets_path, artifacts_dir=build_dir,
            out_path=tmp_path / "out" / "annotations.jsonl",
            data_root=inp.parent, workspace=tmp_path / "ws",
            transport=transport, errors_out=errors_out,
        )

    def test_annotation_nfkc_strip_value_preserved(self, tmp_path, build_dir, gw_env):
        """Raw transcription keeps NFKC+strip ONLY: '型号：ＡＢ-１' -> '型号:AB-1';
        internal case/hyphen preserved; never business-lowercased to 'ab1'."""
        tasks = [task_with_assets("u-1")]
        tasks_by_unit = {t.audit_unit_id: t for t in tasks}
        transport = make_transport(tasks_by_unit, annotation_text="型号：ＡＢ-１")
        result = self.run_ann(tmp_path, build_dir, tasks,
                              [target("a-1", "u-1", "u-1-m1", MODEL_TARGET)],
                              transport=transport)
        assert result.issues == []
        (row,) = result.annotations
        assert row["value"] == "型号:AB-1"  # NFKC: full-width -> ASCII
        assert row["value"] != "ab1"  # business model_normalize NEVER applied
        assert "AB" in row["value"] and "-" in row["value"]  # case + hyphen kept
        assert row["status"] == "value"
        assert row["label"] == "pass"  # whole-unit verdict from inference
        assert row["bbox"] == [0.1, 0.4, 0.6, 0.6]
        assert public_contracts.annotation_errors(row, target("a-1", "u-1", "u-1-m1", MODEL_TARGET),
                                                  task_json(tasks[0])) == []
        assert annotation_semantic_errors(row, target("a-1", "u-1", "u-1-m1", MODEL_TARGET),
                                          tasks[0]) == []

    def test_label_is_whole_unit_not_target_validity(self, tmp_path, build_dir, gw_env):
        """The annotated field itself is perfectly readable (status=value), but the
        unit's rule verdict is reject (voided label) — label must be the UNIT's."""
        tasks = [task_with_assets("u-void")]
        tasks_by_unit = {t.audit_unit_id: t for t in tasks}

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            prompt = body["messages"][0]["content"][0]["text"]
            if "提取目标" in prompt:
                return ok_response(annotation_reply("LED-100"))
            cited = re.findall(r'"material_id":\s*"([^"]+)"', prompt)
            return ok_response(voided_observation_reply(cited))

        result = self.run_ann(tmp_path, build_dir, tasks,
                              [target("a-v1", "u-void", "u-void-m1", MODEL_TARGET)],
                              transport=httpx.MockTransport(handler))
        assert result.issues == []
        (row,) = result.annotations
        assert row["status"] == "value" and row["value"] == "LED-100"
        assert row["label"] == "reject"  # unit verdict, NOT target validity

    def test_unreadable_enforces_null_value_and_valid_bbox(self, tmp_path, build_dir,
                                                           gw_env):
        tasks = [task_with_assets("u-1")]
        tasks_by_unit = {t.audit_unit_id: t for t in tasks}
        transport = make_transport(tasks_by_unit, annotation_text="不要读这个",
                                   annotation_status="unreadable")
        result = self.run_ann(tmp_path, build_dir, tasks,
                              [target("a-u1", "u-1", "u-1-m1", MODEL_TARGET)],
                              transport=transport)
        assert result.issues == []
        (row,) = result.annotations
        assert row["status"] == "unreadable"
        assert row["value"] is None  # unreadable REQUIRES JSON null
        from o2_public_contracts.contracts import bbox_ok
        assert bbox_ok(row["bbox"])  # occlusion-region bbox stays valid
        assert public_contracts.annotation_errors(
            row, target("a-u1", "u-1", "u-1-m1", MODEL_TARGET),
            task_json(tasks[0])) == []

    def test_invalid_value_isolated_per_target(self, tmp_path, build_dir, gw_env):
        """A readable-but-malformed material number is invalid_value for ITS target
        while a valid model target on another field/unit stays status=value."""
        tasks = [task_with_assets("u-bad"), task_with_assets("u-ok")]
        tasks_by_unit = {t.audit_unit_id: t for t in tasks}
        transport = make_transport(tasks_by_unit, annotation_by_keyword={
            "材料编号": ("编号：NOT-A-NUMBER!!", "invalid_value"),
            "型号": ("LED-100", "value"),
        })
        result = self.run_ann(tmp_path, build_dir, tasks, [
            target("a-bad", "u-bad", "u-bad-m1", NUMBER_TARGET),
            target("a-ok", "u-ok", "u-ok-m1", MODEL_TARGET),
        ], transport=transport)
        assert result.issues == []
        by_id = {r["annotation_id"]: r for r in result.annotations}
        assert by_id["a-bad"]["status"] == "invalid_value"
        assert by_id["a-bad"]["value"] == "编号:NOT-A-NUMBER!!"
        # the OTHER field's target is unaffected (stays value)
        assert by_id["a-ok"]["status"] == "value"
        all_targets = [target("a-bad", "u-bad", "u-bad-m1", NUMBER_TARGET),
                       target("a-ok", "u-ok", "u-ok-m1", MODEL_TARGET)]
        for row in result.annotations:
            tgt = next(x for x in all_targets
                       if x["annotation_id"] == row["annotation_id"])
            task = tasks_by_unit[row["audit_unit_id"]]
            assert public_contracts.annotation_errors(row, tgt, task_json(task)) == []

    def test_extraction_failure_goes_to_errors_jsonl_not_fabricated(
            self, tmp_path, build_dir, gw_env):
        tasks = [task_with_assets("u-1")]
        errors_path = tmp_path / "out" / "custom.errors.jsonl"
        result = self.run_ann(tmp_path, build_dir, tasks,
                              [target("a-1", "u-1", "u-1-m1", MODEL_TARGET)],
                              transport=make_transport({"u-1": tasks[0]}),
                              fail_annotation=True,
                              errors_out=errors_path)
        assert result.annotations == []  # no fabricated unreadable / fake answer
        records = [json.loads(l) for l in
                   errors_path.read_text(encoding="utf-8").splitlines()]
        assert len(records) == 1
        assert records[0]["annotation_id"] == "a-1"
        assert records[0]["error_code"] == "gateway_http_error"
        # the annotations file itself is empty
        assert (tmp_path / "out" / "annotations.jsonl").read_text(encoding="utf-8") == ""

    def test_inference_failure_not_fabricated_as_unreadable(self, tmp_path, build_dir,
                                                            gw_env):
        tasks = [task_with_assets("u-1")]
        tasks_by_unit = {t.audit_unit_id: t for t in tasks}
        # upstream observation rounds fail -> unit error -> label None
        transport = make_transport(tasks_by_unit, fail_units={"u-1"})
        result = self.run_ann(tmp_path, build_dir, tasks,
                              [target("a-1", "u-1", "u-1-m1", MODEL_TARGET)],
                              transport=transport)
        assert result.annotations == []
        assert result.issues[0]["error_code"] == "inference_failed"
        assert result.issues[0]["detail"]["error_code"] == "gateway_http_error"

    def test_shared_unit_inferred_once_for_multiple_targets(self, tmp_path, build_dir,
                                                            gw_env):
        tasks = [task_with_assets("u-1")]
        tasks_by_unit = {t.audit_unit_id: t for t in tasks}
        transport = make_transport(tasks_by_unit)
        observe_calls = transport.calls  # type: ignore[attr-defined]
        result = self.run_ann(tmp_path, build_dir, tasks, [
            target("a-1", "u-1", "u-1-m1", MODEL_TARGET),
            target("a-2", "u-1", "u-1-m2", NUMBER_TARGET),
        ], transport=transport)
        assert result.issues == []
        assert len(result.annotations) == 2
        # one unit -> the controller gap loop ran for exactly one unit
        assert set(observe_calls["observe"]) == {"u-1"}
        labels = {r["label"] for r in result.annotations}
        assert labels == {"pass"}

    def test_invalid_target_recorded_not_annotated(self, tmp_path, build_dir, gw_env):
        tasks = [task_with_assets("u-1")]
        tasks_by_unit = {t.audit_unit_id: t for t in tasks}
        transport = make_transport(tasks_by_unit)
        bad_target = target("a-bad", "u-unknown", "m1", MODEL_TARGET)
        bad_target["value_type"] = "wrong_type"  # violates annotation_task.schema.json
        result = self.run_ann(tmp_path, build_dir, tasks, [bad_target], transport=transport)
        assert result.annotations == []
        assert result.issues[0]["error_code"] == "invalid_target"

    def test_missing_bbox_is_an_error_not_fabricated(self, tmp_path, build_dir, gw_env):
        tasks = [task_with_assets("u-1")]
        tasks_by_unit = {t.audit_unit_id: t for t in tasks}
        transport = make_transport(tasks_by_unit)

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            prompt = body["messages"][0]["content"][0]["text"]
            if "提取目标" in prompt:
                return ok_response(json.dumps({"text": "LED-100", "status": "value",
                                               "bbox": None}))
            cited = re.findall(r'"material_id":\s*"([^"]+)"', prompt)
            return ok_response(observation_reply(cited))

        result = self.run_ann(tmp_path, build_dir, tasks,
                              [target("a-1", "u-1", "u-1-m1", MODEL_TARGET)],
                              transport=httpx.MockTransport(handler))
        assert result.annotations == []
        assert result.issues[0]["error_code"] == "annotation_bbox_missing"

    def test_annotation_errors_empty_on_valid_run(self, tmp_path, build_dir, gw_env):
        tasks = [task_with_assets("u-1"), task_with_assets("u-2")]
        tasks_by_unit = {t.audit_unit_id: t for t in tasks}
        transport = make_transport(tasks_by_unit)
        result = self.run_ann(tmp_path, build_dir, tasks, [
            target("a-1", "u-1", "u-1-m1", MODEL_TARGET),
            target("a-2", "u-2", "u-2-m2", NUMBER_TARGET),
        ], transport=transport)
        for row in result.annotations:
            tgt = target(row["annotation_id"], row["audit_unit_id"], row["material_id"],
                         MODEL_TARGET if row["annotation_id"] == "a-1" else NUMBER_TARGET)
            assert public_contracts.annotation_errors(
                row, tgt, task_json(tasks_by_unit[row["audit_unit_id"]])) == []

    def test_annotate_resumes_without_duplicate_rows_or_recalls(
            self, tmp_path, build_dir, gw_env):
        tasks = [task_with_assets("u-1")]
        tasks_by_unit = {t.audit_unit_id: t for t in tasks}
        targets = [target("a-1", "u-1", "u-1-m1", MODEL_TARGET)]
        transport = make_transport(tasks_by_unit)
        first = self.run_ann(tmp_path, build_dir, tasks, targets, transport=transport,
                             errors_out=tmp_path / "e1.jsonl")
        assert len(first.annotations) == 1
        transport2 = make_transport(tasks_by_unit)
        second = self.run_ann(tmp_path, build_dir, tasks, targets, transport=transport2)
        assert len(second.annotations) == 1
        assert second.annotations[0] == first.annotations[0]
        # resumed run: no new observation or annotation HTTP calls at all
        assert transport2.calls["observe"] == []  # type: ignore[attr-defined]
        assert transport2.calls["annotate"] == []  # type: ignore[attr-defined]

    def test_artifact_digest_byte_identical_after_annotate(self, tmp_path, build_dir,
                                                           gw_env):
        before = artifact_hash.tree_digest(build_dir)
        tasks = [task_with_assets("u-1")]
        tasks_by_unit = {t.audit_unit_id: t for t in tasks}
        result = self.run_ann(tmp_path, build_dir, tasks,
                              [target("a-1", "u-1", "u-1-m1", MODEL_TARGET)],
                              transport=make_transport(tasks_by_unit))
        assert artifact_hash.tree_digest(build_dir) == before
        assert result.artifact_root_sha256 == before

    def test_annotate_cli_end_to_end(self, tmp_path, build_dir, capsys, gw_env,
                                     monkeypatch):
        tasks = [task_with_assets("u-1")]
        tasks_by_unit = {t.audit_unit_id: t for t in tasks}
        transport = make_transport(tasks_by_unit)
        # annotate.py imports build_gateway from the infer module namespace;
        # patch the reference IT holds so the CLI uses the test transport.
        import o2_audit.runtime.annotate as ann_mod

        patch_cli_gateway(monkeypatch, ann_mod, transport)

        inp = write_task_inputs(tmp_path, tasks)
        targets_path = write_targets(
            tmp_path, [target("a-1", "u-1", "u-1-m1", MODEL_TARGET)])
        rc = cli_main([
            "--workspace", str(tmp_path / "ws"),
            "annotate",
            "--input", str(inp),
            "--targets", str(targets_path),
            "--artifacts", str(build_dir),
            "--out", str(tmp_path / "out" / "ann.jsonl"),
            "--errors-out", str(tmp_path / "out" / "ann.errors.jsonl"),
            "--run-id", "cli-ann-1",
        ])
        assert rc == 0
        summary = json.loads(capsys.readouterr().out)
        assert summary["annotated"] == 1 and summary["errors"] == 0
        rows = [json.loads(l) for l in
                (tmp_path / "out" / "ann.jsonl").read_text(encoding="utf-8").splitlines()]
        assert rows[0]["value"] == "型号:AB-1"
        assert rows[0]["label"] == "pass"

    def test_annotate_usage_includes_target_extraction_calls(
            self, tmp_path, build_dir, gw_env):
        tasks = [task_with_assets("u-1")]
        tasks_by_unit = {t.audit_unit_id: t for t in tasks}
        transport = make_transport(tasks_by_unit)
        result = self.run_ann(tmp_path, build_dir, tasks,
                              [target("a-1", "u-1", "u-1-m1", MODEL_TARGET)],
                              transport=transport)
        # usage must include BOTH observation calls and the target annotation call
        expected_calls = len(transport.calls["observe"]) + len(transport.calls["annotate"])
        assert len(transport.calls["annotate"]) == 1
        assert result.usage["calls"] == expected_calls


# ---------------------------------------------------------------------------
# Supervisor regression tests: ontology predicates & public metadata shapes
# ---------------------------------------------------------------------------

class TestOntologyAndMetadataRegressions:
    def test_all_62_rules_declare_valid_required_predicates(self):
        from o2_audit.model_gateway.prompts import KNOWN_PREDICATES
        from o2_audit.rules.registry import default_registry

        reg = default_registry()
        assert len(reg) == 62
        for rid, rv in reg.keys():
            plan = reg.get_plan(rid, rv)
            assert plan.required_predicates, f"{rid}@{rv} missing required_predicates"
            for pred in plan.required_predicates:
                assert pred in KNOWN_PREDICATES, f"{rid}@{rv}: unknown predicate {pred!r}"

    def test_r050_accepts_public_declared_components_list(self):
        from conftest import ent, obs
        from o2_audit.rules.registry import default_registry
        from o2_audit.runtime.facts import FactView

        reg = default_registry()
        task = make_task(
            audit_unit_id="u-r050-list", rule_id="R050", rule_version="3.0",
            metadata={"declared_components": [
                {"component_id": "a", "model": "EDU-753"},
                {"component_id": "b", "model": "EDU-268"},
            ]},
            materials=[MaterialRef(material_id="c1", role="certificate_pages",
                                   relative_path="assets/c1.png",
                                   media_type="image/png", page_index=1)],
        )
        facts_ok = FactView()
        facts_ok.add_entity(ent("C1", "certificate"))
        facts_ok.add_observation(obs(
            "C1", "component_model_map", {"a": "EDU-753", "b": "EDU-268"},
            raw={"a": "EDU-753", "b": "EDU-268"}, source=("c1", 1),
        ))
        out_ok = reg.evaluate(task, facts_ok)
        assert out_ok.label == "pass" and out_ok.evidence

        facts_swapped = FactView()
        facts_swapped.add_entity(ent("C1", "certificate"))
        facts_swapped.add_observation(obs(
            "C1", "component_model_map", {"a": "EDU-268", "b": "EDU-753"},
            raw={"a": "EDU-268", "b": "EDU-753"}, source=("c1", 1),
        ))
        out_swapped = reg.evaluate(task, facts_swapped)
        assert out_swapped.label == "reject"

    def test_r048_accepts_metadata_withdrawal_date(self):
        from conftest import ent, obs
        from o2_audit.rules.registry import default_registry
        from o2_audit.runtime.facts import FactView

        reg = default_registry()
        task = make_task(
            audit_unit_id="u-r048-meta", rule_id="R048", rule_version="3.0",
            review_date="2029-07-12",
            metadata={"withdrawal_date": "2029-07-12"},
            materials=[MaterialRef(material_id="c1", role="certificate_pages",
                                   relative_path="assets/c1.png",
                                   media_type="image/png", page_index=1)],
        )
        facts = FactView()
        facts.add_entity(ent("C1", "certificate"))
        facts.add_observation(obs("C1", "issue_date", "2028-01-01", source=("c1", 1)))
        facts.add_observation(obs("C1", "valid_from", "2028-01-01", source=("c1", 1)))
        facts.add_observation(obs("C1", "valid_until", "2030-12-31", source=("c1", 1)))
        out = reg.evaluate(task, facts)
        assert out.label == "reject"
        assert out.reason_code == "review_on_or_after_withdrawal"

