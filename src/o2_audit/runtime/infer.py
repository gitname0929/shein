"""Batch inference runner (Stage 03b).

Runs the four-layer :class:`~o2_audit.runtime.controller.AuditController` over
every unit of ``INPUT_JSONL`` bound to a frozen ``BUILD_DIR``:

- The artifact is verified with
  :func:`o2_audit.build.loader.verify_build` (manifest digests + capability
  hash binding) and loaded READ-ONLY; the whole-directory root SHA256 is
  recomputed after the run and any change raises
  :class:`ArtifactIntegrityError` (zero writes inside ``BUILD_DIR``).
- ``INPUT_JSONL`` is read strictly via the vendored public ``read_rows``
  (duplicate keys / NaN / Infinity / 64 MiB-512 KiB-100k limits) and each row
  is typed into :class:`~o2_audit.domain.models.AuditInput` and re-checked
  with the public ``input_errors``.
- Execution state, observations, step checkpoints, attempts and usage live in
  the workspace SQLite (never inside the artifact). The default ``run_id`` is
  deterministic (artifact root digest + input digest), so re-running the same
  batch RESUMES: completed units are recovered from their final-step
  checkpoints without another gateway call and final rows are never
  duplicated.
- Every input ``audit_unit_id`` yields exactly one output row in input order:
  tool/network/missing-config failures become protocol-legal
  ``execution_status="error"`` rows (``label=None``) — the denominator never
  shrinks; a rule pair missing from the frozen manifest becomes an
  ``unsupported_rule`` error row (never a fabricated verdict).
- Every row is validated with the vendored ``prediction_errors`` and
  :func:`o2_audit.protocol.checks.prediction_semantic_errors` before it is
  written; a violation is a programming error and aborts the run.
- Reference/gold files are never read anywhere in this module.

The optional ``transport`` / ``gateway`` parameters are TEST-ONLY injection
points (httpx.MockTransport / stub gateway); production inference always uses
the real :class:`~o2_audit.model_gateway.gateway.ZCodeObservationGateway`.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from o2_audit.build.loader import ArtifactIntegrityError, LoadedArtifact, verify_build
from o2_audit.domain.models import AuditInput, ExecutionError
from o2_audit.model_gateway.cache import ExtractionCache
from o2_audit.model_gateway.config import ModelConfig, load_model_config
from o2_audit.model_gateway.gateway import ZCodeObservationGateway
from o2_audit.protocol.checks import input_semantic_errors, prediction_semantic_errors
from o2_audit.protocol.jsonio import ProtocolError, read_jsonl_strict
from o2_audit.rules.base import UnsupportedRuleError
from o2_audit.storage.store import TaskStore
from o2_public_contracts import artifact_hash
from o2_public_contracts import contracts as public_contracts

_TRACE_NAME = "trace.jsonl"


@dataclass
class InferenceResult:
    """Summary of one batch inference run (honest accounting, no scores)."""

    run_id: str
    predictions: list[dict[str, Any]]
    out_path: Path
    trace_path: Path
    data_root: Path
    artifact_version: str
    artifact_root_sha256: str
    completed: int = 0
    errors: int = 0
    usage: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "units": len(self.predictions),
            "completed": self.completed,
            "errors": self.errors,
            "artifact_version": self.artifact_version,
            "artifact_root_sha256": self.artifact_root_sha256,
            "data_root": str(self.data_root),
            "out": str(self.out_path),
            "trace": str(self.trace_path),
            "usage": self.usage,
        }


# ---------------------------------------------------------------------------
# input loading (strict, public-contract bound)
# ---------------------------------------------------------------------------

def load_input_tasks(input_jsonl: str | Path) -> list[AuditInput]:
    """Strictly read INPUT_JSONL and type every row as an :class:`AuditInput`.

    Raises :class:`ProtocolError` for malformed rows, duplicate
    ``audit_unit_id``s or rows violating the public ``input_errors`` contract.
    """
    path = Path(input_jsonl)
    rows = read_jsonl_strict(path, missing_ok=False)
    tasks: list[AuditInput] = []
    seen: set[str] = set()
    for i, row in enumerate(rows, 1):
        try:
            task = AuditInput.model_validate(row)
        except Exception as e:
            raise ProtocolError(f"{path.name}:{i}: invalid audit input row: {e}") from e
        errors = input_semantic_errors(task)
        if errors:
            raise ProtocolError(
                f"{path.name}:{i}: audit unit {task.audit_unit_id!r} violates the "
                f"public input contract: {errors}")
        if task.audit_unit_id in seen:
            raise ProtocolError(
                f"{path.name}:{i}: duplicate audit_unit_id {task.audit_unit_id!r}")
        seen.add(task.audit_unit_id)
        tasks.append(task)
    return tasks


def resolve_data_root(input_jsonl: str | Path, data_root: str | Path | None) -> Path:
    """Explicit ``--data-root`` wins; otherwise the INPUT_JSONL parent directory
    (the protocol convention: material paths are relative to the task JSONL,
    e.g. ``assets/<sample>/<file>.png`` next to it)."""
    if data_root is not None:
        return Path(data_root)
    return Path(input_jsonl).resolve().parent


def default_run_id(kind: str, artifact_root_sha256: str, input_path: Path) -> str:
    """Deterministic run identity: re-running the SAME batch against the SAME
    frozen artifact resumes instead of duplicating final rows."""
    input_sha = artifact_hash.digest_bytes(Path(input_path).read_bytes())
    return f"{kind}-{artifact_root_sha256[:12]}-{input_sha[:12]}"


def safe_name(run_id: str) -> str:
    """Filesystem-safe trace-directory name for an arbitrary run id."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", run_id)[:128] or "run"


# ---------------------------------------------------------------------------
# gateway construction (real stack; test-only transport injection)
# ---------------------------------------------------------------------------

def build_gateway(
    model_config: ModelConfig,
    data_root: Path,
    cache: ExtractionCache | None,
    transport: Any = None,
) -> ZCodeObservationGateway:
    """Real :class:`ZCodeObservationGateway` over the configured endpoint.

    ``transport`` is a TEST-ONLY injection (e.g. ``httpx.MockTransport``);
    production callers pass ``None`` and credentials resolve from the
    environment at call time (never logged, never persisted).
    """
    from o2_audit.model_gateway.client import MessagesClient

    client = MessagesClient(model_config, transport=transport)
    return ZCodeObservationGateway(client, data_root=data_root, cache=cache)


def load_config_or_default(model_config_path: str | Path | None) -> ModelConfig:
    if model_config_path is not None:
        return load_model_config(model_config_path)
    return ModelConfig()


# ---------------------------------------------------------------------------
# protocol-legal fallback rows (never fabricated verdicts)
# ---------------------------------------------------------------------------

def execution_error_row(task: AuditInput, error_code: str, message: str,
                        artifact_version: str, run_id: str) -> dict[str, Any]:
    """``execution_status="error"`` row with ``label=None`` (never ambiguous)."""
    return {
        "audit_unit_id": task.audit_unit_id,
        "rule_id": task.rule_id,
        "rule_version": task.rule_version,
        "execution_status": "error",
        "label": None,
        "applicability": None,
        "reason_code": "execution_error",
        "reason": message,
        "evidence": [],
        "artifact_version": artifact_version,
        "trace_id": f"{run_id}:{task.audit_unit_id}"[:256],
        "latency_ms": 0,
        "cost_units": None,
        "error_code": error_code,
    }


def task_json(task: AuditInput) -> dict[str, Any]:
    """Flat protocol-1.3.1 JSON shape of a typed task (metadata unwrapped)."""
    d = task.model_dump(mode="json")
    d["metadata"] = task.metadata.values
    return d


def _validate_row(row: dict[str, Any], task: AuditInput) -> None:
    errors = prediction_semantic_errors(row, task)
    if not errors:
        errors = public_contracts.prediction_errors(row, task_json(task))
    if errors:
        raise RuntimeError(
            f"inference produced protocol-invalid output for {task.audit_unit_id!r}: "
            f"{errors}; row={row!r}")


# ---------------------------------------------------------------------------
# shared execution core (used by infer AND annotate)
# ---------------------------------------------------------------------------

@dataclass
class UnitExecution:
    """Per-unit controller execution bound to one frozen artifact."""

    artifact: LoadedArtifact
    gateway: Any
    store: TaskStore
    run_id: str
    trace_records: list[dict[str, Any]] = field(default_factory=list)

    def run_unit(self, task: AuditInput) -> dict[str, Any]:
        """One protocol-legal prediction row for ``task`` (resumable)."""
        started = time.monotonic()
        controller = self.artifact.controller(
            gateway=self.gateway, store=self.store, run_id=self.run_id)
        self.gateway.bind_task(
            task, plan_identity=f"{task.rule_id}:{task.rule_version}")
        try:
            row = controller.run(task)
        except UnsupportedRuleError as e:
            # The requested (rule_id, rule_version) is not in the frozen
            # manifest: record an honest execution error, never a verdict.
            row = execution_error_row(
                task, "unsupported_rule",
                f"requested rule {task.rule_id}@{task.rule_version} is not in the "
                f"frozen artifact manifest: {e}",
                self.artifact_version, self.run_id)
        except ExecutionError as e:
            row = execution_error_row(
                task, e.error_code, e.message, self.artifact_version, self.run_id)
        _validate_row(row, task)
        self.trace_records.append({
            "type": "unit",
            "run_id": self.run_id,
            "audit_unit_id": task.audit_unit_id,
            "rule_id": task.rule_id,
            "rule_version": task.rule_version,
            "execution_status": row["execution_status"],
            "label": row.get("label"),
            "reason_code": row.get("reason_code"),
            "error_code": row.get("error_code"),
            "latency_ms": row.get("latency_ms"),
            "wall_ms": max(0, int((time.monotonic() - started) * 1000)),
            "usage": self.gateway.usage_summary() if hasattr(self.gateway, "usage_summary") else {},
        })
        return row

    @property
    def artifact_version(self) -> str:
        versions = {e["artifact_version"] for e in self.artifact.entries}
        assert len(versions) == 1
        return versions.pop()


def execute_units(
    tasks: list[AuditInput],
    *,
    artifact: LoadedArtifact,
    gateway: Any,
    store: TaskStore,
    run_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run every unit through the controller, preserving input order.

    Returns ``(prediction_rows, trace_records)``.
    """
    execution = UnitExecution(artifact=artifact, gateway=gateway, store=store,
                              run_id=run_id)
    rows = [execution.run_unit(task) for task in tasks]
    execution.trace_records.append({
        "type": "summary",
        "run_id": run_id,
        "units": len(rows),
        "completed": sum(1 for r in rows if r["execution_status"] == "completed"),
        "errors": sum(1 for r in rows if r["execution_status"] == "error"),
    })
    return rows, execution.trace_records


# ---------------------------------------------------------------------------
# batch entry point
# ---------------------------------------------------------------------------

def run_inference(
    input_jsonl: str | Path,
    artifacts_dir: str | Path,
    out_path: str | Path,
    *,
    data_root: str | Path | None = None,
    model_config_path: str | Path | None = None,
    workspace: str | Path | None = None,
    run_id: str | None = None,
    transport: Any = None,
    gateway: Any = None,
) -> InferenceResult:
    """Run batch inference and write ``PREDICTIONS_JSONL``.

    Returns an :class:`InferenceResult` summary. Raises
    :class:`ArtifactIntegrityError` for tampered/modified artifacts and
    :class:`ProtocolError` for invalid input files.
    """
    started = time.monotonic()
    in_path = Path(input_jsonl)
    out = Path(out_path)
    artifact = verify_build(artifacts_dir)  # frozen, read-only
    digest_before = artifact_hash.tree_digest(artifact.build_dir)

    tasks = load_input_tasks(in_path)
    root = resolve_data_root(in_path, data_root)

    ws = Path(workspace) if workspace is not None else \
        Path(__file__).resolve().parents[3] / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    store = TaskStore(ws / "state.sqlite3")
    try:
        config = load_config_or_default(model_config_path)
        effective_run_id = run_id or default_run_id(
            "infer", digest_before, in_path)
        cache = ExtractionCache(store)
        gw = gateway if gateway is not None else \
            build_gateway(config, root, cache, transport=transport)

        rows, trace_records = execute_units(
            tasks, artifact=artifact, gateway=gw, store=store,
            run_id=effective_run_id)

        digest_after = artifact_hash.tree_digest(artifact.build_dir)
        if digest_after != digest_before:
            raise ArtifactIntegrityError(
                "frozen artifact changed during inference "
                f"(before {digest_before}, after {digest_after})")

        completed = sum(1 for r in rows if r["execution_status"] == "completed")
        errors = sum(1 for r in rows if r["execution_status"] == "error")
        usage = gw.usage_summary() if hasattr(gw, "usage_summary") else {}
        trace_records.append({
            "type": "run_summary",
            "run_id": effective_run_id,
            "artifact_root_sha256_before": digest_before,
            "artifact_root_sha256_after": digest_after,
            "units": len(rows),
            "completed": completed,
            "errors": errors,
            "usage": usage,
            "wall_ms": max(0, int((time.monotonic() - started) * 1000)),
        })

        out.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(
            json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n" for r in rows)
        out.write_text(payload, encoding="utf-8")

        trace_path = ws / "runs" / safe_name(effective_run_id) / _TRACE_NAME
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text(
            "".join(json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n"
                    for r in trace_records),
            encoding="utf-8")

        versions = {e["artifact_version"] for e in artifact.entries}
        artifact_version = next(iter(versions)) if len(versions) == 1 else ""
        return InferenceResult(
            run_id=effective_run_id,
            predictions=rows,
            out_path=out,
            trace_path=trace_path,
            data_root=root,
            artifact_version=rows[0]["artifact_version"] if rows else artifact_version,
            artifact_root_sha256=digest_after,
            completed=completed,
            errors=errors,
            usage=usage,
        )
    finally:
        store.close()


__all__ = [
    "InferenceResult",
    "build_gateway",
    "default_run_id",
    "execute_units",
    "execution_error_row",
    "load_config_or_default",
    "load_input_tasks",
    "resolve_data_root",
    "run_inference",
    "safe_name",
]
