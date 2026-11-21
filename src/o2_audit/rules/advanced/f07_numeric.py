"""F07 参数与数值关系 (R037-R042).

R037 容量单位换算一致, R038 净重不大于总重, R039 范围包含申报值,
R040 部件比例总和, R041 双面数值容差, R042 日期加天数有效期.

ONLY the conversions/tolerances explicitly authorized by the rule text live
in the plan (e.g. 1 L = 1000 mL; g/kg 1000x; cm/mm 10x; 1% against the larger
value). Exact Decimal arithmetic throughout.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from o2_audit.domain.models import (
    AuditInput,
    EntityKind,
    ObservationStatus,
    RequirementStatus,
)
from o2_audit.operators.dates import DateParseError, add_days, parse_date
from o2_audit.operators.numbers import convert_exact, decimal_of, in_interval
from o2_audit.operators.text import annotation_nfkc_strip
from o2_audit.runtime.facts import FactView
from o2_audit.rules.advanced.base import (
    decide,
    entities_of_kind_with,
    ev,
    field_check,
    metadata_value,
    read_quantity,
    req,
)
from o2_audit.rules.base import RuleOutcome, RulePlan


class F07Plan(RulePlan):
    """F07 typed plans: conversions/tolerances are rule-authorized parameters."""

    #: R037: 1 L == 1000 mL is the ONLY authorized conversion.
    capacity_factors: dict[tuple[str, str], Decimal] = {("L", "mL"): Decimal(1000)}
    capacity_units: tuple[str, ...] = ("mL", "L")
    #: R038/R041: g <-> kg by 1000 only.
    weight_factors: dict[tuple[str, str], Decimal] = {("kg", "g"): Decimal(1000)}
    weight_units: tuple[str, ...] = ("g", "kg")
    #: R039: cm <-> mm by 10 only.
    height_factors: dict[tuple[str, str], Decimal] = {("mm", "cm"): Decimal("0.1")}
    height_units: tuple[str, ...] = ("cm", "mm")
    #: R041: 1% of the larger value; exactly 1% allowed.
    weight_rel_tolerance: Decimal = Decimal("0.01")
    #: R042: 生产后 30 个自然日.
    validity_days: int = 30


APPLICABILITY: dict[str, Any] = {
    rid: (lambda task, plan: ("applicable", []))
    for rid in ("R037", "R038", "R039", "R040", "R041", "R042")
}


def _quantities(facts: FactView, predicate: str, kind: EntityKind = EntityKind.LABEL):
    return [(t[0], *read_quantity(facts, t[0].entity_id, predicate))
            for t in entities_of_kind_with(facts, kind, predicate)]


def _single_quantity(facts: FactView, predicate: str,
                     kind: EntityKind = EntityKind.LABEL):
    pairs = entities_of_kind_with(facts, kind, predicate)
    if not pairs:
        return None, ObservationStatus.INCOMPLETE_SCOPE, None, None
    e = pairs[0][0]
    status, quantity, obs = read_quantity(facts, e.entity_id, predicate)
    return e, status, quantity, obs


# ---------------------------------------------------------------------------
# R037 容量单位换算一致
# ---------------------------------------------------------------------------


def evaluate_r037(task: AuditInput, plan: F07Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    declared = metadata_value(task, "declared_capacity_ml")
    if declared is None:
        return decide([req(
            "capacity", RequirementStatus.UNKNOWN, "declared_capacity_missing",
            "declared_capacity_ml 缺失，无法比较", [])], plan=plan)
    try:
        target = decimal_of(declared)
    except ValueError:
        return decide([req(
            "capacity", RequirementStatus.UNKNOWN, "declared_capacity_invalid",
            f"declared_capacity_ml {declared!r} 非法，无法比较", [])], plan=plan)

    e, status, quantity, obs = _single_quantity(facts, "capacity")
    if e is None:
        return decide([req(
            "capacity", RequirementStatus.UNKNOWN, "capacity_unobserved",
            "未观察到包装容量，证据不足", [])], plan=plan)
    if status == ObservationStatus.CONFIRMED_MISSING:
        return decide([req(
            "capacity", RequirementStatus.VIOLATED, "capacity_confirmed_missing",
            "完整清晰范围内确认容量字段缺失", ev(obs, "容量缺失", quote))], plan=plan)
    if status != ObservationStatus.READABLE:
        return decide([req(
            "capacity", RequirementStatus.UNKNOWN, "capacity_unreadable",
            "包装容量读不清，按证据不足处理", ev(obs, "容量不可读", quote))], plan=plan)
    value, unit = quantity
    if unit not in plan.capacity_units:
        # 不允许其他单位 (explicit in rule text).
        return decide([req(
            "capacity", RequirementStatus.VIOLATED, "capacity_unit_not_authorized",
            f"容量单位 {unit!r} 不在本条允许的 {plan.capacity_units} 之内",
            ev(obs, f"单位 {unit}", quote))], plan=plan)
    try:
        ml = convert_exact(value, unit, "mL", plan.capacity_factors)
    except ValueError as exc:
        return decide([req(
            "capacity", RequirementStatus.VIOLATED, "capacity_conversion_not_authorized",
            f"未授权的单位换算: {exc}", ev(obs, f"{value} {unit}", quote))], plan=plan)
    if ml == target:
        return decide([req(
            "capacity", RequirementStatus.SATISFIED, "capacity_matches",
            f"包装容量 {value} {unit} 换算为 {ml} mL 后与申报值 {target} mL 一致"
            "（仅使用 1 L = 1000 mL 的授权换算，无额外误差）", [])], plan=plan)
    return decide([req(
        "capacity", RequirementStatus.VIOLATED, "capacity_mismatch",
        f"包装容量 {value} {unit} 换算为 {ml} mL，与申报值 {target} mL 不一致"
        "（不允许额外误差）", ev(obs, f"{value} {unit}", quote))], plan=plan)


# ---------------------------------------------------------------------------
# R038 净重不大于总重
# ---------------------------------------------------------------------------


def evaluate_r038(task: AuditInput, plan: F07Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    e, net_status, net, net_obs = _single_quantity(facts, "net_weight")
    if e is None:
        return decide([req(
            "weights", RequirementStatus.UNKNOWN, "weights_unobserved",
            "未观察到净重/总重，证据不足", [])], plan=plan)
    _s2, total, total_obs = read_quantity(facts, e.entity_id, "total_weight")
    results: list = []
    if net_status == ObservationStatus.CONFIRMED_MISSING or total is None and _total_missing(facts, e.entity_id):
        results.append(req(
            "weights", RequirementStatus.VIOLATED, "weight_field_missing",
            "完整清晰范围内确认净重或总重字段缺失", ev(net_obs, "重量字段缺失", quote)))
        return decide(results, plan=plan)
    if net_status != ObservationStatus.READABLE or total is None:
        return decide([req(
            "weights", RequirementStatus.UNKNOWN, "weight_unreadable",
            "净重或总重读不清，按证据不足处理", ev(net_obs, "重量不可读", quote))],
            plan=plan)
    net_g = _to_g(net, plan, results, quote, net_obs)
    total_g = _to_g(total, plan, results, quote, total_obs)
    if net_g is None or total_g is None:
        return decide(results, plan=plan)
    if net_g <= 0 or total_g <= 0:
        results.append(req(
            "weights", RequirementStatus.VIOLATED, "weight_not_positive",
            f"净重/总重须为正数（观察到 {net_g} g 与 {total_g} g）", []))
        return decide(results, plan=plan)
    if net_g > total_g:
        results.append(req(
            "weights", RequirementStatus.VIOLATED, "net_exceeds_total",
            f"净重 {net_g} g 大于总重 {total_g} g，不满足要求", []))
        return decide(results, plan=plan)
    results.append(req(
        "weights", RequirementStatus.SATISFIED, "net_le_total",
        f"净重 {net_g} g 不大于总重 {total_g} g（仅使用 g/kg 1000 倍授权换算）", []))
    return decide(results, plan=plan)


def _total_missing(facts: FactView, entity_id: str) -> bool:
    s, _v, _o = read_quantity(facts, entity_id, "total_weight")
    return s == ObservationStatus.CONFIRMED_MISSING


def _to_g(q: tuple, plan: F07Plan, results: list, quote, obs):
    value, unit = q
    if unit not in plan.weight_units:
        results.append(req(
            "weights", RequirementStatus.VIOLATED, "weight_unit_not_authorized",
            f"重量单位 {unit!r} 不在本条允许的 {plan.weight_units} 之内",
            ev(obs, f"单位 {unit}", quote)))
        return None
    try:
        return convert_exact(value, unit, "g", plan.weight_factors)
    except ValueError as exc:
        results.append(req(
            "weights", RequirementStatus.VIOLATED, "weight_conversion_not_authorized",
            f"未授权的单位换算: {exc}", ev(obs, f"{value} {unit}", quote)))
        return None


# ---------------------------------------------------------------------------
# R039 范围包含申报值
# ---------------------------------------------------------------------------


def evaluate_r039(task: AuditInput, plan: F07Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    declared = metadata_value(task, "declared_height_cm")
    if declared is None:
        return decide([req(
            "height_range", RequirementStatus.UNKNOWN, "declared_height_missing",
            "declared_height_cm 缺失，无法核验区间包含", [])], plan=plan)
    try:
        target_cm = decimal_of(declared)
    except ValueError:
        return decide([req(
            "height_range", RequirementStatus.UNKNOWN, "declared_height_invalid",
            f"declared_height_cm {declared!r} 非法，无法比较", [])], plan=plan)

    e, lo_status, lo, lo_obs = _single_quantity(facts, "adjustable_height_min")
    if e is None:
        return decide([req(
            "height_range", RequirementStatus.UNKNOWN, "height_range_unobserved",
            "未观察到可调高度区间，证据不足", [])], plan=plan)
    _s, hi, hi_obs = read_quantity(facts, e.entity_id, "adjustable_height_max")
    results: list = []
    if lo_status == ObservationStatus.CONFIRMED_MISSING or _max_missing(facts, e.entity_id):
        results.append(req(
            "height_range", RequirementStatus.VIOLATED, "height_endpoint_missing",
            "完整清晰范围内确认可调高度区间端点缺失", ev(lo_obs, "端点缺失", quote)))
        return decide(results, plan=plan)
    if lo_status != ObservationStatus.READABLE or hi is None:
        return decide([req(
            "height_range", RequirementStatus.UNKNOWN, "height_unreadable",
            "可调高度区间读不清，按证据不足处理", ev(lo_obs, "区间不可读", quote))],
            plan=plan)

    def to_cm(q, obs):
        value, unit = q
        if unit not in plan.height_units:
            results.append(req(
                "height_range", RequirementStatus.VIOLATED, "height_unit_not_authorized",
                f"高度单位 {unit!r} 不在本条允许的 {plan.height_units} 之内",
                ev(obs, f"单位 {unit}", quote)))
            return None
        try:
            return convert_exact(value, unit, "cm", plan.height_factors)
        except ValueError as exc:
            results.append(req(
                "height_range", RequirementStatus.VIOLATED, "height_conversion_not_authorized",
                f"未授权的单位换算: {exc}", ev(obs, f"{value} {unit}", quote)))
            return None

    lo_cm = to_cm(lo, lo_obs)
    hi_cm = to_cm(hi, hi_obs)
    if lo_cm is None or hi_cm is None:
        return decide(results, plan=plan)
    if lo_cm > hi_cm:
        results.append(req(
            "height_range", RequirementStatus.VIOLATED, "height_range_reversed",
            f"可调高度区间 [{lo_cm}, {hi_cm}] cm 无序", []))
        return decide(results, plan=plan)
    if in_interval(target_cm, lo_cm, hi_cm):
        results.append(req(
            "height_range", RequirementStatus.SATISFIED, "declared_in_range",
            f"区间 [{lo_cm}, {hi_cm}] cm 包含申报值 {target_cm} cm（端点等于也允许；"
            "仅使用 cm/mm 10 倍授权换算）", []))
        return decide(results, plan=plan)
    results.append(req(
        "height_range", RequirementStatus.VIOLATED, "declared_out_of_range",
        f"区间 [{lo_cm}, {hi_cm}] cm 不包含申报值 {target_cm} cm",
        ev(lo_obs, f"区间 {lo_cm}-{hi_cm} cm", quote)))
    return decide(results, plan=plan)


def _max_missing(facts: FactView, entity_id: str) -> bool:
    s, _v, _o = read_quantity(facts, entity_id, "adjustable_height_max")
    return s == ObservationStatus.CONFIRMED_MISSING


# ---------------------------------------------------------------------------
# R040 部件比例总和
# ---------------------------------------------------------------------------


def evaluate_r040(task: AuditInput, plan: F07Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    result = field_check(
        facts, EntityKind.LABEL, "component_percentages", "filler_percentages",
        check=lambda v: _check_percentages(v, plan),
        quote=quote,
        missing_reason="完整清晰范围内确认某类填充物百分比缺失"
                       "（不允许把缺失值当作零）",
        unreadable_reason="填充物百分比读不清，按证据不足处理")
    return decide([result], plan=plan)


def _check_percentages(value: Any, plan: F07Plan) -> tuple[bool, str]:
    if not isinstance(value, list) or not value:
        return False, f"填充物百分比 {value!r} 不是非空数值列表"
    numbers: list[Decimal] = []
    for x in value:
        try:
            numbers.append(decimal_of(x))
        except ValueError:
            return False, f"填充物百分比含非法数值 {x!r}"
    if len(numbers) != 3:
        return False, f"须声明三类填充物百分比，实际 {len(numbers)} 项"
    for n in numbers:
        if not in_interval(n, 0, 100):
            return False, f"百分比 {n} 不在 0 至 100 之间"
    total = sum(numbers, Decimal(0))
    if total != Decimal(100):
        return False, f"三类百分比合计为 {total}%，须恰好为 100%"
    return True, ""


# ---------------------------------------------------------------------------
# R041 双面数值容差
# ---------------------------------------------------------------------------


def evaluate_r041(task: AuditInput, plan: F07Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    e, front_status, front, front_obs = _single_quantity(facts, "front_weight")
    if e is None:
        return decide([req(
            "face_weights", RequirementStatus.UNKNOWN, "face_weights_unobserved",
            "未观察到正面/背面标称重量，证据不足", [])], plan=plan)
    _s, back, back_obs = read_quantity(facts, e.entity_id, "back_weight")
    results: list = []
    if front_status == ObservationStatus.CONFIRMED_MISSING or _back_missing(facts, e.entity_id):
        results.append(req(
            "face_weights", RequirementStatus.VIOLATED, "face_weight_missing",
            "完整清晰范围内确认正面或背面标称重量缺失", ev(front_obs, "重量缺失", quote)))
        return decide(results, plan=plan)
    if front_status != ObservationStatus.READABLE or back is None:
        return decide([req(
            "face_weights", RequirementStatus.UNKNOWN, "face_weight_unreadable",
            "正面或背面重量读不清，按证据不足处理", ev(front_obs, "重量不可读", quote))],
            plan=plan)

    def to_g(q, obs):
        value, unit = q
        if unit not in plan.weight_units:
            results.append(req(
                "face_weights", RequirementStatus.VIOLATED, "weight_unit_not_authorized",
                f"重量单位 {unit!r} 不在本条允许的 {plan.weight_units} 之内",
                ev(obs, f"单位 {unit}", quote)))
            return None
        try:
            return convert_exact(value, unit, "g", plan.weight_factors)
        except ValueError as exc:
            results.append(req(
                "face_weights", RequirementStatus.VIOLATED, "weight_conversion_not_authorized",
                f"未授权的单位换算: {exc}", ev(obs, f"{value} {unit}", quote)))
            return None

    front_g = to_g(front, front_obs)
    back_g = to_g(back, back_obs)
    if front_g is None or back_g is None:
        return decide(results, plan=plan)
    if front_g <= 0 or back_g <= 0:
        results.append(req(
            "face_weights", RequirementStatus.VIOLATED, "face_weight_not_positive",
            f"两面重量须大于零（观察到 {front_g} g 与 {back_g} g）", []))
        return decide(results, plan=plan)
    larger = max(front_g, back_g)
    limit = plan.weight_rel_tolerance * larger
    diff = abs(front_g - back_g)
    if diff <= limit:  # 恰好 1% 允许 (inclusive endpoint)
        return decide([req(
            "face_weights", RequirementStatus.SATISFIED, "face_tolerance_ok",
            f"正面/背面重量 {front_g} g 与 {back_g} g 换算为 g 后相差 {diff} g，"
            f"不超过较大值的 {plan.weight_rel_tolerance * 100}%（恰好 1% 允许）", [])],
            plan=plan)
    return decide([req(
        "face_weights", RequirementStatus.VIOLATED, "face_tolerance_exceeded",
        f"正面/背面重量 {front_g} g 与 {back_g} g 相差 {diff} g，超过较大值 {larger} g "
        f"的 {plan.weight_rel_tolerance * 100}%", ev(front_obs, f"{front_g}/{back_g} g", quote))],
        plan=plan)


def _back_missing(facts: FactView, entity_id: str) -> bool:
    s, _v, _o = read_quantity(facts, entity_id, "back_weight")
    return s == ObservationStatus.CONFIRMED_MISSING


# ---------------------------------------------------------------------------
# R042 日期加天数有效期
# ---------------------------------------------------------------------------


def evaluate_r042(task: AuditInput, plan: F07Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    found = entities_of_kind_with(facts, EntityKind.LABEL, "production_date") or \
        entities_of_kind_with(facts, EntityKind.DOCUMENT, "production_date")
    if not found:
        return decide([req(
            "use_window", RequirementStatus.UNKNOWN, "production_date_unobserved",
            "未观察到生产日期，证据不足", [])], plan=plan)
    _e, status, value, obs = found[0]
    if status != ObservationStatus.READABLE:
        return decide([req(
            "use_window", RequirementStatus.UNKNOWN, "production_date_unreadable",
            "生产日期读不清，按证据不足处理", ev(obs, "生产日期不可读", quote))],
            plan=plan)
    try:
        produced = parse_date(str(value))
    except DateParseError:
        return decide([req(
            "use_window", RequirementStatus.VIOLATED, "production_date_invalid",
            f"生产日期 {value!r} 不是合法日期", ev(obs, f"生产日期 {value}", quote))],
            plan=plan)
    try:
        review = parse_date(task.review_date)
    except DateParseError:
        return decide([req(
            "use_window", RequirementStatus.UNKNOWN, "review_date_invalid",
            f"任务审核基准日期 {task.review_date!r} 非法，无法核验", [])], plan=plan)
    deadline = add_days(produced, plan.validity_days)
    if produced <= review <= deadline:
        return decide([req(
            "use_window", RequirementStatus.SATISFIED, "within_use_window",
            f"审核基准日期 {review} 在 [{produced}, {deadline}] 内"
            f"（生产后 {plan.validity_days} 个自然日，含端点）", [])], plan=plan)
    if review < produced:
        return decide([req(
            "use_window", RequirementStatus.VIOLATED, "review_before_production",
            f"审核基准日期 {review} 早于生产日 {produced}，不满足要求",
            ev(obs, f"生产日期 {produced}", quote))], plan=plan)
    return decide([req(
        "use_window", RequirementStatus.VIOLATED, "use_window_expired",
        f"审核基准日期 {review} 晚于生产日加 {plan.validity_days} 天的截止日 {deadline}",
        ev(obs, f"生产日期 {produced}", quote))], plan=plan)


PLAN_CLASS = F07Plan

EVALUATORS: dict[str, Any] = {
    "R037": evaluate_r037,
    "R038": evaluate_r038,
    "R039": evaluate_r039,
    "R040": evaluate_r040,
    "R041": evaluate_r041,
    "R042": evaluate_r042,
}

__all__ = ["F07Plan", "PLAN_CLASS", "APPLICABILITY", "EVALUATORS"]
