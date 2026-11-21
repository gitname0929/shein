"""F05 图形与视觉关系 (R025-R030).

R025 空心圆标志, R026 三角点标志结构, R027 成对图标次序, R028 颜色编码与声明匹配,
R029 图标数量约束, R030 标志最小相对高度.
Geometry thresholds/counts come from the plan; operators only compute.
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
from o2_audit.operators.geometry import relative_position
from o2_audit.operators.numbers import decimal_of
from o2_audit.operators.text import annotation_nfkc_strip
from o2_audit.runtime.facts import FactView
from o2_audit.rules.advanced.base import (
    decide,
    entities_of_kind_with,
    ev,
    metadata_value,
    not_applicable_outcome,
    read,
    req,
)
from o2_audit.rules.base import RuleOutcome, RulePlan


class F05Plan(RulePlan):
    """F05 typed plans: R029 star count and R030 minimum height ratio are this
    family's rule parameters."""

    star_count: int = 3
    min_height_ratio: Decimal = Decimal("1.5")


def _applicability_r028(task: AuditInput, plan: F05Plan) -> tuple[str, list[str]]:
    edition = metadata_value(task, "color_edition")
    if edition is None:
        return "unknown", ["color_edition"]
    if annotation_nfkc_strip(str(edition)) in ("蓝版", "黄版"):
        return "applicable", []
    return "not_applicable", []


APPLICABILITY: dict[str, Any] = {
    "R025": lambda task, plan: ("applicable", []),
    "R026": lambda task, plan: ("applicable", []),
    "R027": lambda task, plan: ("applicable", []),
    "R028": _applicability_r028,
    "R029": lambda task, plan: ("applicable", []),
    "R030": lambda task, plan: ("applicable", []),
}


def _marks(facts: FactView, shape: str) -> list:
    return [t for t in entities_of_kind_with(facts, EntityKind.OBJECT, "shape_type")
            if t[1] == ObservationStatus.READABLE and t[2] == shape]


def _objects_observed(facts: FactView) -> bool:
    """True iff ANY observation was recorded on ANY EntityKind.OBJECT entity.

    An empty fact state (nothing inspected yet) is evidence insufficient —
    it must never be read as a proven absence of marks.
    """
    object_ids = {e.entity_id for e in facts.entities_of_kind(EntityKind.OBJECT)}
    return any(o.entity_id in object_ids for o in facts.all_observations())


# ---------------------------------------------------------------------------
# R025 空心圆标志
# ---------------------------------------------------------------------------


def evaluate_r025(task: AuditInput, plan: F05Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    circles = _marks(facts, "circle")
    if not circles:
        any_shape = entities_of_kind_with(facts, EntityKind.OBJECT, "shape_type")
        if any_shape:
            return decide([req(
                "circle_mark", RequirementStatus.VIOLATED, "declared_mark_missing",
                f"申报使用“圆标”的包装上未观察到闭合空心圆（观察到 "
                f"{[t[2] for t in any_shape]}）", [])], plan=plan)
        return decide([req(
            "circle_mark", RequirementStatus.UNKNOWN, "circle_mark_unobserved",
            "未观察到圆标形状信息，证据不足", [])], plan=plan)
    e = circles[0][0]
    results: list = []
    fill_s, fill_v, fill_obs = read(facts, e.entity_id, "is_filled")
    close_s, close_v, close_obs = read(facts, e.entity_id, "is_closed")
    text_s, text_v, text_obs = read(facts, e.entity_id, "text_inside_mark")
    # Protocol decision order: proven violations take precedence over unknowns.
    if fill_s == ObservationStatus.READABLE and fill_v is True:
        results.append(req(
            "circle_mark", RequirementStatus.VIOLATED, "circle_filled",
            "观察到实心圆，不符合空心圆要求", ev(fill_obs, "圆标为实心", quote)))
    if close_s == ObservationStatus.READABLE and close_v is not True:
        results.append(req(
            "circle_mark", RequirementStatus.VIOLATED, "circle_not_closed",
            "观察到明显未闭合的圆弧，不符合闭合空心圆要求",
            ev(close_obs, "圆弧未闭合", quote)))
    if text_s == ObservationStatus.READABLE and text_v is True:
        results.append(req(
            "circle_mark", RequirementStatus.VIOLATED, "text_inside_circle",
            "圆内出现文字，不符合圆标要求", ev(text_obs, "圆内有文字", quote)))
    if results:
        return decide(results, plan=plan)
    if fill_s != ObservationStatus.READABLE or close_s != ObservationStatus.READABLE:
        # 遮挡无法确认 -> ambiguous (explicit in rule text).
        return decide([req(
            "circle_mark", RequirementStatus.UNKNOWN, "circle_structure_unverifiable",
            "遮挡导致无法确认圆标的实心/闭合结构，按证据不足处理",
            ev(fill_obs or close_obs, "结构不可确认", quote))], plan=plan)
    return decide([req(
        "circle_mark", RequirementStatus.SATISFIED, "hollow_circle_ok",
        "观察到闭合空心圆且圆内无文字", [])], plan=plan)


# ---------------------------------------------------------------------------
# R026 三角点标志结构
# ---------------------------------------------------------------------------


def evaluate_r026(task: AuditInput, plan: F05Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    triangles = _marks(facts, "triangle")
    results: list = []
    if not triangles:
        if not _objects_observed(facts):
            # Nothing inspected yet: evidence insufficient, not a proven absence.
            results.append(req(
                "triangle_border", RequirementStatus.UNKNOWN, "unobserved",
                "未观察到任何图形检查结果，证据不足（不能据此认定缺少三角形）", []))
            return decide(results, plan=plan)
        results.append(req(
            "triangle_border", RequirementStatus.VIOLATED, "triangle_missing",
            "未观察到闭合三角形边框，缺少三角点标的组成元素", []))
        return decide(results, plan=plan)
    e = triangles[0][0]
    close_s, close_v, close_obs = read(facts, e.entity_id, "is_closed")
    if close_s == ObservationStatus.READABLE and close_v is not True:
        results.append(req(
            "triangle_border", RequirementStatus.VIOLATED, "triangle_not_closed",
            "三角形边框明显未闭合，不符合要求", ev(close_obs, "边框未闭合", quote)))
        return decide(results, plan=plan)
    if close_s != ObservationStatus.READABLE:
        results.append(req(
            "triangle_border", RequirementStatus.UNKNOWN, "triangle_unverifiable",
            "遮挡导致无法确认三角形边框是否闭合，按证据不足处理",
            ev(close_obs, "边框不可确认", quote)))
        return decide(results, plan=plan)

    dots = _marks(facts, "dot")
    if not dots:
        results.append(req(
            "dot", RequirementStatus.VIOLATED, "dot_missing",
            "未观察到框内实心点，缺少三角点标的组成元素", []))
        return decide(results, plan=plan)
    inside = []
    for d, _s, _v, d_obs in dots:
        pos_s, pos_v, _o = read(facts, d.entity_id, "relative_position")
        if pos_s == ObservationStatus.READABLE and pos_v == "inside":
            inside.append((d, d_obs))
        elif pos_s == ObservationStatus.READABLE and pos_v == "outside":
            results.append(req(
                "dot", RequirementStatus.VIOLATED, "dot_outside_triangle",
                "实心点位于三角形框外，不符合要求", ev(d_obs, "点在框外", quote)))
            return decide(results, plan=plan)
    if len(inside) == 1:
        results.append(req(
            "dot", RequirementStatus.SATISFIED, "single_dot_inside",
            "闭合三角形边框内恰有一个实心点", []))
        return decide(results, plan=plan)
    if len(inside) > 1:
        results.append(req(
            "dot", RequirementStatus.VIOLATED, "multiple_dots",
            f"框内出现 {len(inside)} 个实心点，不符合单点要求", []))
        return decide(results, plan=plan)
    results.append(req(
        "dot", RequirementStatus.UNKNOWN, "dot_position_unverifiable",
        "实心点与三角形边框的相对位置无法确认，按证据不足处理", []))
    return decide(results, plan=plan)


# ---------------------------------------------------------------------------
# R027 成对图标次序
# ---------------------------------------------------------------------------


def evaluate_r027(task: AuditInput, plan: F05Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    circles = _marks(facts, "circle")
    squares = _marks(facts, "square")
    results: list = []
    if not circles or not squares:
        if not _objects_observed(facts):
            # Nothing inspected yet: evidence insufficient, not a proven absence.
            results.append(req(
                "pair_icons", RequirementStatus.UNKNOWN, "unobserved",
                "未观察到任何图形检查结果，证据不足（不能据此认定缺少圆标或方标）", []))
            return decide(results, plan=plan)
        # 须同时有圆标和方标; missing one with observed scope -> 不符合.
        missing = "圆标" if not circles else "方标"
        results.append(req(
            "pair_icons", RequirementStatus.VIOLATED, "pair_icon_missing",
            f"指定包装正视图中缺少{missing}，不符合成对图标要求", []))
        return decide(results, plan=plan)
    circle, square = circles[0][0], squares[0][0]

    def center_obs(e) -> tuple:
        s, v, obs = read(facts, e.entity_id, "mark_bbox")
        if s == ObservationStatus.READABLE and isinstance(v, (list, tuple)) and len(v) == 4:
            try:
                return ("bbox", tuple(float(x) for x in v), obs)
            except (TypeError, ValueError):
                return ("bad", None, obs)
        return ("missing", None, obs)

    c_kind, c_val, c_obs = center_obs(circle)
    s_kind, s_val, s_obs = center_obs(square)
    if c_kind == "missing" or s_kind == "missing" or c_kind == "bad" or s_kind == "bad":
        pos_s, pos_v, pos_obs = read(facts, circle.entity_id, "relative_position")
        if pos_s == ObservationStatus.READABLE:
            pos = annotation_nfkc_strip(str(pos_v))
            if pos == "left_of":
                return decide([req(
                    "pair_icons", RequirementStatus.SATISFIED, "circle_left_of_square",
                    "圆标中心位于方标中心左侧", ev(pos_obs, "圆标在方标左侧", quote))],
                    plan=plan)
            if pos in ("right_of", "above", "below", "adjacent"):
                return decide([req(
                    "pair_icons", RequirementStatus.VIOLATED, "circle_not_left",
                    f"圆标未位于方标左侧（观察为 {pos}）；上下排列但横坐标相同不符合",
                    ev(pos_obs, f"圆标相对方标 {pos}", quote))], plan=plan)
        return decide([req(
            "pair_icons", RequirementStatus.UNKNOWN, "icon_position_unverifiable",
            "圆标与方标的相对位置无法确认，按证据不足处理", [])], plan=plan)
    pos = relative_position(c_val, s_val)
    if pos == "left":
        return decide([req(
            "pair_icons", RequirementStatus.SATISFIED, "circle_left_of_square",
            "圆标中心位于方标中心左侧（由 bbox 中心计算）",
            ev(c_obs, "圆标 bbox", quote))], plan=plan)
    return decide([req(
        "pair_icons", RequirementStatus.VIOLATED, "circle_not_left",
        f"圆标未位于方标左侧（相对位置 {pos}）；上下排列但横坐标相同不符合",
        ev(c_obs, "圆标 bbox", quote))], plan=plan)


# ---------------------------------------------------------------------------
# R028 颜色编码与声明匹配
# ---------------------------------------------------------------------------


def evaluate_r028(task: AuditInput, plan: F05Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    edition = annotation_nfkc_strip(str(metadata_value(task, "color_edition")))
    expected_color = "blue" if edition == "蓝版" else "yellow"
    expected_cn = "蓝色" if edition == "蓝版" else "黄色"

    dots = [t for t in entities_of_kind_with(facts, EntityKind.OBJECT, "shape_type")
            if t[1] == ObservationStatus.READABLE and t[2] in ("dot", "circle")]
    if not dots:
        return decide([req(
            "color_dot", RequirementStatus.UNKNOWN, "color_dot_unobserved",
            "未观察到标识区圆点，证据不足", [])], plan=plan)
    e = dots[0][0]
    mode_s, mode_v, mode_obs = read(facts, e.entity_id, "color_mode")
    if mode_s == ObservationStatus.READABLE and mode_v == "grayscale":
        # 黑白照片无法判断颜色 -> ambiguous (explicit in rule text).
        return decide([req(
            "color_dot", RequirementStatus.UNKNOWN, "grayscale_photo",
            "黑白照片无法判断颜色，按证据不足处理", ev(mode_obs, "灰度图", quote))],
            plan=plan)
    color_s, color_v, color_obs = read(facts, e.entity_id, "mark_color")
    if color_s != ObservationStatus.READABLE:
        return decide([req(
            "color_dot", RequirementStatus.UNKNOWN, "mark_color_unverifiable",
            "圆点颜色无法确认，按证据不足处理", ev(color_obs, "颜色不确定", quote))],
            plan=plan)
    if annotation_nfkc_strip(str(color_v)) == expected_color:
        return decide([req(
            "color_dot", RequirementStatus.SATISFIED, "color_matches_declaration",
            f"声明“{edition}”的标识区包含{expected_cn}圆点", ev(color_obs, "圆点颜色", quote))],
            plan=plan)
    return decide([req(
        "color_dot", RequirementStatus.VIOLATED, "color_mismatch",
        f"声明“{edition}”须包含{expected_cn}圆点，实际观察到 {color_v!r}",
        ev(color_obs, f"圆点颜色 {color_v}", quote))], plan=plan)


# ---------------------------------------------------------------------------
# R029 图标数量约束
# ---------------------------------------------------------------------------


def evaluate_r029(task: AuditInput, plan: F05Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    expected = plan.star_count
    stars = [t for t in entities_of_kind_with(facts, EntityKind.OBJECT, "shape_type")
             if t[1] == ObservationStatus.READABLE and t[2] == "star"]
    pkgs = [t for t in entities_of_kind_with(facts, EntityKind.PACKAGE, "front_fully_shown")
            if t[1] == ObservationStatus.READABLE and t[2] is True]

    def _package_shape_confirmed_absent() -> bool:
        for e in facts.entities_of_kind(EntityKind.PACKAGE):
            s, _v, _o = read(facts, e.entity_id, "shape_type")
            if s == ObservationStatus.CONFIRMED_MISSING:
                return True
        return False

    if not stars:
        if pkgs and _package_shape_confirmed_absent():
            # Front fully shown AND star shapes confirmed absent -> 0 != 3 proven.
            return decide([req(
                "star_count", RequirementStatus.VIOLATED, "star_count_wrong",
                f"包装主面完整展示且确认无星形标志（0 枚），须恰好 {expected} 枚", [])],
                plan=plan)
        return decide([req(
            "star_count", RequirementStatus.UNKNOWN, "stars_unobserved",
            "未观察到星形标志，证据不足", [])], plan=plan)

    # 主面未完整展示时不能认定数量满足 (explicit in rule text).
    if not pkgs:
        return decide([req(
            "star_count", RequirementStatus.UNKNOWN, "front_view_incomplete",
            "包装主面未完整展示，不能认定星形数量满足要求", [])], plan=plan)

    unvoided: list = []
    for e, _s, _v, obs in stars:
        # 1) void status: a readable True excludes the star; an unverifiable
        #    void status must not silently count the star.
        vs, vv, _vo = read(facts, e.entity_id, "void_marked")
        if vs == ObservationStatus.READABLE and vv is True:
            continue  # 作废星形不计入
        if vs in (ObservationStatus.UNREADABLE, ObservationStatus.INCOMPLETE_SCOPE,
                  ObservationStatus.IDENTITY_UNKNOWN):
            return decide([req(
                "star_count", RequirementStatus.UNKNOWN, "star_void_status_unverifiable",
                "某星形是否被作废无法确认，不能确定未作废实心星形数量，按证据不足处理",
                ev(obs, "作废状态不可确认", quote))], plan=plan)
        # (READABLE, False) or confirmed_missing void mark -> not voided.
        # 2) fill status: only solid stars count; unverifiable fill -> unknown.
        fs, fv, fobs = read(facts, e.entity_id, "is_filled")
        if fs == ObservationStatus.READABLE and fv is False:
            continue  # 空心星形不计入“实心星形”
        if fs in (ObservationStatus.UNREADABLE, ObservationStatus.INCOMPLETE_SCOPE,
                  ObservationStatus.IDENTITY_UNKNOWN):
            return decide([req(
                "star_count", RequirementStatus.UNKNOWN, "star_fill_unverifiable",
                "某星形是否为实心无法确认，不能确定实心星形数量，按证据不足处理",
                ev(fobs, "实心状态不可确认", quote))], plan=plan)
        unvoided.append((e, obs))
    if len(unvoided) == expected:
        return decide([req(
            "star_count", RequirementStatus.SATISFIED, "star_count_exact",
            f"包装正面恰好出现 {expected} 个未作废的实心星形标志", [])], plan=plan)
    return decide([req(
        "star_count", RequirementStatus.VIOLATED, "star_count_wrong",
        f"包装正面出现 {len(unvoided)} 个未作废实心星形标志，须恰好 {expected} 枚"
        "（两枚或四枚均不符合）", [])], plan=plan)


# ---------------------------------------------------------------------------
# R030 标志最小相对高度
# ---------------------------------------------------------------------------


def evaluate_r030(task: AuditInput, plan: F05Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    marks = [t for t in entities_of_kind_with(facts, EntityKind.OBJECT, "height_ratio")]
    if not marks:
        return decide([req(
            "mark_height", RequirementStatus.UNKNOWN, "height_ratio_unobserved",
            "未观察到标志与型号文字的高度比例，证据不足", [])], plan=plan)
    e, s, v, obs = marks[0]
    # 指定标准正视图; 透视或遮挡影响测量时判为 ambiguous.
    vs, vv, vobs = read(facts, e.entity_id, "view_type")
    if vs != ObservationStatus.READABLE or vv != "front":
        return decide([req(
            "mark_height", RequirementStatus.UNKNOWN, "non_standard_view",
            "视图不是指定标准正视图，测量比例不可靠，按证据不足处理",
            ev(vobs, "视图非标准正视图", quote))], plan=plan)
    if s != ObservationStatus.READABLE:
        return decide([req(
            "mark_height", RequirementStatus.UNKNOWN, "height_ratio_unmeasurable",
            "透视或遮挡影响测量，比例无法确认，按证据不足处理",
            ev(obs, "比例不可测", quote))], plan=plan)
    try:
        ratio = decimal_of(v)
    except ValueError:
        return decide([req(
            "mark_height", RequirementStatus.UNKNOWN, "height_ratio_invalid",
            f"高度比例值 {v!r} 非法，无法比较", ev(obs, "比例非法", quote))],
            plan=plan)
    if ratio >= plan.min_height_ratio:
        # 恰好等于允许 (explicit in rule text).
        return decide([req(
            "mark_height", RequirementStatus.SATISFIED, "height_ratio_ok",
            f"标准正视图中标志高度比例 {ratio} 不小于要求值 {plan.min_height_ratio}", [])],
            plan=plan)
    return decide([req(
        "mark_height", RequirementStatus.VIOLATED, "height_ratio_too_small",
        f"标志高度比例 {ratio} 小于要求值 {plan.min_height_ratio}",
        ev(obs, f"比例 {ratio}", quote))], plan=plan)


PLAN_CLASS = F05Plan

EVALUATORS: dict[str, Any] = {
    "R025": evaluate_r025,
    "R026": evaluate_r026,
    "R027": evaluate_r027,
    "R028": evaluate_r028,
    "R029": evaluate_r029,
    "R030": evaluate_r030,
}

__all__ = ["F05Plan", "PLAN_CLASS", "APPLICABILITY", "EVALUATORS"]
