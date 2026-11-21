"""Persistent SQLite-backed background job runner (Stage 05).

Executes long-running operations (single/bulk builds, inference, annotation,
safe iteration, isolated plugin evaluation) on a dedicated background worker
thread — NEVER inside FastAPI request handlers.

Key invariants:
- Job state, per-item checkpoints (``job_items``), and attempt history
  (``job_attempts``) live in workspace SQLite (never inside frozen artifacts).
- Idempotency keys prevent duplicate enqueueing of active/completed jobs.
- ``retry_job`` creates ONE effective new job linked via ``parent_job_id``,
  preserving the old job record intact (P-TASK-10).
- On startup/restart, any job left ``running`` is marked ``interrupted``;
  ``resume_job`` resumes from persisted ``job_items`` / ``steps`` checkpoints
  without re-running completed items (P-TASK-11).
- Audit/annotate jobs bind to the frozen ``build_dir`` selected at submission
  time; activating/rolling back pointers later does NOT affect running jobs.
- ``build``, ``audit_infer``, ``annotate``, and ``iterate`` never read
  reference/gold labels.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from o2_audit.build.compiler import BuildError, compile_build
from o2_audit.build.importer import import_rules_jsonl
from o2_audit.build.loader import ArtifactIntegrityError, verify_build
from o2_audit.iterate.engine import IterationError, run_iteration
from o2_audit.protocol.jsonio import ProtocolError, read_jsonl_strict
from o2_audit.resources import resources_root
from o2_audit.runtime.annotate import run_annotation
from o2_audit.runtime.infer import load_input_tasks, run_inference, task_json
from o2_audit.storage.store import TaskStore
from o2_public_contracts import artifact_hash


def _short_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _default_rules_sources() -> list[Path]:
    """Bundled read-only public rule files (B001@3.0, B001@3.1, R001..R060@3.0)."""
    rules_dir = resources_root() / "rules"
    sources: list[Path] = []
    for name in ("基础规则B001_3.0.json", "基础规则B001_3.1.json", "进阶规则60条.jsonl"):
        p = rules_dir / name
        if p.is_file():
            sources.append(p)
    return sources


def import_rules_into_store(
    store: TaskStore,
    *,
    jsonl_text: str | None = None,
    file_path: str | Path | None = None,
    source_name: str = "uploaded_rules.jsonl",
    workspace_dir: Path,
) -> dict[str, Any]:
    """Import rules JSONL (or bundled default 62 rules) into SQLite with
    deduplication and per-line error preservation (P-TASK-01..03, P-UX-03)."""
    staging_dir = workspace_dir / "imports"
    staging_dir.mkdir(parents=True, exist_ok=True)
    batch_id = _short_id("imp")

    if file_path is not None:
        src_path = Path(file_path)
        source_name = src_path.name
    elif jsonl_text is not None:
        src_path = staging_dir / f"{batch_id}.jsonl"
        src_path.write_text(jsonl_text, encoding="utf-8")
    else:
        # Build combined JSONL from bundled B001 3.0 + B001 3.1 + 60 advanced rules
        combined_lines: list[str] = []
        for p in _default_rules_sources():
            if p.suffix == ".json":
                obj = json.loads(p.read_text(encoding="utf-8"))
                combined_lines.append(json.dumps(obj, ensure_ascii=False))
            else:
                for ln in p.read_text(encoding="utf-8").splitlines():
                    if ln.strip():
                        combined_lines.append(ln.strip())
        src_path = staging_dir / f"{batch_id}_bundled_62.jsonl"
        src_path.write_text("\n".join(combined_lines) + "\n", encoding="utf-8")
        source_name = "bundled_62_rules.jsonl"

    imp = import_rules_jsonl(src_path)
    newly_inserted = 0
    for rec in imp.valid:
        inserted = store.upsert_imported_rule(
            rule_id=rec.rule_id,
            rule_version=rec.rule_version,
            title=rec.title,
            body=rec.rule_text,
            source_file=source_name,
            source_sha256=rec.source_line_sha256,
            rule_family_id=rec.rule_family_id,
            raw_row=rec.raw,
        )
        if inserted:
            newly_inserted += 1

    issues_dicts = [
        {
            "line_no": iss.get("line_number"),
            "rule_id": iss.get("rule_id"),
            "code": iss.get("error_code"),
            "object": f"{source_name}:line {iss.get('line_number')}"
                      + (f" ({iss.get('rule_id')})" if iss.get("rule_id") else ""),
            "cause": iss.get("message", ""),
            "next_action": iss.get("next_action") or "已保留其余合法规则；请修正该行后重新导入。",
        }
        for iss in imp.issues
    ]
    total_lines = len(imp.valid) + len(imp.issues) + len(imp.duplicates)
    store.record_rule_import_batch(
        batch_id=batch_id,
        source_name=source_name,
        total_lines=total_lines,
        valid_count=len(imp.valid),
        unique_count=len(imp.valid),
        duplicate_count=len(imp.duplicates),
        issues=issues_dicts,
    )

    all_rules = store.list_imported_rules()
    adv_unique = {
        r["rule_id"] for r in all_rules
        if r["rule_id"].startswith("R") and len(r["rule_id"]) == 4 and r["rule_id"][1:].isdigit()
    }
    return {
        "batch_id": batch_id,
        "source_name": source_name,
        "total_lines": total_lines,
        "valid_count": len(imp.valid),
        "newly_inserted": newly_inserted,
        "duplicate_count": len(imp.duplicates),
        "issue_count": len(issues_dicts),
        "issues": issues_dicts,
        "total_stored_rules": len(all_rules),
        "unique_advanced_rules": len(adv_unique),
    }


def _write_rules_jsonl_from_store(
    store: TaskStore,
    target_path: Path,
    selected_pairs: list[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """Materialize imported rules from SQLite into a strict JSONL file for compiler."""
    rows = store.list_imported_rules()
    if selected_pairs is not None:
        wanted = set(selected_pairs)
        rows = [r for r in rows if (r["rule_id"], r["rule_version"]) in wanted]
    target_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r["raw_row"], ensure_ascii=False) for r in rows]
    target_path.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
    return [(r["rule_id"], r["rule_version"]) for r in rows]


class JobRunner:
    """Single-process persistent background worker with handler registry."""

    def __init__(
        self,
        store: TaskStore,
        workspace_dir: str | Path,
        *,
        gateway_factory: Callable[[], Any] | None = None,
        transport_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.store = store
        self.workspace_dir = Path(workspace_dir)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self._gateway_factory = gateway_factory
        self._transport_factory = transport_factory

        self._queue: queue.Queue[str | None] = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._done_events: dict[str, threading.Event] = {}
        self._events_lock = threading.Lock()

        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "build_single": self._handle_build_single,
            "build_bulk": self._handle_build_bulk,
            "audit_infer": self._handle_audit_infer,
            "annotate": self._handle_annotate,
            "iterate": self._handle_iterate,
            "evaluate": self._handle_evaluate,
        }

    # -- lifecycle ------------------------------------------------------------

    def start(self, *, auto_resume_queued: bool = True) -> list[str]:
        """Start background worker thread and mark any stale running jobs as interrupted."""
        interrupted = self.store.mark_running_jobs_interrupted()
        if self._thread is not None and self._thread.is_alive():
            return interrupted
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._worker_loop, name="o2-job-worker", daemon=True
        )
        self._thread.start()
        if auto_resume_queued:
            for job in reversed(self.store.list_jobs(status="queued", limit=200)):
                self._enqueue_id(job["job_id"])
        return interrupted

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self._queue.put(None)
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None

    def _enqueue_id(self, job_id: str) -> None:
        with self._events_lock:
            self._done_events.setdefault(job_id, threading.Event()).clear()
        self._queue.put(job_id)

    def _signal_done(self, job_id: str) -> None:
        with self._events_lock:
            ev = self._done_events.get(job_id)
            if ev is not None:
                ev.set()

    def wait_for_job(self, job_id: str, timeout: float = 30.0) -> dict[str, Any]:
        """Block until a job leaves queued/running state (helpful for tests)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.store.get_job(job_id)
            if job is not None and job["status"] not in ("queued", "running"):
                return job
            with self._events_lock:
                ev = self._done_events.get(job_id)
            if ev is not None:
                ev.wait(timeout=0.05)
            else:
                time.sleep(0.02)
        job = self.store.get_job(job_id)
        if job is None:
            raise KeyError(f"job not found: {job_id}")
        return job

    # -- submission / retry / resume -----------------------------------------

    def submit_job(
        self,
        job_kind: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        artifact_version: str | None = None,
        artifact_root_sha256: str | None = None,
        parent_job_id: str | None = None,
    ) -> dict[str, Any]:
        if job_kind not in self._handlers:
            raise ValueError(f"unknown job_kind: {job_kind!r}")
        job_id = _short_id(job_kind.replace("_", "-"))
        job = self.store.create_job(
            job_id=job_id,
            job_kind=job_kind,
            payload=payload,
            idempotency_key=idempotency_key,
            artifact_version=artifact_version,
            artifact_root_sha256=artifact_root_sha256,
            parent_job_id=parent_job_id,
        )
        # Only enqueue if this call actually created a queued job (not an
        # already-completed idempotent hit).
        if job["status"] == "queued" and job["job_id"] == job_id:
            self._enqueue_id(job_id)
        return job

    def retry_job(self, job_id: str) -> dict[str, Any]:
        """Create ONE effective new job retrying a failed/interrupted/completed job,
        preserving the original job record and linking ``parent_job_id`` (P-TASK-10)."""
        orig = self.store.get_job(job_id)
        if orig is None:
            raise KeyError(f"job not found: {job_id!r}")
        retry_idem = f"retry:{job_id}:{orig['attempt_count'] + 1}"
        # Check if a retry for this exact failed job is already queued/running/completed
        new_job = self.submit_job(
            orig["job_kind"],
            orig["payload"],
            idempotency_key=retry_idem,
            artifact_version=orig["artifact_version"],
            artifact_root_sha256=orig["artifact_root_sha256"],
            parent_job_id=job_id,
        )
        return new_job

    def resume_job(self, job_id: str) -> dict[str, Any]:
        """Resume an interrupted or failed job in-place, preserving completed
        ``job_items`` / ``steps`` checkpoints (P-TASK-11)."""
        job = self.store.get_job(job_id)
        if job is None:
            raise KeyError(f"job not found: {job_id!r}")
        if job["status"] == "completed":
            return job
        if job["status"] == "running":
            return job
        self.store.update_job_status(job_id, "queued")
        self._enqueue_id(job_id)
        return self.store.get_job(job_id) or job

    # -- worker loop ----------------------------------------------------------

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                job_id = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if job_id is None:
                break
            try:
                self._execute_job(job_id)
            finally:
                self._signal_done(job_id)

    def _execute_job(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        if job is None or job["status"] in ("completed", "cancelled"):
            return
        handler = self._handlers.get(job["job_kind"])
        if handler is None:
            self.store.update_job_status(
                job_id, "failed",
                error={
                    "object": job_id,
                    "cause": f"unregistered job_kind {job['job_kind']!r}",
                    "next_action": "检查任务类型配置",
                },
                finished=True,
            )
            return

        attempt_no = job["attempt_count"] + 1
        self.store.update_job_status(job_id, "running", increment_attempt=True)
        self.store.record_job_attempt(job_id, attempt_no, "running", {"kind": job["job_kind"]})
        try:
            res = handler(self.store.get_job(job_id) or job)
            self.store.update_job_status(
                job_id,
                "completed",
                result=res,
                artifact_version=res.get("artifact_version"),
                artifact_root_sha256=res.get("root_sha256") or res.get("artifact_root_sha256") or res.get("candidate_root_sha256"),
                finished=True,
            )
            self.store.record_job_attempt(
                job_id, attempt_no, "completed", {"summary": res}, finished=True
            )
        except Exception as exc:
            err_payload = {
                "object": f"{job['job_kind']}:{job_id}",
                "code": type(exc).__name__,
                "cause": str(exc),
                "next_action": "检查失败详情并修正配置/输入后点击重试，或回滚至已验证基线版本。",
            }
            self.store.update_job_status(job_id, "failed", error=err_payload, finished=True)
            self.store.record_job_attempt(
                job_id, attempt_no, "failed", err_payload, finished=True
            )

    # -- handlers -------------------------------------------------------------

    def _handle_build_single(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = job["payload"]
        rule_id = str(payload.get("rule_id") or "").strip()
        rule_version = str(payload.get("rule_version") or "").strip()
        if not rule_id or not rule_version:
            raise BuildError("rule_id and rule_version are required for single build")
        item_key = f"{rule_id}@{rule_version}"
        self.store.upsert_job_item(job["job_id"], item_key, "running")

        row = self.store.get_imported_rule(rule_id, rule_version)
        if row is None:
            err = {
                "object": item_key,
                "cause": f"规则 {item_key} 尚未导入到规则库中",
                "next_action": "请先在规则管理中导入该规则，再重新构建。",
            }
            self.store.upsert_job_item(job["job_id"], item_key, "failed", error=err)
            raise BuildError(err["cause"])

        av = str(payload.get("artifact_version") or f"single-{rule_id}-{rule_version}-{job['job_id'][-6:]}")
        vname = str(payload.get("version_name") or av)
        out_dir = self.workspace_dir / "artifacts" / vname
        rules_file = self.workspace_dir / "build_inputs" / f"{job['job_id']}.jsonl"
        _write_rules_jsonl_from_store(self.store, rules_file, [(rule_id, rule_version)])

        try:
            result = compile_build(
                rules_jsonl=rules_file,
                config=payload.get("config") or {"artifact_version": av},
                out_dir=out_dir,
                artifact_version=av,
                store=self.store,
                version_name=vname,
                overwrite=bool(payload.get("overwrite", True)),
            )
        except Exception as exc:
            self.store.upsert_job_item(
                job["job_id"], item_key, "failed",
                error={"object": item_key, "cause": str(exc), "next_action": "修正规则或配置后点击重试。"},
            )
            raise

        entry = result.manifest["entries"][0]
        self.store.upsert_job_item(
            job["job_id"], item_key, "completed",
            artifact_version=result.artifact_version,
            artifact_sha256=entry["sha256"],
            result={"path": entry["path"], "sha256": entry["sha256"]},
        )
        return {
            "build_dir": str(result.build_dir),
            "version_name": vname,
            "artifact_version": result.artifact_version,
            "root_sha256": result.root_sha256,
            "built_rules": len(result.built),
            "succeeded": 1,
            "failed": 0,
        }

    def _handle_build_bulk(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = job["payload"]
        requested: list[tuple[str, str]] | None = None
        if payload.get("rule_pairs"):
            requested = [(p[0], p[1]) for p in payload["rule_pairs"]]

        stored = self.store.list_imported_rules()
        if requested is not None:
            wanted = set(requested)
            targets = [r for r in stored if (r["rule_id"], r["rule_version"]) in wanted]
            # Preserve requested pairs missing from store so they surface as genuine failed items!
            found_pairs = {(r["rule_id"], r["rule_version"]) for r in targets}
            missing_pairs = [p for p in requested if p not in found_pairs]
        else:
            targets = stored
            missing_pairs = []

        if not targets and not missing_pairs:
            raise BuildError("规则库为空，请先导入规则后再批量构建。")

        # Preserve already-completed job_items on resume (P-TASK-11)
        existing_items = {
            it["item_key"]: it for it in self.store.list_job_items(job["job_id"])
        }
        av = str(payload.get("artifact_version") or f"bulk-{job['job_id'][-8:]}")
        vname = str(payload.get("version_name") or av)
        out_dir = self.workspace_dir / "artifacts" / vname

        failed_keys: list[str] = []
        valid_pairs: list[tuple[str, str]] = []
        from o2_audit.rules.registry import default_registry
        reg = default_registry()

        for rid, rv in missing_pairs:
            key = f"{rid}@{rv}"
            failed_keys.append(key)
            self.store.upsert_job_item(
                job["job_id"], key, "failed",
                error={"object": key, "cause": "规则未导入规则库", "next_action": "请导入该规则后重试"},
            )

        for r in targets:
            rid, rv = r["rule_id"], r["rule_version"]
            key = f"{rid}@{rv}"
            prev = existing_items.get(key)
            if prev and prev["status"] == "completed":
                valid_pairs.append((rid, rv))
                continue
            if (rid, rv) not in reg:
                failed_keys.append(key)
                self.store.upsert_job_item(
                    job["job_id"], key, "failed",
                    error={
                        "object": key,
                        "cause": f"规则 {key} 未在确定性执行引擎中注册",
                        "next_action": "检查规则ID/版本号或补齐规则实现后重试。",
                    },
                )
            else:
                valid_pairs.append((rid, rv))

        if not valid_pairs:
            raise BuildError(f"批量构建无可执行规则（失败 {len(failed_keys)} 条）")

        rules_file = self.workspace_dir / "build_inputs" / f"{job['job_id']}.jsonl"
        _write_rules_jsonl_from_store(self.store, rules_file, valid_pairs)

        result = compile_build(
            rules_jsonl=rules_file,
            config=payload.get("config") or {"artifact_version": av},
            out_dir=out_dir,
            artifact_version=av,
            store=self.store,
            version_name=vname,
            overwrite=True,
        )
        entries_by_key = {
            f"{e['rule_id']}@{e['rule_version']}": e for e in result.manifest["entries"]
        }
        for rid, rv in valid_pairs:
            key = f"{rid}@{rv}"
            e = entries_by_key[key]
            self.store.upsert_job_item(
                job["job_id"], key, "completed",
                artifact_version=result.artifact_version,
                artifact_sha256=e["sha256"],
                result={"path": e["path"], "sha256": e["sha256"]},
            )

        return {
            "build_dir": str(result.build_dir),
            "version_name": vname,
            "artifact_version": result.artifact_version,
            "root_sha256": result.root_sha256,
            "built_rules": len(valid_pairs),
            "succeeded": len(valid_pairs),
            "failed": len(failed_keys),
            "failed_items": failed_keys,
        }

    def _resolve_build_dir(self, payload: dict[str, Any]) -> tuple[Path, str]:
        if payload.get("build_dir"):
            p = Path(payload["build_dir"])
            return p, str(payload.get("version_name") or p.name)
        vname = payload.get("version_name") or self.store.active_version_name()
        if not vname:
            raise BuildError("未指定构建产物且当前没有激活版本；请先构建并激活版本。")
        rec = self.store.get_artifact_version(vname)
        if rec is None:
            raise BuildError(f"版本 {vname!r} 未注册")
        return Path(rec["build_dir"]), vname

    def _handle_audit_infer(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = job["payload"]
        build_dir, vname = self._resolve_build_dir(payload)
        input_jsonl = Path(payload["input_jsonl"])
        data_root = payload.get("data_root")
        out_path = Path(
            payload.get("out_path")
            or (self.workspace_dir / "outputs" / f"{job['job_id']}_predictions.jsonl")
        )
        run_id = str(payload.get("run_id") or f"job-{job['job_id']}")

        tasks = load_input_tasks(input_jsonl)
        tasks_by_id = {t.audit_unit_id: task_json(t) for t in tasks}

        # Resume checkpoint check (P-TASK-11): skip already-completed items
        existing_done_ids = {
            it["item_key"]
            for it in self.store.list_job_items(job["job_id"])
            if it["status"] == "completed"
        }
        preserved_preds: dict[str, dict[str, Any]] = {}
        pending_tasks = []
        for t in tasks:
            uid = t.audit_unit_id
            if uid in existing_done_ids:
                prior = self.store.get_audit_result(f"{job['job_id']}:{uid}")
                if prior is not None and isinstance(prior.get("prediction"), dict):
                    preserved_preds[uid] = prior["prediction"]
                    continue
            pending_tasks.append(t)

        gateway = self._gateway_factory() if self._gateway_factory else None
        transport = self._transport_factory() if self._transport_factory else None

        new_preds: dict[str, dict[str, Any]] = {}
        effective_data_root = Path(data_root) if data_root else input_jsonl.parent
        artifact_version = job.get("artifact_version") or ""
        artifact_root_sha256 = job.get("artifact_root_sha256") or ""

        if pending_tasks:
            if preserved_preds:
                pending_jsonl = self.workspace_dir / "resume_inputs" / f"{job['job_id']}.jsonl"
                pending_jsonl.parent.mkdir(parents=True, exist_ok=True)
                pending_jsonl.write_text(
                    "\n".join(
                        json.dumps(tasks_by_id[t.audit_unit_id], ensure_ascii=False)
                        for t in pending_tasks
                    ) + "\n",
                    encoding="utf-8",
                )
                effective_input = pending_jsonl
            else:
                effective_input = input_jsonl

            res = run_inference(
                input_jsonl=effective_input,
                artifacts_dir=build_dir,
                out_path=out_path,
                data_root=effective_data_root,
                model_config_path=payload.get("model_config"),
                workspace=self.workspace_dir,
                run_id=run_id,
                gateway=gateway,
                transport=transport,
            )
            artifact_version = res.artifact_version
            artifact_root_sha256 = res.artifact_root_sha256
            effective_data_root = res.data_root

            for pred in res.predictions:
                uid = pred["audit_unit_id"]
                new_preds[uid] = pred
                rid = f"{job['job_id']}:{uid}"
                self.store.record_audit_result(
                    result_id=rid,
                    job_id=job["job_id"],
                    run_id=res.run_id,
                    audit_unit_id=uid,
                    rule_id=pred["rule_id"],
                    rule_version=pred["rule_version"],
                    artifact_version=res.artifact_version,
                    artifact_root_sha256=res.artifact_root_sha256,
                    data_root=str(res.data_root),
                    out_path=str(out_path),
                    task=tasks_by_id[uid],
                    prediction=pred,
                )
                self.store.upsert_job_item(
                    job["job_id"], uid,
                    "completed" if pred["execution_status"] == "completed" else "failed",
                    artifact_version=res.artifact_version,
                    result={"label": pred.get("label"), "applicability": pred.get("applicability")},
                    error={"code": pred.get("error_code"), "reason": pred.get("reason")}
                          if pred["execution_status"] == "error" else None,
                )

        # Write full merged predictions in original input order when resuming
        merged_preds = [
            preserved_preds[t.audit_unit_id] if t.audit_unit_id in preserved_preds else new_preds[t.audit_unit_id]
            for t in tasks
        ]
        if preserved_preds:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(
                "\n".join(json.dumps(p, ensure_ascii=False, allow_nan=False) for p in merged_preds) + "\n",
                encoding="utf-8",
            )

        return {
            "run_id": run_id,
            "version_name": vname,
            "build_dir": str(build_dir),
            "input_jsonl": str(input_jsonl),
            "out_path": str(out_path),
            "artifact_version": artifact_version,
            "artifact_root_sha256": artifact_root_sha256,
            "total": len(merged_preds),
            "completed": sum(1 for p in merged_preds if p.get("execution_status") == "completed"),
            "errors": sum(1 for p in merged_preds if p.get("execution_status") == "error"),
            "skipped_completed_items": len(preserved_preds),
            "newly_executed_items": len(new_preds),
        }

    def _handle_annotate(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = job["payload"]
        build_dir, vname = self._resolve_build_dir(payload)
        input_jsonl = Path(payload["input_jsonl"])
        targets_jsonl = Path(payload["targets_jsonl"])
        out_path = Path(
            payload.get("out_path")
            or (self.workspace_dir / "outputs" / f"{job['job_id']}_annotations.jsonl")
        )
        run_id = str(payload.get("run_id") or f"ann-{job['job_id']}")
        gateway = self._gateway_factory() if self._gateway_factory else None
        transport = self._transport_factory() if self._transport_factory else None

        res = run_annotation(
            input_jsonl=input_jsonl,
            targets_jsonl=targets_jsonl,
            artifacts_dir=build_dir,
            out_path=out_path,
            data_root=payload.get("data_root"),
            model_config_path=payload.get("model_config"),
            workspace=self.workspace_dir,
            run_id=run_id,
            gateway=gateway,
            transport=transport,
        )
        summary = res.summary()
        summary["version_name"] = vname
        summary["build_dir"] = str(build_dir)
        return summary

    def _handle_iterate(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = job["payload"]
        base_dir, _ = self._resolve_build_dir({"build_dir": payload.get("base_dir"),
                                               "version_name": payload.get("base_version_name")})
        feedback_path = Path(payload["feedback_path"])
        input_jsonl = Path(payload["input_jsonl"])
        vname = str(payload.get("version_name") or f"cand-{job['job_id'][-8:]}")
        out_dir = Path(payload.get("out_dir") or (self.workspace_dir / "artifacts" / vname))
        res = run_iteration(
            base_dir=base_dir,
            feedback_path=feedback_path,
            input_jsonl=input_jsonl,
            out_dir=out_dir,
            artifact_version=payload.get("artifact_version"),
            version_name=vname,
            store=self.store,
            overwrite=bool(payload.get("overwrite", False)),
        )
        return res.summary()

    def _handle_evaluate(self, job: dict[str, Any]) -> dict[str, Any]:
        # Isolated evaluation import strictly inside evaluate handler
        from o2_audit.evaluation.manifest import SCHEMA_VERSION, load_plugin_registry
        from o2_audit.evaluation.runner import run_plugins
        from o2_public_contracts import contracts as public_contracts

        payload = job["payload"]
        inputs = public_contracts.read_rows(payload["inputs_path"])
        outputs = public_contracts.read_rows(payload["outputs_path"])
        references = (
            public_contracts.read_rows(payload["references_path"])
            if payload.get("references_path")
            else []
        )
        context: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "purpose": payload.get("purpose") or "product_local_evaluation",
        }
        if payload.get("expected_artifact_version"):
            context["expected_artifact_version"] = payload["expected_artifact_version"]
        if payload.get("baseline_outputs_path"):
            context["baseline_outputs"] = public_contracts.read_rows(payload["baseline_outputs_path"])

        registry = load_plugin_registry(payload.get("plugin_config_path"))
        # Apply persisted SQLite plugin toggles
        for row in self.store.list_plugin_configs():
            if registry.get(row["plugin_id"]) is not None:
                registry = registry.with_enabled(row["plugin_id"], row["enabled"])

        reports = run_plugins(
            registry,
            inputs=inputs,
            outputs=outputs,
            references=references,
            context=context,
            plugin_ids=payload.get("plugin_ids"),
            timeout=payload.get("timeout"),
            extra_pythonpath=payload.get("extra_pythonpath"),
        )
        recorded: list[dict[str, Any]] = []
        for rep in reports:
            eval_id = _short_id("eval")
            status = rep.get("status") or ("failed" if rep.get("errors") else "ok")
            self.store.record_eval_run(
                eval_id=eval_id,
                plugin_id=str(rep.get("plugin_id") or "unknown"),
                plugin_version=str(rep.get("plugin_version") or "unknown"),
                status=status,
                inputs_path=str(payload["inputs_path"]),
                outputs_path=str(payload["outputs_path"]),
                references_path=str(payload["references_path"]) if payload.get("references_path") else None,
                report=rep,
            )
            recorded.append({
                "eval_id": eval_id,
                "plugin_id": rep.get("plugin_id"),
                "plugin_version": rep.get("plugin_version"),
                "status": status,
                "invoked": rep.get("invoked", status != "disabled"),
                "metrics": rep.get("metrics", {}),
                "errors": rep.get("errors", []),
            })
        return {"evaluations": recorded, "count": len(recorded)}
