"""Comprehensive tests for Stage 04 evaluation plugins & isolation.

Covers every locally verifiable acceptance item in L-FUNC-01..16 and
L-STRUCT-01..04, plus live ``o2-audit evaluate`` CLI execution against the
public development dataset.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pytest

from o2_audit.cli import main as cli_main
from o2_audit.evaluation import (
    PluginRegistry,
    PluginSpec,
    compare_predictions,
    evaluate_protection_gate,
    load_plugin_registry,
    run_plugin,
    run_plugins,
    save_plugin_registry,
)
from o2_audit.evaluation.conclusion import evaluate as eval_conclusion
from o2_audit.evaluation.evidence import evaluate as eval_evidence
from o2_audit.resources import resources_root
from o2_public_contracts import contracts as public_contracts

PUBLIC_DEV = resources_root() / "public_dev" / "公开开发集"
PUBLIC_INPUTS = PUBLIC_DEV / "inputs" / "audit_inputs.jsonl"
PUBLIC_REFS = PUBLIC_DEV / "references" / "audit_gold.jsonl"
PUBLIC_PREDS = PUBLIC_DEV / "reference_predictions.jsonl"


# ---------------------------------------------------------------------------
# Synthetic builders
# ---------------------------------------------------------------------------

def _make_task(uid: str, rule_id: str = "B001", rule_version: str = "3.0",
               mat_id: str | None = None, page: int = 1) -> dict[str, Any]:
    mid = mat_id or f"m-{uid}"
    return {
        "audit_unit_id": uid,
        "sample_id": f"s-{uid}",
        "rule_id": rule_id,
        "rule_version": rule_version,
        "review_date": "2029-05-01",
        "metadata": {"market": "Q", "category": "桌面灯", "power_mode": "可充电"},
        "materials": [
            {
                "material_id": mid,
                "role": "package_images",
                "relative_path": f"assets/{mid}.png",
                "media_type": "image/png",
                "page_index": page,
            }
        ],
    }


def _make_ref(uid: str, label: str = "pass", applicability: str = "applicable",
              rule_id: str = "B001", rule_version: str = "3.0",
              evidence: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "audit_unit_id": uid,
        "rule_id": rule_id,
        "rule_version": rule_version,
        "label": label,
        "applicability": applicability,
    }
    if evidence is not None:
        row["evidence"] = evidence
    return row


def _make_pred(uid: str, label: str | None = "pass",
               applicability: str | None = "applicable",
               execution_status: str = "completed",
               rule_id: str = "B001", rule_version: str = "3.0",
               artifact_version: str = "v1",
               evidence: list[dict[str, Any]] | None = None,
               mat_id: str | None = None,
               reason_code: str = "ok",
               error_code: str | None = None) -> dict[str, Any]:
    mid = mat_id or f"m-{uid}"
    if evidence is None:
        if execution_status == "error" or applicability == "not_applicable":
            evidence = []
        else:
            evidence = [{
                "material_id": mid,
                "page_index": 1,
                "fact": "型号：EDU-753",
                "bbox": [0.1, 0.2, 0.3, 0.4],
            }]
    row: dict[str, Any] = {
        "audit_unit_id": uid,
        "rule_id": rule_id,
        "rule_version": rule_version,
        "execution_status": execution_status,
        "label": label,
        "applicability": applicability,
        "reason_code": reason_code,
        "reason": "test reason",
        "evidence": evidence,
        "artifact_version": artifact_version,
        "trace_id": f"tr-{uid}",
        "latency_ms": 10,
        "cost_units": None,
    }
    if error_code is not None:
        row["error_code"] = error_code
    return row


# ---------------------------------------------------------------------------
# L-FUNC-01..09: Conclusion quality plugin
# ---------------------------------------------------------------------------

class TestConclusionPlugin:
    def test_l_func_01_standard_metrics(self):
        # 10 units: 6 decisive predictions (5 correct, 1 wrong), 4 ambiguous (1 correct, 3 wrong)
        # -> accuracy = 6/10 = 0.6, machine_review_rate = 6/10 = 0.6, machine_accuracy = 5/6
        tasks = [_make_task(f"u{i}") for i in range(10)]
        refs = (
            [_make_ref(f"u{i}", "pass", "applicable") for i in range(5)]
            + [_make_ref("u5", "reject", "applicable")]
            + [_make_ref("u6", "ambiguous", "applicable")]
            + [_make_ref(f"u{i}", "pass", "applicable") for i in range(7, 10)]
        )
        preds = (
            [_make_pred(f"u{i}", "pass", "applicable") for i in range(5)]  # 5 decisive right
            + [_make_pred("u5", "pass", "applicable")]                     # 1 decisive wrong
            + [_make_pred("u6", "ambiguous", "applicable")]                # 1 amb right
            + [_make_pred(f"u{i}", "ambiguous", "applicable") for i in range(7, 10)]  # 3 amb wrong
        )
        rep = eval_conclusion(tasks, preds, refs, {"schema_version": "1.3.1", "purpose": "test"})
        m = rep["metrics"]
        assert rep["errors"] == []
        assert math.isclose(m["accuracy"], 0.6, abs_tol=1e-12)
        assert math.isclose(m["machine_review_rate"], 0.6, abs_tol=1e-12)
        assert math.isclose(m["machine_accuracy"], 5 / 6, abs_tol=1e-12)
        assert m["all"] == 10 and m["correct"] == 6

    def test_l_func_02_public_dev_all_ambiguous_64(self):
        # Exact rubric check: 64 public dev tasks, copy reference_predictions,
        # set label="ambiguous", applicability="applicable", reason_code="model_review"
        tasks = public_contracts.read_rows(PUBLIC_INPUTS)
        refs = public_contracts.read_rows(PUBLIC_REFS)
        preds = public_contracts.read_rows(PUBLIC_PREDS)
        assert len(tasks) == 64 and len(refs) == 64

        mutated_preds = []
        for p in preds:
            row = copy.deepcopy(p)
            row["label"] = "ambiguous"
            row["applicability"] = "applicable"
            row["reason_code"] = "model_review"
            mutated_preds.append(row)

        rep = eval_conclusion(tasks, mutated_preds, refs, {"schema_version": "1.3.1", "purpose": "l_func_02"})
        m = rep["metrics"]
        assert rep["errors"] == []
        assert m["all"] == 64
        assert m["correct"] == 12
        assert m["invalid"] == 0
        assert math.isclose(m["accuracy"], 0.1875, abs_tol=1e-12)
        assert math.isclose(m["machine_review_rate"], 0.0, abs_tol=1e-12)
        assert m["machine_accuracy"] is None
        assert math.isclose(m["correct_decisive_coverage"], 0.0, abs_tol=1e-12)
        assert math.isclose(m["quality"], 0.13125, abs_tol=1e-12)

    def test_l_func_03_all_missing(self):
        tasks = [_make_task("u1"), _make_task("u2")]
        refs = [_make_ref("u1", "pass"), _make_ref("u2", "reject")]
        rep = eval_conclusion(tasks, [], refs, {"schema_version": "1.3.1", "purpose": "test"})
        m = rep["metrics"]
        assert m["all"] == 2 and m["missing"] == 2
        assert m["accuracy"] == 0.0
        assert m["machine_review_rate"] == 0.0
        assert m["machine_accuracy"] is None

    def test_l_func_04_duplicate_output_is_invalid_not_best_of(self):
        tasks = [_make_task("u1"), _make_task("u2")]
        refs = [_make_ref("u1", "pass"), _make_ref("u2", "pass")]
        preds = [
            _make_pred("u1", "pass"),
            _make_pred("u1", "reject"),  # duplicate! must be invalid even though one is pass
            _make_pred("u2", "pass"),
        ]
        rep = eval_conclusion(tasks, preds, refs, {"schema_version": "1.3.1", "purpose": "test"})
        m = rep["metrics"]
        assert m["all"] == 2 and m["invalid"] == 1 and m["correct"] == 1
        d_u1 = next(d for d in rep["diagnostics"] if d["audit_unit_id"] == "u1")
        assert d_u1["outcome"] == "invalid" and "duplicate_output" in d_u1["issues"]

    def test_l_func_05_execution_error_kept_in_denominator(self):
        tasks = [_make_task("u1"), _make_task("u2")]
        refs = [_make_ref("u1", "pass"), _make_ref("u2", "pass")]
        preds = [
            _make_pred("u1", None, None, execution_status="error",
                       reason_code="execution_error", error_code="gateway_timeout"),
            _make_pred("u2", "pass"),
        ]
        rep = eval_conclusion(tasks, preds, refs, {"schema_version": "1.3.1", "purpose": "test"})
        m = rep["metrics"]
        assert m["all"] == 2 and m["error"] == 1 and m["correct"] == 1
        assert math.isclose(m["accuracy"], 0.5, abs_tol=1e-12)
        assert math.isclose(m["machine_review_rate"], 0.5, abs_tol=1e-12)

    def test_l_func_06_invalid_label_and_bool_page_index(self):
        tasks = [_make_task("u1"), _make_task("u2")]
        refs = [_make_ref("u1", "pass"), _make_ref("u2", "pass")]
        p1 = _make_pred("u1", "approved")  # illegal label
        p2 = _make_pred("u2", "pass", evidence=[{
            "material_id": "m-u2", "page_index": True, "fact": "型号：EDU-753",
        }])  # bool page_index is illegal
        rep = eval_conclusion(tasks, [p1, p2], refs, {"schema_version": "1.3.1", "purpose": "test"})
        m = rep["metrics"]
        assert m["all"] == 2 and m["invalid"] == 2 and m["correct"] == 0

    def test_l_func_07_wrong_rule_version_and_wrong_artifact_version(self):
        tasks = [_make_task("u1", "B001", "3.0"), _make_task("u2", "B001", "3.1")]
        refs = [_make_ref("u1", "pass", "applicable", "B001", "3.0"),
                _make_ref("u2", "pass", "applicable", "B001", "3.1")]
        preds = [
            _make_pred("u1", "pass", rule_id="B001", rule_version="3.1", artifact_version="v1"),
            _make_pred("u2", "pass", rule_id="B001", rule_version="3.1", artifact_version="wrong-art"),
        ]
        rep = eval_conclusion(
            tasks, preds, refs,
            {"schema_version": "1.3.1", "purpose": "test", "expected_artifact_version": "v1"},
        )
        assert rep["metrics"]["all"] == 2
        assert rep["metrics"]["invalid"] == 2
        assert rep["metrics"]["correct"] == 0

    def test_l_func_08_extraneous_units_do_not_inflate_denominator(self):
        tasks = [_make_task("u1")]
        refs = [_make_ref("u1", "pass")]
        preds = [_make_pred("u1", "pass"), _make_pred("u-extra", "pass")]
        rep = eval_conclusion(tasks, preds, refs, {"schema_version": "1.3.1", "purpose": "test"})
        m = rep["metrics"]
        assert m["all"] == 1 and m["correct"] == 1 and m["extraneous"] == 1
        assert m["accuracy"] == 1.0

    def test_l_func_09_empty_references_returns_null_metrics(self):
        rep = eval_conclusion([], [], [], {"schema_version": "1.3.1", "purpose": "test"})
        m = rep["metrics"]
        assert rep["errors"] == []
        assert m["all"] == 0
        assert m["accuracy"] is None
        assert m["machine_review_rate"] is None
        assert m["machine_accuracy"] is None
        assert m["quality"] is None

    def test_all_ambiguous_truth_sets_u_equals_a(self):
        tasks = [_make_task("u1"), _make_task("u2")]
        refs = [_make_ref("u1", "ambiguous"), _make_ref("u2", "ambiguous")]
        preds = [_make_pred("u1", "ambiguous"), _make_pred("u2", "pass")]
        rep = eval_conclusion(tasks, preds, refs, {"schema_version": "1.3.1", "purpose": "test"})
        m = rep["metrics"]
        assert m["binary_truth"] == 0
        assert math.isclose(m["accuracy"], 0.5, abs_tol=1e-12)
        assert math.isclose(m["correct_decisive_coverage"], m["accuracy"], abs_tol=1e-12)
        assert math.isclose(m["quality"], 0.5, abs_tol=1e-12)

    def test_contrast_groups_computed_only_when_supplied(self):
        tasks = [_make_task("u1"), _make_task("u2")]
        refs = [_make_ref("u1", "pass"), _make_ref("u2", "reject")]
        preds = [_make_pred("u1", "pass"), _make_pred("u2", "reject")]
        rep_no = eval_conclusion(tasks, preds, refs, {"schema_version": "1.3.1", "purpose": "test"})
        assert rep_no["contrasts"] is None
        rep_yes = eval_conclusion(tasks, preds, refs, {
            "schema_version": "1.3.1",
            "purpose": "test",
            "contrast_groups": [{"group_id": "g1", "rule_id": "B001", "members": ["u1", "u2"]}],
        })
        assert rep_yes["contrasts"]["by_rule"] == {"B001": 1.0}


# ---------------------------------------------------------------------------
# L-FUNC-10..12: Evidence structure vs semantic content grounding
# ---------------------------------------------------------------------------

class TestEvidencePlugin:
    def test_l_func_10_valid_structure_and_semantic_support(self):
        task = _make_task("u1", mat_id="m1", page=1)
        ref = _make_ref("u1", evidence=[{
            "material_id": "m1",
            "page_index": 1,
            "visible_facts": [
                {"text": "型号：EDU-753", "readable": True, "bbox": [0.1, 0.2, 0.3, 0.4]},
                {"text": "材料编号：QLAB-753826", "readable": True, "bbox": [0.1, 0.5, 0.4, 0.6]},
            ],
            "shape": {},
            "cut_coords": None,
        }])
        pred = _make_pred("u1", evidence=[
            {"material_id": "m1", "page_index": 1, "fact": "型号：EDU-753", "bbox": [0.1, 0.2, 0.3, 0.4]},
            {"material_id": "m1", "page_index": 1, "fact": "材料编号：QLAB-753826", "bbox": [0.1, 0.5, 0.4, 0.6]},
        ])
        rep = eval_evidence([task], [pred], [ref], {"schema_version": "1.3.1", "purpose": "test"})
        assert rep["metrics"]["structurally_valid_items"] == 2
        assert rep["metrics"]["semantically_supported_items"] == 2
        assert rep["metrics"]["semantically_unsupported_items"] == 0
        assert rep["metrics"]["semantic_grounding_rate"] == 1.0
        for chk in rep["diagnostics"][0]["evidence_checks"]:
            assert chk["structure_valid"] is True
            assert chk["grounded"] is True

    def test_l_func_11_invalid_refs_separate_missing_material_and_bbox(self):
        task = _make_task("u1", mat_id="m1", page=1)
        pred = _make_pred("u1", evidence=[
            {"material_id": "m-nonexistent", "page_index": 1, "fact": "型号：EDU-753"},
            {"material_id": "m1", "page_index": 1, "fact": "型号：EDU-753", "bbox": [0.8, 0.2, 0.1, 0.4]},
            {"material_id": "m1", "page_index": True, "fact": "型号：EDU-753"},
        ])
        rep = eval_evidence([task], [pred], [], {"schema_version": "1.3.1", "purpose": "test"})
        checks = rep["diagnostics"][0]["evidence_checks"]
        assert "material_not_found" in checks[0]["structure_issues"]
        assert "bbox_invalid" not in checks[0]["structure_issues"]
        assert "bbox_invalid" in checks[1]["structure_issues"]
        assert "material_not_found" not in checks[1]["structure_issues"]
        assert "page_index_invalid" in checks[2]["structure_issues"]
        assert all(c["structure_valid"] is False and c["grounded"] is False for c in checks)

    def test_l_func_12_unsupported_and_unverifiable_never_fake_grounded_true(self):
        task = _make_task("u1", mat_id="m1", page=1)
        # Case A: structurally valid material_id, NO reference visible_facts -> grounded MUST be None
        pred_no_ref = _make_pred("u1", evidence=[
            {"material_id": "m1", "page_index": 1, "fact": "型号：EDU-753", "bbox": [0.1, 0.2, 0.3, 0.4]},
        ])
        rep_unverifiable = eval_evidence([task], [pred_no_ref], [], {"schema_version": "1.3.1", "purpose": "test"})
        chk = rep_unverifiable["diagnostics"][0]["evidence_checks"][0]
        assert chk["structure_valid"] is True
        assert chk["grounded"] is None
        assert chk["semantic_status"] == "unverifiable"
        assert rep_unverifiable["metrics"]["semantically_supported_items"] == 0
        assert rep_unverifiable["metrics"]["semantically_unverifiable_items"] == 1
        assert rep_unverifiable["metrics"]["semantic_grounding_rate"] is None

        # Case B: reference has visible_facts, claim contradicts value / fabricates unreadable / bad bbox
        ref = _make_ref("u1", evidence=[{
            "material_id": "m1",
            "page_index": 1,
            "visible_facts": [
                {"text": "型号：EDU-753", "readable": True, "bbox": [0.1, 0.2, 0.3, 0.4]},
                {"text": "材料编号：QLAB-753826", "readable": False, "bbox": [0.1, 0.5, 0.4, 0.6]},
            ],
            "shape": {},
            "cut_coords": None,
        }])
        pred_bad = _make_pred("u1", evidence=[
            {"material_id": "m1", "page_index": 1, "fact": "型号：WRONG-999", "bbox": [0.1, 0.2, 0.3, 0.4]},
            {"material_id": "m1", "page_index": 1, "fact": "材料编号：QLAB-753826", "bbox": [0.1, 0.5, 0.4, 0.6]},
            {"material_id": "m1", "page_index": 1, "fact": "型号：EDU-753", "bbox": [0.7, 0.7, 0.9, 0.9]},
            {"material_id": "m1", "page_index": 1, "fact": "包装标签缺型号", "bbox": [0.1, 0.2, 0.3, 0.4]},
        ])
        rep_bad = eval_evidence([task], [pred_bad], [ref], {"schema_version": "1.3.1", "purpose": "test"})
        c = rep_bad["diagnostics"][0]["evidence_checks"]
        assert c[0]["structure_valid"] is True and c[0]["grounded"] is False and "value_contradicted" in c[0]["semantic_issues"]
        assert c[1]["structure_valid"] is True and c[1]["grounded"] is False and "unreadable_fact_fabricated" in c[1]["semantic_issues"]
        assert c[2]["structure_valid"] is True and c[2]["grounded"] is False and "bbox_region_mismatch" in c[2]["semantic_issues"]
        assert c[3]["structure_valid"] is True and c[3]["grounded"] is False and "absence_claim_contradicted" in c[3]["semantic_issues"]


# ---------------------------------------------------------------------------
# L-FUNC-13..15 & L-STRUCT-01..03: Subprocess runner isolation
# ---------------------------------------------------------------------------

class TestPluginIsolationRunner:
    @pytest.fixture
    def fixture_plugins_dir(self, tmp_path: Path) -> Path:
        mod = tmp_path / "dummy_eval_plugins.py"
        mod.write_text(
            """
import math, time
from pathlib import Path

def crashing(inputs, outputs, references, context):
    raise RuntimeError("boom from plugin")

def slow(inputs, outputs, references, context):
    time.sleep(5.0)
    return {
        "plugin_id": "dummy.slow",
        "plugin_version": "1.0",
        "input_schema_version": "1.3.1",
        "metrics": {},
        "diagnostics": [],
        "errors": [],
    }

def mutating(inputs, outputs, references, context):
    outputs.append({"audit_unit_id": "hacked"})
    return {
        "plugin_id": "dummy.mutating",
        "plugin_version": "1.0",
        "input_schema_version": "1.3.1",
        "metrics": {},
        "diagnostics": [],
        "errors": [],
    }

def nan_output(inputs, outputs, references, context):
    return {
        "plugin_id": "dummy.nan",
        "plugin_version": "1.0",
        "input_schema_version": "1.3.1",
        "metrics": {"score": math.nan},
        "diagnostics": [],
        "errors": [],
    }

def side_effect_marker(inputs, outputs, references, context):
    marker = context.get("marker_file")
    if marker:
        Path(marker).write_text("INVOKED", encoding="utf-8")
    return {
        "plugin_id": "dummy.marker",
        "plugin_version": "1.0",
        "input_schema_version": "1.3.1",
        "metrics": {"called": 1},
        "diagnostics": [],
        "errors": [],
    }
""",
            encoding="utf-8",
        )
        return tmp_path

    def test_l_func_13_crash_isolated_and_healthy_plugin_succeeds(self, fixture_plugins_dir: Path):
        tasks = [_make_task("u1")]
        refs = [_make_ref("u1", "pass")]
        preds = [_make_pred("u1", "pass")]
        preds_before = json.dumps(preds, sort_keys=True)

        reg = PluginRegistry([
            PluginSpec("dummy.crash", "dummy_eval_plugins:crashing", "1.0"),
            PluginSpec("o2.conclusion", "o2_audit.evaluation.conclusion:evaluate", "1.0.0"),
        ])
        reports = run_plugins(
            reg, tasks, preds, refs,
            {"schema_version": "1.3.1", "purpose": "isolation"},
            extra_pythonpath=[str(fixture_plugins_dir)],
        )
        assert len(reports) == 2
        assert reports[0]["errors"][0]["code"] == "plugin_failed"
        assert reports[1]["errors"] == []
        assert reports[1]["metrics"]["accuracy"] == 1.0
        assert json.dumps(preds, sort_keys=True) == preds_before

    def test_l_func_14_timeout_isolated(self, fixture_plugins_dir: Path):
        spec = PluginSpec("dummy.slow", "dummy_eval_plugins:slow", "1.0", timeout_seconds=0.5)
        rep = run_plugin(
            spec, [_make_task("u1")], [_make_pred("u1")], [_make_ref("u1")],
            {"schema_version": "1.3.1", "purpose": "timeout"},
            extra_pythonpath=[str(fixture_plugins_dir)],
        )
        assert rep["errors"][0]["code"] == "plugin_timeout"
        assert rep["metrics"] == {}

    def test_l_func_15_disabled_plugin_never_invoked(self, fixture_plugins_dir: Path, tmp_path: Path):
        marker = tmp_path / "should_not_exist.txt"
        reg = PluginRegistry([
            PluginSpec("dummy.disabled", "dummy_eval_plugins:side_effect_marker", "1.0", enabled=False),
            PluginSpec("o2.conclusion", "o2_audit.evaluation.conclusion:evaluate", "1.0.0", enabled=True),
        ])
        reports = run_plugins(
            reg, [_make_task("u1")], [_make_pred("u1")], [_make_ref("u1")],
            {"schema_version": "1.3.1", "purpose": "disabled_test", "marker_file": str(marker)},
            extra_pythonpath=[str(fixture_plugins_dir)],
        )
        assert not marker.exists()
        assert reports[0]["status"] == "disabled"
        assert reports[0]["invoked"] is False
        assert reports[1]["errors"] == [] and reports[1]["metrics"]["accuracy"] == 1.0

    def test_l_struct_03_input_mutation_and_nan_rejected(self, fixture_plugins_dir: Path):
        tasks = [_make_task("u1")]
        preds = [_make_pred("u1")]
        refs = [_make_ref("u1")]
        snap = json.dumps([tasks, preds, refs], sort_keys=True)

        rep_mut = run_plugin(
            PluginSpec("dummy.mut", "dummy_eval_plugins:mutating", "1.0"),
            tasks, preds, refs, {"schema_version": "1.3.1", "purpose": "mut"},
            extra_pythonpath=[str(fixture_plugins_dir)],
        )
        assert rep_mut["errors"][0]["code"] == "plugin_mutated_inputs"
        assert json.dumps([tasks, preds, refs], sort_keys=True) == snap

        rep_nan = run_plugin(
            PluginSpec("dummy.nan", "dummy_eval_plugins:nan_output", "1.0"),
            tasks, preds, refs, {"schema_version": "1.3.1", "purpose": "nan"},
            extra_pythonpath=[str(fixture_plugins_dir)],
        )
        assert rep_nan["errors"][0]["code"] == "plugin_invalid_output"

    def test_manifest_roundtrip_and_toggle(self, tmp_path: Path):
        reg = load_plugin_registry()
        toggled = reg.with_enabled("evidence", False)
        cfg_path = tmp_path / "plugins.json"
        save_plugin_registry(toggled, cfg_path)
        reloaded = load_plugin_registry(cfg_path)
        assert reloaded.resolve("evidence").enabled is False
        assert reloaded.resolve("conclusion").enabled is True


# ---------------------------------------------------------------------------
# L-FUNC-16: Version comparison & protection gate
# ---------------------------------------------------------------------------

class TestVersionComparisonAndGate:
    def test_l_func_16_with_truth_and_per_rule_version_alignment(self):
        tasks = [
            _make_task("u1", "B001", "3.0"),
            _make_task("u2", "B001", "3.0"),
            _make_task("u3", "B001", "3.1"),
            _make_task("u4", "B001", "3.1"),
        ]
        refs = [
            _make_ref("u1", "pass", "applicable", "B001", "3.0"),
            _make_ref("u2", "reject", "applicable", "B001", "3.0"),
            _make_ref("u3", "pass", "applicable", "B001", "3.1"),
            _make_ref("u4", "reject", "applicable", "B001", "3.1"),
        ]
        base = [
            _make_pred("u1", "reject", "applicable", rule_version="3.0"),  # wrong -> will be repaired
            _make_pred("u2", "reject", "applicable", rule_version="3.0"),  # right -> stays right
            _make_pred("u3", "pass", "applicable", rule_version="3.1"),    # right -> becomes new_error
            _make_pred("u4", "pass", "applicable", rule_version="3.1"),    # wrong -> stays wrong
        ]
        cand = [
            _make_pred("u1", "pass", "applicable", rule_version="3.0"),    # repaired!
            _make_pred("u2", "reject", "applicable", rule_version="3.0"),  # unchanged_correct
            _make_pred("u3", "reject", "applicable", rule_version="3.1"),  # new_error!
            _make_pred("u4", "ambiguous", "applicable", rule_version="3.1"),  # unchanged_error
        ]
        comp = compare_predictions(tasks, base, cand, refs)
        assert comp["mode"] == "reference_comparison"
        assert comp["repaired_ids"] == ["u1"]
        assert comp["new_error_ids"] == ["u3"]
        assert comp["unchanged_correct_ids"] == ["u2"]
        assert comp["unchanged_error_ids"] == ["u4"]
        assert set(comp["by_rule_version"]) == {"B001@3.0", "B001@3.1"}
        assert comp["by_rule_version"]["B001@3.0"]["repaired_ids"] == ["u1"]
        assert comp["by_rule_version"]["B001@3.1"]["new_error_ids"] == ["u3"]

    def test_compare_without_truth_never_claims_repairs(self):
        tasks = [_make_task("u1"), _make_task("u2")]
        base = [_make_pred("u1", "pass"), _make_pred("u2", "reject")]
        cand = [_make_pred("u1", "reject"), _make_pred("u2", "reject")]
        comp = compare_predictions(tasks, base, cand, references=None)
        assert comp["mode"] == "prediction_diff_only"
        assert comp["changed_prediction_ids"] == ["u1"]
        assert comp["unchanged_prediction_ids"] == ["u2"]
        assert comp["repaired_ids"] is None
        assert comp["new_error_ids"] is None

    def test_protection_gate_pass_and_block_cases(self):
        r_tasks = [_make_task("r1"), _make_task("r2")]
        r_refs = [_make_ref("r1", "pass"), _make_ref("r2", "reject")]
        p_tasks = [_make_task("p1"), _make_task("p2")]
        p_refs = [_make_ref("p1", "pass"), _make_ref("p2", "reject")]

        # Clean improvement on repair + zero regression on protected -> eligible
        g_ok = evaluate_protection_gate(
            r_refs, r_tasks, p_refs, p_tasks,
            outputs={
                "baseline_repair": [_make_pred("r1", "reject"), _make_pred("r2", "reject")],
                "candidate_repair": [_make_pred("r1", "pass"), _make_pred("r2", "reject")],
                "baseline_protected": [_make_pred("p1", "pass"), _make_pred("p2", "reject")],
                "candidate_protected": [_make_pred("p1", "pass"), _make_pred("p2", "reject")],
            },
        )
        assert g_ok["official"] is False
        assert g_ok["comparison_inputs_valid"] is True
        assert g_ok["promotion_eligible"] is True

        # New protected error -> blocked
        g_prot = evaluate_protection_gate(
            r_refs, r_tasks, p_refs, p_tasks,
            outputs={
                "baseline_repair": [_make_pred("r1", "reject"), _make_pred("r2", "reject")],
                "candidate_repair": [_make_pred("r1", "pass"), _make_pred("r2", "reject")],
                "baseline_protected": [_make_pred("p1", "pass"), _make_pred("p2", "reject")],
                "candidate_protected": [_make_pred("p1", "reject"), _make_pred("p2", "reject")],
            },
        )
        assert g_prot["promotion_eligible"] is False
        assert g_prot["sets"]["protected"]["new_error_ids"] == ["p1"]

        # New execution error -> blocked
        g_err = evaluate_protection_gate(
            r_refs, r_tasks, p_refs, p_tasks,
            outputs={
                "baseline_repair": [_make_pred("r1", "pass"), _make_pred("r2", "reject")],
                "candidate_repair": [
                    _make_pred("r1", None, None, execution_status="error",
                               reason_code="execution_error", error_code="gateway_timeout"),
                    _make_pred("r2", "reject"),
                ],
                "baseline_protected": [_make_pred("p1", "pass"), _make_pred("p2", "reject")],
                "candidate_protected": [_make_pred("p1", "pass"), _make_pred("p2", "reject")],
            },
        )
        assert g_err["promotion_eligible"] is False
        assert "r1" in g_err["new_execution_failure_ids"]


# ---------------------------------------------------------------------------
# L-STRUCT-02, L-STRUCT-04 & Live CLI smoke on public dev set
# ---------------------------------------------------------------------------

class TestStructureIsolationAndCLI:
    def test_l_struct_04_production_modules_never_import_evaluation(self):
        src_root = Path(__file__).resolve().parent.parent / "src" / "o2_audit"
        forbidden_dirs = ("build", "domain", "materials", "model_gateway", "operators", "protocol", "rules", "runtime", "storage")
        violations: list[str] = []
        for d in forbidden_dirs:
            folder = src_root / d
            if not folder.exists():
                continue
            for py_file in folder.rglob("*.py"):
                tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            if "o2_audit.evaluation" in alias.name:
                                violations.append(f"{py_file}: import {alias.name}")
                    elif isinstance(node, ast.ImportFrom):
                        mod = node.module or ""
                        if mod.startswith("o2_audit.evaluation"):
                            violations.append(f"{py_file}: from {mod}")
        assert violations == []

    def test_cli_evaluate_live_on_public_dev_conclusion_and_evidence(self, tmp_path: Path, capsys):
        # Verify L-STRUCT-03: input/prediction/reference files remain byte-identical
        before_hashes = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (PUBLIC_INPUTS, PUBLIC_PREDS, PUBLIC_REFS)
        }

        rep_conc = tmp_path / "conclusion_report.json"
        rc1 = cli_main([
            "evaluate",
            "--plugin", "conclusion",
            "--inputs", str(PUBLIC_INPUTS),
            "--outputs", str(PUBLIC_PREDS),
            "--references", str(PUBLIC_REFS),
            "--out", str(rep_conc),
        ])
        assert rc1 == 0
        conc = json.loads(rep_conc.read_text(encoding="utf-8"))
        assert conc["plugin_id"] == "o2.conclusion"
        assert conc["plugin_version"] == "1.0.0"
        assert conc["input_schema_version"] == "1.3.1"
        assert conc["metrics"]["all"] == 64
        assert conc["metrics"]["correct"] == 64
        assert conc["metrics"]["accuracy"] == 1.0
        assert conc["metrics"]["quality"] == 1.0

        rep_ev = tmp_path / "evidence_report.json"
        rc2 = cli_main([
            "evaluate",
            "--plugin", "evidence",
            "--inputs", str(PUBLIC_INPUTS),
            "--outputs", str(PUBLIC_PREDS),
            "--references", str(PUBLIC_REFS),
            "--out", str(rep_ev),
        ])
        assert rc2 == 0
        ev = json.loads(rep_ev.read_text(encoding="utf-8"))
        assert ev["plugin_id"] == "o2.evidence"
        assert ev["input_schema_version"] == "1.3.1"
        assert ev["metrics"]["structurally_valid_units"] == 64
        assert ev["metrics"]["structurally_invalid_items"] == 0
        assert ev["metrics"]["semantically_supported_items"] > 0
        assert ev["metrics"]["semantically_unsupported_items"] == 0

        after_hashes = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (PUBLIC_INPUTS, PUBLIC_PREDS, PUBLIC_REFS)
        }
        assert after_hashes == before_hashes
