"""Rule/version-aligned version comparator and local candidate protection gate.

- Strictly aligns records by ``(audit_unit_id, rule_id, rule_version)`` —
  never mixes different rules or rule versions.
- **With references**: computes ``repaired_ids``, ``new_error_ids``,
  ``unchanged_correct_ids``, ``unchanged_error_ids`` using vendored
  ``o2_public_contracts.quality.metric`` joint-correct IDs.
- **Without references**: reports ONLY ``changed_prediction_ids`` /
  ``unchanged_prediction_ids`` / ``error_delta`` (NEVER claims an unverified
  prediction difference is a "repaired error").
- Exposes :func:`evaluate_protection_gate` delegating directly to vendored
  ``o2_public_contracts.quality.gate`` for explicit local/public evaluation
  vectors, clearly marked ``official=False`` (formal hidden-set promotion gate
  remains organizer-owned).
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from o2_public_contracts import contracts as public_contracts
from o2_public_contracts import quality as public_quality

PLUGIN_ID = "o2.version_compare"
PLUGIN_VERSION = "1.0.0"
INPUT_SCHEMA_VERSION = "1.3.1"


def _prediction_signature(row: dict[str, Any] | None) -> tuple[Any, ...] | None:
    if row is None:
        return None
    return (
        row.get("rule_id"),
        row.get("rule_version"),
        row.get("execution_status"),
        row.get("label"),
        row.get("applicability"),
        row.get("reason_code"),
    )


def _index_single_outputs(
    outputs: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    by_id: dict[str, dict[str, Any]] = {}
    dups: set[str] = set()
    for row in outputs:
        if not isinstance(row, dict):
            continue
        uid = row.get("audit_unit_id")
        if not isinstance(uid, str) or not uid:
            continue
        if uid in by_id:
            dups.add(uid)
        else:
            by_id[uid] = row
    return by_id, dups


def compare_predictions(
    inputs: list[dict[str, Any]],
    baseline_outputs: list[dict[str, Any]],
    candidate_outputs: list[dict[str, Any]],
    references: list[dict[str, Any]] | None = None,
    *,
    baseline_artifact_versions: dict[str, str] | None = None,
    candidate_artifact_versions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Compare baseline vs candidate predictions aligned by ``(audit_unit_id, rule_id, rule_version)``."""
    ins = public_quality.index_inputs(inputs)

    # Without references: strictly prediction diff only (never claim repairs/new errors).
    if not references:
        base_map, base_dups = _index_single_outputs(baseline_outputs)
        cand_map, cand_dups = _index_single_outputs(candidate_outputs)
        changed: list[str] = []
        unchanged: list[str] = []
        version_mismatch_ids: list[str] = []
        by_rule_version: dict[str, dict[str, Any]] = {}

        grouped_uids: dict[str, list[str]] = defaultdict(list)
        for uid, task in ins.items():
            rv_key = f"{task['rule_id']}@{task['rule_version']}"
            grouped_uids[rv_key].append(uid)

        base_errors = 0
        cand_errors = 0
        for rv_key in sorted(grouped_uids):
            rv_changed: list[str] = []
            rv_unchanged: list[str] = []
            for uid in sorted(grouped_uids[rv_key]):
                task = ins[uid]
                b = base_map.get(uid)
                c = cand_map.get(uid)
                if b and (b.get("rule_id") != task["rule_id"] or b.get("rule_version") != task["rule_version"]):
                    version_mismatch_ids.append(uid)
                if c and (c.get("rule_id") != task["rule_id"] or c.get("rule_version") != task["rule_version"]):
                    if uid not in version_mismatch_ids:
                        version_mismatch_ids.append(uid)
                if b and b.get("execution_status") == "error":
                    base_errors += 1
                if c and c.get("execution_status") == "error":
                    cand_errors += 1
                if (
                    uid in base_dups
                    or uid in cand_dups
                    or _prediction_signature(b) != _prediction_signature(c)
                ):
                    changed.append(uid)
                    rv_changed.append(uid)
                else:
                    unchanged.append(uid)
                    rv_unchanged.append(uid)
            by_rule_version[rv_key] = {
                "total": len(grouped_uids[rv_key]),
                "changed_prediction_count": len(rv_changed),
                "unchanged_prediction_count": len(rv_unchanged),
                "changed_prediction_ids": rv_changed,
            }

        return {
            "mode": "prediction_diff_only",
            "has_references": False,
            "official": False,
            "total": len(ins),
            "changed_prediction_ids": sorted(changed),
            "unchanged_prediction_ids": sorted(unchanged),
            "changed_prediction_count": len(changed),
            "unchanged_prediction_count": len(unchanged),
            "baseline_error_count": base_errors,
            "candidate_error_count": cand_errors,
            "error_delta": cand_errors - base_errors,
            "version_mismatch_ids": sorted(version_mismatch_ids),
            "repaired_ids": None,
            "new_error_ids": None,
            "unchanged_correct_ids": None,
            "unchanged_error_ids": None,
            "by_rule_version": by_rule_version,
            "note": (
                "No reference labels were provided; only prediction differences are "
                "reported (changes are NOT labeled as repaired or new errors)."
            ),
        }

    # With references: official joint-correct set comparison via quality.metric
    base_m = public_quality.metric(
        references, baseline_outputs, ins, artifact_versions=baseline_artifact_versions
    )
    cand_m = public_quality.metric(
        references, candidate_outputs, ins, artifact_versions=candidate_artifact_versions
    )
    all_ids = sorted(r["audit_unit_id"] for r in references)
    base_ok = set(base_m["correct_ids"])
    cand_ok = set(cand_m["correct_ids"])

    repaired_ids = sorted(cand_ok - base_ok)
    new_error_ids = sorted(base_ok - cand_ok)
    unchanged_correct_ids = sorted(base_ok & cand_ok)
    unchanged_error_ids = sorted(set(all_ids) - base_ok - cand_ok)
    new_execution_failure_ids = sorted(
        uid
        for uid, out in cand_m["outcomes"].items()
        if out in ("invalid", "missing", "error")
        and base_m["outcomes"][uid] not in ("invalid", "missing", "error")
    )

    # Strict per-(rule_id, rule_version) alignment
    refs_by_rv: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in references:
        refs_by_rv[f"{r['rule_id']}@{r['rule_version']}"].append(r)

    by_rule_version = {}
    for rv_key in sorted(refs_by_rv):
        sub_refs = refs_by_rv[rv_key]
        sub_ids = {r["audit_unit_id"] for r in sub_refs}
        sub_base = [
            p for p in baseline_outputs
            if isinstance(p, dict) and p.get("audit_unit_id") in sub_ids
        ]
        sub_cand = [
            p for p in candidate_outputs
            if isinstance(p, dict) and p.get("audit_unit_id") in sub_ids
        ]
        bm = public_quality.metric(sub_refs, sub_base, ins, baseline_artifact_versions)
        cm = public_quality.metric(sub_refs, sub_cand, ins, candidate_artifact_versions)
        bok, cok = set(bm["correct_ids"]), set(cm["correct_ids"])
        by_rule_version[rv_key] = {
            "all": len(sub_refs),
            "baseline_accuracy": bm["accuracy"],
            "candidate_accuracy": cm["accuracy"],
            "baseline_quality": bm["quality"],
            "candidate_quality": cm["quality"],
            "repaired_ids": sorted(cok - bok),
            "new_error_ids": sorted(bok - cok),
            "unchanged_correct_ids": sorted(bok & cok),
            "unchanged_error_ids": sorted(sub_ids - bok - cok),
        }

    return {
        "mode": "reference_comparison",
        "has_references": True,
        "official": False,
        "total": len(all_ids),
        "baseline": {
            "accuracy": base_m["accuracy"],
            "quality": base_m["quality"],
            "correct": base_m["correct"],
            "error": base_m["error"],
            "invalid": base_m["invalid"],
            "missing": base_m["missing"],
        },
        "candidate": {
            "accuracy": cand_m["accuracy"],
            "quality": cand_m["quality"],
            "correct": cand_m["correct"],
            "error": cand_m["error"],
            "invalid": cand_m["invalid"],
            "missing": cand_m["missing"],
        },
        "accuracy_delta": (
            (cand_m["accuracy"] - base_m["accuracy"])
            if (cand_m["accuracy"] is not None and base_m["accuracy"] is not None)
            else None
        ),
        "quality_delta": (
            (cand_m["quality"] - base_m["quality"])
            if (cand_m["quality"] is not None and base_m["quality"] is not None)
            else None
        ),
        "repaired_ids": repaired_ids,
        "new_error_ids": new_error_ids,
        "unchanged_correct_ids": unchanged_correct_ids,
        "unchanged_error_ids": unchanged_error_ids,
        "repaired_count": len(repaired_ids),
        "new_error_count": len(new_error_ids),
        "unchanged_correct_count": len(unchanged_correct_ids),
        "unchanged_error_count": len(unchanged_error_ids),
        "new_execution_failure_ids": new_execution_failure_ids,
        "by_rule_version": by_rule_version,
    }


def evaluate_protection_gate(
    repair_refs: list[dict[str, Any]],
    repair_tasks: list[dict[str, Any]],
    protected_refs: list[dict[str, Any]],
    protected_tasks: list[dict[str, Any]],
    outputs: dict[str, list[dict[str, Any]]],
    versions: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Compute the candidate protection gate on explicit evaluation vectors.

    Delegates directly to ``o2_public_contracts.quality.gate``. Formal
    hidden-set promotion remains organizer-owned (``official=False``).
    """
    result = public_quality.gate(
        repair_refs,
        repair_tasks,
        protected_refs,
        protected_tasks,
        outputs,
        versions=versions,
    )
    return {
        **result,
        "official": False,
        "scope": "local_evaluation_only",
        "note": (
            "Computed on caller-supplied local/public evaluation vectors using "
            "o2_public_contracts.quality.gate. Formal promotion gate uses "
            "organizer-owned hidden repair/protected sets."
        ),
    }


def evaluate(
    inputs: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    references: list[dict[str, Any]],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Protocol 1.3.1 plugin adapter for version comparison.

    Expects ``context["baseline_outputs"]`` (list of baseline predictions);
    ``outputs`` is treated as the candidate predictions.
    """
    ctx = context if isinstance(context, dict) else {}
    baseline_outputs = ctx.get("baseline_outputs")
    if not isinstance(baseline_outputs, list):
        return {
            "plugin_id": PLUGIN_ID,
            "plugin_version": PLUGIN_VERSION,
            "input_schema_version": INPUT_SCHEMA_VERSION,
            "metrics": {},
            "diagnostics": [],
            "errors": [{
                "code": "missing_baseline_outputs",
                "detail": "context['baseline_outputs'] list is required for version_compare",
            }],
            "official": False,
        }

    try:
        comp = compare_predictions(
            inputs=inputs,
            baseline_outputs=baseline_outputs,
            candidate_outputs=outputs,
            references=references or None,
            baseline_artifact_versions=ctx.get("baseline_artifact_versions"),
            candidate_artifact_versions=ctx.get("candidate_artifact_versions"),
        )
    except public_contracts.ContractError as exc:
        return {
            "plugin_id": PLUGIN_ID,
            "plugin_version": PLUGIN_VERSION,
            "input_schema_version": INPUT_SCHEMA_VERSION,
            "metrics": {},
            "diagnostics": [],
            "errors": [{"code": "contract_error", "detail": str(exc)}],
            "official": False,
        }

    if comp["has_references"]:
        metrics = {
            "total": comp["total"],
            "baseline_accuracy": comp["baseline"]["accuracy"],
            "candidate_accuracy": comp["candidate"]["accuracy"],
            "accuracy_delta": comp["accuracy_delta"],
            "baseline_quality": comp["baseline"]["quality"],
            "candidate_quality": comp["candidate"]["quality"],
            "quality_delta": comp["quality_delta"],
            "repaired_count": comp["repaired_count"],
            "new_error_count": comp["new_error_count"],
            "unchanged_correct_count": comp["unchanged_correct_count"],
            "unchanged_error_count": comp["unchanged_error_count"],
            "new_execution_failure_count": len(comp["new_execution_failure_ids"]),
        }
    else:
        metrics = {
            "total": comp["total"],
            "changed_prediction_count": comp["changed_prediction_count"],
            "unchanged_prediction_count": comp["unchanged_prediction_count"],
            "baseline_error_count": comp["baseline_error_count"],
            "candidate_error_count": comp["candidate_error_count"],
            "error_delta": comp["error_delta"],
        }

    return {
        "plugin_id": PLUGIN_ID,
        "plugin_version": PLUGIN_VERSION,
        "input_schema_version": INPUT_SCHEMA_VERSION,
        "metrics": metrics,
        "comparison": comp,
        "diagnostics": [
            {"rule_version": rv, **info}
            for rv, info in comp["by_rule_version"].items()
        ],
        "errors": [],
        "official": False,
    }
