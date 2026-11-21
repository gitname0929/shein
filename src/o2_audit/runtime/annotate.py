"""Target-directed annotation runner (Stage 03b).

``annotate`` reads the audit tasks (``INPUT_JSONL``, protocol input shape) and
the annotation targets (``TARGETS_JSONL``, ``annotation_task.schema.json``) and
produces ``annotation.schema.json`` rows:

- The WHOLE-UNIT rule ``label`` (``pass``/``reject``/``ambiguous``) comes from
  the SAME inference pipeline as :mod:`o2_audit.runtime.infer` — the label is
  the rule verdict over ALL of the unit's materials, never "the annotated text
  field is valid". Units referenced by several targets are inferred ONCE
  (deduplicated) and their label is shared.
- Target extraction goes through ``gateway.annotate_target(...)`` on the exact
  ``(material_id, page_index)``. The returned ``text`` is normalized ONLY with
  ``annotation_nfkc_strip`` (Unicode NFKC + ``strip()``); internal case,
  punctuation and spaces are preserved verbatim — business ``model_normalize``
  is NEVER applied to annotation values.
- ``status`` semantics per 标注任务与样例.md: ``value`` = target readable and
  valid; ``unreadable`` = target not readable (``value`` forced to JSON null);
  ``invalid_value`` = readable target itself violates the field's format/value
  convention. Another field's violation never turns this target into
  ``invalid_value`` (each target is extracted independently).
- Failures are HONEST: inference failure (``execution_status="error"`` /
  ``label=None``), target extraction failure, unlocatable bbox, schema-invalid
  rows and invalid target references are written as structured records to a
  sibling ``<out>.errors.jsonl`` (or ``--errors-out``) — no fabricated
  ``unreadable`` rows and no fake answers in ``ANNOTATIONS_JSONL``.
- Emitted rows are validated with the vendored
  ``o2_public_contracts.contracts.annotation_errors`` before writing.
- Annotation state is checkpointed in the workspace SQLite keyed by the exact
  target payload + unit label, so re-runs resume without duplicate rows or
  repeated gateway calls. The frozen artifact stays byte-identical (root
  digest re-verified after the run).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema

from o2_audit.build.loader import ArtifactIntegrityError
from o2_audit.domain.models import AuditInput, ExecutionError
from o2_audit.model_gateway.cache import ExtractionCache
from o2_audit.protocol.checks import annotation_semantic_errors
from o2_audit.protocol.jsonio import ProtocolError, dump_json_strict, read_jsonl_strict
from o2_audit.resources import protocol_dir
from o2_audit.storage.store import TaskStore
from o2_public_contracts import artifact_hash
from o2_public_contracts import contracts as public_contracts

from o2_audit.runtime.infer import (
    build_gateway,
    default_run_id,
    execute_units,
    load_config_or_default,
    load_input_tasks,
    resolve_data_root,
    safe_name,
    task_json,
)

TARGET_SCHEMA_PATH = protocol_dir() / "annotation_task.schema.json"

_FIELDS = {
    "annotation_id", "audit_unit_id", "material_id", "page_index",
    "target", "value_type", "normalization", "bbox_format",
}


@dataclass
class AnnotationIssue:
    """One structured, honest failure record (goes to ``<out>.errors.jsonl``)."""

    annotation_id: str | None
    audit_unit_id: str | None
    error_code: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def record(self) -> dict[str, Any]:
        return {
            "annotation_id": self.annotation_id,
            "audit_unit_id": self.audit_unit_id,
            "error_code": self.error_code,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass
class AnnotationResult:
    """Summary of one annotation run (no fabricated rows, no fake scores)."""

    run_id: str
    annotations: list[dict[str, Any]]
    issues: list[dict[str, Any]]
    out_path: Path
    errors_path: Path
    trace_path: Path
    artifact_version: str
    artifact_root_sha256: str
    usage: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "targets": len(self.annotations) + len(self.issues),
            "annotated": len(self.annotations),
            "errors": len(self.issues),
            "artifact_version": self.artifact_version,
            "artifact_root_sha256": self.artifact_root_sha256,
            "out": str(self.out_path),
            "errors_out": str(self.errors_path),
            "trace": str(self.trace_path),
            "usage": self.usage,
        }


# ---------------------------------------------------------------------------
# target loading (annotation_task.schema.json + public strictness)
# ---------------------------------------------------------------------------

def load_targets(targets_jsonl: str | Path,
                 tasks_by_id: dict[str, AuditInput],
                 ) -> tuple[list[dict[str, Any]], list[AnnotationIssue]]:
    """Strictly load and validate annotation targets.

    Schema violations, duplicate ``annotation_id``s and targets referencing an
    unknown audit unit are reported as issues (never silently dropped); the
    remaining targets are returned in input order.
    """
    schema = json.loads(TARGET_SCHEMA_PATH.read_text(encoding="utf-8"))
    path = Path(targets_jsonl)
    rows = read_jsonl_strict(path, missing_ok=False)
    targets: list[dict[str, Any]] = []
    issues: list[AnnotationIssue] = []
    seen: set[str] = set()
    for i, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            issues.append(AnnotationIssue(
                None, None, "invalid_target",
                f"{path.name}:{i}: target row is not a JSON object"))
            continue
        try:
            jsonschema.validate(instance=row, schema=schema)
        except jsonschema.ValidationError as e:
            issues.append(AnnotationIssue(
                row.get("annotation_id") if isinstance(row.get("annotation_id"), str) else None,
                row.get("audit_unit_id") if isinstance(row.get("audit_unit_id"), str) else None,
                "invalid_target",
                f"{path.name}:{i}: target violates annotation_task.schema.json: "
                f"{e.message}"))
            continue
        # Schema-level "integer" would accept 1.0 / booleans via JSON Schema
        # semantics; the protocol requires a strict JSON integer.
        if isinstance(row["page_index"], bool) or type(row["page_index"]) is not int \
                or row["page_index"] < 1:
            issues.append(AnnotationIssue(
                row["annotation_id"], row["audit_unit_id"], "invalid_target",
                f"{path.name}:{i}: page_index must be a strict JSON integer >= 1"))
            continue
        if row["annotation_id"] in seen:
            issues.append(AnnotationIssue(
                row["annotation_id"], row["audit_unit_id"], "duplicate_target",
                f"{path.name}:{i}: duplicate annotation_id {row['annotation_id']!r}"))
            continue
        task = tasks_by_id.get(row["audit_unit_id"])
        if task is None:
            issues.append(AnnotationIssue(
                row["annotation_id"], row["audit_unit_id"], "unknown_audit_unit",
                f"{path.name}:{i}: target references audit_unit_id "
                f"{row['audit_unit_id']!r} which is not in INPUT_JSONL"))
            continue
        seen.add(row["annotation_id"])
        targets.append(row)
    return targets, issues


def _target_material(task: AuditInput, material_id: str,
                     page_index: int) -> Any | None:
    """The task material the target points at (exact material + page), or None."""
    for m in task.materials:
        if m.material_id == material_id:
            return m if m.page_index == page_index else None
    return None


def _annotation_digest(target: dict[str, Any], label: str) -> str:
    payload = dump_json_strict({"target": target, "label": label})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# batch entry point
# ---------------------------------------------------------------------------

def run_annotation(
    input_jsonl: str | Path,
    targets_jsonl: str | Path,
    artifacts_dir: str | Path,
    out_path: str | Path,
    *,
    data_root: str | Path | None = None,
    model_config_path: str | Path | None = None,
    workspace: str | Path | None = None,
    run_id: str | None = None,
    errors_out: str | Path | None = None,
    transport: Any = None,
    gateway: Any = None,
) -> AnnotationResult:
    """Run target-directed annotation and write ``ANNOTATIONS_JSONL``.

    Errors are recorded in ``<out>.errors.jsonl`` (sibling by default) —
    failed targets never produce fabricated annotation rows.
    """
    in_path = Path(input_jsonl)
    targets_path = Path(targets_jsonl)
    out = Path(out_path)
    errors_path = Path(errors_out) if errors_out is not None else \
        Path(f"{out}.errors.jsonl")

    from o2_audit.build.loader import verify_build

    artifact = verify_build(artifacts_dir)  # frozen, read-only
    digest_before = artifact_hash.tree_digest(artifact.build_dir)

    tasks = load_input_tasks(in_path)
    tasks_by_id = {t.audit_unit_id: t for t in tasks}
    root = resolve_data_root(in_path, data_root)

    targets, issues = load_targets(targets_path, tasks_by_id)

    ws = Path(workspace) if workspace is not None else \
        Path(__file__).resolve().parents[3] / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    store = TaskStore(ws / "state.sqlite3")
    try:
        config = load_config_or_default(model_config_path)
        if run_id:
            effective_run_id = run_id
        else:
            combined_sha = artifact_hash.digest_bytes(
                in_path.read_bytes() + b"\x00" + targets_path.read_bytes()
            )
            effective_run_id = f"annotate-{digest_before[:12]}-{combined_sha[:12]}"

        # -- 1. whole-unit inference for every REFERENCED unit (deduplicated)
        referenced: list[str] = []
        seen_units: set[str] = set()
        for t in targets:
            uid = t["audit_unit_id"]
            if uid not in seen_units:
                seen_units.add(uid)
                referenced.append(uid)
        unit_tasks = [tasks_by_id[uid] for uid in referenced]
        unit_rows: dict[str, dict[str, Any]] = {}
        trace_records: list[dict[str, Any]] = []
        usage: dict[str, int] = {}
        gateway_obj = gateway
        if gateway_obj is None:
            cache = ExtractionCache(store)
            gateway_obj = build_gateway(config, root, cache, transport=transport)
        if unit_tasks:
            rows, trace_records = execute_units(
                unit_tasks, artifact=artifact, gateway=gateway_obj, store=store,
                run_id=effective_run_id)
            unit_rows = {r["audit_unit_id"]: r for r in rows}

        # -- 2. target-directed extraction (one gateway call per target)
        annotations: list[dict[str, Any]] = []
        for target in targets:
            aid = target["annotation_id"]
            uid = target["audit_unit_id"]
            task = tasks_by_id[uid]
            issue = _annotate_one_target(
                target, task, unit_rows.get(uid),
                artifact_version=_artifact_version(artifact),
                gateway=gateway_obj, store=store, run_id=effective_run_id)
            if isinstance(issue, dict):
                annotations.append(issue)
            else:
                issues.append(issue)

        usage = gateway_obj.usage_summary() if hasattr(gateway_obj, "usage_summary") else {}

        digest_after = artifact_hash.tree_digest(artifact.build_dir)
        if digest_after != digest_before:
            raise ArtifactIntegrityError(
                "frozen artifact changed during annotation "
                f"(before {digest_before}, after {digest_after})")

        trace_records = list(trace_records)
        trace_records.append({
            "type": "annotation_target",
            "run_id": effective_run_id,
            "targets": len(targets),
            "annotated": len(annotations),
            "issues": len(issues),
            "usage": usage,
        })

        out.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(
            json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n"
            for r in annotations)
        out.write_text(payload, encoding="utf-8")

        errors_path.parent.mkdir(parents=True, exist_ok=True)
        errors_payload = "".join(
            json.dumps(i, ensure_ascii=False, allow_nan=False) + "\n"
            for i in (issue.record() for issue in issues))
        errors_path.write_text(errors_payload, encoding="utf-8")

        trace_path = ws / "runs" / safe_name(effective_run_id) / "trace.jsonl"
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text(
            "".join(json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n"
                    for r in trace_records),
            encoding="utf-8")

        return AnnotationResult(
            run_id=effective_run_id,
            annotations=annotations,
            issues=[i.record() for i in issues],
            out_path=out,
            errors_path=errors_path,
            trace_path=trace_path,
            artifact_version=_artifact_version(artifact),
            artifact_root_sha256=digest_after,
            usage=usage,
        )
    finally:
        store.close()


def _artifact_version(artifact: Any) -> str:
    versions = {e["artifact_version"] for e in artifact.entries}
    assert len(versions) == 1
    return versions.pop()


def _annotate_one_target(
    target: dict[str, Any],
    task: AuditInput,
    unit_row: dict[str, Any] | None,
    *,
    artifact_version: str,
    gateway: Any,
    store: TaskStore,
    run_id: str,
) -> dict[str, Any] | AnnotationIssue:
    """Extract one target; returns the annotation row or an honest issue."""
    aid = target["annotation_id"]
    uid = target["audit_unit_id"]

    # -- the unit's rule verdict must exist and be completed
    if unit_row is None or unit_row.get("execution_status") != "completed" \
            or unit_row.get("label") is None:
        detail = {
            "execution_status": unit_row.get("execution_status") if unit_row else None,
            "error_code": unit_row.get("error_code") if unit_row else None,
            "reason_code": unit_row.get("reason_code") if unit_row else None,
        }
        return AnnotationIssue(
            aid, uid, "inference_failed",
            "whole-unit inference did not complete; no annotation is fabricated",
            detail)
    label = unit_row["label"]

    # -- the target must point at an exact (material, page) of this unit
    material = _target_material(task, target["material_id"], target["page_index"])
    if material is None:
        return AnnotationIssue(
            aid, uid, "target_material_page_not_in_input",
            "target (material_id, page_index) is not a material of the referenced "
            "audit unit")

    # -- resume: identical target + identical unit label reuses the stored row
    step_name = f"annotate:{aid}"
    digest = _annotation_digest(target, label)
    cached = store.completed_step_output(run_id, uid, step_name, digest)
    if cached is not None:
        store.record_attempt(run_id, uid, step_name, "recovered", {"digest": digest})
        return cached

    # -- extraction via the real gateway on the exact material/page
    try:
        gateway.bind_task(task, plan_identity=f"{task.rule_id}:{task.rule_version}")
        result = gateway.annotate_target(target["target"], [target["material_id"]])
    except ExecutionError as e:
        store.record_attempt(run_id, uid, step_name, "error",
                             {"error_code": e.error_code, "message": e.message})
        return AnnotationIssue(
            aid, uid, e.error_code,
            "target extraction failed; no fabricated unreadable row is written",
            {"message": e.message})

    if result.bbox is None:
        return AnnotationIssue(
            aid, uid, "annotation_bbox_missing",
            "the target region could not be located; a bbox is never fabricated",
            {"status": result.status})

    row: dict[str, Any] = {
        "annotation_id": aid,
        "audit_unit_id": uid,
        "material_id": target["material_id"],
        "page_index": target["page_index"],
        "value": result.text if result.status in ("value", "invalid_value") else None,
        "status": result.status,
        "label": label,
        "bbox": [float(v) for v in result.bbox],
    }

    errors = annotation_semantic_errors(row, target, task)
    if not errors:
        errors = public_contracts.annotation_errors(row, target, task_json(task))
    if errors:
        store.record_attempt(run_id, uid, step_name, "error", {"errors": errors})
        return AnnotationIssue(
            aid, uid, "annotation_invalid",
            "assembled annotation row violates the public annotation contract",
            {"errors": errors, "row": row})

    store.record_step(run_id, uid, step_name, digest, 0, "completed", row, None)
    store.record_attempt(run_id, uid, step_name, "completed", {"status": result.status})
    return row


__all__ = [
    "AnnotationIssue",
    "AnnotationResult",
    "load_targets",
    "run_annotation",
]
