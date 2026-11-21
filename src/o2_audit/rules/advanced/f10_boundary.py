"""F10 适用条件与规则边界 (R055-R060).

R055 充电品类条件触发, R056 多件装数量条件触发, R057 两种方案择一完整满足,
R058 生产日期决定版本, R059 同级冲突条款处理, R060 未定义阈值处理.

边界语义（总则 §1/§5 + 口径补充说明）:
- R055/R056 的适用条件未知/非法时 applicability=unknown (label=ambiguous)。
- R057 任一方案完整满足即可；两个不完整方案不得拼接。
- R058 版本由生产日期选择，与审核日期无关；前缀大小写精确比较。
- R059 蓝盒+空心圆触发同级冲突 -> ambiguous；非蓝盒或确认无圆等分支保持有效。
- R060 “较小”未定义：分支依赖该阈值时判为 ambiguous，绝不借用 B001 的
  100/80 mm；把型号放在外包装的合规分支不受影响。
"""
from __future__ import annotations

from typing import Any

from o2_audit.domain.models import (
    AuditInput,
    EntityKind,
    ObservationStatus,
    RequirementStatus,
)
from o2_audit.operators.dates import DateParseError, parse_date
from o2_audit.operators.text import (
    annotation_nfkc_strip,
    contains_verbatim_linebreaks,
    exact_case_match,
)
from o2_audit.runtime.facts import FactView
from o2_audit.rules.advanced.base import (
    decide,
    entities_of_kind_with,
    ev,
    metadata_value,
    not_applicable_outcome,
    positive_int_or_none,
    read,
    req,
)
from o2_audit.rules.base import RuleOutcome, RulePlan


class F10Plan(RulePlan):
    """F10 typed plans: R058's production-date boundary and prefixes are rule
    parameters (verbatim from the rule text)."""

    #: R058: production date strictly before this -> OLD-, on/after -> NEW-.
    prefix_boundary_date: str = "2026-01-01"
    prefix_before: str = "OLD-"
    prefix_on_or_after: str = "NEW-"


APPLICABILITY: dict[str, Any] = {}


def _applicability_r055(task: AuditInput, plan: F10Plan) -> tuple[str, list[str]]:
    category = metadata_value(task, "category")
    power = metadata_value(task, "power_mode")
    missing = [k for k, v in (("category", category), ("power_mode", power)) if v is None]
    if category is not None and annotation_nfkc_strip(str(category)) != "桌面灯":
        return "not_applicable", []
    if power is not None and annotation_nfkc_strip(str(power)) != "可充电":
        return "not_applicable", []
    if missing:
        return "unknown", missing
    return "applicable", []


def _applicability_r056(task: AuditInput, plan: F10Plan) -> tuple[str, list[str]]:
    """declared_quantity > 1 -> applicable; == 1 -> not_applicable; 缺失或不是
    正整数 -> ambiguous (unknown applicability) — per the exact rule text."""
    qty = positive_int_or_none(metadata_value(task, "declared_quantity"))
    if qty is None:
        return "unknown", ["declared_quantity"]
    if qty == 1:
        return "not_applicable", []
    return "applicable", []


APPLICABILITY["R055"] = _applicability_r055
APPLICABILITY["R056"] = _applicability_r056
for _rid in ("R057", "R058", "R059", "R060"):
    APPLICABILITY[_rid] = lambda task, plan: ("applicable", [])


# ---------------------------------------------------------------------------
# R055 充电品类条件触发
# ---------------------------------------------------------------------------


def evaluate_r055(task: AuditInput, plan: F10Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    expected = "充电前阅读说明"
    found = entities_of_kind_with(facts, EntityKind.LABEL, "verbatim_text")
    if not found:
        return decide([req(
            "read_instruction", RequirementStatus.UNKNOWN, "instruction_unobserved",
            "未观察到包装文字内容，证据不足", [])], plan=plan)
    _e, status, value, obs = found[0]
    if status == ObservationStatus.CONFIRMED_MISSING:
        return decide([req(
            "read_instruction", RequirementStatus.VIOLATED, "instruction_missing",
            f"完整清晰范围内确认包装缺少“{expected}”", ev(obs, "说明缺失", quote))],
            plan=plan)
    if status != ObservationStatus.READABLE or not isinstance(value, str):
        return decide([req(
            "read_instruction", RequirementStatus.UNKNOWN, "instruction_unreadable",
            "包装文字读不清，按证据不足处理", ev(obs, "文字不可读", quote))], plan=plan)
    if contains_verbatim_linebreaks(value, expected):
        return decide([req(
            "read_instruction", RequirementStatus.SATISFIED, "instruction_present",
            f"包装包含“{expected}”", ev(obs, f"文字 {value}", quote))], plan=plan)
    return decide([req(
        "read_instruction", RequirementStatus.VIOLATED, "instruction_missing",
        f"包装文字 {value!r} 中未出现“{expected}”", ev(obs, f"文字 {value}", quote))],
        plan=plan)


# ---------------------------------------------------------------------------
# R056 多件装数量条件触发
# ---------------------------------------------------------------------------


def evaluate_r056(task: AuditInput, plan: F10Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    found = entities_of_kind_with(facts, EntityKind.LABEL, "contents_list")
    if not found:
        return decide([req(
            "contents_list", RequirementStatus.UNKNOWN, "contents_unobserved",
            "未观察到“内含物”清单，证据不足", [])], plan=plan)
    _e, status, value, obs = found[0]
    if status == ObservationStatus.CONFIRMED_MISSING:
        return decide([req(
            "contents_list", RequirementStatus.VIOLATED, "contents_list_missing",
            "declared_quantity > 1 时包装须有“内含物”清单，完整清晰范围内确认缺失",
            ev(obs, "清单缺失", quote))], plan=plan)
    if status != ObservationStatus.READABLE:
        return decide([req(
            "contents_list", RequirementStatus.UNKNOWN, "contents_unreadable",
            "“内含物”清单读不清，按证据不足处理", ev(obs, "清单不可读", quote))],
            plan=plan)
    return decide([req(
        "contents_list", RequirementStatus.SATISFIED, "contents_list_present",
        f"多件装包装给出“内含物”清单（{value!r}）", ev(obs, "清单", quote))],
        plan=plan)


# ---------------------------------------------------------------------------
# R057 两种方案择一完整满足
# ---------------------------------------------------------------------------


def evaluate_r057(task: AuditInput, plan: F10Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    labels = facts.entities_of_kind(EntityKind.LABEL)
    number_labels = [e for e in labels if facts.claims(e.entity_id, "material_number")]
    if not labels and not number_labels:
        return decide([req(
            "number_alternative", RequirementStatus.UNKNOWN, "number_unobserved",
            "未观察到材料编号相关信息，证据不足", [])], plan=plan)

    _confirmed_bad = (ObservationStatus.CONFIRMED_MISSING,)
    _uncertain = (ObservationStatus.UNREADABLE, ObservationStatus.INCOMPLETE_SCOPE,
                  ObservationStatus.IDENTITY_UNKNOWN)

    # 方案一: 包装上可见（材料编号在非附签载体上可读）。
    alt1_complete = False
    alt1_confirmed_bad = False  # 明确不完整（编号确认缺失）
    alt1_unknown = False        # 要素不确定
    for e in number_labels:
        cs, cv, _o = read(facts, e.entity_id, "carrier")
        ns, nv, nobs = read(facts, e.entity_id, "material_number")
        if ns == ObservationStatus.READABLE and cs == ObservationStatus.READABLE \
                and cv == "outer_package":
            alt1_complete = True
        elif ns in _confirmed_bad:
            # 编号确认缺失：包装方案被明确证伪（无论载体归属）。
            alt1_confirmed_bad = True
        elif cs == ObservationStatus.READABLE and cv == "outer_package":
            alt1_unknown = True  # 编号在包装上但不可读
        elif cs in _uncertain:
            alt1_unknown = True  # 载体不确定

    # 方案二: 固定附签上可见且连接关系明确（两要素须同属一个方案，不得拼接）。
    alt2_complete = False
    alt2_confirmed_bad = False
    alt2_unknown = False
    for e in number_labels:
        cs, cv, _o = read(facts, e.entity_id, "carrier")
        if cs != ObservationStatus.READABLE or cv != "attached_tag":
            continue
        ns, nv, nobs = read(facts, e.entity_id, "material_number")
        as_, av, aobs = read(facts, e.entity_id, "attached_to_package")
        number_ok = ns == ObservationStatus.READABLE
        attached_ok = as_ == ObservationStatus.READABLE and av is True
        if number_ok and attached_ok:
            alt2_complete = True
        elif ns in _confirmed_bad or as_ in _confirmed_bad \
                or (as_ == ObservationStatus.READABLE and av is False):
            # 一个要素被明确证伪：方案本身确认不完整。
            alt2_confirmed_bad = True
        else:
            alt2_unknown = True  # 一个要素完整、另一个不确定

    results: list = []
    if alt1_complete or alt2_complete:
        which = "包装上可见" if alt1_complete else "固定附签上可见且连接关系明确"
        results.append(req(
            "number_alternative", RequirementStatus.SATISFIED, "one_complete_alternative",
            f"材料编号通过方案“{which}”完整满足（任一方案完整即可）", []))
        return decide(results, plan=plan)
    # 两个不完整方案不得拼接: 任一要素不确定 -> ambiguous; 两个方案均被明确证伪 -> reject.
    if alt1_unknown or alt2_unknown:
        results.append(req(
            "number_alternative", RequirementStatus.UNKNOWN, "alternative_incomplete_unknown",
            "两个方案各自存在无法确认的要素，不能拼接成满足；按证据不足处理", []))
        return decide(results, plan=plan)
    if alt1_confirmed_bad and alt2_confirmed_bad:
        results.append(req(
            "number_alternative", RequirementStatus.VIOLATED, "no_complete_alternative",
            "完整清晰范围内确认两个方案均不完整（包装上编号缺失、附签编号缺失或连接关系缺失），"
            "且不得拼接两个不完整方案", []))
        return decide(results, plan=plan)
    if number_labels:
        results.append(req(
            "number_alternative", RequirementStatus.VIOLATED, "no_complete_alternative",
            "完整清晰范围内确认两个方案均不完整（包装上不可见、附签不可见或连接关系缺失），"
            "且不得拼接两个不完整方案", []))
        return decide(results, plan=plan)
    results.append(req(
        "number_alternative", RequirementStatus.UNKNOWN, "number_unobserved",
        "未观察到材料编号相关信息，证据不足", []))
    return decide(results, plan=plan)


# ---------------------------------------------------------------------------
# R058 生产日期决定版本
# ---------------------------------------------------------------------------


def evaluate_r058(task: AuditInput, plan: F10Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    found = entities_of_kind_with(facts, EntityKind.LABEL, "production_date") or \
        entities_of_kind_with(facts, EntityKind.DOCUMENT, "production_date")
    if not found:
        return decide([req(
            "model_prefix", RequirementStatus.UNKNOWN, "production_date_unobserved",
            "未观察到生产日期，无法选择版本分支", [])], plan=plan)
    _e, status, value, obs = found[0]
    if status != ObservationStatus.READABLE:
        return decide([req(
            "model_prefix", RequirementStatus.UNKNOWN, "production_date_unreadable",
            "生产日期读不清，无法选择版本分支", ev(obs, "生产日期不可读", quote))],
            plan=plan)
    try:
        produced = parse_date(str(value))
    except DateParseError:
        return decide([req(
            "model_prefix", RequirementStatus.VIOLATED, "production_date_invalid",
            f"生产日期 {value!r} 不是合法日期", ev(obs, f"生产日期 {value}", quote))],
            plan=plan)
    boundary = parse_date(plan.prefix_boundary_date)
    required_prefix = plan.prefix_before if produced < boundary else plan.prefix_on_or_after

    # 前缀大小写精确比较 (explicit in rule text); 审核日期不参与分支选择.
    model_found = entities_of_kind_with(facts, EntityKind.LABEL, "model")
    if not model_found:
        return decide([req(
            "model_prefix", RequirementStatus.UNKNOWN, "model_unobserved",
            "未观察到包装型号，无法核验前缀", [])], plan=plan)
    _me, m_status, m_value, m_obs = model_found[0]
    if m_status != ObservationStatus.READABLE:
        return decide([req(
            "model_prefix", RequirementStatus.UNKNOWN, "model_unreadable",
            "包装型号读不清，无法核验前缀", ev(m_obs, "型号不可读", quote))],
            plan=plan)
    model_text = annotation_nfkc_strip(str(m_value))
    if model_text.startswith(required_prefix):
        return decide([req(
            "model_prefix", RequirementStatus.SATISFIED, "prefix_matches_production_date",
            f"生产日期 {produced} 决定要求前缀 {required_prefix}（精确大小写），"
            f"包装型号 {model_text!r} 符合", [])], plan=plan)
    return decide([req(
        "model_prefix", RequirementStatus.VIOLATED, "prefix_mismatch",
        f"生产日期 {produced} 要求前缀 {required_prefix}（精确大小写），"
        f"包装型号 {model_text!r} 不符（版本按生产日期选择，与审核日期无关）",
        ev(m_obs, f"型号 {model_text}", quote))], plan=plan)


# ---------------------------------------------------------------------------
# R059 同级冲突条款处理
# ---------------------------------------------------------------------------


def evaluate_r059(task: AuditInput, plan: F10Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    boxes = [t for t in entities_of_kind_with(facts, EntityKind.PACKAGE, "box_color")]
    if not boxes:
        return decide([req(
            "circle_rule", RequirementStatus.UNKNOWN, "box_color_unobserved",
            "未观察到练习盒颜色信息，无法确定适用分支", [])], plan=plan)
    e, s, v, obs = boxes[0]
    if s != ObservationStatus.READABLE:
        return decide([req(
            "circle_rule", RequirementStatus.UNKNOWN, "box_color_unreadable",
            "练习盒颜色读不清，无法确定适用分支", ev(obs, "颜色不可读", quote))],
            plan=plan)
    is_blue = annotation_nfkc_strip(str(v)) in ("blue", "蓝色", "蓝盒", "蓝色练习盒")

    if is_blue:
        # 确认的蓝盒：两条同级条款（“所有练习盒正面须展示空心圆”与
        # “蓝色练习盒正面不得出现空心圆”）同时触发，无论圆是否出现；
        # 原文未指定例外或优先级 -> ambiguous (题目本身的语义缺口).
        return decide([req(
            "circle_rule", RequirementStatus.CONFLICT, "same_level_conflict",
            "“所有练习盒正面须展示空心圆”与“蓝色练习盒正面不得出现空心圆”为同级要求，"
            "确认的蓝盒无论正面是否出现空心圆，两项均同时触发；"
            "原文未指定例外或优先级，判为 ambiguous",
            ev(obs, f"盒色 {v}", quote))], plan=plan)

    # 非蓝盒：第二项条款不触发，第一项条款独立 governs。
    circles = [t for t in entities_of_kind_with(facts, EntityKind.OBJECT, "shape_type")
               if t[1] == ObservationStatus.READABLE and t[2] == "circle"]
    circle_status_known = bool(circles) or _circle_confirmed_absent(facts)
    if not circle_status_known:
        return decide([req(
            "circle_rule", RequirementStatus.UNKNOWN, "circle_presence_unknown",
            "正面是否出现空心圆无法确认，按证据不足处理", [])], plan=plan)
    if not circles:
        return decide([req(
            "circle_rule", RequirementStatus.VIOLATED, "circle_missing",
            "完整清晰范围内确认练习盒正面未出现空心圆，违反“所有练习盒都须展示空心圆”",
            [])], plan=plan)
    # 非蓝盒 + 有空心圆: 第一项要求满足，第二项不触发 -> valid non-conflict branch.
    return decide([req(
        "circle_rule", RequirementStatus.SATISFIED, "circle_shown_non_blue",
        f"练习盒（{v}）正面展示空心圆，满足要求且未触发蓝盒例外条款",
        ev(obs, f"盒色 {v}", quote))], plan=plan)


def _circle_confirmed_absent(facts: FactView) -> bool:
    for e in facts.entities_of_kind(EntityKind.PACKAGE):
        s, _v, _o = read(facts, e.entity_id, "shape_type")
        if s == ObservationStatus.CONFIRMED_MISSING:
            return True
    return False


# ---------------------------------------------------------------------------
# R060 未定义阈值处理
# ---------------------------------------------------------------------------


def evaluate_r060(task: AuditInput, plan: F10Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    labels = [e for e in facts.entities_of_kind(EntityKind.LABEL)
              if facts.claims(e.entity_id, "model")]
    if not labels:
        return decide([req(
            "size_branch", RequirementStatus.UNKNOWN, "model_label_unobserved",
            "未观察到型号标签，证据不足", [])], plan=plan)
    e = labels[0]
    cs, cv, cobs = read(facts, e.entity_id, "carrier")
    if cs != ObservationStatus.READABLE:
        return decide([req(
            "size_branch", RequirementStatus.UNKNOWN, "carrier_unknown",
            "型号载体无法确认，按证据不足处理", ev(cobs, "载体不确定", quote))],
            plan=plan)
    if cv == "outer_package":
        # 不受影响的合规分支: 型号在外包装，无论“较小”与否均符合.
        return decide([req(
            "size_branch", RequirementStatus.SATISFIED, "model_on_outer_package",
            "型号位于外包装，符合要求（不依赖未定义的“较小”阈值）",
            ev(cobs, "型号在外包装", quote))], plan=plan)
    if cv == "attached_tag":
        # “较小”的尺寸范围/判别方法在本规则及任务附件中均未定义 -> ambiguous.
        # 绝不借用 B001 的 100/80 mm 或任何常识阈值（总则 §5）.
        return decide([req(
            "size_branch", RequirementStatus.UNKNOWN, "undefined_small_threshold",
            "型号位于附签：本条仅在“较小的练习盒”时允许，而“较小”的尺寸范围或判别方法"
            "在本规则及任务附件中均未定义，无法确定该分支是否可用，判为 ambiguous",
            ev(cobs, "型号在附签", quote))], plan=plan)
    return decide([req(
        "size_branch", RequirementStatus.VIOLATED, "model_carrier_invalid",
        f"型号载体 {cv!r} 既不是外包装也不能证明属于“较小”附签例外",
        ev(cobs, f"载体 {cv}", quote))], plan=plan)


PLAN_CLASS = F10Plan

EVALUATORS: dict[str, Any] = {
    "R055": evaluate_r055,
    "R056": evaluate_r056,
    "R057": evaluate_r057,
    "R058": evaluate_r058,
    "R059": evaluate_r059,
    "R060": evaluate_r060,
}

__all__ = ["F10Plan", "PLAN_CLASS", "APPLICABILITY", "EVALUATORS"]
