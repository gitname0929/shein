"""Single-process FastAPI + Jinja2 operational product (Stage 05).

Exposes both HTML operational views (P-UX-01..04) and JSON APIs (P-TASK-01..16)
calling the exact same build / infer / annotate / iterate / evaluation services
as the ``o2-audit`` CLI.

Key invariants:
- Long-running operations execute in :class:`~o2_audit.jobs.runner.JobRunner`
  (background worker thread backed by SQLite), never inside request handlers.
- Frozen artifact directories are strictly read-only; all runtime state lives
  in ``workspace_dir/state.sqlite3``.
- Material image serving resolves ONLY registered ``audit_results`` entries
  inside their registered ``data_root`` (strict traversal/symlink defense).
- Reference/gold labels are NEVER accepted by build, infer, annotate, or
  iterate endpoints.
"""
from __future__ import annotations

import io
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field

from o2_audit.build.lifecycle import (
    activate_version,
    compare_versions,
    rollback_to,
    version_status,
)
from o2_audit.build.loader import ArtifactIntegrityError, verify_manifest_only
from o2_audit.domain.models import ExecutionError, MaterialRef
from o2_audit.jobs.runner import JobRunner, import_rules_into_store
from o2_audit.materials.loader import load_material
from o2_audit.storage.store import TaskStore
from o2_public_contracts import contracts as public_contracts

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


# ---------------------------------------------------------------------------
# Request schemas (strict, never accepting gold labels on inference endpoints)
# ---------------------------------------------------------------------------

class RuleImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    jsonl_text: str | None = None
    file_path: str | None = None
    source_name: str = "uploaded_rules.jsonl"
    use_bundled_62: bool = False


class SingleBuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_id: str
    rule_version: str
    artifact_version: str | None = None
    version_name: str | None = None
    idempotency_key: str | None = None


class BulkBuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_pairs: list[tuple[str, str]] | None = None
    artifact_version: str | None = None
    version_name: str | None = None
    idempotency_key: str | None = None


class AuditRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_jsonl: str
    data_root: str | None = None
    version_name: str | None = None
    build_dir: str | None = None
    model_config_path: str | None = None
    run_id: str | None = None
    idempotency_key: str | None = None


class AnnotateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_jsonl: str
    targets_jsonl: str
    data_root: str | None = None
    version_name: str | None = None
    build_dir: str | None = None
    model_config_path: str | None = None
    run_id: str | None = None
    idempotency_key: str | None = None


class IterateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_version_name: str | None = None
    base_dir: str | None = None
    feedback_path: str
    input_jsonl: str
    artifact_version: str | None = None
    version_name: str | None = None
    idempotency_key: str | None = None


class VersionPointerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str


class PluginToggleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class EvaluateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inputs_path: str
    outputs_path: str
    references_path: str | None = None
    baseline_outputs_path: str | None = None
    plugin_ids: list[str] | None = None
    expected_artifact_version: str | None = None
    timeout: float | None = None
    extra_pythonpath: list[str] | None = None


class CompareJobsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    baseline_job_id: str
    candidate_job_id: str
    references_path: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sync_default_plugins(store: TaskStore) -> list[dict[str, Any]]:
    from o2_audit.evaluation.manifest import load_plugin_registry

    reg = load_plugin_registry()
    persisted = {row["plugin_id"]: row for row in store.list_plugin_configs()}
    out: list[dict[str, Any]] = []
    for spec in reg.list_specs():
        if spec.plugin_id not in persisted:
            store.upsert_plugin_config(spec.plugin_id, spec.to_dict(), spec.enabled)
            enabled = spec.enabled
        else:
            enabled = persisted[spec.plugin_id]["enabled"]
        d = spec.to_dict()
        d["enabled"] = enabled
        out.append(d)
    # Include any custom persisted plugins
    known = {d["plugin_id"] for d in out}
    for pid, row in persisted.items():
        if pid not in known:
            cfg = dict(row["config"])
            cfg["enabled"] = row["enabled"]
            out.append(cfg)
    return out


def _rule_text_for_result(store: TaskStore, result: dict[str, Any]) -> dict[str, Any]:
    """Resolve frozen rule text & quotes for P-TASK-08 / P-UX-02."""
    rid, rv = result["rule_id"], result["rule_version"]
    # 1. Prefer the exact frozen artifact bound to this result
    for rec in store.list_artifact_versions():
        if rec["root_sha256"] == result["artifact_root_sha256"]:
            try:
                art = verify_manifest_only(rec["build_dir"])
                bundle = art.bundles.get((rid, rv))
                if bundle:
                    plan = bundle.get("plan") or {}
                    return {
                        "rule_id": rid,
                        "rule_version": rv,
                        "title": plan.get("title", ""),
                        "raw_rule_text": plan.get("raw_rule_text", ""),
                        "quotes": plan.get("quotes", {}),
                        "extraction_guidance": plan.get("extraction_guidance", []),
                        "source": f"frozen_artifact:{rec['name']}",
                    }
            except Exception:
                pass
    # 2. Fallback to imported rule table
    imp = store.get_imported_rule(rid, rv)
    if imp is not None:
        return {
            "rule_id": rid,
            "rule_version": rv,
            "title": imp["title"],
            "raw_rule_text": imp["body"],
            "quotes": {},
            "extraction_guidance": [],
            "source": "imported_rules_store",
        }
    return {
        "rule_id": rid,
        "rule_version": rv,
        "title": "",
        "raw_rule_text": "",
        "quotes": {},
        "extraction_guidance": [],
        "source": "unavailable",
    }


def _parse_bbox_param(raw: str | None) -> tuple[float, float, float, float] | None:
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 4:
        raise HTTPException(status_code=400, detail="bbox must have 4 comma-separated floats")
    try:
        vals = tuple(float(p) for p in parts)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"invalid bbox float: {exc}") from exc
    if not (0.0 <= vals[0] < vals[2] <= 1.0 and 0.0 <= vals[1] < vals[3] <= 1.0):
        raise HTTPException(status_code=400, detail="bbox coordinates must satisfy 0<=x1<x2<=1 and 0<=y1<y2<=1")
    return vals  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# FastAPI application factory
# ---------------------------------------------------------------------------

def create_app(
    workspace_dir: str | Path,
    *,
    gateway_factory: Callable[[], Any] | None = None,
    transport_factory: Callable[[], Any] | None = None,
    start_worker: bool = True,
) -> FastAPI:
    ws = Path(workspace_dir)
    ws.mkdir(parents=True, exist_ok=True)
    store = TaskStore(ws / "state.sqlite3")
    runner = JobRunner(
        store, ws, gateway_factory=gateway_factory, transport_factory=transport_factory
    )
    _sync_default_plugins(store)
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_worker:
            app.state.interrupted_on_startup = runner.start()
        try:
            yield
        finally:
            runner.stop()
            store.close()

    app = FastAPI(
        title="O2 AI 商品审核官 — 运维与审核工作台",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.workspace_dir = ws
    app.state.store = store
    app.state.runner = runner
    app.state.interrupted_on_startup = []

    # -- HTML Dashboard (P-UX-01..04) ----------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        rules = store.list_imported_rules()
        adv_unique = sorted({
            r["rule_id"] for r in rules
            if r["rule_id"].startswith("R") and len(r["rule_id"]) == 4 and r["rule_id"][1:].isdigit()
        })
        batches = store.list_rule_import_batches(limit=10)
        raw_jobs = store.list_jobs(limit=50)
        jobs = []
        for j in raw_jobs:
            items = store.list_job_items(j["job_id"])
            jobs.append({
                **j,
                "total_items": len(items),
                "done_items": sum(1 for it in items if it["status"] == "completed"),
                "error_items": sum(1 for it in items if it["status"] in ("failed", "error")),
            })
        versions = version_status(store)
        for v in versions["versions"]:
            try:
                art = verify_manifest_only(v["build_dir"])
                v["rule_count"] = len(art.entries)
            except Exception:
                v["rule_count"] = None
        results = store.list_audit_results(limit=50)
        evals = store.list_eval_runs(limit=20)
        plugins = _sync_default_plugins(store)
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "rules": rules,
                "adv_unique_count": len(adv_unique),
                "batches": batches,
                "jobs": jobs,
                "versions": versions,
                "results": results,
                "evals": evals,
                "plugins": plugins,
                "workspace_dir": str(ws),
            },
        )

    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "workspace_dir": str(ws),
            "active_version": store.active_version_name(),
            "interrupted_on_startup": app.state.interrupted_on_startup,
        }

    # -- P-TASK-01..03: Rule Import & Management -----------------------------

    @app.get("/api/rules")
    def list_rules():
        rules = store.list_imported_rules()
        adv_unique = sorted({
            r["rule_id"] for r in rules
            if r["rule_id"].startswith("R") and len(r["rule_id"]) == 4 and r["rule_id"][1:].isdigit()
        })
        return {
            "total": len(rules),
            "unique_advanced_rules": len(adv_unique),
            "rules": rules,
            "import_batches": store.list_rule_import_batches(limit=10),
        }

    @app.post("/api/rules/import")
    def api_import_rules(req: RuleImportRequest):
        try:
            if req.use_bundled_62:
                summary = import_rules_into_store(store, workspace_dir=ws)
            elif req.file_path:
                summary = import_rules_into_store(
                    store, file_path=req.file_path, workspace_dir=ws
                )
            elif req.jsonl_text is not None:
                summary = import_rules_into_store(
                    store,
                    jsonl_text=req.jsonl_text,
                    source_name=req.source_name,
                    workspace_dir=ws,
                )
            else:
                raise HTTPException(
                    status_code=400,
                    detail="provide use_bundled_62=true, file_path, or jsonl_text",
                )
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={
                    "object": req.file_path or req.source_name,
                    "cause": str(exc),
                    "next_action": "检查规则文件路径后重新导入。",
                },
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "object": req.file_path or req.source_name,
                    "cause": str(exc),
                    "next_action": "检查 JSONL 编码与结构后重新导入。",
                },
            ) from exc
        return summary

    # -- P-TASK-04..05: Single & Bulk Builds ---------------------------------

    @app.post("/api/builds/single")
    def api_build_single(req: SingleBuildRequest):
        job = runner.submit_job(
            "build_single",
            req.model_dump(exclude_none=True),
            idempotency_key=req.idempotency_key,
        )
        return job

    @app.post("/api/builds/bulk")
    def api_build_bulk(req: BulkBuildRequest):
        job = runner.submit_job(
            "build_bulk",
            req.model_dump(exclude_none=True),
            idempotency_key=req.idempotency_key,
        )
        return job

    # -- P-TASK-06: Audit / Annotate Runs (bound to initial frozen artifact) --

    def _pin_artifact(version_name: str | None, build_dir: str | None) -> tuple[str, str, str]:
        if build_dir:
            p = Path(build_dir)
            art = verify_manifest_only(p)
            versions = sorted({e["artifact_version"] for e in art.entries})
            from o2_public_contracts import artifact_hash
            return str(p), versions[-1], artifact_hash.tree_digest(p)
        vname = version_name or store.active_version_name()
        if not vname:
            raise HTTPException(
                status_code=400,
                detail={
                    "object": "active_artifact",
                    "cause": "未指定构建版本且当前无激活版本",
                    "next_action": "请先执行单条或批量构建并激活版本。",
                },
            )
        rec = store.get_artifact_version(vname)
        if rec is None:
            raise HTTPException(status_code=404, detail=f"artifact version not found: {vname}")
        return rec["build_dir"], rec["artifact_version"], rec["root_sha256"]

    @app.post("/api/audit/run")
    def api_audit_run(req: AuditRunRequest):
        try:
            pinned_dir, av, root_sha = _pin_artifact(req.version_name, req.build_dir)
        except ArtifactIntegrityError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        payload = {
            "input_jsonl": req.input_jsonl,
            "data_root": req.data_root,
            "build_dir": pinned_dir,
            "version_name": req.version_name or store.active_version_name(),
            "model_config": req.model_config_path,
            "run_id": req.run_id,
        }
        return runner.submit_job(
            "audit_infer",
            payload,
            idempotency_key=req.idempotency_key,
            artifact_version=av,
            artifact_root_sha256=root_sha,
        )

    @app.post("/api/annotate/run")
    def api_annotate_run(req: AnnotateRunRequest):
        try:
            pinned_dir, av, root_sha = _pin_artifact(req.version_name, req.build_dir)
        except ArtifactIntegrityError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        payload = {
            "input_jsonl": req.input_jsonl,
            "targets_jsonl": req.targets_jsonl,
            "data_root": req.data_root,
            "build_dir": pinned_dir,
            "version_name": req.version_name or store.active_version_name(),
            "model_config": req.model_config_path,
            "run_id": req.run_id,
        }
        return runner.submit_job(
            "annotate",
            payload,
            idempotency_key=req.idempotency_key,
            artifact_version=av,
            artifact_root_sha256=root_sha,
        )

    # -- P-TASK-10..12: Jobs, Retry, Resume, Export --------------------------

    @app.get("/api/jobs")
    def api_list_jobs(
        job_kind: str | None = None,
        status: str | None = None,
        limit: int = Query(100, ge=1, le=500),
    ):
        return {"jobs": store.list_jobs(job_kind=job_kind, status=status, limit=limit)}

    @app.get("/api/jobs/{job_id}")
    def api_get_job(job_id: str, item_status: str | None = None):
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
        items = store.list_job_items(job_id, status=item_status)
        attempts = store.list_job_attempts(job_id)
        return {**job, "items": items, "attempts": attempts}

    @app.post("/api/jobs/{job_id}/retry")
    def api_retry_job(job_id: str):
        try:
            return runner.retry_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/jobs/{job_id}/resume")
    def api_resume_job(job_id: str):
        try:
            return runner.resume_job(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/jobs/{job_id}/export")
    def api_export_job(job_id: str, fmt: str = Query("jsonl", pattern="^(jsonl|json)$")):
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
        results = store.list_audit_results(job_id=job_id, limit=100000)
        preds = [r["prediction"] for r in results]
        if fmt == "jsonl":
            body = "\n".join(json.dumps(p, ensure_ascii=False, allow_nan=False) for p in preds)
            if body:
                body += "\n"
            return PlainTextResponse(
                content=body,
                media_type="application/x-ndjson",
                headers={"Content-Disposition": f'attachment; filename="{job_id}.jsonl"'},
            )
        return JSONResponse({
            "job_id": job["job_id"],
            "job_kind": job["job_kind"],
            "status": job["status"],
            "artifact_version": job["artifact_version"],
            "artifact_root_sha256": job["artifact_root_sha256"],
            "count": len(preds),
            "result": job["result"],
            "predictions": preds,
        })

    # -- P-TASK-07..08 & P-UX-02: Results, Rule Trace & Safe Material Serving -

    @app.get("/api/results")
    def api_list_results(job_id: str | None = None, limit: int = Query(100, ge=1, le=500)):
        return {"results": store.list_audit_results(job_id=job_id, limit=limit)}

    @app.get("/api/results/{result_id}")
    def api_get_result(result_id: str):
        res = store.get_audit_result(result_id)
        if res is None:
            raise HTTPException(status_code=404, detail=f"result not found: {result_id}")
        rule_trace = _rule_text_for_result(store, res)
        return {
            **res,
            "rule_trace": rule_trace,
        }

    @app.get("/api/materials/{result_id}/{material_id}")
    def api_serve_material(
        result_id: str,
        material_id: str,
        bbox: str | None = None,
    ):
        """Serve ONLY a material registered on ``result_id`` inside its registered
        ``data_root``. Strictly rejects path traversal, symlink escapes, and
        unregistered materials."""
        res = store.get_audit_result(result_id)
        if res is None:
            raise HTTPException(status_code=404, detail="audit result not found")
        mats = res["task"].get("materials") or []
        mat_dict = next(
            (m for m in mats if isinstance(m, dict) and m.get("material_id") == material_id),
            None,
        )
        if mat_dict is None:
            raise HTTPException(status_code=403, detail="material_id does not belong to this audit unit")

        rel = str(mat_dict.get("relative_path") or "")
        if not public_contracts.safe_relative(rel):
            raise HTTPException(status_code=403, detail="unsafe relative material path rejected")

        root = Path(res["data_root"]).resolve()
        candidate = (root / rel)
        if candidate.is_symlink():
            raise HTTPException(status_code=403, detail="symbolic link material rejected")
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail="path traversal outside registered data_root rejected") from exc

        crop_bbox = _parse_bbox_param(bbox)
        try:
            mat_ref = MaterialRef.model_validate(mat_dict)
            loaded = load_material(root, mat_ref, crop_bbox=crop_bbox)
        except ExecutionError as exc:
            raise HTTPException(status_code=400, detail=f"{exc.error_code}: {exc.message}") from exc

        buf = io.BytesIO()
        loaded.image.save(buf, format="PNG")
        return Response(content=buf.getvalue(), media_type="image/png")

    # -- P-TASK-13..14: Iteration, Versions, Activation, Rollback ------------

    @app.get("/api/versions")
    def api_list_versions():
        status = version_status(store)
        for rec in status["versions"]:
            rec["events"] = store.version_events_for(rec["name"])
            rec["manual_overrides"] = store.manual_overrides_for(rec["name"])
            iter_rep = Path(rec["build_dir"]) / "iteration_report.json"
            if iter_rep.is_file():
                try:
                    rec["iteration_report"] = json.loads(iter_rep.read_text(encoding="utf-8"))
                except Exception:
                    rec["iteration_report"] = None
        return status

    @app.post("/api/iterate")
    def api_iterate(req: IterateRequest):
        return runner.submit_job(
            "iterate",
            req.model_dump(exclude_none=True),
            idempotency_key=req.idempotency_key,
        )

    @app.post("/api/versions/activate")
    def api_activate_version(req: VersionPointerRequest):
        try:
            return activate_version(store, req.name)
        except (KeyError, ArtifactIntegrityError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/versions/rollback")
    def api_rollback_version(req: VersionPointerRequest):
        try:
            return rollback_to(store, req.name)
        except (KeyError, ArtifactIntegrityError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/versions/diff")
    def api_diff_versions(a: str, b: str):
        try:
            return compare_versions(store, a, b)
        except (KeyError, ArtifactIntegrityError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # -- P-TASK-09: Result Comparison ----------------------------------------

    @app.post("/api/compare")
    def api_compare_jobs(req: CompareJobsRequest):
        from o2_audit.evaluation.compare import compare_predictions

        base_rows = store.list_audit_results(job_id=req.baseline_job_id, limit=100000)
        cand_rows = store.list_audit_results(job_id=req.candidate_job_id, limit=100000)
        if not base_rows or not cand_rows:
            raise HTTPException(status_code=404, detail="baseline or candidate job has no stored results")

        inputs_by_id: dict[str, dict[str, Any]] = {}
        for r in base_rows + cand_rows:
            inputs_by_id[r["audit_unit_id"]] = r["task"]
        inputs = list(inputs_by_id.values())
        base_preds = [r["prediction"] for r in base_rows]
        cand_preds = [r["prediction"] for r in cand_rows]

        references = None
        if req.references_path:
            references = public_contracts.read_rows(req.references_path)

        comp = compare_predictions(inputs, base_preds, cand_preds, references=references)
        comp["scope_label"] = (
            "仅限公开开发评估（非正式隐藏集）"
            if references
            else "无参考真值：仅展示预测差异（不称作修复或新增错误）"
        )
        return comp

    # -- P-TASK-15: Plugin Configuration & Isolated Evaluation ---------------

    @app.get("/api/plugins")
    def api_list_plugins():
        return {
            "plugins": _sync_default_plugins(store),
            "eval_runs": store.list_eval_runs(limit=20),
        }

    @app.post("/api/plugins/{plugin_id}/toggle")
    def api_toggle_plugin(plugin_id: str, req: PluginToggleRequest):
        plugins = _sync_default_plugins(store)
        target = next(
            (p for p in plugins if p["plugin_id"] == plugin_id or plugin_id in p.get("aliases", [])),
            None,
        )
        if target is None:
            raise HTTPException(status_code=404, detail=f"plugin not found: {plugin_id}")
        store.upsert_plugin_config(target["plugin_id"], target, req.enabled)
        return {"plugin_id": target["plugin_id"], "enabled": req.enabled}

    @app.post("/api/evaluate/run")
    def api_evaluate_run(req: EvaluateRunRequest):
        return runner.submit_job("evaluate", req.model_dump(exclude_none=True))

    return app
