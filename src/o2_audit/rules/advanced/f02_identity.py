"""F02 商品身份与型号 (R007-R012).

R007 包装型号与申报匹配, R008 套装型号覆盖, R009 序列号唯一性,
R010 主型号与兼容型号分离, R011 新旧型号映射核验, R012 型号相似字符区分.
型号比较一律走总则归一化 (O/0、I/1 永不等价).
"""
from __future__ import annotations

from typing import Any

from o2_audit.domain.models import (
    AuditInput,
    EntityKind,
    ObservationStatus,
    RequirementStatus,
)
from o2_audit.operators.sets import list_contains_model, one_to_one_mapping, set_equal_ignoring_order, unique_items
from o2_audit.operators.text import model_equal, model_normalize
from o2_audit.runtime.facts import FactView
from o2_audit.rules.advanced.base import (
    decide,
    entities_of_kind_with,
    ev,
    field_check,
    metadata_value,
    not_applicable_outcome,
    positive_int_or_none,
    read,
    req,
)
from o2_audit.rules.base import RuleOutcome, RulePlan


class F02Plan(RulePlan):
    """F02 plans: no extra parameters; model semantics come from the 总则."""


APPLICABILITY: dict[str, Any] = {}

# R009's applicability depends on declared_quantity (metadata-level).
def _applicability_r009(task: AuditInput, plan: F02Plan) -> tuple[str, list[str]]:
    qty = metadata_value(task, "declared_quantity")
    n = positive_int_or_none(qty)
    if n is None:
        return "unknown", ["declared_quantity"]
    if n == 1:
        return "not_applicable", []
    return "applicable", []


def _applicability_r012(task: AuditInput, plan: F02Plan) -> tuple[str, list[str]]:
    """R012 only triggers when the declared model actually contains a digit
    0 or 1; a missing declared_model stays applicable so the evaluator can
    return its consequential unknown."""
    declared = metadata_value(task, "declared_model")
    if declared is not None and not ("0" in str(declared) or "1" in str(declared)):
        return "not_applicable", []
    return "applicable", []


APPLICABILITY["R007"] = lambda task, plan: ("applicable", [])
APPLICABILITY["R008"] = lambda task, plan: ("applicable", [])
APPLICABILITY["R009"] = _applicability_r009
APPLICABILITY["R010"] = lambda task, plan: ("applicable", [])
APPLICABILITY["R011"] = lambda task, plan: ("applicable", [])
APPLICABILITY["R012"] = _applicability_r012


# ---------------------------------------------------------------------------
# R007 包装型号与申报匹配
# ---------------------------------------------------------------------------


def evaluate_r007(task: AuditInput, plan: F02Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    declared = metadata_value(task, "declared_model")
    if declared is None:
        return decide([req(
            "model_match", RequirementStatus.UNKNOWN, "declared_model_missing",
            "申报型号缺失，无法进行型号一致性比较", [])], plan=plan)
    declared_s = str(declared)

    def check(value: Any) -> tuple[bool, str]:
        if model_equal(str(value), declared_s):
            return True, ""
        return False, (f"包装型号字段 {value!r} 与申报型号 {declared_s!r} "
                       f"经型号归一化后不一致")

    result = field_check(facts, EntityKind.LABEL, "model", "model_match",
                         check=check, quote=quote,
                         missing_reason="完整型号区域内明确缺失型号字段",
                         unreadable_reason="包装型号字段读不清，判为 ambiguous")
    return decide([result], plan=plan)


# ---------------------------------------------------------------------------
# R008 套装型号覆盖
# ---------------------------------------------------------------------------


def evaluate_r008(task: AuditInput, plan: F02Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    declared = metadata_value(task, "declared_component_models")
    results: list = []
    if not isinstance(declared, list) or not declared:
        results.append(req(
            "component_set", RequirementStatus.UNKNOWN, "declared_components_missing",
            "任务未提供 declared_component_models 组件型号清单，无法核验", []))
        return decide(results, plan=plan)

    found = entities_of_kind_with(facts, EntityKind.PACKAGE, "component_models")
    if not found:
        results.append(req(
            "component_set", RequirementStatus.UNKNOWN, "component_list_unobserved",
            "未观察到套装外包装的组件型号清单，证据不足", []))
        return decide(results, plan=plan)
    for _e, status, _v, obs in found:
        if status != ObservationStatus.READABLE:
            # 列表不完整可见时判为 ambiguous (explicit in rule text).
            results.append(req(
                "component_set", RequirementStatus.UNKNOWN, "component_list_incomplete",
                "组件型号清单不完整可见，按证据不足处理", ev(obs, "清单不完整", quote)))
            return decide(results, plan=plan)
    printed: list[str] = []
    for _e, _s, value, obs in found:
        if isinstance(value, list):
            printed.extend(str(m) for m in value)
    if set_equal_ignoring_order(printed, [str(m) for m in declared]):
        # 重复出现不增加覆盖: set equality deliberately ignores duplicates.
        results.append(req(
            "component_set", RequirementStatus.SATISFIED, "component_set_matches",
            f"组件型号集合与申报一致（重复项不增加覆盖）: {sorted(set(printed))}", []))
        return decide(results, plan=plan)
    missing = [m for m in declared if not list_contains_model(printed, str(m))]
    extra = [m for m in printed if not list_contains_model([str(d) for d in declared], m)]
    results.append(req(
        "component_set", RequirementStatus.VIOLATED, "component_set_mismatch",
        f"组件型号集合不一致（缺少 {missing}，多出 {extra}），缺少或多出任何型号均不符合",
        []))
    return decide(results, plan=plan)


# ---------------------------------------------------------------------------
# R009 序列号唯一性
# ---------------------------------------------------------------------------


def evaluate_r009(task: AuditInput, plan: F02Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    qty = positive_int_or_none(metadata_value(task, "declared_quantity"))
    results: list = []
    items = entities_of_kind_with(facts, EntityKind.OBJECT, "serial_number")
    if not items:
        results.append(req(
            "serial_uniqueness", RequirementStatus.UNKNOWN, "serials_unobserved",
            "未观察到任何商品本体的序列号，证据不足", []))
        return decide(results, plan=plan)

    serials: list[str] = []
    for e, status, value, obs in items:
        if status == ObservationStatus.CONFIRMED_MISSING:
            results.append(req(
                f"serial:{e.entity_id}", RequirementStatus.VIOLATED, "serial_confirmed_missing",
                "完整清晰范围内确认某件商品缺失序列号（缺号判为 reject）",
                ev(obs, "确认序列号缺失", quote)))
            return decide(results, plan=plan)
        if status != ObservationStatus.READABLE:
            # 因图像质量无法确认时判为 ambiguous (explicit in rule text).
            results.append(req(
                f"serial:{e.entity_id}", RequirementStatus.UNKNOWN, "serial_unreadable",
                "因图像质量无法确认序列号，按证据不足处理", ev(obs, "序列号不可读", quote)))
            return decide(results, plan=plan)
        serials.append(str(value))

    if not unique_items(serials):
        dupes = sorted({s for s in serials if serials.count(s) > 1})
        results.append(req(
            "serial_uniqueness", RequirementStatus.VIOLATED, "serial_duplicates",
            f"独立销售的多件商品出现重复序列号 {dupes}（重复判为 reject）", []))
        return decide(results, plan=plan)
    if qty is not None and len(serials) < qty:
        results.append(req(
            "serial_uniqueness", RequirementStatus.VIOLATED, "serial_missing_unit",
            f"申报 {qty} 件独立销售商品，仅观察到 {len(serials)} 个可读序列号（缺号判为 reject）",
            []))
        return decide(results, plan=plan)
    results.append(req(
        "serial_uniqueness", RequirementStatus.SATISFIED, "serials_unique",
        f"{len(serials)} 件商品的序列号均可读且互不相同", []))
    return decide(results, plan=plan)


# ---------------------------------------------------------------------------
# R010 主型号与兼容型号分离
# ---------------------------------------------------------------------------


def evaluate_r010(task: AuditInput, plan: F02Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    declared = metadata_value(task, "declared_model")
    if declared is None:
        return decide([req(
            "main_model", RequirementStatus.UNKNOWN, "declared_model_missing",
            "申报型号缺失，无法核对主型号字段", [])], plan=plan)
    declared_s = str(declared)

    main_entities = entities_of_kind_with(facts, EntityKind.LABEL, "main_model")
    compat_entities = entities_of_kind_with(facts, EntityKind.LABEL, "compatible_models")
    results: list = []

    if not main_entities and not compat_entities:
        results.append(req(
            "main_model", RequirementStatus.UNKNOWN, "model_roles_unobserved",
            "未观察到主型号或兼容型号字段，证据不足", []))
        return decide(results, plan=plan)

    # Declared model only in the compatibility list -> 不满足要求 (explicit).
    compat_values = [v for _e, s, v, _o in compat_entities if s == ObservationStatus.READABLE]
    printed_compat: list[str] = []
    for v in compat_values:
        if isinstance(v, list):
            printed_compat.extend(str(m) for m in v)
    if not main_entities or all(s == ObservationStatus.CONFIRMED_MISSING
                                for _e, s, _v, _o in main_entities):
        if list_contains_model(printed_compat, declared_s):
            results.append(req(
                "main_model", RequirementStatus.VIOLATED, "declared_model_only_compatible",
                f"申报型号 {declared_s!r} 只出现在兼容型号列表中，不满足主型号匹配要求", []))
            return decide(results, plan=plan)

    for e, status, value, obs in main_entities:
        if status == ObservationStatus.CONFIRMED_MISSING:
            results.append(req(
                f"main_model:{e.entity_id}", RequirementStatus.VIOLATED,
                "main_model_field_missing",
                "完整清晰范围内确认主型号字段缺失", ev(obs, "主型号字段缺失", quote)))
            return decide(results, plan=plan)
        if status != ObservationStatus.READABLE:
            results.append(req(
                f"main_model:{e.entity_id}", RequirementStatus.UNKNOWN,
                "main_model_unreadable",
                "主型号字段读不清，按证据不足处理", ev(obs, "主型号不可读", quote)))
            return decide(results, plan=plan)
        if not model_equal(str(value), declared_s):
            results.append(req(
                f"main_model:{e.entity_id}", RequirementStatus.VIOLATED,
                "main_model_mismatch",
                f"主型号字段 {value!r} 与申报型号 {declared_s!r} 不匹配；"
                "只出现在兼容型号列表中不满足要求",
                ev(obs, f"主型号为 {value}", quote)))
            return decide(results, plan=plan)
    results.append(req(
        "main_model", RequirementStatus.SATISFIED, "main_model_matches",
        f"主型号字段与申报型号 {declared_s!r} 经归一化后一致", []))
    return decide(results, plan=plan)


# ---------------------------------------------------------------------------
# R011 新旧型号映射核验
# ---------------------------------------------------------------------------


def evaluate_r011(task: AuditInput, plan: F02Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    declared = metadata_value(task, "declared_model")
    if declared is None:
        return decide([req(
            "old_new_map", RequirementStatus.UNKNOWN, "declared_model_missing",
            "申报型号缺失，无法核验替代关系", [])], plan=plan)
    declared_s = str(declared)

    old_entities = entities_of_kind_with(facts, EntityKind.PACKAGE, "old_model")
    if not old_entities:
        if facts.all_entities():
            # 包装未使用 old_model -> 本条条件不成立.
            return not_applicable_outcome("包装未使用 old_model，本条不适用")
        return decide([req(
            "old_new_map", RequirementStatus.UNKNOWN, "old_model_unobserved",
            "未观察到包装 old_model 信息，证据不足", [])], plan=plan)

    old_status, old_value, old_obs = old_entities[0][1], old_entities[0][2], old_entities[0][3]
    if old_status != ObservationStatus.READABLE:
        return decide([req(
            "old_new_map", RequirementStatus.UNKNOWN, "old_model_unreadable",
            "包装 old_model 读不清，无法核验替代关系", ev(old_obs, "old_model 不可读", quote))],
            plan=plan)
    old_s = str(old_value)

    certs = entities_of_kind_with(facts, EntityKind.CERTIFICATE, "replacement_map")
    if not certs:
        return decide([req(
            "old_new_map", RequirementStatus.VIOLATED, "replacement_map_missing",
            f"包装使用 old_model {old_s!r}，但证明文件未明确写出替代关系；"
            "只同时列出两个型号而无替代关系不满足要求", [])], plan=plan)
    for c, status, value, obs in certs:
        if status != ObservationStatus.READABLE or not isinstance(value, dict):
            return decide([req(
                "old_new_map", RequirementStatus.UNKNOWN, "replacement_map_unreadable",
                "证明文件的替代关系无法确认，按证据不足处理",
                ev(obs, "替代关系不可读", quote))], plan=plan)
        # Keyed correspondence: old_model -> declared_model, one-to-one.
        expected = {old_s: declared_s}
        matches_old = any(model_equal(str(k), old_s) for k in value.keys())
        target = next((str(v) for k, v in value.items() if model_equal(str(k), old_s)), None)
        if not matches_old or target is None:
            continue
        if model_equal(target, declared_s) and one_to_one_mapping(value):
            return decide([req(
                "old_new_map", RequirementStatus.SATISFIED, "replacement_mapping_ok",
                f"证明文件明确给出 {old_s!r} -> {declared_s!r} 的一对一替代关系",
                ev(obs, f"替代关系 {value}", quote))], plan=plan)
        return decide([req(
            "old_new_map", RequirementStatus.VIOLATED, "replacement_mapping_invalid",
            f"证明文件的替代关系 {value!r} 与所需 {old_s!r} -> {declared_s!r} "
            "一对一替代关系不一致", ev(obs, f"替代关系 {value}", quote))], plan=plan)
    return decide([req(
        "old_new_map", RequirementStatus.VIOLATED, "replacement_mapping_missing_old",
        f"证明文件替代关系中未包含 old_model {old_s!r} 的条目", [])], plan=plan)


# ---------------------------------------------------------------------------
# R012 型号相似字符区分
# ---------------------------------------------------------------------------


# Simultaneous similar-character canonicalization: O/I (both cases, since
# model_normalize lowercases ASCII letters) are folded onto 0/1 in ONE pass.
_SIMILAR_TRANSLATION = str.maketrans("OIoi", "0101")


def evaluate_r012(task: AuditInput, plan: F02Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    declared = metadata_value(task, "declared_model")
    if declared is None:
        return decide([req(
            "char_identity", RequirementStatus.UNKNOWN, "declared_model_missing",
            "申报型号缺失，无法进行字符身份匹配", [])], plan=plan)
    declared_s = str(declared)
    if not ("0" in declared_s or "1" in declared_s):
        # 规则仅在型号含数字 0 或 1 时触发.
        return not_applicable_outcome("申报型号不含数字 0 或 1，本条不适用")

    found = entities_of_kind_with(facts, EntityKind.LABEL, "model")
    if not found:
        return decide([req(
            "char_identity", RequirementStatus.UNKNOWN, "model_unobserved",
            "未观察到包装型号，证据不足", [])], plan=plan)
    for e, status, value, obs in found:
        if status == ObservationStatus.UNREADABLE:
            # 图像无法区分相似字符时判为 ambiguous (explicit in rule text).
            return decide([req(
                f"char_identity:{e.entity_id}", RequirementStatus.UNKNOWN,
                "similar_chars_indistinguishable",
                "图像无法区分 O/0、I/1 等相似字符，按证据不足处理",
                ev(obs, "相似字符不可区分", quote))], plan=plan)
        if status != ObservationStatus.READABLE:
            return decide([req(
                f"char_identity:{e.entity_id}", RequirementStatus.UNKNOWN,
                "model_unreadable",
                "包装型号不可读，按证据不足处理", ev(obs, "型号不可读", quote))],
                plan=plan)
        printed = str(value)
        if model_equal(printed, declared_s):
            continue  # exact character-identity match (O/0, I/1 stay distinct)
        printed_norm = model_normalize(printed)
        declared_norm = model_normalize(declared_s)
        # Simultaneous canonicalization: O/I fold onto 0/1 in one pass. A
        # printed model whose canon matches the declared canon while its
        # normalized form differs proves an O/0 or I/1 substitution.
        if printed_norm != declared_norm and \
                printed_norm.translate(_SIMILAR_TRANSLATION) == _substitute_similar(declared_s):
            return decide([req(
                f"char_identity:{e.entity_id}", RequirementStatus.VIOLATED,
                "similar_char_substitution",
                f"包装型号 {printed!r} 用字母 O/I 替代了申报型号中的数字 0/1，"
                "不接受相似字符替代", ev(obs, f"包装型号为 {printed}", quote))],
                plan=plan)
        return decide([req(
            f"char_identity:{e.entity_id}", RequirementStatus.VIOLATED,
            "model_mismatch",
            f"包装型号 {printed!r} 与申报型号 {declared_s!r} 不一致",
            ev(obs, f"包装型号为 {printed}", quote))], plan=plan)
    return decide([req(
        "char_identity", RequirementStatus.SATISFIED, "char_identity_preserved",
        f"包装型号与申报型号 {declared_s!r} 按字符身份匹配，O/0、I/1 未被替代", [])],
        plan=plan)


def _substitute_similar(model: str) -> str:
    """Declared model with O/I simultaneously canonicalized onto 0/1: a printed
    model equal to this (under model normalization) while NOT equal to the
    declared model itself proves an O/0 or I/1 substitution."""
    return model_normalize(model).translate(_SIMILAR_TRANSLATION)


PLAN_CLASS = F02Plan

EVALUATORS: dict[str, Any] = {
    "R007": evaluate_r007,
    "R008": evaluate_r008,
    "R009": evaluate_r009,
    "R010": evaluate_r010,
    "R011": evaluate_r011,
    "R012": evaluate_r012,
}

__all__ = ["F02Plan", "PLAN_CLASS", "APPLICABILITY", "EVALUATORS"]
