"""Stage 02a semantic tests: registry strictness (62 pairs), per-rule R001-R060
coverage (pass / reject / ambiguous with applicability and reason codes), the
named supervisor acceptance gates, and the generalized AuditController.

All fact graphs are synthetic builders — nothing here performs image/OCR work
and the StaticObservationGateway is a clearly test-only mock.
"""
from __future__ import annotations

import pytest

from o2_audit.domain.models import MaterialRef
from o2_audit.protocol.checks import prediction_semantic_errors
from o2_audit.rules.base import UnsupportedRuleError
from o2_audit.rules.registry import RuleRegistry, default_registry
from o2_audit.runtime.controller import AuditController
from o2_audit.runtime.facts import FactView
from o2_audit.runtime.gateway import ObservationBatch, StaticObservationGateway

from conftest import ent, make_task, obs

REG = default_registry()


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def fx(*specs) -> FactView:
    """Compact fact builder: each spec is ``(kind, entity_id, *preds)`` where a
    pred is ``(predicate, value)`` / ``(predicate, value, status)`` /
    ``(predicate, value, status, scope_complete)``."""
    entities, observations = [], []
    for spec in specs:
        kind, eid, *preds = spec
        entities.append(ent(eid, kind))
        for p in preds:
            predicate, value = p[0], p[1]
            status = p[2] if len(p) > 2 else "readable"
            scope = p[3] if len(p) > 3 else False
            observations.append(obs(eid, predicate, value, status=status,
                                    scope_complete=scope))
    return FactView(entities=entities, observations=observations)


def m(material_id: str, role: str) -> MaterialRef:
    return MaterialRef(material_id=material_id, role=role,
                       relative_path=f"assets/{material_id}.png",
                       media_type="image/png", page_index=1)


def evaluate(rule_id, facts, metadata=None, materials=None,
             review_date="2026-06-15", rule_version="3.0"):
    """Run one advanced rule through the registry (same applicability
    semantics as the controller)."""
    task = make_task(metadata=metadata, materials=materials, rule_id=rule_id,
                     rule_version=rule_version, review_date=review_date,
                     audit_unit_id=f"u-{rule_id}-{abs(hash(facts)) % 10000}")
    return REG.evaluate(task, facts), task


def check(outcome, label, applicability="applicable", reason_code=None):
    assert outcome.label == label, f"label={outcome.label} reason={outcome.reason_code}"
    assert outcome.applicability == applicability, \
        f"applicability={outcome.applicability}"
    if reason_code is not None:
        assert outcome.reason_code == reason_code, \
            f"reason_code={outcome.reason_code} (wanted {reason_code})"
    return outcome


# ---------------------------------------------------------------------------
# 1. registry completeness & strictness
# ---------------------------------------------------------------------------


def test_registry_has_exactly_62_pairs():
    expected = {("B001", "3.0"), ("B001", "3.1")} | {
        (f"R{i:03d}", "3.0") for i in range(1, 61)}
    assert set(REG.keys()) == expected
    assert len(REG) == 62


def test_every_plan_has_text_family_and_quotes():
    for key in REG.keys():
        plan = REG.get_plan(*key)
        assert plan.title.strip(), key
        assert plan.raw_rule_text.strip(), key
        assert plan.quotes and all(v.strip() for v in plan.quotes.values()), key
        if key[0].startswith("R"):
            assert plan.quotes.get("rule_text", "").strip(), key
        assert plan.rule_family_id.strip(), key


@pytest.mark.parametrize("rule_id,version", [
    ("R999", "3.0"), ("R061", "3.0"), ("R001", "3.1"), ("B001", "2.0"),
])
def test_unknown_rule_or_version_raises(rule_id, version):
    with pytest.raises(UnsupportedRuleError):
        REG.get_plan(rule_id, version)
    # UnsupportedRuleError is also a KeyError
    with pytest.raises(KeyError):
        REG.get_plan(rule_id, version)


def test_registry_evaluate_matches_controller_applicability():
    # R009 with declared_quantity=1: registry.evaluate and the controller must
    # produce the same not_applicable applicability semantics.
    task = make_task(rule_id="R009", metadata={"declared_quantity": 1},
                     audit_unit_id="u-consistency")
    outcome = REG.evaluate(task, fx())
    assert outcome.applicability == "not_applicable"
    assert outcome.label == "pass"
    assert outcome.reason_code == "rule_not_applicable"


# ---------------------------------------------------------------------------
# 2. F01 实拍证据完整性 (R001-R006)
# ---------------------------------------------------------------------------


def test_r001_boundary_visible_with_interior_obstructed_passes():
    outcome, _ = evaluate("R001", fx(
        ("package", "P1", ("boundary_visible", True), ("interior_obstructed", True))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r001_cropped_boundary_rejects():
    outcome, _ = evaluate("R001", fx(
        ("package", "P1", ("boundary_visible", False))))
    check(outcome, "reject", reason_code="boundary_cropped")


def test_r001_obstructed_boundary_ambiguous():
    outcome, _ = evaluate("R001", fx(
        ("package", "P1", ("boundary_visible", None, "unreadable"))))
    check(outcome, "ambiguous", reason_code="boundary_unverifiable")


def test_r001_no_main_view_submitted_rejects():
    outcome, _ = evaluate("R001", fx(), materials=[m("m9", "unrelated_role")])
    check(outcome, "reject", reason_code="main_view_not_submitted")


def test_r001_not_applicable_without_outer_package():
    outcome, _ = evaluate("R001", fx(), metadata={"has_outer_package": False})
    check(outcome, "pass", applicability="not_applicable",
          reason_code="rule_not_applicable")


def test_r002_multi_face_photo_covers_declared_faces():
    outcome, _ = evaluate("R002", fx(
        ("package", "P1", ("visible_faces", ["正面", "背面", "侧面"]))),
        metadata={"required_faces": ["正面", "背面"]})
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r002_missing_face_rejects():
    outcome, _ = evaluate("R002", fx(
        ("package", "P1", ("visible_faces", ["正面"]))),
        metadata={"required_faces": ["正面", "背面"]})
    check(outcome, "reject", reason_code="required_face_missing")


def test_r002_missing_required_faces_ambiguous_but_applicable():
    outcome, _ = evaluate("R002", fx(
        ("package", "P1", ("visible_faces", ["正面"]))),
        metadata={"required_faces": None})
    check(outcome, "ambiguous", applicability="applicable",
          reason_code="required_faces_missing")


def test_r003_distinct_printed_pages_with_same_source_page_pass():
    outcome, _ = evaluate("R003", fx(
        ("document", "D1", ("printed_page_number", 1)),
        ("document", "D2", ("printed_page_number", 2)),
        ("document", "D3", ("printed_page_number", 3))),
        metadata={"certificate_total_pages": 3})
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r003_missing_page_rejects():
    outcome, _ = evaluate("R003", fx(
        ("document", "D1", ("printed_page_number", 1)),
        ("document", "D2", ("printed_page_number", 2))),
        metadata={"certificate_total_pages": 3})
    check(outcome, "reject", reason_code="page_missing")


def test_r003_out_of_range_page_rejects():
    outcome, _ = evaluate("R003", fx(
        ("document", "D1", ("printed_page_number", 4))),
        metadata={"certificate_total_pages": 3})
    check(outcome, "reject", reason_code="page_number_out_of_range")


def test_r003_unreadable_printed_page_ambiguous():
    outcome, _ = evaluate("R003", fx(
        ("document", "D1", ("printed_page_number", 1)),
        ("document", "D2", ("printed_page_number", None, "unreadable"))),
        metadata={"certificate_total_pages": 3})
    check(outcome, "ambiguous", reason_code="printed_page_number_unreadable")


def test_r004_attached_tag_connection_shown_passes():
    outcome, _ = evaluate("R004", fx(
        ("label", "L1", ("carrier", "attached_tag"), ("attached_to_package", True))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r004_detached_tag_rejects():
    outcome, _ = evaluate("R004", fx(
        ("label", "L1", ("carrier", "attached_tag"),
         ("attached_to_package", None, "confirmed_missing", True))))
    check(outcome, "reject", reason_code="detached_tag_only")


def test_r004_connection_uncertain_ambiguous():
    outcome, _ = evaluate("R004", fx(
        ("label", "L1", ("carrier", "attached_tag"),
         ("attached_to_package", None, "unreadable"))))
    check(outcome, "ambiguous", reason_code="tag_connection_unverifiable")


def test_r004_no_tag_usage_not_applicable():
    outcome, _ = evaluate("R004", fx(("label", "L1", ("model", "LED-100"))))
    check(outcome, "pass", applicability="not_applicable",
          reason_code="rule_not_applicable")


def test_r005_unobstructed_same_field_supplement_passes():
    outcome, _ = evaluate("R005", fx(
        ("label", "L1", ("interior_obstructed", True), ("model", "M-1")),
        ("label", "L2", ("supplement_field_identity", "M-1"),
         ("interior_obstructed", False))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r005_all_views_obstructed_rejects():
    outcome, _ = evaluate("R005", fx(
        ("label", "L1", ("interior_obstructed", True), ("model", "M-1")),
        ("label", "L2", ("supplement_field_identity", "M-1"),
         ("interior_obstructed", True))))
    check(outcome, "reject", reason_code="supplement_obstructed")


def test_r005_different_business_anchor_rejects():
    outcome, _ = evaluate("R005", fx(
        ("label", "L1", ("interior_obstructed", True), ("model", "M-1")),
        ("label", "L2", ("supplement_field_identity", "M-2"),
         ("interior_obstructed", False))))
    check(outcome, "reject", reason_code="supplement_different_field")


def test_r005_uncertain_supplement_identity_ambiguous():
    outcome, _ = evaluate("R005", fx(
        ("label", "L1", ("interior_obstructed", True), ("model", "M-1")),
        ("label", "L2", ("supplement_field_identity", None, "unreadable"))))
    check(outcome, "ambiguous", reason_code="supplement_identity_ambiguous")


def test_r005_not_obstructed_passes():
    outcome, _ = evaluate("R005", fx(
        ("label", "L1", ("interior_obstructed", False))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r005_target_anchor_unreadable_ambiguous():
    outcome, _ = evaluate("R005", fx(
        ("label", "L1", ("interior_obstructed", True)),
        ("label", "L2", ("supplement_field_identity", "M-1"),
         ("interior_obstructed", False))))
    check(outcome, "ambiguous", reason_code="target_anchor_unreadable")


def test_r006_photo_source_passes():
    outcome, _ = evaluate("R006", fx(
        ("document", "D1", ("material_authenticity", "photo"))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r006_design_render_only_rejects():
    outcome, _ = evaluate("R006", fx(
        ("document", "D1", ("material_authenticity", "design"))))
    check(outcome, "reject", reason_code="design_render_only")


def test_r006_unknown_source_ambiguous():
    outcome, _ = evaluate("R006", fx(
        ("document", "D1", ("material_authenticity", "screenshot"))))
    check(outcome, "ambiguous", reason_code="source_type_unknown")


# ---------------------------------------------------------------------------
# 3. F02 商品身份与型号 (R007-R012)
# ---------------------------------------------------------------------------


def test_r007_model_match_passes():
    outcome, _ = evaluate("R007", fx(("label", "L1", ("model", "LED-100"))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r007_model_mismatch_rejects():
    outcome, _ = evaluate("R007", fx(("label", "L1", ("model", "LED-200"))))
    check(outcome, "reject", reason_code="field_check_failed")


def test_r007_model_unreadable_ambiguous():
    outcome, _ = evaluate("R007", fx(("label", "L1", ("model", None, "unreadable"))))
    check(outcome, "ambiguous", reason_code="field_unreadable")


def test_r008_component_set_matches():
    outcome, _ = evaluate("R008", fx(
        ("package", "P1", ("component_models", ["B", "A"]))),
        metadata={"declared_component_models": ["A", "B"]})
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r008_component_set_mismatch_rejects():
    outcome, _ = evaluate("R008", fx(
        ("package", "P1", ("component_models", ["A"]))),
        metadata={"declared_component_models": ["A", "B"]})
    check(outcome, "reject", reason_code="component_set_mismatch")


def test_r008_declared_components_missing_ambiguous():
    outcome, _ = evaluate("R008", fx(("package", "P1")),
                          metadata={"declared_component_models": None})
    check(outcome, "ambiguous", reason_code="declared_components_missing")


def test_r009_unique_serials_pass():
    outcome, _ = evaluate("R009", fx(
        ("object", "O1", ("serial_number", "S1")),
        ("object", "O2", ("serial_number", "S2"))),
        metadata={"declared_quantity": 2})
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r009_duplicate_serials_reject():
    outcome, _ = evaluate("R009", fx(
        ("object", "O1", ("serial_number", "S1")),
        ("object", "O2", ("serial_number", "S1"))),
        metadata={"declared_quantity": 2})
    check(outcome, "reject", reason_code="serial_duplicates")


def test_r009_missing_serial_reject():
    outcome, _ = evaluate("R009", fx(
        ("object", "O1", ("serial_number", "S1"))),
        metadata={"declared_quantity": 2})
    check(outcome, "reject", reason_code="serial_missing_unit")


def test_r009_confirmed_missing_serial_reject():
    outcome, _ = evaluate("R009", fx(
        ("object", "O1", ("serial_number", "S1")),
        ("object", "O2", ("serial_number", None, "confirmed_missing", True))),
        metadata={"declared_quantity": 2})
    check(outcome, "reject", reason_code="serial_confirmed_missing")


def test_r009_single_unit_not_applicable():
    outcome, _ = evaluate("R009", fx(), metadata={"declared_quantity": 1})
    check(outcome, "pass", applicability="not_applicable",
          reason_code="rule_not_applicable")


def test_r009_unreadable_serial_ambiguous():
    outcome, _ = evaluate("R009", fx(
        ("object", "O1", ("serial_number", None, "unreadable"))),
        metadata={"declared_quantity": 2})
    check(outcome, "ambiguous", reason_code="serial_unreadable")


def test_r010_main_model_matches():
    outcome, _ = evaluate("R010", fx(
        ("label", "L1", ("main_model", "LED-100"))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r010_declared_model_only_compatible_rejects():
    outcome, _ = evaluate("R010", fx(
        ("label", "L1", ("compatible_models", ["LED-100"]))))
    check(outcome, "reject", reason_code="declared_model_only_compatible")


def test_r010_main_model_unreadable_ambiguous():
    outcome, _ = evaluate("R010", fx(
        ("label", "L1", ("main_model", None, "unreadable"))))
    check(outcome, "ambiguous", reason_code="main_model_unreadable")


def test_r010_main_model_mismatch_rejects():
    outcome, _ = evaluate("R010", fx(
        ("label", "L1", ("main_model", "LED-200"))))
    check(outcome, "reject", reason_code="main_model_mismatch")


def test_r011_replacement_mapping_ok():
    outcome, _ = evaluate("R011", fx(
        ("package", "P1", ("old_model", "OLD-1")),
        ("certificate", "C1", ("replacement_map", {"OLD-1": "LED-100"}))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r011_mapping_invalid_rejects():
    outcome, _ = evaluate("R011", fx(
        ("package", "P1", ("old_model", "OLD-1")),
        ("certificate", "C1", ("replacement_map", {"OLD-1": "OTHER"}))))
    check(outcome, "reject", reason_code="replacement_mapping_invalid")


def test_r011_missing_replacement_map_rejects():
    outcome, _ = evaluate("R011", fx(
        ("package", "P1", ("old_model", "OLD-1"))))
    check(outcome, "reject", reason_code="replacement_map_missing")


def test_r011_no_old_model_not_applicable():
    outcome, _ = evaluate("R011", fx(("package", "P1", ("model", "LED-100"))))
    check(outcome, "pass", applicability="not_applicable",
          reason_code="rule_not_applicable")


def test_r011_old_model_unreadable_ambiguous():
    outcome, _ = evaluate("R011", fx(
        ("package", "P1", ("old_model", None, "unreadable"))))
    check(outcome, "ambiguous", reason_code="old_model_unreadable")


def test_r012_o0_and_i1_substitution_rejected():
    outcome, _ = evaluate("R012", fx(
        ("label", "L1", ("model", "ABO-I"))),
        metadata={"declared_model": "AB0-1"})
    check(outcome, "reject", reason_code="similar_char_substitution")


def test_r012_exact_match_passes():
    outcome, _ = evaluate("R012", fx(
        ("label", "L1", ("model", "AB0-1"))),
        metadata={"declared_model": "AB0-1"})
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r012_indistinguishable_ambiguous():
    outcome, _ = evaluate("R012", fx(
        ("label", "L1", ("model", None, "unreadable"))),
        metadata={"declared_model": "AB0-1"})
    check(outcome, "ambiguous", reason_code="similar_chars_indistinguishable")


def test_r012_model_without_0_or_1_not_applicable():
    outcome, _ = evaluate("R012", fx(
        ("label", "L1", ("model", "ABX"))),
        metadata={"declared_model": "ABX"})
    check(outcome, "pass", applicability="not_applicable",
          reason_code="rule_not_applicable")


# ---------------------------------------------------------------------------
# 4. F03 字段完整性与格式 (R013-R018)
# ---------------------------------------------------------------------------


def test_r013_number_format_pass():
    outcome, _ = evaluate("R013", fx(
        ("label", "L1", ("material_number", "QLAB-123456"))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r013_number_format_reject():
    outcome, _ = evaluate("R013", fx(
        ("label", "L1", ("material_number", "qlab-123456"))))
    check(outcome, "reject", reason_code="field_check_failed")


def test_r013_number_unreadable_ambiguous():
    outcome, _ = evaluate("R013", fx(
        ("label", "L1", ("material_number", None, "unreadable"))))
    check(outcome, "ambiguous", reason_code="field_unreadable")


def test_r014_batch_format_pass():
    outcome, _ = evaluate("R014", fx(
        ("label", "L1", ("batch_number", "20260101-001"))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r014_batch_format_reject():
    outcome, _ = evaluate("R014", fx(
        ("label", "L1", ("batch_number", "20260132-001"))))
    check(outcome, "reject", reason_code="field_check_failed")


def test_r014_batch_unreadable_ambiguous():
    outcome, _ = evaluate("R014", fx(
        ("label", "L1", ("batch_number", None, "unreadable"))))
    check(outcome, "ambiguous", reason_code="field_unreadable")


def test_r015_piece_count_pass():
    outcome, _ = evaluate("R015", fx(("label", "L1", ("piece_count", "3件"))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r015_piece_count_zero_rejects():
    outcome, _ = evaluate("R015", fx(("label", "L1", ("piece_count", "0件"))))
    check(outcome, "reject", reason_code="field_check_failed")


def test_r015_piece_count_unreadable_ambiguous():
    outcome, _ = evaluate("R015", fx(
        ("label", "L1", ("piece_count", None, "unreadable"))))
    check(outcome, "ambiguous", reason_code="field_unreadable")


def test_r016_support_url_pass():
    outcome, _ = evaluate("R016", fx(
        ("label", "L1", ("support_url", "https://example.com"))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r016_qr_only_rejects():
    outcome, _ = evaluate("R016", fx(("label", "L1", ("qr_present", True))))
    check(outcome, "reject", reason_code="qr_only_no_address_text")


def test_r016_url_unobserved_ambiguous():
    outcome, _ = evaluate("R016", fx(("label", "L1", ("batch_number", "X"))))
    check(outcome, "ambiguous", reason_code="support_url_unobserved")


def test_r017_interval_ordered_pass():
    outcome, _ = evaluate("R017", fx(
        ("label", "L1", ("valid_from", "2026-01-01"), ("valid_until", "2026-12-31"))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r017_one_endpoint_only_rejects():
    outcome, _ = evaluate("R017", fx(
        ("label", "L1", ("valid_from", "2026-01-01"),
         ("valid_until", None, "confirmed_missing", True))))
    check(outcome, "reject", reason_code="interval_one_endpoint_only")


def test_r017_reversed_interval_rejects():
    outcome, _ = evaluate("R017", fx(
        ("label", "L1", ("valid_from", "2026-12-31"), ("valid_until", "2026-01-01"))))
    check(outcome, "reject", reason_code="interval_reversed")


def test_r017_date_unreadable_ambiguous():
    outcome, _ = evaluate("R017", fx(
        ("label", "L1", ("valid_from", None, "unreadable"),
         ("valid_until", "2026-12-31"))))
    check(outcome, "ambiguous", reason_code="interval_date_unreadable")


def test_r018_bilingual_pair_pass():
    outcome, _ = evaluate("R018", fx(
        ("label", "L1", ("product_name_cn", "台灯"), ("product_name_en", "Lamp"))),
        metadata={"language_mode": "双语"})
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r018_missing_english_name_rejects():
    outcome, _ = evaluate("R018", fx(
        ("label", "L1", ("product_name_cn", "台灯"),
         ("product_name_en", None, "confirmed_missing", True))),
        metadata={"language_mode": "双语"})
    check(outcome, "reject", reason_code="bilingual_pair_missing")


def test_r018_name_unreadable_ambiguous():
    outcome, _ = evaluate("R018", fx(
        ("label", "L1", ("product_name_cn", None, "unreadable"),
         ("product_name_en", "Lamp"))),
        metadata={"language_mode": "双语"})
    check(outcome, "ambiguous", reason_code="bilingual_pair_unreadable")


def test_r018_non_bilingual_not_applicable():
    outcome, _ = evaluate("R018", fx(("label", "L1")),
                          metadata={"language_mode": "中文"})
    check(outcome, "pass", applicability="not_applicable",
          reason_code="rule_not_applicable")


# ---------------------------------------------------------------------------
# 5. F04 标签位置与载体 (R019-R024)
# ---------------------------------------------------------------------------


def test_r019_front_location_with_unreadable_value_still_passes():
    outcome, _ = evaluate("R019", fx(
        ("label", "L1", ("model", None, "unreadable"), ("view_type", "front"))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r019_front_package_view_with_back_label_rejects():
    outcome, _ = evaluate("R019", fx(
        ("package", "P1", ("view_type", "front")),
        ("label", "L1", ("model", "LED-100"), ("view_type", "back"))))
    check(outcome, "reject", reason_code="model_not_on_front")


def test_r019_front_view_missing_ambiguous():
    outcome, _ = evaluate("R019", fx(
        ("label", "L1", ("model", "LED-100"), ("view_type", "back"))))
    check(outcome, "ambiguous", reason_code="front_view_not_submitted")


def test_r020_product_body_with_unreadable_serial_passes():
    outcome, _ = evaluate("R020", fx(
        ("object", "O1", ("serial_carrier", "product_body"),
         ("serial_number", None, "unreadable"))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r020_wrong_carrier_rejects():
    outcome, _ = evaluate("R020", fx(
        ("object", "O1", ("serial_carrier", "outer_package"))))
    check(outcome, "reject", reason_code="serial_wrong_carrier")


def test_r020_carrier_unknown_ambiguous():
    outcome, _ = evaluate("R020", fx(
        ("object", "O1", ("serial_carrier", None, "unreadable"))))
    check(outcome, "ambiguous", reason_code="serial_carrier_unknown")


def test_r021_same_label_pass():
    outcome, _ = evaluate("R021", fx(
        ("label", "L1", ("model", "LED-100"), ("material_number", "QLAB-123456"))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r021_split_across_labels_rejects():
    outcome, _ = evaluate("R021", fx(
        ("label", "L1", ("model", "LED-100")),
        ("label", "L2", ("material_number", "QLAB-123456"))))
    check(outcome, "reject", reason_code="fields_split_across_labels")


def test_r021_field_identity_unknown_ambiguous():
    outcome, _ = evaluate("R021", fx(("label", "L1", ("batch_number", "X"))))
    check(outcome, "ambiguous", reason_code="label_identity_unknown")


def test_r022_small_package_fixed_tag_pass():
    outcome, _ = evaluate("R022", fx(
        ("label", "L1", ("material_number", "QLAB-123456"),
         ("carrier", "attached_tag"), ("attached_to_package", True))),
        metadata={"package_longest_edge_mm": 50})
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r022_tag_at_exact_80_rejects():
    outcome, _ = evaluate("R022", fx(
        ("label", "L1", ("material_number", "QLAB-123456"),
         ("carrier", "attached_tag"), ("attached_to_package", True))),
        metadata={"package_longest_edge_mm": 80})
    check(outcome, "reject", reason_code="tag_not_allowed_for_size")


def test_r022_missing_size_ambiguous_but_applicable():
    outcome, _ = evaluate("R022", fx(
        ("label", "L1", ("material_number", "QLAB-123456"),
         ("carrier", "attached_tag"), ("attached_to_package", True))),
        metadata={"package_longest_edge_mm": None})
    check(outcome, "ambiguous", applicability="applicable",
          reason_code="package_size_missing")


def test_r022_outer_package_pass():
    outcome, _ = evaluate("R022", fx(
        ("label", "L1", ("material_number", "QLAB-123456"),
         ("carrier", "outer_package"))),
        metadata={"package_longest_edge_mm": 120})
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r023_tear_line_blank_margin_pass():
    outcome, _ = evaluate("R023", fx(
        ("label", "L1", ("line_crosses_field", ["blank_margin"]))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r023_tear_line_crosses_model_rejects():
    outcome, _ = evaluate("R023", fx(
        ("label", "L1", ("line_crosses_field", ["model"]))))
    check(outcome, "reject", reason_code="tear_line_crosses_field")


def test_r023_tear_line_unverifiable_ambiguous():
    outcome, _ = evaluate("R023", fx(
        ("label", "L1", ("line_crosses_field", None, "unreadable"))))
    check(outcome, "ambiguous", reason_code="tear_line_unverifiable")


def test_r024_upper_half_and_midline_pass():
    for pos in ("upper_half", "on_midline"):
        outcome, _ = evaluate("R024", fx(
            ("object", "O1", ("relative_position", pos), ("view_type", "front"))))
        check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r024_lower_half_rejects():
    outcome, _ = evaluate("R024", fx(
        ("object", "O1", ("relative_position", "lower_half"), ("view_type", "front"))))
    check(outcome, "reject", reason_code="mark_in_lower_half")


def test_r024_non_standard_view_ambiguous():
    outcome, _ = evaluate("R024", fx(
        ("object", "O1", ("relative_position", "upper_half"), ("view_type", "side"))))
    check(outcome, "ambiguous", reason_code="non_standard_view")


# ---------------------------------------------------------------------------
# 6. F05 图形与视觉关系 (R025-R030)
# ---------------------------------------------------------------------------


def test_r025_hollow_closed_circle_pass():
    outcome, _ = evaluate("R025", fx(
        ("object", "O1", ("shape_type", "circle"), ("is_filled", False),
         ("is_closed", True), ("text_inside_mark", False))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r025_filled_or_unclosed_or_text_rejects():
    for preds in (
            (("shape_type", "circle"), ("is_filled", True)),
            (("shape_type", "circle"), ("is_closed", False)),
            (("shape_type", "circle"), ("text_inside_mark", True))):
        outcome, _ = evaluate("R025", fx(("object", "O1", *preds)))
        check(outcome, "reject")


def test_r025_obstructed_structure_ambiguous():
    outcome, _ = evaluate("R025", fx(
        ("object", "O1", ("shape_type", "circle"), ("is_filled", None, "unreadable"))))
    check(outcome, "ambiguous", reason_code="circle_structure_unverifiable")


def test_r026_triangle_dot_pass():
    outcome, _ = evaluate("R026", fx(
        ("object", "T1", ("shape_type", "triangle"), ("is_closed", True)),
        ("object", "D1", ("shape_type", "dot"), ("relative_position", "inside"))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r026_dot_outside_rejects():
    outcome, _ = evaluate("R026", fx(
        ("object", "T1", ("shape_type", "triangle"), ("is_closed", True)),
        ("object", "D1", ("shape_type", "dot"), ("relative_position", "outside"))))
    check(outcome, "reject", reason_code="dot_outside_triangle")


def test_r026_two_dots_rejects():
    outcome, _ = evaluate("R026", fx(
        ("object", "T1", ("shape_type", "triangle"), ("is_closed", True)),
        ("object", "D1", ("shape_type", "dot"), ("relative_position", "inside")),
        ("object", "D2", ("shape_type", "dot"), ("relative_position", "inside"))))
    check(outcome, "reject", reason_code="multiple_dots")


def test_r026_unclosed_triangle_rejects():
    outcome, _ = evaluate("R026", fx(
        ("object", "T1", ("shape_type", "triangle"), ("is_closed", False)),
        ("object", "D1", ("shape_type", "dot"), ("relative_position", "inside"))))
    check(outcome, "reject", reason_code="triangle_not_closed")


def test_r026_unobserved_ambiguous_not_reject():
    outcome, _ = evaluate("R026", FactView())
    check(outcome, "ambiguous", reason_code="unobserved")


def test_r027_circle_left_of_square_via_bbox_pass():
    outcome, _ = evaluate("R027", fx(
        ("object", "C1", ("shape_type", "circle"), ("mark_bbox", [0.1, 0.4, 0.2, 0.5])),
        ("object", "S1", ("shape_type", "square"), ("mark_bbox", [0.6, 0.4, 0.7, 0.5]))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r027_vertical_stack_same_x_rejects():
    outcome, _ = evaluate("R027", fx(
        ("object", "C1", ("shape_type", "circle"), ("mark_bbox", [0.3, 0.1, 0.4, 0.2])),
        ("object", "S1", ("shape_type", "square"), ("mark_bbox", [0.3, 0.6, 0.4, 0.7]))))
    check(outcome, "reject", reason_code="circle_not_left")


def test_r027_unobserved_ambiguous_not_reject():
    outcome, _ = evaluate("R027", FactView())
    check(outcome, "ambiguous", reason_code="unobserved")


def test_r028_matching_color_pass():
    outcome, _ = evaluate("R028", fx(
        ("object", "D1", ("shape_type", "dot"), ("mark_color", "blue"))),
        metadata={"color_edition": "蓝版"})
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r028_wrong_color_rejects():
    outcome, _ = evaluate("R028", fx(
        ("object", "D1", ("shape_type", "dot"), ("mark_color", "yellow"))),
        metadata={"color_edition": "蓝版"})
    check(outcome, "reject", reason_code="color_mismatch")


def test_r028_grayscale_photo_ambiguous():
    outcome, _ = evaluate("R028", fx(
        ("object", "D1", ("shape_type", "dot"), ("color_mode", "grayscale"),
         ("mark_color", "blue"))),
        metadata={"color_edition": "蓝版"})
    check(outcome, "ambiguous", reason_code="grayscale_photo")


def _star(eid, *, voided=False, hollow=False):
    preds = [("shape_type", "star")]
    if voided:
        preds.append(("void_marked", True))
    else:
        preds.append(("void_marked", False))
    if hollow:
        preds.append(("is_filled", False))
    else:
        preds.append(("is_filled", True))
    return ("object", eid, *preds)


def test_r029_three_solid_one_voided_one_hollow_pass():
    outcome, _ = evaluate("R029", fx(
        ("package", "P1", ("front_fully_shown", True)),
        _star("S1"), _star("S2"), _star("S3"),
        _star("S4", voided=True), _star("S5", hollow=True)))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


@pytest.mark.parametrize("count", [2, 4])
def test_r029_wrong_solid_star_count_rejects(count):
    outcome, _ = evaluate("R029", fx(
        ("package", "P1", ("front_fully_shown", True)),
        *[_star(f"S{i}") for i in range(count)]))
    check(outcome, "reject", reason_code="star_count_wrong")


def test_r029_incomplete_front_view_ambiguous():
    outcome, _ = evaluate("R029", fx(_star("S1"), _star("S2"), _star("S3")))
    check(outcome, "ambiguous", reason_code="front_view_incomplete")


def test_r029_void_status_unverifiable_ambiguous():
    outcome, _ = evaluate("R029", fx(
        ("package", "P1", ("front_fully_shown", True)),
        _star("S1"), _star("S2"), _star("S3"),
        ("object", "S4", ("shape_type", "star"), ("void_marked", None, "unreadable"))))
    check(outcome, "ambiguous", reason_code="star_void_status_unverifiable")


def test_r029_fill_status_unverifiable_ambiguous():
    outcome, _ = evaluate("R029", fx(
        ("package", "P1", ("front_fully_shown", True)),
        _star("S1"), _star("S2"), _star("S3"),
        ("object", "S4", ("shape_type", "star"), ("void_marked", False),
         ("is_filled", None, "unreadable"))))
    check(outcome, "ambiguous", reason_code="star_fill_unverifiable")


def test_r029_zero_stars_confirmed_rejects():
    outcome, _ = evaluate("R029", fx(
        ("package", "P1", ("front_fully_shown", True),
         ("shape_type", None, "confirmed_missing", True))))
    check(outcome, "reject", reason_code="star_count_wrong")


def test_r030_height_ratio_exact_pass():
    outcome, _ = evaluate("R030", fx(
        ("object", "O1", ("height_ratio", 1.5), ("view_type", "front"))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r030_height_ratio_too_small_rejects():
    outcome, _ = evaluate("R030", fx(
        ("object", "O1", ("height_ratio", 1.4), ("view_type", "front"))))
    check(outcome, "reject", reason_code="height_ratio_too_small")


def test_r030_non_standard_view_ambiguous():
    outcome, _ = evaluate("R030", fx(
        ("object", "O1", ("height_ratio", 1.8), ("view_type", "top"))))
    check(outcome, "ambiguous", reason_code="non_standard_view")


# ---------------------------------------------------------------------------
# 7. F06 警示与说明内容 (R031-R036)
# ---------------------------------------------------------------------------


def test_r031_fixed_warning_linebreak_pass():
    outcome, _ = evaluate("R031", fx(
        ("label", "L1", ("verbatim_text", "仅供\n练习使用"))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r031_shortened_warning_rejects():
    outcome, _ = evaluate("R031", fx(("label", "L1", ("verbatim_text", "练习用"))))
    check(outcome, "reject", reason_code="field_check_failed")


def test_r031_warning_unreadable_ambiguous():
    outcome, _ = evaluate("R031", fx(
        ("label", "L1", ("verbatim_text", None, "unreadable"))))
    check(outcome, "ambiguous", reason_code="field_unreadable")


def test_r032_indoor_only_without_outdoor_pass():
    outcome, _ = evaluate("R032", fx(
        ("label", "L1", ("indoor_only_text", True))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r032_no_indoor_restriction_with_outdoor_pass():
    outcome, _ = evaluate("R032", fx(
        ("label", "L1", ("indoor_only_text", False), ("outdoor_durable_text", True))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")
    assert outcome.results[0].reason_code == "no_indoor_restriction"


def test_r032_confirmed_missing_indoor_with_outdoor_pass():
    outcome, _ = evaluate("R032", fx(
        ("label", "L1", ("indoor_only_text", None, "confirmed_missing", True),
         ("outdoor_durable_text", True))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")
    assert outcome.results[0].reason_code == "no_indoor_restriction"


def test_r032_both_restrictions_rejects():
    outcome, _ = evaluate("R032", fx(
        ("label", "L1", ("indoor_only_text", True), ("outdoor_durable_text", True))))
    check(outcome, "reject", reason_code="contradictory_restrictions")


def test_r032_indoor_unreadable_ambiguous():
    outcome, _ = evaluate("R032", fx(
        ("label", "L1", ("indoor_only_text", None, "unreadable"))))
    check(outcome, "ambiguous", reason_code="restriction_text_unverifiable")


def test_r033_exact_steps_pass():
    outcome, _ = evaluate("R033", fx(
        ("document", "D1", ("step_list", ["1 连接底座", "2 固定主体", "3 检查连接"]))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r033_swapped_steps_reject():
    outcome, _ = evaluate("R033", fx(
        ("document", "D1",
         ("step_list", ["2 固定主体", "1 连接底座", "3 检查连接"]))))
    check(outcome, "reject", reason_code="steps_invalid")


def test_r033_duplicate_step_number_reject():
    outcome, _ = evaluate("R033", fx(
        ("document", "D1",
         ("step_list", ["1 连接底座", "1 固定主体", "3 检查连接"]))))
    check(outcome, "reject", reason_code="steps_invalid")


def test_r033_steps_unreadable_ambiguous():
    outcome, _ = evaluate("R033", fx(
        ("document", "D1", ("step_list", None, "unreadable"))))
    check(outcome, "ambiguous", reason_code="steps_unreadable")


def test_r034_clauses_complete_pass():
    outcome, _ = evaluate("R034", fx(
        ("label", "L1", ("disassembly_clauses", ["使用时不可拆卸", "收纳时可拆卸"]))),
        metadata={"storage_disassemblable": True})
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r034_missing_clause_reject():
    outcome, _ = evaluate("R034", fx(
        ("label", "L1", ("disassembly_clauses", ["使用时不可拆卸"]))),
        metadata={"storage_disassemblable": True})
    check(outcome, "reject", reason_code="clauses_invalid")


def test_r034_not_disassemblable_not_applicable():
    outcome, _ = evaluate("R034", fx(("label", "L1")),
                          metadata={"storage_disassemblable": False})
    check(outcome, "pass", applicability="not_applicable",
          reason_code="rule_not_applicable")


def test_r034_condition_unknown_ambiguous():
    outcome, _ = evaluate("R034", fx(("label", "L1")),
                          metadata={"storage_disassemblable": None})
    check(outcome, "ambiguous", applicability="unknown",
          reason_code="applicability_unknown")


def test_r035_english_sentence_pass():
    outcome, _ = evaluate("R035", fx(
        ("label", "L1", ("language_text", ["Keep dry"]))),
        metadata={"language_mode": "英文"})
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r035_wrong_case_rejects():
    outcome, _ = evaluate("R035", fx(
        ("label", "L1", ("language_text", ["keep dry"]))),
        metadata={"language_mode": "英文"})
    check(outcome, "reject", reason_code="required_sentence_missing")


def test_r035_bilingual_requires_both():
    outcome, _ = evaluate("R035", fx(
        ("label", "L1", ("language_text", ["保持干燥"]))),
        metadata={"language_mode": "双语"})
    check(outcome, "reject", reason_code="required_sentence_missing")


def test_r035_text_unreadable_ambiguous():
    outcome, _ = evaluate("R035", fx(
        ("label", "L1", ("language_text", None, "unreadable"))),
        metadata={"language_mode": "英文"})
    check(outcome, "ambiguous", reason_code="language_text_unreadable")


def test_r036_negation_intact_pass():
    outcome, _ = evaluate("R036", fx(("label", "L1", ("verbatim_text", "请勿倒置"))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r036_negation_dropped_rejects():
    outcome, _ = evaluate("R036", fx(("label", "L1", ("verbatim_text", "请倒置"))))
    check(outcome, "reject", reason_code="negation_dropped")


def test_r036_negation_unreadable_ambiguous():
    outcome, _ = evaluate("R036", fx(
        ("label", "L1", ("verbatim_text", None, "unreadable"))))
    check(outcome, "ambiguous", reason_code="negation_unreadable")


# ---------------------------------------------------------------------------
# 8. F07 参数与数值关系 (R037-R042)
# ---------------------------------------------------------------------------


def test_r037_authorized_conversion_pass():
    outcome, _ = evaluate("R037", fx(
        ("label", "L1", ("capacity", {"value": "0.5", "unit": "L"}))),
        metadata={"declared_capacity_ml": 500})
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r037_mismatch_rejects():
    outcome, _ = evaluate("R037", fx(
        ("label", "L1", ("capacity", {"value": 600, "unit": "mL"}))),
        metadata={"declared_capacity_ml": 500})
    check(outcome, "reject", reason_code="capacity_mismatch")


def test_r037_unauthorized_unit_rejects():
    outcome, _ = evaluate("R037", fx(
        ("label", "L1", ("capacity", {"value": 1, "unit": "cL"}))),
        metadata={"declared_capacity_ml": 10})
    check(outcome, "reject", reason_code="capacity_unit_not_authorized")


def test_r037_declared_missing_ambiguous():
    outcome, _ = evaluate("R037", fx(("label", "L1", ("capacity", None))),
                          metadata={"declared_capacity_ml": None})
    check(outcome, "ambiguous", reason_code="declared_capacity_missing")


def test_r038_net_le_total_pass():
    outcome, _ = evaluate("R038", fx(
        ("label", "L1", ("net_weight", {"value": 100, "unit": "g"}),
         ("total_weight", {"value": 1, "unit": "kg"}))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r038_net_exceeds_total_rejects():
    outcome, _ = evaluate("R038", fx(
        ("label", "L1", ("net_weight", {"value": 2, "unit": "kg"}),
         ("total_weight", {"value": 1, "unit": "kg"}))))
    check(outcome, "reject", reason_code="net_exceeds_total")


def test_r038_weight_unreadable_ambiguous():
    outcome, _ = evaluate("R038", fx(
        ("label", "L1", ("net_weight", None, "unreadable"),
         ("total_weight", {"value": 1, "unit": "kg"}))))
    check(outcome, "ambiguous", reason_code="weight_unreadable")


def test_r039_declared_in_range_pass():
    outcome, _ = evaluate("R039", fx(
        ("label", "L1", ("adjustable_height_min", {"value": 20, "unit": "cm"}),
         ("adjustable_height_max", {"value": 40, "unit": "cm"}))),
        metadata={"declared_height_cm": 30})
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r039_out_of_range_rejects():
    outcome, _ = evaluate("R039", fx(
        ("label", "L1", ("adjustable_height_min", {"value": 20, "unit": "cm"}),
         ("adjustable_height_max", {"value": 40, "unit": "cm"}))),
        metadata={"declared_height_cm": 50})
    check(outcome, "reject", reason_code="declared_out_of_range")


def test_r039_range_unreadable_ambiguous():
    outcome, _ = evaluate("R039", fx(
        ("label", "L1", ("adjustable_height_min", None, "unreadable"),
         ("adjustable_height_max", {"value": 40, "unit": "cm"}))),
        metadata={"declared_height_cm": 30})
    check(outcome, "ambiguous", reason_code="height_unreadable")


def test_r040_percentages_sum_pass():
    outcome, _ = evaluate("R040", fx(
        ("label", "L1", ("component_percentages", [30, 40, 30]))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r040_wrong_sum_rejects():
    outcome, _ = evaluate("R040", fx(
        ("label", "L1", ("component_percentages", [30, 40, 20]))))
    check(outcome, "reject", reason_code="field_check_failed")


def test_r040_missing_as_zero_rejects():
    outcome, _ = evaluate("R040", fx(
        ("label", "L1", ("component_percentages", [50, 50]))))
    check(outcome, "reject", reason_code="field_check_failed")


def test_r040_percentages_unreadable_ambiguous():
    outcome, _ = evaluate("R040", fx(
        ("label", "L1", ("component_percentages", None, "unreadable"))))
    check(outcome, "ambiguous", reason_code="field_unreadable")


def test_r041_one_percent_inclusive_pass():
    outcome, _ = evaluate("R041", fx(
        ("label", "L1", ("front_weight", {"value": 100, "unit": "g"}),
         ("back_weight", {"value": 101, "unit": "g"}))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r041_tolerance_exceeded_rejects():
    outcome, _ = evaluate("R041", fx(
        ("label", "L1", ("front_weight", {"value": 100, "unit": "g"}),
         ("back_weight", {"value": 102, "unit": "g"}))))
    check(outcome, "reject", reason_code="face_tolerance_exceeded")


def test_r041_back_unreadable_ambiguous():
    outcome, _ = evaluate("R041", fx(
        ("label", "L1", ("front_weight", {"value": 100, "unit": "g"}),
         ("back_weight", None, "unreadable"))))
    check(outcome, "ambiguous", reason_code="face_weight_unreadable")


def test_r042_within_window_pass():
    outcome, _ = evaluate("R042", fx(
        ("label", "L1", ("production_date", "2026-06-01"))),
        review_date="2026-06-15")
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r042_thirty_days_inclusive_pass():
    outcome, _ = evaluate("R042", fx(
        ("label", "L1", ("production_date", "2026-05-16"))),
        review_date="2026-06-15")  # 2026-05-16 + 30 days == review date
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r042_expired_rejects():
    outcome, _ = evaluate("R042", fx(
        ("label", "L1", ("production_date", "2026-06-01"))),
        review_date="2026-07-02")
    check(outcome, "reject", reason_code="use_window_expired")


def test_r042_production_unreadable_ambiguous():
    outcome, _ = evaluate("R042", fx(
        ("label", "L1", ("production_date", None, "unreadable"))),
        review_date="2026-06-15")
    check(outcome, "ambiguous", reason_code="production_date_unreadable")


# ---------------------------------------------------------------------------
# 9. F08 证明文件结构与时效 (R043-R048)
# ---------------------------------------------------------------------------


def test_r043_cert_number_present_pass():
    outcome, _ = evaluate("R043", fx(
        ("certificate", "C1", ("material_number", "QLAB-123456"))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r043_no_certificate_material_rejects():
    outcome, _ = evaluate("R043", fx(), materials=[m("m1", "package_photos")])
    check(outcome, "reject", reason_code="certificate_not_submitted")


def test_r043_number_missing_rejects():
    outcome, _ = evaluate("R043", fx(
        ("certificate", "C1", ("material_number", None, "confirmed_missing", True))))
    check(outcome, "reject", reason_code="cert_number_missing")


def test_r043_certificate_uninspected_ambiguous():
    outcome, _ = evaluate("R043", fx(), materials=[m("m3", "certificate_pages")])
    check(outcome, "ambiguous", reason_code="certificate_uninspected")


def test_r044_validity_covers_review_pass():
    outcome, _ = evaluate("R044", fx(
        ("certificate", "C1", ("valid_from", "2026-01-01"),
         ("valid_until", "2026-12-31"))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r044_validity_not_covering_rejects():
    outcome, _ = evaluate("R044", fx(
        ("certificate", "C1", ("valid_from", "2025-01-01"),
         ("valid_until", "2026-05-31"))))
    check(outcome, "reject", reason_code="validity_not_covering")


def test_r044_validity_unreadable_ambiguous():
    outcome, _ = evaluate("R044", fx(
        ("certificate", "C1", ("valid_from", None, "unreadable"),
         ("valid_until", "2026-12-31"))))
    check(outcome, "ambiguous", reason_code="validity_unreadable")


def test_r045_coverage_contains_declared_pass():
    outcome, _ = evaluate("R045", fx(
        ("certificate", "C1", ("covered_models", ["LED-100", "LED-200"]))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r045_coverage_missing_declared_rejects():
    outcome, _ = evaluate("R045", fx(
        ("certificate", "C1", ("covered_models", ["LED-200"]))))
    check(outcome, "reject", reason_code="coverage_missing_declared")


def test_r045_wildcard_shortcut_rejects():
    outcome, _ = evaluate("R045", fx(
        ("certificate", "C1", ("covered_models", ["*"]))))
    check(outcome, "reject", reason_code="coverage_shortcut_not_accepted")


def test_r045_coverage_unreadable_ambiguous():
    outcome, _ = evaluate("R045", fx(
        ("certificate", "C1", ("covered_models", None, "unreadable"))))
    check(outcome, "ambiguous", reason_code="coverage_unreadable")


def test_r046_clear_void_rejects_even_when_validity_covers():
    outcome, _ = evaluate("R046", fx(
        ("certificate", "C1", ("void_marked", True),
         ("valid_from", "2026-01-01"), ("valid_until", "2026-12-31"))))
    check(outcome, "reject", reason_code="void_marked_reject")


def test_r046_not_voided_pass():
    outcome, _ = evaluate("R046", fx(
        ("certificate", "C1", ("void_marked", False))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r046_void_unclear_ambiguous():
    outcome, _ = evaluate("R046", fx(
        ("certificate", "C1", ("void_marked", None, "unreadable"))))
    check(outcome, "ambiguous", reason_code="void_mark_unclear")


def test_r047_version_match_pass():
    outcome, _ = evaluate("R047", fx(
        ("certificate", "C1", ("document_version", "V1.0"))),
        metadata={"required_document_version": "V1.0"})
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r047_version_mismatch_rejects():
    outcome, _ = evaluate("R047", fx(
        ("certificate", "C1", ("document_version", "V2.0"))),
        metadata={"required_document_version": "V1.0"})
    check(outcome, "reject", reason_code="field_check_failed")


def test_r047_required_version_missing_ambiguous():
    outcome, _ = evaluate("R047", fx(
        ("certificate", "C1", ("document_version", "V1.0"))),
        metadata={"required_document_version": None})
    check(outcome, "ambiguous", reason_code="required_version_missing")


def test_r048_withdrawal_before_issue_ambiguous():
    outcome, _ = evaluate("R048", fx(
        ("certificate", "C1", ("withdrawal_date", "2026-01-01"),
         ("issue_date", "2026-02-01"))))
    check(outcome, "ambiguous", reason_code="withdrawal_before_issue")


def test_r048_review_on_withdrawal_date_rejects():
    outcome, _ = evaluate("R048", fx(
        ("certificate", "C1", ("withdrawal_date", "2026-06-15"),
         ("issue_date", "2026-01-01"))),
        review_date="2026-06-15")
    check(outcome, "reject", reason_code="review_on_or_after_withdrawal")


def test_r048_review_before_withdrawal_within_validity_pass():
    outcome, _ = evaluate("R048", fx(
        ("certificate", "C1", ("withdrawal_date", "2026-07-01"),
         ("issue_date", "2026-01-01"), ("valid_from", "2026-01-01"),
         ("valid_until", "2026-12-31"))),
        review_date="2026-06-15")
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r048_no_withdrawal_date_pass():
    outcome, _ = evaluate("R048", fx(
        ("certificate", "C1", ("withdrawal_date", None, "confirmed_missing", True))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


# ---------------------------------------------------------------------------
# 10. F09 跨材料关系核验 (R049-R054)
# ---------------------------------------------------------------------------


def test_r049_three_way_consistent_pass():
    outcome, _ = evaluate("R049", fx(
        ("label", "L1", ("material_number", "QLAB-123456")),
        ("certificate", "C1", ("material_number", "QLAB-123456"))),
        metadata={"declared_material_number": "QLAB-123456"})
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r049_inconsistent_rejects():
    outcome, _ = evaluate("R049", fx(
        ("label", "L1", ("material_number", "QLAB-123456")),
        ("certificate", "C1", ("material_number", "QLAB-999999"))),
        metadata={"declared_material_number": "QLAB-123456"})
    check(outcome, "reject", reason_code="three_way_inconsistent")


def test_r049_third_party_unobserved_ambiguous():
    outcome, _ = evaluate("R049", fx(
        ("label", "L1", ("material_number", "QLAB-123456"))),
        metadata={"declared_material_number": "QLAB-123456"})
    check(outcome, "ambiguous", reason_code="three_way_unobserved")


def test_r050_keyed_correspondence_pass():
    outcome, _ = evaluate("R050", fx(
        ("certificate", "C1",
         ("component_model_map", {"C1": "M1", "C2": "M2"}))),
        metadata={"declared_component_map": {"C1": "M1", "C2": "M2"}})
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r050_keyed_correspondence_tolerates_key_whitespace():
    outcome, _ = evaluate("R050", fx(
        ("certificate", "C1",
         ("component_model_map", {" C1 ": "M1", "C2 ": "M2"}))),
        metadata={"declared_component_map": {"C1": "M1", "C2": "M2"}})
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r050_swapped_models_with_identical_sets_reject():
    outcome, _ = evaluate("R050", fx(
        ("certificate", "C1",
         ("component_model_map", {"C1": "M2", "C2": "M1"}))),
        metadata={"declared_component_map": {"C1": "M1", "C2": "M2"}})
    check(outcome, "reject", reason_code="component_pairs_mismatch")


def test_r050_table_unobserved_ambiguous():
    outcome, _ = evaluate("R050", fx(("label", "L1")),
                          metadata={"declared_component_map": {"C1": "M1"}})
    check(outcome, "ambiguous", reason_code="component_table_unobserved")


def test_r051_batch_in_interval_pass():
    outcome, _ = evaluate("R051", fx(
        ("label", "L1", ("batch_number", 100)),
        ("certificate", "C1", ("batch_interval", [50, 150]))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r051_batch_out_of_interval_rejects():
    outcome, _ = evaluate("R051", fx(
        ("label", "L1", ("batch_number", 200)),
        ("certificate", "C1", ("batch_interval", [50, 150]))))
    check(outcome, "reject", reason_code="batch_out_of_interval")


def test_r051_interval_unobserved_ambiguous():
    outcome, _ = evaluate("R051", fx(
        ("label", "L1", ("batch_number", 100))))
    check(outcome, "ambiguous", reason_code="interval_unobserved")


def test_r052_spec_matches_own_model_pass():
    outcome, _ = evaluate("R052", fx(
        ("label", "L1", ("package_spec", "A4 50页")),
        ("certificate", "C1", ("model_spec_map", {"M1": "A4 50页"}))),
        metadata={"declared_model": "M1"})
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r052_cannot_borrow_another_models_spec():
    outcome, _ = evaluate("R052", fx(
        ("label", "L1", ("package_spec", "A4 50页")),
        ("certificate", "C1", ("model_spec_map", {"M2": "A4 50页"}))),
        metadata={"declared_model": "M1"})
    check(outcome, "reject", reason_code="spec_not_listed_for_model")


def test_r052_spec_mismatch_rejects():
    outcome, _ = evaluate("R052", fx(
        ("label", "L1", ("package_spec", "A4 50页")),
        ("certificate", "C1", ("model_spec_map", {"M1": "A5"}))),
        metadata={"declared_model": "M1"})
    check(outcome, "reject", reason_code="spec_mismatch")


def test_r052_package_spec_unobserved_ambiguous():
    outcome, _ = evaluate("R052", fx(("label", "L1", ("model", "M1"))),
                          metadata={"declared_model": "M1"})
    check(outcome, "ambiguous", reason_code="package_spec_unobserved")


def test_r053_figure_anchor_match_pass():
    outcome, _ = evaluate("R053", fx(
        ("label", "L1", ("material_number", "QLAB-123456"), ("model", "LED-100")),
        ("certificate", "C1", ("figure_model", "LED-100"),
         ("figure_material_number", "QLAB-123456"))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r053_figure_anchor_mismatch_rejects():
    outcome, _ = evaluate("R053", fx(
        ("label", "L1", ("material_number", "QLAB-123456"), ("model", "LED-100")),
        ("certificate", "C1", ("figure_model", "LED-200"),
         ("figure_material_number", "QLAB-123456"))))
    check(outcome, "reject", reason_code="figure_anchor_mismatch")


def test_r053_anchor_unobserved_ambiguous():
    outcome, _ = evaluate("R053", fx(
        ("label", "L1", ("material_number", "QLAB-123456"))))
    check(outcome, "ambiguous", reason_code="anchor_unobserved")


def test_r054_conflict_is_ambiguous_with_valid_evidence():
    outcome, _ = evaluate("R054", fx(
        ("certificate", "C1", ("material_number", "QLAB-1"),
         ("void_marked", False), ("contact_info", "甲公司")),
        ("certificate", "C2", ("material_number", "QLAB-1"),
         ("void_marked", False), ("contact_info", "乙公司"))))
    check(outcome, "ambiguous", reason_code="same_number_contact_conflict")
    # No Pydantic crash: every result carries valid Evidence rows
    assert outcome.evidence, "conflict must cite evidence"
    for row in outcome.evidence:
        assert row.material_id and row.fact.strip()


def test_r054_one_voided_one_active_pass():
    outcome, _ = evaluate("R054", fx(
        ("certificate", "C1", ("material_number", "QLAB-1"),
         ("void_marked", True), ("contact_info", "甲公司")),
        ("certificate", "C2", ("material_number", "QLAB-1"),
         ("void_marked", False), ("contact_info", "乙公司"))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")
    assert outcome.results[0].reason_code == "single_active_certificate"


def test_r054_single_certificate_pass():
    outcome, _ = evaluate("R054", fx(
        ("certificate", "C1", ("material_number", "QLAB-1"),
         ("void_marked", False), ("contact_info", "甲公司"))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")
    assert outcome.results[0].reason_code == "single_active_certificate"


def test_r054_unreadable_other_number_ambiguous():
    outcome, _ = evaluate("R054", fx(
        ("certificate", "C1", ("material_number", "QLAB-1"),
         ("void_marked", False), ("contact_info", "甲公司")),
        ("certificate", "C2", ("material_number", None, "unreadable"))))
    check(outcome, "ambiguous", reason_code="certificate_number_unreadable")


def test_r054_no_certificates_ambiguous():
    outcome, _ = evaluate("R054", fx(("label", "L1")))
    check(outcome, "ambiguous", reason_code="certificates_unobserved")


# ---------------------------------------------------------------------------
# 11. F10 适用条件与规则边界 (R055-R060)
# ---------------------------------------------------------------------------


def test_r055_instruction_present_pass():
    outcome, _ = evaluate("R055", fx(
        ("label", "L1", ("verbatim_text", "充电前阅读说明"))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r055_instruction_missing_rejects():
    outcome, _ = evaluate("R055", fx(
        ("label", "L1", ("verbatim_text", "随意使用"))))
    check(outcome, "reject", reason_code="instruction_missing")


def test_r055_false_conjunct_short_circuits_missing_other():
    # category known-false even though power_mode is missing -> not_applicable
    outcome, _ = evaluate("R055", fx(("label", "L1")),
                          metadata={"category": "台灯", "power_mode": None})
    check(outcome, "pass", applicability="not_applicable",
          reason_code="rule_not_applicable")


def test_r055_condition_unknown_ambiguous():
    outcome, _ = evaluate("R055", fx(("label", "L1")),
                          metadata={"power_mode": None})
    check(outcome, "ambiguous", applicability="unknown",
          reason_code="applicability_unknown")


@pytest.mark.parametrize("qty", [0, "2", True, None])
def test_r056_invalid_quantity_ambiguous_unknown(qty):
    outcome, _ = evaluate("R056", fx(("label", "L1")),
                          metadata={"declared_quantity": qty})
    check(outcome, "ambiguous", applicability="unknown",
          reason_code="applicability_unknown")


def test_r056_single_unit_not_applicable():
    outcome, _ = evaluate("R056", fx(("label", "L1")),
                          metadata={"declared_quantity": 1})
    check(outcome, "pass", applicability="not_applicable",
          reason_code="rule_not_applicable")


def test_r056_multi_unit_with_contents_pass():
    outcome, _ = evaluate("R056", fx(
        ("label", "L1", ("contents_list", ["主机", "底座"]))),
        metadata={"declared_quantity": 2})
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r056_multi_unit_contents_missing_rejects():
    outcome, _ = evaluate("R056", fx(
        ("label", "L1", ("contents_list", None, "confirmed_missing", True))),
        metadata={"declared_quantity": 2})
    check(outcome, "reject", reason_code="contents_list_missing")


def test_r057_outer_package_alternative_pass():
    outcome, _ = evaluate("R057", fx(
        ("label", "L1", ("carrier", "outer_package"),
         ("material_number", "QLAB-123456"))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r057_fixed_tag_alternative_pass():
    outcome, _ = evaluate("R057", fx(
        ("label", "L1", ("carrier", "attached_tag"),
         ("material_number", "QLAB-123456"), ("attached_to_package", True))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r057_two_confirmed_incomplete_alternatives_reject():
    outcome, _ = evaluate("R057", fx(
        ("label", "L1", ("carrier", "outer_package"),
         ("material_number", None, "confirmed_missing", True)),
        ("label", "L2", ("carrier", "attached_tag"),
         ("material_number", "QLAB-123456"), ("attached_to_package", False))))
    check(outcome, "reject", reason_code="no_complete_alternative")


def test_r057_uncertain_element_ambiguous():
    outcome, _ = evaluate("R057", fx(
        ("label", "L1", ("carrier", "outer_package"),
         ("material_number", None, "unreadable"))))
    check(outcome, "ambiguous", reason_code="alternative_incomplete_unknown")


def test_r058_production_date_requires_old_prefix():
    outcome, _ = evaluate("R058", fx(
        ("label", "L1", ("production_date", "2025-12-31"), ("model", "OLD-X"))),
        review_date="2026-06-15")  # review date does NOT pick the branch
    check(outcome, "pass", reason_code="all_requirements_satisfied")


def test_r058_exact_case_prefix_rejects():
    outcome, _ = evaluate("R058", fx(
        ("label", "L1", ("production_date", "2025-12-31"), ("model", "old-x"))),
        review_date="2026-06-15")
    check(outcome, "reject", reason_code="prefix_mismatch")


def test_r058_boundary_date_requires_new_prefix():
    outcome, _ = evaluate("R058", fx(
        ("label", "L1", ("production_date", "2026-01-01"), ("model", "OLD-A"))))
    check(outcome, "reject", reason_code="prefix_mismatch")


def test_r058_production_unreadable_ambiguous():
    outcome, _ = evaluate("R058", fx(
        ("label", "L1", ("production_date", None, "unreadable"), ("model", "OLD-X"))))
    check(outcome, "ambiguous", reason_code="production_date_unreadable")


def test_r059_blue_with_circle_ambiguous_conflict():
    outcome, _ = evaluate("R059", fx(
        ("package", "P1", ("box_color", "blue")),
        ("object", "O1", ("shape_type", "circle"))))
    check(outcome, "ambiguous", reason_code="same_level_conflict")


def test_r059_blue_without_circle_still_conflict():
    outcome, _ = evaluate("R059", fx(
        ("package", "P1", ("box_color", "blue"),
         ("shape_type", None, "confirmed_missing", True))))
    check(outcome, "ambiguous", reason_code="same_level_conflict")


def test_r059_non_blue_with_circle_pass():
    outcome, _ = evaluate("R059", fx(
        ("package", "P1", ("box_color", "yellow")),
        ("object", "O1", ("shape_type", "circle"))))
    check(outcome, "pass", reason_code="all_requirements_satisfied")
    assert outcome.results[0].reason_code == "circle_shown_non_blue"


def test_r059_non_blue_confirmed_absent_rejects():
    outcome, _ = evaluate("R059", fx(
        ("package", "P1", ("box_color", "yellow"),
         ("shape_type", None, "confirmed_missing", True))))
    check(outcome, "reject", reason_code="circle_missing")


def test_r059_circle_presence_unknown_ambiguous():
    outcome, _ = evaluate("R059", fx(("package", "P1", ("box_color", "yellow"))))
    check(outcome, "ambiguous", reason_code="circle_presence_unknown")


def test_r060_outer_package_unaffected_pass():
    outcome, _ = evaluate("R060", fx(
        ("label", "L1", ("model", "LED-100"), ("carrier", "outer_package"))),
        metadata={"package_longest_edge_mm": 50})
    check(outcome, "pass", reason_code="all_requirements_satisfied")
    assert outcome.results[0].reason_code == "model_on_outer_package"


def test_r060_attached_tag_undefined_small_threshold_ambiguous():
    outcome, _ = evaluate("R060", fx(
        ("label", "L1", ("model", "LED-100"), ("carrier", "attached_tag"))),
        metadata={"package_longest_edge_mm": 50})
    check(outcome, "ambiguous", applicability="applicable",
          reason_code="undefined_small_threshold")


def test_r060_invalid_carrier_rejects():
    outcome, _ = evaluate("R060", fx(
        ("label", "L1", ("model", "LED-100"), ("carrier", "instruction_paper"))))
    check(outcome, "reject", reason_code="model_carrier_invalid")


def test_r060_carrier_unknown_ambiguous():
    outcome, _ = evaluate("R060", fx(
        ("label", "L1", ("model", "LED-100"), ("carrier", None, "unreadable"))))
    check(outcome, "ambiguous", reason_code="carrier_unknown")


# ---------------------------------------------------------------------------
# 12. Generalized AuditController end-to-end (StaticObservationGateway)
# ---------------------------------------------------------------------------


def _one_material(material_id="m1", role="package_photos"):
    return [m(material_id, role)]


def test_controller_r001_gateway_round_produces_protocol_valid_row():
    gateway = StaticObservationGateway([ObservationBatch(
        entities=[ent("P1", "package")],
        observations=[obs("P1", "boundary_visible", True)],
    )])
    task = make_task(rule_id="R001", audit_unit_id="u-c-r001",
                     metadata={"has_outer_package": True},
                     materials=_one_material())
    row = AuditController(plan=None, gateway=gateway).run(task)
    assert row["execution_status"] == "completed"
    assert row["label"] == "pass"
    assert row["applicability"] == "applicable"
    assert prediction_semantic_errors(row, task) == []
    assert len(gateway.calls) == 1  # NeedEvidence actually drove the inspection


def test_controller_r037_unit_conversion_round():
    gateway = StaticObservationGateway([ObservationBatch(
        entities=[ent("L1", "label")],
        observations=[obs("L1", "capacity", {"value": "0.5", "unit": "L"})],
    )])
    task = make_task(rule_id="R037", audit_unit_id="u-c-r037",
                     metadata={"declared_capacity_ml": 500},
                     materials=_one_material())
    row = AuditController(plan=None, gateway=gateway).run(task)
    assert row["label"] == "pass"
    assert prediction_semantic_errors(row, task) == []


def test_controller_r050_swapped_map_rejects_end_to_end():
    gateway = StaticObservationGateway([ObservationBatch(
        entities=[ent("C1", "certificate")],
        observations=[obs("C1", "component_model_map", {"C1": "M2", "C2": "M1"},
                          source=("m3", 1))],
    )])
    task = make_task(rule_id="R050", audit_unit_id="u-c-r050",
                     metadata={"declared_component_map": {"C1": "M1", "C2": "M2"}},
                     materials=_one_material("m3", "certificate_pages"))
    row = AuditController(plan=None, gateway=gateway).run(task)
    assert row["label"] == "reject"
    assert row["reason_code"] == "component_pairs_mismatch"
    assert prediction_semantic_errors(row, task) == []


def test_controller_r059_conflict_ambiguous_end_to_end():
    gateway = StaticObservationGateway([ObservationBatch(
        entities=[ent("P1", "package"), ent("O1", "object")],
        observations=[obs("P1", "box_color", "blue"), obs("O1", "shape_type", "circle")],
    )])
    task = make_task(rule_id="R059", audit_unit_id="u-c-r059",
                     materials=_one_material())
    row = AuditController(plan=None, gateway=gateway).run(task)
    assert row["label"] == "ambiguous"
    assert row["reason_code"] == "same_level_conflict"
    assert prediction_semantic_errors(row, task) == []


def test_controller_r060_unaffected_and_undefined_branches():
    # branch 1: model on outer package -> pass
    gateway1 = StaticObservationGateway([ObservationBatch(
        entities=[ent("L1", "label")],
        observations=[obs("L1", "model", "LED-100"), obs("L1", "carrier", "outer_package")],
    )])
    task1 = make_task(rule_id="R060", audit_unit_id="u-c-r060a",
                      materials=_one_material())
    row1 = AuditController(plan=None, gateway=gateway1).run(task1)
    assert row1["label"] == "pass"
    assert row1["reason_code"] == "all_requirements_satisfied"
    assert prediction_semantic_errors(row1, task1) == []
    # branch 2: attached tag -> undefined "较小" threshold -> ambiguous
    gateway2 = StaticObservationGateway([ObservationBatch(
        entities=[ent("L2", "label")],
        observations=[obs("L2", "model", "LED-100"), obs("L2", "carrier", "attached_tag")],
    )])
    task2 = make_task(rule_id="R060", audit_unit_id="u-c-r060b",
                      metadata={"package_longest_edge_mm": 50},
                      materials=_one_material())
    row2 = AuditController(plan=None, gateway=gateway2).run(task2)
    assert row2["label"] == "ambiguous"
    assert row2["reason_code"] == "undefined_small_threshold"
    assert prediction_semantic_errors(row2, task2) == []


def test_controller_not_applicable_short_circuits_before_gateway():
    gateway = StaticObservationGateway([])
    task = make_task(rule_id="R055", audit_unit_id="u-c-na",
                     metadata={"category": "台灯", "power_mode": None},
                     materials=_one_material())
    row = AuditController(plan=None, gateway=gateway).run(task)
    assert row["applicability"] == "not_applicable"
    assert row["label"] == "pass"
    assert gateway.calls == []  # never reached the gateway
    assert prediction_semantic_errors(row, task) == []


def test_controller_unknown_applicability_short_circuits_before_gateway():
    gateway = StaticObservationGateway([])
    task = make_task(rule_id="R056", audit_unit_id="u-c-ua",
                     metadata={"declared_quantity": None},
                     materials=_one_material())
    row = AuditController(plan=None, gateway=gateway).run(task)
    assert row["applicability"] == "unknown"
    assert row["label"] == "ambiguous"
    assert gateway.calls == []
    assert prediction_semantic_errors(row, task) == []


def test_controller_multi_task_reuse_resets_inspected():
    batch1 = ObservationBatch(
        entities=[ent("P1", "package")],
        observations=[obs("P1", "boundary_visible", True)])
    batch2 = ObservationBatch(
        entities=[ent("O9", "object")],
        observations=[obs("O9", "shape_type", "circle"), obs("O9", "is_filled", False),
                      obs("O9", "is_closed", True)])
    gateway = StaticObservationGateway([batch1, batch2])
    controller = AuditController(plan=None, gateway=gateway)
    task1 = make_task(rule_id="R001", audit_unit_id="u-iso-a",
                      metadata={"has_outer_package": True},
                      materials=_one_material("m1", "package_photos"))
    row1 = controller.run(task1)
    assert row1["label"] == "pass"
    assert ("m1", 1) in controller._inspected
    task2 = make_task(rule_id="R025", audit_unit_id="u-iso-b",
                      materials=_one_material("m9", "package_photos"))
    row2 = controller.run(task2)
    assert row2["label"] == "pass"
    # inspected set was reset between tasks: no leak from unit A into unit B
    assert ("m1", 1) not in controller._inspected
    assert ("m9", 1) in controller._inspected
    assert prediction_semantic_errors(row1, task1) == []
    assert prediction_semantic_errors(row2, task2) == []


def test_controller_with_explicit_advanced_plan():
    plan = REG.get_plan("R025", "3.0")
    gateway = StaticObservationGateway([ObservationBatch(
        entities=[ent("O1", "object")],
        observations=[obs("O1", "shape_type", "circle"), obs("O1", "is_filled", False),
                      obs("O1", "is_closed", True)])])
    task = make_task(rule_id="R025", audit_unit_id="u-c-plan",
                     materials=_one_material())
    row = AuditController(plan=plan, gateway=gateway).run(task)
    assert row["label"] == "pass"
    assert prediction_semantic_errors(row, task) == []


def test_controller_rejects_mismatched_explicit_plan():
    plan = REG.get_plan("R001", "3.0")
    controller = AuditController(plan=plan, gateway=StaticObservationGateway([]))
    task = make_task(rule_id="R002", audit_unit_id="u-c-mismatch",
                     materials=_one_material())
    with pytest.raises(UnsupportedRuleError):
        controller.run(task)


def test_controller_unknown_rule_raises():
    task = make_task(rule_id="R999", audit_unit_id="u-c-unknown",
                     materials=_one_material())
    with pytest.raises(UnsupportedRuleError):
        AuditController(plan=None, gateway=StaticObservationGateway([])).run(task)
