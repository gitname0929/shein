"""F01 实拍证据完整性 (R001-R006).

Rules: R001 包装主视图完整性, R002 声明检查面覆盖, R003 证明文件页码齐全,
R004 附签连接关系取证, R005 遮挡信息补证, R006 可变信息使用实拍证据.
All wording comes verbatim from resources/rules/进阶规则60条.jsonl plus the
口径补充说明 (page_index vs printed page numbers, R005 business-anchor
identity, design/render source semantics).
"""
from __future__ import annotations

from typing import Any

from o2_audit.domain.models import AuditInput, EntityKind, ObservationStatus, RequirementStatus
from o2_audit.operators.text import annotation_nfkc_strip, material_number_equal, model_equal
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


class F01Plan(RulePlan):
    """F01 plans; no numeric parameters — the family is evidence-shape only."""


# ---------------------------------------------------------------------------
# applicability (metadata-level only; observation-conditional branches stay
# inside the evaluators and may still return not_applicable outcomes)
# ---------------------------------------------------------------------------


def applicability_r001(task: AuditInput, plan: F01Plan) -> tuple[str, list[str]]:
    """R001 applies to units WITH an outer package; only an explicit
    ``has_outer_package=False`` declaration removes applicability."""
    v = metadata_value(task, "has_outer_package")
    if v is False:
        return "not_applicable", []
    return "applicable", []


def applicability_r006(task: AuditInput, plan: F01Plan) -> tuple[str, list[str]]:
    v = metadata_value(task, "has_variable_info")
    if v is False:
        return "not_applicable", []
    return "applicable", []


def _always_applicable(task: AuditInput, plan: F01Plan) -> tuple[str, list[str]]:
    return "applicable", []


APPLICABILITY: dict[str, Any] = {
    "R001": applicability_r001,
    "R002": _always_applicable,
    "R003": _always_applicable,
    "R004": _always_applicable,
    "R005": _always_applicable,
    "R006": applicability_r006,
}


def _has_material_role(task: AuditInput, plan: F01Plan, roles: tuple[str, ...]) -> bool:
    return any(m.role in roles for m in task.materials)


# ---------------------------------------------------------------------------
# R001 包装主视图完整性
# ---------------------------------------------------------------------------


def evaluate_r001(task: AuditInput, plan: F01Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    found = entities_of_kind_with(facts, EntityKind.PACKAGE, "boundary_visible")
    if not found:
        if not _has_material_role(task, plan, plan.material_roles):
            # Rule text: 未提交主视图判为 reject (explicit missing = violation).
            return decide([req(
                "main_view", RequirementStatus.VIOLATED, "main_view_not_submitted",
                "任务未提交任何可用于核验包装主面四边界的实拍材料，判为不符合", [])],
                plan=plan, reject_reason="未提交主视图")
        return decide([req(
            "main_view", RequirementStatus.UNKNOWN, "main_view_unobserved",
            "已提交材料中未记录到主视图边界观察，证据不足", [])], plan=plan)

    for _e, status, value, obs in found:
        if status == ObservationStatus.CONFIRMED_MISSING:
            return decide([req(
                "main_view", RequirementStatus.VIOLATED, "main_view_confirmed_absent",
                "完整清晰检查范围内确认未包含包装主视图", ev(obs, "确认无主视图", quote))],
                plan=plan)
    for _e, status, _v, obs in found:
        if status != ObservationStatus.READABLE:
            # 遮挡/画质导致边界不能确认 -> ambiguous (never inferred absence).
            return decide([req(
                "main_view", RequirementStatus.UNKNOWN, "boundary_unverifiable",
                "遮挡或画质导致无法确认主面四个边界是否完整", ev(obs, "边界不可确认", quote))],
                plan=plan)
    for _e, status, value, obs in found:
        if value is False:
            return decide([req(
                "main_view", RequirementStatus.VIOLATED, "boundary_cropped",
                "主视图中至少一个边界被明确裁去", ev(obs, "边界被裁去", quote))],
                plan=plan)
    # boundary_visible=True: interior text obstruction does NOT affect the
    # four-boundary check (口径补充说明: 内部文字被遮挡不等于主面边界被遮挡).
    first = found[0]
    interior_status, interior_value, interior_obs = read(
        facts, first[0].entity_id, "interior_obstructed")
    results = [req(
        "main_view", RequirementStatus.SATISFIED, "boundary_visible",
        "包装主面四个边界完整可见",
        ev(first[3], "主面四边界可见", quote))]
    if interior_status == ObservationStatus.READABLE and interior_value is True:
        results.append(req(
            "interior", RequirementStatus.SATISFIED, "interior_obstruction_irrelevant",
            "内部内容被遮挡不影响主面边界完整性要求", ev(interior_obs, "内部遮挡")))
    return decide(results, plan=plan)


# ---------------------------------------------------------------------------
# R002 声明检查面覆盖
# ---------------------------------------------------------------------------


def evaluate_r002(task: AuditInput, plan: F01Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    required = metadata_value(task, "required_faces")
    results: list = []
    if not isinstance(required, list) or not required:
        # 口径: R002缺required_faces属于applicable下的证据不足 -> ambiguous.
        results.append(req(
            "face_coverage", RequirementStatus.UNKNOWN, "required_faces_missing",
            "任务未提供 required_faces 检查面清单，无法核验覆盖情况", []))
        return decide(results, plan=plan)

    found = entities_of_kind_with(facts, EntityKind.PACKAGE, "visible_faces")
    if not found:
        if not _has_material_role(task, plan, plan.material_roles):
            results.append(req(
                "face_coverage", RequirementStatus.VIOLATED, "face_photos_not_submitted",
                "未提交任何包装实拍材料，声明的检查面无法覆盖", []))
            return decide(results, plan=plan)
        results.append(req(
            "face_coverage", RequirementStatus.UNKNOWN, "faces_unobserved",
            "已提交材料中未记录到检查面识别观察，证据不足", []))
        return decide(results, plan=plan)

    for _e, status, _v, obs in found:
        if status != ObservationStatus.READABLE:
            results.append(req(
                "face_coverage", RequirementStatus.UNKNOWN, "face_coverage_incomplete",
                "检查面识别范围不完整，无法确认全部声明面已覆盖",
                ev(obs, "检查面识别不完整", quote)))
            return decide(results, plan=plan)

    covered: set[str] = set()
    for _e, _s, value, obs in found:
        if isinstance(value, list):
            covered.update(annotation_nfkc_strip(str(f)) for f in value)
    missing = [f for f in required
               if annotation_nfkc_strip(str(f)) not in covered]
    if missing:
        results.append(req(
            "face_coverage", RequirementStatus.VIOLATED, "required_face_missing",
            f"明确缺少声明的检查面实拍图: {missing}",
            [e for _e, _s, _v, o in found for e in ev(o, "检查面覆盖记录", quote)]))
        return decide(results, plan=plan)
    results.append(req(
        "face_coverage", RequirementStatus.SATISFIED, "faces_covered",
        f"声明的全部检查面 {required} 均已被实拍图覆盖（一张图可覆盖多面）",
        [e for _e, _s, _v, o in found for e in ev(o, "检查面覆盖记录", quote)]))
    return decide(results, plan=plan)


# ---------------------------------------------------------------------------
# R003 证明文件页码齐全
# ---------------------------------------------------------------------------


def evaluate_r003(task: AuditInput, plan: F01Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    total = metadata_value(task, "certificate_total_pages")
    results: list = []
    if isinstance(total, bool) or not isinstance(total, int) or total <= 0:
        # 口径: 参数不可确认时仍为 applicable，证据不足 -> ambiguous.
        results.append(req(
            "page_numbers", RequirementStatus.UNKNOWN, "total_pages_missing",
            "任务未提供合法的证明文件总页数 N，无法核验页码齐全", []))
        return decide(results, plan=plan)

    pages = entities_of_kind_with(facts, EntityKind.DOCUMENT, "printed_page_number")
    submitted = [m for m in task.materials if m.role == "certificate_pages"]
    if not pages:
        if not submitted:
            results.append(req(
                "page_numbers", RequirementStatus.VIOLATED, "certificate_pages_not_submitted",
                f"任务要求提交第 1 至 {total} 页，实际未提交任何证明文件页", []))
            return decide(results, plan=plan)
        results.append(req(
            "page_numbers", RequirementStatus.UNKNOWN, "page_numbers_unobserved",
            "已提交证明文件页尚未记录页码观察，证据不足", []))
        return decide(results, plan=plan)

    numbers: list[int] = []
    for _e, status, value, obs in pages:
        if status != ObservationStatus.READABLE:
            # 页码不可读判为 ambiguous (explicit in rule text).
            results.append(req(
                "page_numbers", RequirementStatus.UNKNOWN, "printed_page_number_unreadable",
                "某页图内印刷页码不可读，按证据不足处理", ev(obs, "页码不可读", quote)))
            return decide(results, plan=plan)
        if isinstance(value, bool) or not isinstance(value, int):
            results.append(req(
                "page_numbers", RequirementStatus.UNKNOWN, "printed_page_number_invalid",
                f"图内印刷页码 {value!r} 不是合法整数页码", ev(obs, "页码非法", quote)))
            return decide(results, plan=plan)
        numbers.append(value)

    out_of_range = [n for n in numbers if n < 1 or n > total]
    if out_of_range:
        results.append(req(
            "page_numbers", RequirementStatus.VIOLATED, "page_number_out_of_range",
            f"图内印刷页码 {out_of_range} 超出 1 至 {total} 范围", []))
        return decide(results, plan=plan)
    if len(set(numbers)) != len(numbers):
        results.append(req(
            "page_numbers", RequirementStatus.VIOLATED, "duplicate_page_numbers",
            f"不同页面出现重复的印刷页码 {sorted(numbers)}，说明存在缺页", []))
        return decide(results, plan=plan)
    missing = [n for n in range(1, total + 1) if n not in set(numbers)]
    if missing:
        # source page_index is irrelevant here: 多个PNG的page_index=1不代表重复页.
        results.append(req(
            "page_numbers", RequirementStatus.VIOLATED, "page_missing",
            f"图内印刷页码缺少 {missing}，明确缺页"
            + (f"（仅提交 {len(submitted)} 页材料）" if len(submitted) < total else ""), []))
        return decide(results, plan=plan)
    results.append(req(
        "page_numbers", RequirementStatus.SATISFIED, "pages_complete",
        f"图内印刷页码 1 至 {total} 齐全且可核对", []))
    return decide(results, plan=plan)


# ---------------------------------------------------------------------------
# R004 附签连接关系取证
# ---------------------------------------------------------------------------


def evaluate_r004(task: AuditInput, plan: F01Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    # Applicability is observation-conditional: 附签承载审核字段时.
    tag_candidates = [e for e in facts.entities_of_kind(EntityKind.LABEL)
                      if facts.claims(e.entity_id, "attached_to_package")
                      or (facts.claims(e.entity_id, "carrier")
                          and read(facts, e.entity_id, "carrier")[1] == "attached_tag")]
    if not tag_candidates:
        if facts.all_entities():
            return not_applicable_outcome(
                "未观察到使用附签承载审核字段，本条不施加约束")
        return decide([req(
            "tag_connection", RequirementStatus.UNKNOWN, "tag_usage_unobserved",
            "未观察到任何标签载体信息，无法确定是否适用附签取证要求", [])], plan=plan)

    for tag in tag_candidates:
        status, value, obs = read(facts, tag.entity_id, "attached_to_package")
        if status == ObservationStatus.READABLE and value is True:
            return decide([req(
                "tag_connection", RequirementStatus.SATISFIED, "tag_connection_shown",
                "已有实拍图同时显示附签、连接件与相连的商品或包装",
                ev(obs, "附签连接关系已确认", quote))], plan=plan)
        if status == ObservationStatus.CONFIRMED_MISSING:
            return decide([req(
                "tag_connection", RequirementStatus.VIOLATED, "detached_tag_only",
                "仅提交脱离载体的附签照片，未取得连接关系取证",
                ev(obs, "确认无连接关系画面", quote))], plan=plan)
        # unreadable / incomplete / identity unknown / not yet observed
        return decide([req(
            "tag_connection", RequirementStatus.UNKNOWN, "tag_connection_unverifiable",
            "附签与商品/包装的连接关系无法确认，证据不足",
            ev(obs, "连接关系不确定", quote))], plan=plan)
    return decide([], plan=plan)  # pragma: no cover - loop always returns


# ---------------------------------------------------------------------------
# R005 遮挡信息补证
# ---------------------------------------------------------------------------


def evaluate_r005(task: AuditInput, plan: F01Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    # Target field entities: any label whose interior is confirmed obstructed.
    # read() returns a 3-tuple — compare only (status, value).
    obstructed = [e for e in facts.entities_of_kind(EntityKind.LABEL)
                  if read(facts, e.entity_id, "interior_obstructed")[:2]
                  == (ObservationStatus.READABLE, True)]
    if not obstructed:
        blocked = entities_of_kind_with(facts, EntityKind.LABEL, "interior_obstructed")
        if blocked:
            status, value = blocked[0][1], blocked[0][2]
            if status == ObservationStatus.CONFIRMED_MISSING or (
                    status == ObservationStatus.READABLE and value is False):
                return decide([req(
                    "obstruction", RequirementStatus.SATISFIED, "no_obstruction",
                    "完整清晰范围内确认主视图无遮挡，无需补充视图", ev(blocked[0][3], "确认无遮挡"))],
                    plan=plan)
            return decide([req(
                "obstruction", RequirementStatus.UNKNOWN, "obstruction_unverifiable",
                "主视图是否被遮挡无法确认，证据不足",
                ev(blocked[0][3], "遮挡状态不确定", quote))], plan=plan)
        return decide([req(
            "obstruction", RequirementStatus.UNKNOWN, "obstruction_unobserved",
            "未观察到遮挡状态信息，证据不足", [])], plan=plan)

    target = obstructed[0]
    # Business anchor of the target field (型号/材料编号等可见业务锚点).
    anchor_status = anchor_value = None
    anchor_pred = None
    cmp_fn_used = None
    for pred, cmp_fn in (("material_number", material_number_equal), ("model", model_equal)):
        s, v, _o = read(facts, target.entity_id, pred)
        if s == ObservationStatus.READABLE and v is not None:
            anchor_status, anchor_value, anchor_pred, cmp_fn_used = s, v, pred, cmp_fn
            break

    supplements = [e for e in facts.entities_of_kind(EntityKind.LABEL)
                   if e.entity_id != target.entity_id
                   and facts.claims(e.entity_id, "supplement_field_identity")]
    results: list = []
    if not supplements:
        s, _v, obs = read(facts, target.entity_id, "supplement_field_identity")
        if s == ObservationStatus.CONFIRMED_MISSING:
            results.append(req(
                "supplement", RequirementStatus.VIOLATED, "obstructed_without_supplement",
                "已明确遮挡且确认未提供同一字段的无遮挡补充视图",
                ev(obs, "确认未补图", quote)))
            return decide(results, plan=plan)
        results.append(req(
            "supplement", RequirementStatus.UNKNOWN, "supplement_presence_unknown",
            "已明确遮挡，但是否提供补充视图无法确认，证据不足", []))
        return decide(results, plan=plan)

    # Scan ALL supplement candidates: one unobstructed same-field supplement
    # is enough; an obstructed same-field supplement is an explicit failure;
    # uncertain identity/anchor stays ambiguous; all clearly different
    # business anchors cannot repair the target.
    def obstruction_of(e) -> str:
        """'clear' | 'blocked' | 'unknown' for one supplement entity."""
        s, v, _o = read(facts, e.entity_id, "interior_obstructed")
        if s == ObservationStatus.CONFIRMED_MISSING:
            return "clear"
        if s == ObservationStatus.READABLE:
            return "blocked" if v is True else "clear"
        return "unknown"

    matched: list[tuple[Any, str, Any]] = []  # (entity, obstruction, identity obs)
    identity_unreadable = False
    for sup in supplements:
        s_status, s_value, s_obs = read(facts, sup.entity_id, "supplement_field_identity")
        if s_status != ObservationStatus.READABLE:
            # 补图是否同一字段不能确认 -> ambiguous (never a decorative ID match).
            identity_unreadable = True
            continue
        if anchor_status is not None and cmp_fn_used is not None \
                and cmp_fn_used(str(s_value), str(anchor_value)):
            matched.append((sup, obstruction_of(sup), s_obs))

    if any(o == "clear" for _s, o, _ob in matched):
        results.append(req(
            "supplement", RequirementStatus.SATISFIED, "supplement_matches_target",
            f"补充视图经业务锚点（{anchor_pred}={anchor_value}）确认与被遮挡字段为"
            "同一字段且无遮挡", []))
        return decide(results, plan=plan)
    if any(o == "blocked" for _s, o, _ob in matched):
        results.append(req(
            "supplement", RequirementStatus.VIOLATED, "supplement_obstructed",
            "补充视图自身同样被遮挡，属于明确没有合格补图", []))
        return decide(results, plan=plan)
    if identity_unreadable:
        results.append(req(
            "supplement", RequirementStatus.UNKNOWN, "supplement_identity_ambiguous",
            "补充视图与被遮挡字段是否同一业务字段不能确认（档案号等装饰编号不作为关联依据）",
            []))
        return decide(results, plan=plan)
    if anchor_status is None or cmp_fn_used is None:
        # Target anchor unreadable AND supplement identity readable: the
        # supplement exists but the same-field link cannot be verified.
        results.append(req(
            "supplement", RequirementStatus.UNKNOWN, "target_anchor_unreadable",
            "被遮挡字段的业务锚点不可读，无法确认补图与目标字段的同一性", []))
        return decide(results, plan=plan)
    if matched:
        # all matched supplements have unverifiable obstruction status
        results.append(req(
            "supplement", RequirementStatus.UNKNOWN, "supplement_obstruction_unverifiable",
            "同一字段的补充视图是否被遮挡无法确认，按证据不足处理", []))
        return decide(results, plan=plan)
    results.append(req(
        "supplement", RequirementStatus.VIOLATED, "supplement_different_field",
        "补充视图的业务锚点与目标字段锚点明确不同，不能补同一目标", []))
    return decide(results, plan=plan)


# ---------------------------------------------------------------------------
# R006 可变信息使用实拍证据
# ---------------------------------------------------------------------------


def evaluate_r006(task: AuditInput, plan: F01Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    found = entities_of_kind_with(facts, EntityKind.DOCUMENT, "material_authenticity")
    results: list = []
    if not found:
        if not _has_material_role(task, plan, plan.material_roles):
            results.append(req(
                "source_type", RequirementStatus.VIOLATED, "no_photo_evidence_submitted",
                "批次号/序列号只能由实拍图作为证据，任务未提交任何可承载实拍证据的材料", []))
            return decide(results, plan=plan)
        results.append(req(
            "source_type", RequirementStatus.UNKNOWN, "source_type_unobserved",
            "已提交材料的来源类型尚未记录观察，证据不足", []))
        return decide(results, plan=plan)

    values: list[str] = []
    for _e, status, value, obs in found:
        if status != ObservationStatus.READABLE:
            results.append(req(
                "source_type", RequirementStatus.UNKNOWN, "source_type_unreadable",
                "材料来源类型信息不可读，证据不足", ev(obs, "来源类型不可读", quote)))
            return decide(results, plan=plan)
        v = str(value)
        if v not in ("photo", "design", "render"):
            # 文件来源类型未知 -> ambiguous.
            results.append(req(
                "source_type", RequirementStatus.UNKNOWN, "source_type_unknown",
                "文件来源类型未知，无法确认可变信息来自实拍图",
                ev(obs, f"来源类型为 {v}", quote)))
            return decide(results, plan=plan)
        values.append(v)
    if any(v == "photo" for v in values):
        results.append(req(
            "source_type", RequirementStatus.SATISFIED, "photo_evidence_present",
            "存在实拍图材料，可变信息可由实拍图承载",
            [e for _e, _s, _v, o in found for e in ev(o, "来源类型记录", quote)][:3]))
        return decide(results, plan=plan)
    results.append(req(
        "source_type", RequirementStatus.VIOLATED, "design_render_only",
        "材料仅包含标为设计稿或渲染图的文件，不能作为批次号/序列号的本任务证据",
        [e for _e, _s, _v, o in found for e in ev(o, "来源类型记录", quote)]))
    return decide(results, plan=plan)


PLAN_CLASS = F01Plan

EVALUATORS: dict[str, Any] = {
    "R001": evaluate_r001,
    "R002": evaluate_r002,
    "R003": evaluate_r003,
    "R004": evaluate_r004,
    "R005": evaluate_r005,
    "R006": evaluate_r006,
}

__all__ = ["F01Plan", "PLAN_CLASS", "APPLICABILITY", "EVALUATORS"]
