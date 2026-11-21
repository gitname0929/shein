"""B001 semantics and the four-layer controller, using synthetic fact graphs
and the TEST-ONLY StaticObservationGateway (no vision/OCR is implemented)."""
from __future__ import annotations

import pytest

from o2_audit.domain.models import ExecutionError
from o2_audit.protocol.checks import prediction_semantic_errors
from o2_audit.runtime.controller import AuditController
from o2_audit.runtime.facts import FactView
from o2_audit.runtime.gateway import ObservationBatch, StaticObservationGateway
from o2_audit.rules.b001.evaluate import evaluate_b001
from o2_audit.rules.b001.plan import plan_3_0, plan_3_1
from o2_audit.storage.store import TaskStore

from conftest import ent, make_task, obs, pass_batch, pass_facts


def facts_from(batch: ObservationBatch, *, drop=(), entities=None, extra=()) -> FactView:
    """Build a FactView from a batch, optionally dropping predicates and
    appending replacement observations."""
    observations = [o for o in batch.observations
                    if not (o.entity_id, o.predicate) in drop]
    observations.extend(extra)
    return FactView(entities=entities if entities is not None else list(batch.entities),
                    observations=observations)


def run_outcome(task, plan, facts):
    return evaluate_b001(task, plan, facts)


# ---------------------------------------------------------------------------
# carrier threshold: 90 mm tag allowed in 3.0, rejected in 3.1, exact boundaries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("edge,plan,expected", [
    (90, plan_3_0(), "pass"),
    (100, plan_3_0(), "reject"),   # boundary: 100 mm is NOT a small package
    (99, plan_3_0(), "pass"),
    (90, plan_3_1(), "reject"),
    (80, plan_3_1(), "reject"),    # boundary: 80 mm is NOT a small package
    (79, plan_3_1(), "pass"),
])
def test_attached_tag_threshold_boundaries(edge, plan, expected):
    task = make_task(metadata={"package_longest_edge_mm": edge}, rule_version=plan.rule_version)
    outcome = run_outcome(task, plan, pass_facts(edge_mm=edge))
    assert outcome.label == expected
    if expected == "reject":
        assert any(r.reason_code == "tag_not_allowed_for_size" for r in outcome.results)


def test_outer_package_label_allowed_at_threshold():
    task = make_task(metadata={"package_longest_edge_mm": 100})
    outcome = run_outcome(task, plan_3_0(),
                          pass_facts(carrier="outer_package", edge_mm=100))
    assert outcome.label == "pass"


def test_compliant_label_not_rejected_by_extra_nonconflicting_tag():
    # one outer_package label (compliant) + one attached_tag on a big package
    facts = pass_facts(carrier="outer_package", label_id="L1")
    facts.add_entity(ent("L2", "label"))
    facts.add_observation(obs("L2", "void_marked", False, source=("m1", 1)))
    facts.add_observation(obs("L2", "model", "LED-100", source=("m1", 1)))
    facts.add_observation(obs("L2", "material_number", "QLAB-123456", source=("m1", 1)))
    facts.add_observation(obs("L2", "carrier", "attached_tag", source=("m2", 1)))
    task = make_task(metadata={"package_longest_edge_mm": 120})
    outcome = run_outcome(task, plan_3_0(), facts)
    assert outcome.label == "pass"


def test_only_attached_tag_on_big_package_rejects():
    task = make_task(metadata={"package_longest_edge_mm": 120})
    outcome = run_outcome(task, plan_3_0(), pass_facts(edge_mm=120))
    assert outcome.label == "reject"


# ---------------------------------------------------------------------------
# tag attachment is checked separately from size
# ---------------------------------------------------------------------------


def test_attached_tag_without_proven_attachment_is_ambiguous():
    batch = pass_batch()
    facts = facts_from(batch, drop=(("L1", "attached_to_package"),))
    task = make_task()
    outcome = run_outcome(task, plan_3_0(), facts)
    assert outcome.label == "ambiguous"
    assert outcome.applicability == "applicable"


def test_confirmed_missing_attachment_rejects():
    batch = pass_batch()
    facts = facts_from(
        batch,
        drop=(("L1", "attached_to_package"),),
        extra=[obs("L1", "attached_to_package", None,
                   status="confirmed_missing", scope_complete=True,
                   source=("m2", 1))],
    )
    task = make_task()
    outcome = run_outcome(task, plan_3_0(), facts)
    assert outcome.label == "reject"
    assert any(r.reason_code == "tag_attachment_absent" for r in outcome.results)


# ---------------------------------------------------------------------------
# same-label prohibition: fields split across two labels are never merged
# ---------------------------------------------------------------------------


def test_fields_split_across_two_labels_rejects():
    batch = pass_batch()
    facts = facts_from(
        batch,
        drop=(("L1", "material_number"),),
        entities=[ent("L1", "label"), ent("L2", "label"), ent("C1", "certificate")],
        extra=[
            # L1: complete clear scope shows NO material number on that label
            obs("L1", "material_number", None, status="confirmed_missing",
                scope_complete=True, source=("m1", 1)),
            # L2: a second physical label carrying the number only
            obs("L2", "void_marked", False, source=("m1", 1)),
            obs("L2", "material_number", "QLAB-123456", source=("m1", 1)),
        ],
    )
    task = make_task()
    outcome = run_outcome(task, plan_3_0(), facts)
    assert outcome.label == "reject"
    assert any(r.reason_code == "label_field_confirmed_missing" for r in outcome.results)


# ---------------------------------------------------------------------------
# voided-only evidence: violation, never pass/unknown (B001 item 5)
# ---------------------------------------------------------------------------


def test_voided_only_label_rejects():
    batch = pass_batch()
    facts = facts_from(
        batch,
        drop=(("L1", "void_marked"),),
        extra=[obs("L1", "void_marked", True, source=("m1", 1))],  # 作废
    )
    task = make_task()
    outcome = run_outcome(task, plan_3_0(), facts)
    assert outcome.label == "reject"
    assert any(r.reason_code == "voided_label_only" for r in outcome.results)


def test_voided_only_certificate_rejects():
    batch = pass_batch()
    facts = facts_from(
        batch,
        drop=(("C1", "void_marked"),),
        extra=[obs("C1", "void_marked", True, source=("m3", 1))],  # 作废
    )
    task = make_task()
    outcome = run_outcome(task, plan_3_0(), facts)
    assert outcome.label == "reject"
    assert any(r.reason_code == "voided_certificate_only" for r in outcome.results)


def test_voided_label_replaced_by_active_label_passes():
    batch = pass_batch(label_id="L1")
    entities = list(batch.entities) + [ent("L2", "label")]
    facts = facts_from(
        batch,
        entities=entities,
        extra=[obs("L2", "void_marked", True, source=("m1", 1))],  # old voided tag
    )
    task = make_task()
    outcome = run_outcome(task, plan_3_0(), facts)
    assert outcome.label == "pass"


# ---------------------------------------------------------------------------
# applicability short-circuits (no gateway calls)
# ---------------------------------------------------------------------------


def test_known_false_applicability_short_circuits_not_applicable():
    gateway = StaticObservationGateway([])
    task = make_task(metadata={"market": "X", "category": None})
    controller = AuditController(plan_3_0(), gateway, max_rounds=3)
    row = controller.run(task)
    assert row["label"] == "pass"
    assert row["applicability"] == "not_applicable"
    assert row["reason_code"] == "rule_not_applicable"
    assert gateway.calls == []


def test_unknown_applicability_is_ambiguous_with_zero_gateway_calls():
    gateway = StaticObservationGateway([])
    task = make_task(metadata={"market": None, "category": None})
    controller = AuditController(plan_3_0(), gateway, max_rounds=3)
    row = controller.run(task)
    assert row["label"] == "ambiguous"
    assert row["applicability"] == "unknown"
    assert gateway.calls == []


def test_known_false_wins_over_other_unknown_conditions():
    task = make_task(metadata={"market": "R", "power_mode": None})
    outcome = run_outcome(task, plan_3_0(), pass_facts())
    assert outcome.applicability == "not_applicable"


# ---------------------------------------------------------------------------
# missing vs unreadable vs execution error (controller level)
# ---------------------------------------------------------------------------


def _single_batch_controller(task, batch, store=None, max_rounds=3):
    gateway = StaticObservationGateway([batch])
    return AuditController(plan_3_0(), gateway, store=store, run_id="r1",
                           max_rounds=max_rounds), gateway


def test_confirmed_missing_label_fields_rejects_via_controller():
    task = make_task()
    batch = pass_batch()
    observations = []
    for o in batch.observations:
        if o.entity_id == "L1" and o.predicate in ("model", "material_number"):
            observations.append(obs("L1", o.predicate, None, status="confirmed_missing",
                                    scope_complete=True, source=("m1", 1)))
        else:
            observations.append(o)
    batch = ObservationBatch(entities=list(batch.entities), observations=observations)
    controller, gateway = _single_batch_controller(task, batch)
    row = controller.run(task)
    assert row["label"] == "reject"
    assert row["execution_status"] == "completed"
    assert prediction_semantic_errors(row, task) == []
    assert len(gateway.calls) >= 1


def test_unreadable_label_fields_is_ambiguous():
    task = make_task()
    batch = pass_batch()
    observations = []
    for o in batch.observations:
        if o.entity_id == "L1" and o.predicate in ("model", "material_number"):
            observations.append(obs("L1", o.predicate, None, status="unreadable",
                                    source=("m1", 1)))
        else:
            observations.append(o)
    batch = ObservationBatch(entities=list(batch.entities), observations=observations)
    controller, gateway = _single_batch_controller(task, batch)
    row = controller.run(task)
    assert row["label"] == "ambiguous"
    assert row["execution_status"] == "completed"
    assert prediction_semantic_errors(row, task) == []


def test_execution_error_is_error_row_with_null_label():
    task = make_task()
    gateway = StaticObservationGateway([], error_on_exhausted=True, error_code="ocr_down")
    controller = AuditController(plan_3_0(), gateway, max_rounds=3)
    row = controller.run(task)
    assert row["execution_status"] == "error"
    assert row["label"] is None
    assert row["error_code"] == "ocr_down"
    assert row["applicability"] is None
    assert prediction_semantic_errors(row, task) == []


# ---------------------------------------------------------------------------
# conflict priority overrides ordinary violations
# ---------------------------------------------------------------------------


def test_label_number_conflict_overrides_model_mismatch():
    facts = pass_facts(label_id="L1")
    facts.add_entity(ent("L2", "label"))
    facts.add_observation(obs("L2", "void_marked", False, source=("m1", 1)))
    facts.add_observation(obs("L2", "model", "OTHER-9", source=("m1", 1)))  # mismatch
    facts.add_observation(obs("L2", "material_number", "QLAB-654321", source=("m1", 1)))
    facts.add_observation(obs("L2", "carrier", "outer_package", source=("m1", 1)))
    task = make_task()
    outcome = run_outcome(task, plan_3_0(), facts)
    assert outcome.label == "ambiguous"
    assert outcome.reason_code == "material_conflict"
    assert any(r.reason_code == "label_number_conflict" for r in outcome.results)


def test_two_labels_with_same_number_is_not_a_conflict():
    facts = pass_facts(label_id="L1")
    facts.add_entity(ent("L2", "label"))
    facts.add_observation(obs("L2", "void_marked", False, source=("m1", 1)))
    facts.add_observation(obs("L2", "model", "LED-100", source=("m1", 1)))
    facts.add_observation(obs("L2", "material_number", "QLAB-123456", source=("m1", 1)))
    facts.add_observation(obs("L2", "carrier", "outer_package", source=("m1", 1)))
    task = make_task()
    outcome = run_outcome(task, plan_3_0(), facts)
    assert outcome.label == "pass"
    assert not any(r.status.value == "conflict" for r in outcome.results)


def test_certificate_period_conflict_overrides_expired_cert():
    facts = pass_facts()
    facts.add_entity(ent("C2", "certificate"))
    facts.add_observation(obs("C2", "void_marked", False, source=("m3", 1)))
    facts.add_observation(obs("C2", "material_number", "QLAB-123456", source=("m3", 1)))
    facts.add_observation(obs("C2", "covered_models", ["LED-100"], source=("m3", 1)))
    # expired period -> ordinary violation, but the period conflict wins
    facts.add_observation(obs("C2", "valid_from", "2025-01-01", source=("m3", 1)))
    facts.add_observation(obs("C2", "valid_until", "2025-12-31", source=("m3", 1)))
    task = make_task()
    outcome = run_outcome(task, plan_3_0(), facts)
    assert outcome.label == "ambiguous"
    assert outcome.reason_code == "material_conflict"
    assert any(r.reason_code == "certificate_period_conflict" for r in outcome.results)


def test_same_number_same_period_certs_are_not_a_conflict():
    facts = pass_facts()
    facts.add_entity(ent("C2", "certificate"))
    facts.add_observation(obs("C2", "void_marked", False, source=("m3", 1)))
    facts.add_observation(obs("C2", "material_number", "QLAB-123456", source=("m3", 1)))
    facts.add_observation(obs("C2", "covered_models", ["LED-100"], source=("m3", 1)))
    facts.add_observation(obs("C2", "valid_from", "2026-01-01", source=("m3", 1)))
    facts.add_observation(obs("C2", "valid_until", "2026-12-31", source=("m3", 1)))
    task = make_task()
    outcome = run_outcome(task, plan_3_0(), facts)
    assert outcome.label == "pass"


# ---------------------------------------------------------------------------
# certificate date endpoints (closed interval)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("valid_from,valid_until,expected", [
    ("2026-06-15", "2026-12-31", "pass"),   # review date == valid_from (endpoint)
    ("2026-01-01", "2026-06-15", "pass"),   # review date == valid_until (endpoint)
    ("2026-06-16", "2026-12-31", "reject"),  # one day after start
    ("2026-01-01", "2026-06-14", "reject"),  # one day before end
    ("2026-12-31", "2026-01-01", "reject"),  # reversed period
    ("2026-02-30", "2026-12-31", "reject"),  # impossible date
])
def test_certificate_validity_endpoints(valid_from, valid_until, expected):
    task = make_task()
    facts = pass_facts(valid_from=valid_from, valid_until=valid_until)
    outcome = run_outcome(task, plan_3_0(), facts)
    assert outcome.label == expected
    if expected == "reject":
        assert any(r.status.value == "violated" and
                   r.reason_code.startswith("validity_") for r in outcome.results)


# ---------------------------------------------------------------------------
# material number format via plan pattern
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("number,expected", [
    ("QLAB-123456", "pass"),
    ("qlab-123456", "reject"),   # lowercase prefix
    ("QLAB-12345", "reject"),    # too few digits
    ("QLAB-1234567", "reject"),  # too many digits
    ("QLAB-O23456", "reject"),   # letter standing in for a digit
])
def test_material_number_format(number, expected):
    task = make_task()
    facts = pass_facts(number=number)
    outcome = run_outcome(task, plan_3_0(), facts)
    assert outcome.label == expected


def test_model_normalization_match():
    task = make_task(metadata={"declared_model": "LED 100"})
    facts = pass_facts(model="LED-100")
    assert run_outcome(task, plan_3_0(), facts).label == "pass"
    task2 = make_task(metadata={"declared_model": "LED-l00"})  # lowercase L vs 1
    assert run_outcome(task2, plan_3_0(), pass_facts(model="LED-100")).label == "reject"


# ---------------------------------------------------------------------------
# NeedEvidence multi-round loop
# ---------------------------------------------------------------------------


def test_need_evidence_loop_converges_to_pass():
    task = make_task()
    gateway = StaticObservationGateway([pass_batch(), ObservationBatch()])
    controller = AuditController(plan_3_0(), gateway, max_rounds=4)
    row = controller.run(task)
    assert row["label"] == "pass"
    assert row["execution_status"] == "completed"
    # the controller requested inspections with permitted actions only
    assert gateway.calls, "controller should request evidence when facts are empty"
    for request in gateway.calls:
        assert request.allowed_actions == ["inspect_material"]
        assert request.candidate_material_ids
    assert prediction_semantic_errors(row, task) == []


def test_need_evidence_round1_partial_then_round2_certificate():
    task = make_task()
    full = pass_batch()
    label_only = ObservationBatch(
        entities=[e for e in full.entities if e.kind.value == "label"],
        observations=[o for o in full.observations if o.entity_id == "L1"],
    )
    cert_only = ObservationBatch(
        entities=[e for e in full.entities if e.kind.value == "certificate"],
        observations=[o for o in full.observations if o.entity_id == "C1"],
    )
    gateway = StaticObservationGateway([label_only, cert_only])
    controller = AuditController(plan_3_0(), gateway, max_rounds=4)
    row = controller.run(task)
    assert row["label"] == "pass"
    assert len(gateway.calls) >= 2
    kinds = {c.entity_kind.value for c in gateway.calls}
    assert kinds == {"label", "certificate"}


def test_gateway_making_no_progress_stops_within_round_bound():
    task = make_task()
    gateway = StaticObservationGateway([])  # always returns empty batches
    controller = AuditController(plan_3_0(), gateway, max_rounds=3)
    row = controller.run(task)
    assert row["label"] == "ambiguous"
    assert len(gateway.calls) <= 3


# ---------------------------------------------------------------------------
# checkpoint recovery / idempotency through the store
# ---------------------------------------------------------------------------


def test_checkpoint_recovery_reuses_completed_step_without_new_gateway_calls(tmp_path):
    store = TaskStore(tmp_path / "state.sqlite3")
    try:
        task = make_task()
        gateway1 = StaticObservationGateway([pass_batch()])
        controller1 = AuditController(plan_3_0(), gateway1, store=store,
                                      run_id="r1", max_rounds=4)
        row1 = controller1.run(task)
        assert row1["label"] == "pass"
        calls_after_run1 = len(gateway1.calls)
        assert calls_after_run1 >= 1

        # second run over the same store: completed step is recovered, the
        # gateway is never called again and no duplicate final row exists
        gateway2 = StaticObservationGateway([pass_batch()])
        controller2 = AuditController(plan_3_0(), gateway2, store=store,
                                      run_id="r1", max_rounds=4)
        row2 = controller2.run(task)
        assert gateway2.calls == []
        assert row2 == row1
        history = store.attempts_for("r1", task.audit_unit_id)
        assert any(h["status"] == "recovered" for h in history)

        # a DIFFERENT run id is not recovered from run r1's records
        gateway3 = StaticObservationGateway([pass_batch()])
        controller3 = AuditController(plan_3_0(), gateway3, store=store,
                                      run_id="r2", max_rounds=4)
        row3 = controller3.run(task)
        assert len(gateway3.calls) >= 1
        assert row3["label"] == "pass"
        assert row3["trace_id"].startswith("r2:")
    finally:
        store.close()


def test_observations_are_run_isolated_in_controller(tmp_path):
    store = TaskStore(tmp_path / "state.sqlite3")
    try:
        task = make_task()
        controller = AuditController(plan_3_0(), StaticObservationGateway([pass_batch()]),
                                     store=store, run_id="baseline", max_rounds=4)
        assert controller.run(task)["label"] == "pass"
        baseline_only = store.observations_for(task.audit_unit_id, run_id="baseline")
        assert baseline_only
        assert store.observations_for(task.audit_unit_id, run_id="other") == []
    finally:
        store.close()


# ---------------------------------------------------------------------------
# every produced output row passes the public prediction validator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("metadata,batches,max_rounds,expected_label", [
    ({"market": "X"}, [], 3, "pass"),  # not_applicable
    ({"market": None}, [], 3, "ambiguous"),  # unknown
    ({}, [pass_batch()], 4, "pass"),
    ({}, [], 3, "ambiguous"),
    ({"declared_model": "OTHER"}, [pass_batch()], 4, "reject"),
])
def test_outputs_pass_public_prediction_validator(metadata, batches, max_rounds, expected_label):
    task = make_task(metadata=metadata)
    gateway = StaticObservationGateway(list(batches))
    controller = AuditController(plan_3_0(), gateway, max_rounds=max_rounds)
    row = controller.run(task)
    assert row["label"] == expected_label
    assert prediction_semantic_errors(row, task) == []


def test_error_row_passes_public_prediction_validator():
    task = make_task()
    gateway = StaticObservationGateway([], error_on_exhausted=True)
    controller = AuditController(plan_3_0(), gateway, max_rounds=2)
    row = controller.run(task)
    assert row["execution_status"] == "error"
    assert prediction_semantic_errors(row, task) == []
