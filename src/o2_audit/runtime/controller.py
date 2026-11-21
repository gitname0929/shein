"""Minimal four-layer vertical controller (Stage 01 prototype).

Layers (mapped from OntoAgent concepts, implemented challenge-specifically —
see docs/architecture.md):

1. Semantic layer: ``FactView`` over accumulated observations + typed
   ``B001Plan`` + persistence in ``TaskStore``.
2. Intent/planning layer: checks applicability from task metadata FIRST
   (short-circuits before any gateway call when ``not_applicable`` or
   ``unknown``); inspects ``NeedEvidence`` gaps and emits permitted
   ``AllowedAction`` requests to the gateway under a strict max-round bound.
3. Constraint/control layer: calls ``evaluate_b001``, applies the protocol
   decision order, distinguishes terminal verdicts from remaining actionable
   gaps, and validates the produced output via the public
   ``prediction_semantic_errors``.
4. Execution layer: invokes the ``ObservationGateway``, records immutable
   observations and step checkpoints in the ``TaskStore`` so re-running a
   completed step recovers without duplicating gateway calls or final records,
   and converts ``ExecutionError`` into a protocol-legal error record
   (``execution_status="error"``, ``label=None``, ``error_code=...``).

The gateway returns observations only; final labels are decided exclusively by
the rule layer.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from o2_audit.domain.models import (
    AllowedAction,
    AuditInput,
    Entity,
    EntityKind,
    Evidence,
    ExecutionError,
    NeedEvidence,
    RequirementStatus,
)
from o2_audit.protocol.checks import prediction_semantic_errors, visual_evidence_required
from o2_audit.runtime.facts import FactView
from o2_audit.runtime.gateway import ObservationGateway
from o2_audit.rules.base import RulePlan, UnsupportedRuleError
from o2_audit.rules.b001.evaluate import applicability_precheck, evaluate_b001
from o2_audit.rules.b001.plan import B001Plan
from o2_audit.rules.registry import RuleRegistry, default_registry
from o2_audit.storage.store import TaskStore

# Unknown reason codes that a material inspection can actually resolve,
# mapped to (entity kind, primary predicate, candidate material roles).
_GAP_MAP: dict[str, tuple[EntityKind, str, tuple[str, ...]]] = {
    "certificate_uninspected": (EntityKind.CERTIFICATE, "material_number", ("certificate_pages",)),
    "certificate_field_unreadable": (EntityKind.CERTIFICATE, "material_number", ("certificate_pages",)),
    "certificate_void_status_unreadable": (EntityKind.CERTIFICATE, "void_marked", ("certificate_pages",)),
    "void_status_unreadable": (EntityKind.LABEL, "void_marked", ("package_photos", "tag_photos")),
    "label_field_unreadable": (EntityKind.LABEL, "model", ("package_photos", "tag_photos")),
    "model_unreadable": (EntityKind.LABEL, "model", ("package_photos", "tag_photos")),
    "material_number_unreadable": (EntityKind.LABEL, "material_number", ("package_photos", "tag_photos")),
    "carrier_identity_unknown": (EntityKind.LABEL, "carrier", ("package_photos", "tag_photos")),
    "tag_connection_uncertain": (EntityKind.LABEL, "attached_to_package", ("tag_photos", "package_photos")),
}
_MAX_GAPS_PER_ROUND = 3
_FINAL_STEP = "final_decision"

# Unknown reason codes that no material inspection can resolve: they stem from
# missing/invalid task metadata or from semantic blocks (undefined thresholds,
# non-standard views, same-level conflicts). A gateway call would be wasted.
_NON_ACTIONABLE_EXACT = frozenset({
    "applicability_unknown",
    "package_size_missing",
    "package_size_invalid",
    "required_faces_missing",
    "total_pages_missing",
    "required_version_missing",
    "language_mode_invalid",
    "review_date_invalid",
    "undefined_small_threshold",
    "grayscale_photo",
    "non_standard_view",
    "withdrawal_before_issue",
    "same_level_conflict",
})


def _non_actionable_reason(code: str) -> bool:
    """Metadata/semantic blocks (declared_*_missing / declared_*_invalid and
    the explicit list) can never be fixed by inspecting materials."""
    if code in _NON_ACTIONABLE_EXACT:
        return True
    return code.startswith("declared_") and code.endswith(("_missing", "_invalid"))


class AuditController:
    """Drives one audit unit through the four-layer cycle.

    Backward compatible with the Stage 01 B001 usage
    (``AuditController(plan, gateway, ...)``). The controller ALSO accepts any
    registered :class:`RulePlan`, or ``plan=None`` with a ``registry``
    (defaulting to :func:`default_registry`) that resolves
    ``(task.rule_id, task.rule_version)`` per task — raising
    :class:`UnsupportedRuleError` for unknown or mismatched pairs.
    """

    def __init__(
        self,
        plan: RulePlan | None = None,
        gateway: ObservationGateway | None = None,
        store: TaskStore | None = None,
        run_id: str = "prototype-run",
        artifact_version: str = "b001-prototype-0.1.0",
        max_rounds: int = 3,
        registry: RuleRegistry | None = None,
    ) -> None:
        self.plan = plan
        self.gateway = gateway
        self.store = store
        self.run_id = run_id
        self.artifact_version = artifact_version
        if max_rounds < 0:
            raise ValueError("max_rounds must be >= 0")
        self.max_rounds = max_rounds
        self.registry = registry
        # Materials whose inspection was actually requested (used to cite
        # honestly-inspected-but-uninformative materials when no observation
        # could be recorded, keeping completed rows protocol-legal).
        self._inspected: set[tuple[str, int]] = set()
        # Plan resolved for the currently running task (None outside run()).
        self._active_plan: RulePlan | None = None

    # -- public entry ------------------------------------------------------

    def _registry(self) -> RuleRegistry:
        return self.registry if self.registry is not None else default_registry()

    def _resolve_plan(self, task: AuditInput) -> RulePlan:
        """Plan for this task: the explicit plan must match the requested
        (rule_id, version); with ``plan=None`` the registry resolves it and
        raises :class:`UnsupportedRuleError` for unregistered pairs."""
        if self.plan is not None:
            if (task.rule_id, task.rule_version) \
                    != (self.plan.rule_id, self.plan.rule_version):
                raise UnsupportedRuleError(task.rule_id, task.rule_version)
            return self.plan
        return self._registry().get_plan(task.rule_id, task.rule_version)

    def run(self, task: AuditInput) -> dict[str, Any]:
        started = time.monotonic()
        task_id = task.audit_unit_id
        # Multi-task reuse of one controller must never leak inspected
        # material IDs across units.
        self._inspected = set()
        active_plan = self._resolve_plan(task)
        self._active_plan = active_plan
        if self.store is not None:
            self.store.create_run(self.run_id, "prototype", self.artifact_version, {})
            self.store.register_rule(
                active_plan.rule_id, active_plan.rule_version,
                active_plan.model_dump(mode="json"), self.artifact_version,
            )
            self.store.ensure_task(task_id, self.run_id, task.audit_unit_id,
                                   f"{task.audit_unit_id}:{task.rule_id}:{task.rule_version}")

        facts = self._load_facts(task)
        digest = self._state_digest(task, facts)
        if self.store is not None:
            cached = self.store.completed_step_output(
                self.run_id, task_id, _FINAL_STEP, digest)
            if cached is not None:
                self.store.record_attempt(self.run_id, task_id, _FINAL_STEP,
                                          "recovered", {"digest": digest})
                return cached

        # Intent layer: applicability from metadata first — a known-false or
        # unknown applicability never reaches the gateway. B001 keeps its
        # own (backward-compatible) outcome shape; advanced rules short-circuit
        # through the shared applicability outcomes.
        if isinstance(active_plan, B001Plan):
            applicability, _missing = applicability_precheck(task, active_plan)
            outcome = evaluate_b001(task, active_plan, facts)
            if applicability != "applicable":
                return self._finalize(task, outcome, facts, started)
        else:
            outcome = self._registry().evaluate(task, facts, active_plan)
            if outcome.applicability != "applicable":
                return self._finalize(task, outcome, facts, started)

        rounds = 0
        while rounds < self.max_rounds:
            gaps = self._need_evidence(outcome, task, active_plan)
            if not gaps:
                break
            new_observations = 0
            for gap in gaps:
                result = self._execute_gap(task, facts, gap, rounds)
                if isinstance(result, dict):  # ExecutionError -> protocol row
                    return result
                new_observations += result
            if new_observations == 0:
                break  # gateway made no progress; stop instead of looping
            outcome = self._evaluate(task, active_plan, facts)
            rounds += 1

        return self._finalize(task, outcome, facts, started)

    def _evaluate(self, task: AuditInput, plan: RulePlan, facts: FactView):
        if isinstance(plan, B001Plan):
            return evaluate_b001(task, plan, facts)
        return self._registry().evaluate(task, facts, plan)

    # -- semantic layer ------------------------------------------------------

    def _load_facts(self, task: AuditInput) -> FactView:
        facts = FactView()
        if self.store is None:
            return facts
        for obs in self.store.observations_for(task.audit_unit_id, run_id=self.run_id):
            facts.add_observation(obs)
        # Entities live in step checkpoints (observations are the immutable
        # primary record); restore any recorded so far.
        entity_ids: set[str] = set()
        for row in self._completed_inspect_checkpoints(task.audit_unit_id):
            for ent in row:
                if ent["entity_id"] not in entity_ids:
                    entity_ids.add(ent["entity_id"])
                    facts.add_entity(Entity.model_validate(ent))
        return facts

    def _completed_inspect_checkpoints(self, audit_unit_id: str) -> list[list[dict[str, Any]]]:
        assert self.store is not None
        # Entity registries live in one dedicated step checkpoint keyed by the
        # constant digest "entity_registry".
        checkpoint = self.store.completed_step_checkpoint(
            self.run_id, audit_unit_id, "entities", "entity_registry")
        if checkpoint and isinstance(checkpoint.get("entities"), list):
            return [checkpoint["entities"]]
        return []

    def _state_digest(self, task: AuditInput, facts: FactView) -> str:
        plan = self._active_plan
        payload = {
            "task": self._task_json(task),
            "plan": f"{plan.rule_id}:{plan.rule_version}" if plan is not None else None,
            "entities": sorted(e.entity_id for e in facts.all_entities()),
            "observations": [
                o.model_dump_json() for o in
                sorted(facts.all_observations(), key=lambda o: (o.entity_id, o.predicate, o.status.value))
            ],
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False, default=str).encode("utf-8")
        ).hexdigest()

    def _task_json(self, task: AuditInput) -> dict[str, Any]:
        d = task.model_dump(mode="json")
        d["metadata"] = task.metadata.values
        return d

    # -- intent / planning layer ----------------------------------------------

    def _need_evidence(self, outcome: Any, task: AuditInput,
                       plan: RulePlan | None = None) -> list[NeedEvidence]:
        """Turn remaining conclusion-affecting unknowns into permitted
        inspection requests (bounded per round).

        B001 keeps its Stage 01 gap map exactly. For any other plan, an UNKNOWN
        requirement whose reason code is actionable (not a metadata/semantic
        block) is mapped through ``plan.gap_for``; if the role filter matches
        no task material, all task materials become candidates.
        """
        gaps: list[NeedEvidence] = []
        seen: set[tuple[str, str]] = set()
        is_b001 = isinstance(plan, B001Plan)
        for r in outcome.results:
            if r.status != RequirementStatus.UNKNOWN:
                continue
            if r.reason_code in _GAP_MAP:
                kind, predicate, roles = _GAP_MAP[r.reason_code]
            elif is_b001 or _non_actionable_reason(r.reason_code):
                continue
            elif plan is not None:
                hint = plan.gap_for(r.reason_code)
                if hint is None:
                    continue
                kind, predicate, roles = EntityKind(hint[0]), hint[1], hint[2]
            else:
                continue
            if r.reason_code in seen:
                continue
            seen.add(r.reason_code)
            candidates = [m.material_id for m in task.materials if m.role in roles]
            if not candidates and not is_b001:
                # Role filter matched nothing: the inspection may still find
                # the predicate on any submitted material.
                candidates = [m.material_id for m in task.materials]
            gaps.append(NeedEvidence(
                entity_kind=kind,
                predicate=predicate,
                candidate_material_ids=candidates,
                allowed_actions=[AllowedAction.INSPECT_MATERIAL],
                reason=r.reason_code,
            ))
            if len(gaps) >= _MAX_GAPS_PER_ROUND:
                break
        return gaps

    # -- execution layer -------------------------------------------------------

    def _execute_gap(self, task: AuditInput, facts: FactView, gap: NeedEvidence,
                     round_no: int) -> int | dict[str, Any]:
        """Run one inspection request; returns count of NEW observations, or an
        error row on ``ExecutionError``. Completed identical steps are reused
        without another gateway call."""
        task_id = task.audit_unit_id
        gap_digest = hashlib.sha256(
            json.dumps({
                "round": round_no,
                "gap": gap.model_dump(mode="json"),
                "state": self._state_digest(task, facts),
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
               allow_nan=False, default=str).encode("utf-8")
        ).hexdigest()
        step_name = f"inspect:{gap.reason}"
        for mid in gap.candidate_material_ids:
            for m in task.materials:
                if m.material_id == mid:
                    self._inspected.add((m.material_id, m.page_index))
        if self.store is not None:
            cached = self.store.completed_step_output(
                self.run_id, task_id, step_name, gap_digest)
            if cached is not None:
                self.store.record_attempt(self.run_id, task_id, step_name,
                                          "recovered", {"digest": gap_digest})
                return 0  # observations already in store/facts

        try:
            batch = self.gateway.observe(gap)
        except ExecutionError as e:
            if self.store is not None:
                self.store.record_step(self.run_id, task_id, step_name, gap_digest, round_no,
                                       "error", None, None)
                self.store.record_attempt(self.run_id, task_id, step_name, "error",
                                          {"error_code": e.error_code, "message": e.message})
            return self._error_row(task, e)

        stored_new = 0
        if self.store is not None:
            for obs in batch.observations:
                if self.store.add_observation(self.run_id, task.audit_unit_id, obs):
                    stored_new += 1
            self.store.record_step(
                self.run_id, task_id, step_name, gap_digest, round_no, "completed",
                {"observations": len(batch.observations), "entities": len(batch.entities)},
                None,
            )
            self._register_entities(task_id, batch.entities)
            self.store.record_attempt(self.run_id, task_id, step_name, "completed",
                                      {"observations": len(batch.observations),
                                       "entities": len(batch.entities)})
        for ent in batch.entities:
            facts.add_entity(ent)
        for obs in batch.observations:
            facts.add_observation(obs)
        # Progress = observations new to this run's fact state (without a
        # store, every returned observation is new to the in-memory facts).
        new_observations = len(batch.observations) if self.store is None else stored_new
        return new_observations

    def _register_entities(self, task_id: str, entities: list[Entity]) -> None:
        """Merge gateway-reported entities into the entity-registry checkpoint."""
        assert self.store is not None
        checkpoint = self.store.completed_step_checkpoint(
            self.run_id, task_id, "entities", "entity_registry") or {"entities": []}
        known = {e["entity_id"] for e in checkpoint.get("entities", [])}
        merged = list(checkpoint.get("entities", []))
        for ent in entities:
            if ent.entity_id not in known:
                known.add(ent.entity_id)
                merged.append(ent.model_dump(mode="json"))
        self.store.record_step(self.run_id, task_id, "entities", "entity_registry", 0,
                               "completed", None, {"entities": merged})

    # -- control layer ----------------------------------------------------------

    def _fallback_evidence(self, facts: FactView, task: AuditInput) -> list[Evidence]:
        """Protocol-legal minimum evidence when a completed applicable verdict
        produced no per-requirement evidence: cite the observations actually
        recorded, then materials whose inspection was actually requested
        (never fabricated coordinates)."""
        pages = {(m.material_id, m.page_index) for m in task.materials}
        out: list[Evidence] = []
        seen: set[tuple[str, int]] = set()
        for obs in facts.all_observations():
            if obs.source is None:
                continue
            key = (obs.source.material_id, obs.source.page_index)
            if key not in pages or key in seen:
                continue
            seen.add(key)
            out.append(Evidence(
                material_id=obs.source.material_id,
                page_index=obs.source.page_index,
                fact=f"已检查材料并记录观察: {obs.predicate}({obs.status.value})",
            ))
            if len(out) >= 3:
                break
        for mid, page in sorted(self._inspected):
            if (mid, page) in seen:
                continue
            seen.add((mid, page))
            out.append(Evidence(
                material_id=mid,
                page_index=page,
                fact="已完成规定材料检查但未记录到可用观察，证据不足",
            ))
            if len(out) >= 3:
                break
        return out

    def _finalize(self, task: AuditInput, outcome: Any, facts: FactView, started: float) -> dict[str, Any]:
        latency_ms = max(0, int((time.monotonic() - started) * 1000))
        evidence = list(outcome.evidence)
        if (outcome.applicability == "applicable"
                and visual_evidence_required(task, outcome.applicability)
                and not evidence):
            evidence = self._fallback_evidence(facts, task)
        row: dict[str, Any] = {
            "audit_unit_id": task.audit_unit_id,
            "rule_id": task.rule_id,
            "rule_version": task.rule_version,
            "execution_status": "completed",
            "label": outcome.label,
            "applicability": outcome.applicability,
            "reason_code": outcome.reason_code,
            "reason": outcome.reason,
            "evidence": [e.model_dump(mode="json", exclude_none=True) for e in evidence],
            "artifact_version": self.artifact_version,
            "trace_id": f"{self.run_id}:{task.audit_unit_id}"[:256],
            "latency_ms": latency_ms,
            "cost_units": None,
        }
        errors = prediction_semantic_errors(row, task)
        if errors:
            raise RuntimeError(
                f"controller produced protocol-invalid output: {errors}; row={row!r}")
        if self.store is not None:
            facts_state = self._state_digest(task, facts)
            self.store.record_step(self.run_id, task.audit_unit_id, _FINAL_STEP,
                                   facts_state, 0, "completed", row, None)
            self.store.record_attempt(self.run_id, task.audit_unit_id, _FINAL_STEP,
                                      "completed", {"label": outcome.label,
                                                    "reason_code": outcome.reason_code})
            self.store.finish_task(task.audit_unit_id, "completed")
        return row

    def _error_row(self, task: AuditInput, err: ExecutionError) -> dict[str, Any]:
        row: dict[str, Any] = {
            "audit_unit_id": task.audit_unit_id,
            "rule_id": task.rule_id,
            "rule_version": task.rule_version,
            "execution_status": "error",
            "label": None,
            "applicability": None,
            "reason_code": "execution_error",
            "reason": err.message,
            "evidence": [],
            "artifact_version": self.artifact_version,
            "trace_id": f"{self.run_id}:{task.audit_unit_id}"[:256],
            "latency_ms": 0,
            "cost_units": None,
            "error_code": err.error_code,
        }
        errors = prediction_semantic_errors(row, task)
        if errors:
            raise RuntimeError(
                f"controller produced protocol-invalid error row: {errors}; row={row!r}")
        if self.store is not None:
            self.store.record_attempt(self.run_id, task.audit_unit_id, _FINAL_STEP,
                                      "error", {"error_code": err.error_code})
            self.store.finish_task(task.audit_unit_id, "error")
        return row


__all__ = ["AuditController"]
