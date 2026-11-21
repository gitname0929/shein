"""F04 标签位置与载体 (R019-R024).

R019 主型号位于包装正面, R020 序列号位于商品本体, R021 型号与编号位于同一标签,
R022 小包装附签例外, R023 标签不得跨越开封断面, R024 展示面上半区位置.

口径要点: R019/R020 只核验位置与载体身份，不额外要求读出字段值;
R021 永不合并不同标签实体; R022 的 80 mm 阈值来自本条原文（与 B001 无关），
尺寸缺失判为 ambiguous，不从照片估计。
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
from o2_audit.operators.numbers import decimal_of, number_strictly_less
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


class F04Plan(RulePlan):
    """R022's own threshold: package_longest_edge_mm < 80 -> attached tag allowed
    (verbatim from the R022 rule text; unrelated to B001's version thresholds)."""

    small_tag_threshold_mm: Decimal = Decimal("80")


APPLICABILITY: dict[str, Any] = {
    rid: (lambda task, plan: ("applicable", []))
    for rid in ("R019", "R020", "R021", "R022", "R023", "R024")
}


# ---------------------------------------------------------------------------
# R019 主型号位于包装正面
# ---------------------------------------------------------------------------


def evaluate_r019(task: AuditInput, plan: F04Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    # Field-name and carrier-location identity, value readability NOT required:
    # model identity = the label carries a model field claim (any status).
    # A model CONFIRMED MISSING on the front is not a model field on the front.
    targets = [e for e in facts.entities_of_kind(EntityKind.LABEL)
               if facts.claims(e.entity_id, "model")
               and read(facts, e.entity_id, "model")[0] != ObservationStatus.CONFIRMED_MISSING]
    # The front view may be reported on the package entity OR on a label entity.
    front_views = [t for t in entities_of_kind_with(facts, EntityKind.PACKAGE, "view_type")
                   + entities_of_kind_with(facts, EntityKind.LABEL, "view_type")
                   if t[1] == ObservationStatus.READABLE and t[2] == "front"]
    if not front_views:
        # 正面视图未提交按证据不足处理 (explicit in rule text).
        return decide([req(
            "model_placement", RequirementStatus.UNKNOWN, "front_view_not_submitted",
            "正面视图未提交，无法核验主型号是否位于包装正面", [])], plan=plan)
    if not targets:
        return decide([req(
            "model_placement", RequirementStatus.UNKNOWN, "model_label_unobserved",
            "未观察到主型号标签（目标标记本身是否存在无法确认），按证据不足处理", [])],
            plan=plan)

    # Scan ALL model labels: any label on the front satisfies; a model label
    # only on back/side rejects (even when a front package view was submitted).
    on_front = on_wrong_face = view_unknown = None
    for e in targets:
        s, v, obs = read(facts, e.entity_id, "view_type")
        if s == ObservationStatus.READABLE and v == "front":
            on_front = obs
        elif s == ObservationStatus.READABLE and v in ("back", "side"):
            on_wrong_face = (v, obs)
        else:
            view_unknown = obs
    if on_front is not None:
        return decide([req(
            "model_placement", RequirementStatus.SATISFIED, "model_on_front",
            "主型号标签位于任务指定的包装正面（字段值可读性不在此要求内）",
            ev(on_front, "标签位于正面", quote))], plan=plan)
    if on_wrong_face is not None:
        v, obs = on_wrong_face
        return decide([req(
            "model_placement", RequirementStatus.VIOLATED, "model_not_on_front",
            f"主型号标签仅出现在包装{v}，不满足正面位置要求",
            ev(obs, f"标签位于{v}", quote))], plan=plan)
    # 标记位置/视图身份无法确认 -> ambiguous.
    return decide([req(
        "model_placement", RequirementStatus.UNKNOWN, "label_view_unknown",
        "主型号标签所在的视图类型无法确认，按证据不足处理",
        ev(view_unknown, "视图类型不确定", quote))], plan=plan)


# ---------------------------------------------------------------------------
# R020 序列号位于商品本体
# ---------------------------------------------------------------------------


def evaluate_r020(task: AuditInput, plan: F04Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    targets = [e for e in facts.entities_of_kind(EntityKind.OBJECT)
               if facts.claims(e.entity_id, "serial_carrier")]
    if not targets:
        return decide([req(
            "serial_placement", RequirementStatus.UNKNOWN, "serial_carrier_unobserved",
            "未观察到序列号载体信息，按证据不足处理", [])], plan=plan)
    e = targets[0]
    s, v, obs = read(facts, e.entity_id, "serial_carrier")
    if s != ObservationStatus.READABLE:
        return decide([req(
            "serial_placement", RequirementStatus.UNKNOWN, "serial_carrier_unknown",
            "序列号载体（商品本体/外包装/说明纸）无法确认，按证据不足处理",
            ev(obs, "载体不确定", quote))], plan=plan)
    carrier = annotation_nfkc_strip(str(v))
    if carrier == "product_body":
        return decide([req(
            "serial_placement", RequirementStatus.SATISFIED, "serial_on_product_body",
            "序列号直接标记或固定在商品本体上（字段值可读性不在此要求内）",
            ev(obs, "序列号位于商品本体", quote))], plan=plan)
    if carrier in ("outer_package", "instruction_paper"):
        name = "外包装" if carrier == "outer_package" else "可自由取下的说明纸"
        return decide([req(
            "serial_placement", RequirementStatus.VIOLATED, "serial_wrong_carrier",
            f"序列号仅出现在{name}，不满足商品本体标记要求",
            ev(obs, f"序列号位于{carrier}", quote))], plan=plan)
    return decide([req(
        "serial_placement", RequirementStatus.UNKNOWN, "serial_carrier_unrecognized",
        f"未知序列号载体 {v!r}，按证据不足处理", ev(obs, "载体未知", quote))], plan=plan)


# ---------------------------------------------------------------------------
# R021 型号与编号位于同一标签
# ---------------------------------------------------------------------------


def evaluate_r021(task: AuditInput, plan: F04Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    labels = facts.entities_of_kind(EntityKind.LABEL)
    if not labels:
        return decide([req(
            "same_label", RequirementStatus.UNKNOWN, "labels_unobserved",
            "未观察到标签实体，证据不足", [])], plan=plan)
    both = []
    model_only = number_only = 0
    unknown_identity = False
    for e in labels:
        has_model = bool(facts.claims(e.entity_id, "model"))
        has_number = bool(facts.claims(e.entity_id, "material_number"))
        if has_model and has_number:
            both.append(e)
            continue
        if has_model:
            model_only += 1
        elif has_number:
            number_only += 1
        else:
            unknown_identity = True
    if both:
        e = both[0]
        return decide([req(
            "same_label", RequirementStatus.SATISFIED, "fields_on_same_label",
            f"型号与材料编号位于同一张具有可辨认边界的标签（{e.entity_id}）", [])],
            plan=plan)
    if model_only and number_only:
        # 分散在两张标签上，即使同框也不满足要求; separate entities never merge.
        return decide([req(
            "same_label", RequirementStatus.VIOLATED, "fields_split_across_labels",
            "型号与材料编号分散在两张不同标签上，即使同框也不满足要求", [])],
            plan=plan)
    return decide([req(
        "same_label", RequirementStatus.UNKNOWN, "label_identity_unknown",
        "标签上字段的身份无法完整确认，按证据不足处理" if unknown_identity
        else "同框标签中未能同时确认两字段身份，按证据不足处理", [])], plan=plan)


# ---------------------------------------------------------------------------
# R022 小包装附签例外
# ---------------------------------------------------------------------------


def evaluate_r022(task: AuditInput, plan: F04Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    edge_raw = metadata_value(task, "package_longest_edge_mm")
    if edge_raw is None:
        # 尺寸缺失判为 ambiguous，不从照片估计 (explicit in rule text).
        return decide([req(
            "number_carrier", RequirementStatus.UNKNOWN, "package_size_missing",
            "package_longest_edge_mm 缺失，无法判断 80 mm 例外是否可用", [])],
            plan=plan)
    try:
        edge = decimal_of(edge_raw)
    except ValueError:
        return decide([req(
            "number_carrier", RequirementStatus.UNKNOWN, "package_size_invalid",
            f"申报包装最长边值 {edge_raw!r} 非法，无法比较", [])], plan=plan)

    targets = [e for e in facts.entities_of_kind(EntityKind.LABEL)
               if facts.claims(e.entity_id, "material_number")]
    if not targets:
        return decide([req(
            "number_carrier", RequirementStatus.UNKNOWN, "number_label_unobserved",
            "未观察到承载材料编号的标签，证据不足", [])], plan=plan)
    e = targets[0]
    s, v, obs = read(facts, e.entity_id, "carrier")
    if s != ObservationStatus.READABLE:
        return decide([req(
            "number_carrier", RequirementStatus.UNKNOWN, "number_carrier_unknown",
            "材料编号所在载体无法确认，按证据不足处理", ev(obs, "载体不确定", quote))],
            plan=plan)
    if v == "outer_package":
        return decide([req(
            "number_carrier", RequirementStatus.SATISFIED, "number_on_outer_package",
            f"包装最长边 {edge} mm ≥ 阈值 {plan.small_tag_threshold_mm} mm，"
            "材料编号位于外包装，符合要求", ev(obs, "编号在外包装", quote))],
            plan=plan)
    if v == "attached_tag":
        if not number_strictly_less(edge, plan.small_tag_threshold_mm):
            return decide([req(
                "number_carrier", RequirementStatus.VIOLATED, "tag_not_allowed_for_size",
                f"包装最长边 {edge} mm 不小于阈值 {plan.small_tag_threshold_mm} mm，"
                "材料编号须位于外包装", ev(obs, "编号在附签", quote))], plan=plan)
        conn_s, conn_v, conn_obs = read(facts, e.entity_id, "attached_to_package")
        if conn_s == ObservationStatus.READABLE and conn_v is True:
            return decide([req(
                "number_carrier", RequirementStatus.SATISFIED, "number_on_fixed_tag",
                f"包装最长边 {edge} mm 小于 {plan.small_tag_threshold_mm} mm，"
                "材料编号位于固定附签，符合要求",
                ev(conn_obs, "附签已固定", quote))], plan=plan)
        if conn_s == ObservationStatus.CONFIRMED_MISSING:
            return decide([req(
                "number_carrier", RequirementStatus.VIOLATED, "tag_not_fixed",
                "附签未固定在包装上，不满足固定附签例外", ev(conn_obs, "未固定", quote))],
                plan=plan)
        return decide([req(
            "number_carrier", RequirementStatus.UNKNOWN, "tag_fixation_unknown",
            "附签是否固定在包装上无法确认，按证据不足处理",
            ev(conn_obs, "固定关系不确定", quote))], plan=plan)
    return decide([req(
        "number_carrier", RequirementStatus.UNKNOWN, "number_carrier_unrecognized",
        f"未知载体类型 {v!r}，按证据不足处理", ev(obs, "载体未知", quote))], plan=plan)


# ---------------------------------------------------------------------------
# R023 标签不得跨越开封断面
# ---------------------------------------------------------------------------


def evaluate_r023(task: AuditInput, plan: F04Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    targets = [e for e in facts.entities_of_kind(EntityKind.LABEL)
               if facts.claims(e.entity_id, "line_crosses_field")]
    if not targets:
        return decide([req(
            "tear_line", RequirementStatus.UNKNOWN, "tear_line_unobserved",
            "未观察到开封切线与字段的关系信息，按证据不足处理", [])], plan=plan)
    e = targets[0]
    s, v, obs = read(facts, e.entity_id, "line_crosses_field")
    if s != ObservationStatus.READABLE or not isinstance(v, list):
        return decide([req(
            "tear_line", RequirementStatus.UNKNOWN, "tear_line_unverifiable",
            "开封切线穿过哪些区域无法确认，按证据不足处理",
            ev(obs, "切线关系不确定", quote))], plan=plan)
    crossed = {annotation_nfkc_strip(str(x)) for x in v}
    target_fields = {"model", "material_number"}
    hit = crossed & target_fields
    if hit:
        return decide([req(
            "tear_line", RequirementStatus.VIOLATED, "tear_line_crosses_field",
            f"开封切线穿过承载型号/材料编号的字段 {sorted(hit)}，不满足要求",
            ev(obs, f"切线穿过 {sorted(hit)}", quote))], plan=plan)
    # 切线只穿过空白边缘可接受 (explicit in rule text).
    return decide([req(
        "tear_line", RequirementStatus.SATISFIED, "tear_line_only_blank_margin",
        "开封切线只穿过空白边缘，未穿过型号/材料编号字段", [])], plan=plan)


# ---------------------------------------------------------------------------
# R024 展示面上半区位置
# ---------------------------------------------------------------------------


def evaluate_r024(task: AuditInput, plan: F04Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    marks = entities_of_kind_with(facts, EntityKind.OBJECT, "relative_position")
    if not marks:
        return decide([req(
            "mark_position", RequirementStatus.UNKNOWN, "mark_unobserved",
            "未观察到练习标志位置信息，按证据不足处理", [])], plan=plan)
    e, s, v, obs = marks[0]
    # 指定标准正视图 is required to judge proportions.
    vs, vv, vobs = read(facts, e.entity_id, "view_type")
    if vs != ObservationStatus.READABLE or vv != "front":
        # 非标准正视图不足以判定比例 -> ambiguous.
        return decide([req(
            "mark_position", RequirementStatus.UNKNOWN, "non_standard_view",
            "视图不是指定标准正视图，不足以判定上半区比例",
            ev(vobs, "视图非标准正视图", quote))], plan=plan)
    if s != ObservationStatus.READABLE:
        return decide([req(
            "mark_position", RequirementStatus.UNKNOWN, "mark_position_unverifiable",
            "标志中心点位置无法确认，按证据不足处理", ev(obs, "位置不确定", quote))],
            plan=plan)
    pos = annotation_nfkc_strip(str(v))
    if pos in ("upper_half", "on_midline"):
        # 正好在水平中线允许 (explicit in rule text).
        return decide([req(
            "mark_position", RequirementStatus.SATISFIED, "mark_in_upper_half",
            f"标准正视图中练习标志中心点位于展示面上半区（{pos}，中线允许）", [])],
            plan=plan)
    if pos == "lower_half":
        return decide([req(
            "mark_position", RequirementStatus.VIOLATED, "mark_in_lower_half",
            "练习标志中心点位于展示面下半区，不满足要求", ev(obs, "位于下半区", quote))],
            plan=plan)
    return decide([req(
        "mark_position", RequirementStatus.UNKNOWN, "mark_position_unrecognized",
        f"未知位置值 {v!r}，按证据不足处理", ev(obs, "位置未知", quote))], plan=plan)


PLAN_CLASS = F04Plan

EVALUATORS: dict[str, Any] = {
    "R019": evaluate_r019,
    "R020": evaluate_r020,
    "R021": evaluate_r021,
    "R022": evaluate_r022,
    "R023": evaluate_r023,
    "R024": evaluate_r024,
}

__all__ = ["F04Plan", "PLAN_CLASS", "APPLICABILITY", "EVALUATORS"]
