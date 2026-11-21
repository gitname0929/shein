"""F09 跨材料关系核验 (R049-R054).

R049 材料编号跨源一致, R050 套装组件逐项对应, R051 批次号在允许区间内,
R052 包装规格与证明规格对应, R053 证明附图文字锚点匹配, R054 共同编号不同主体冲突.
Keyed correspondences (component_id->model, model->spec) never degrade into
independent set equality.
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
from o2_audit.operators.numbers import decimal_of, in_interval
from o2_audit.operators.sets import dict_correspondence
from o2_audit.operators.text import (
    annotation_nfkc_strip,
    material_number_equal,
    model_equal,
)
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


class F09Plan(RulePlan):
    """F09 typed plans; comparisons reuse the 总则 text/model semantics."""


APPLICABILITY: dict[str, Any] = {
    rid: (lambda task, plan: ("applicable", []))
    for rid in ("R049", "R050", "R051", "R052", "R053", "R054")
}


def _certs_with(facts: FactView, predicate: str):
    return entities_of_kind_with(facts, EntityKind.CERTIFICATE, predicate)


# ---------------------------------------------------------------------------
# R049 材料编号跨源一致
# ---------------------------------------------------------------------------


def evaluate_r049(task: AuditInput, plan: F09Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    declared = metadata_value(task, "declared_material_number")
    results: list = []
    if declared is None:
        results.append(req(
            "three_way_number", RequirementStatus.UNKNOWN, "declared_number_missing",
            "商品申报的材料编号缺失，无法三方核对", []))
        return decide(results, plan=plan)
    declared_s = str(declared)

    label_found = [t for t in entities_of_kind_with(facts, EntityKind.LABEL, "material_number")
                   if t[1] == ObservationStatus.READABLE]
    cert_found = [t for t in _certs_with(facts, "material_number")
                  if t[1] == ObservationStatus.READABLE]
    if not label_found or not cert_found:
        # 未观察到全部三方 -> 证据不足 (仅任意两方一致也不满足本条).
        results.append(req(
            "three_way_number", RequirementStatus.UNKNOWN, "three_way_unobserved",
            "包装、证明首页或商品申报的材料编号尚未全部观察到，无法三方核对", []))
        return decide(results, plan=plan)
    label_number = str(label_found[0][2])
    cert_number = str(cert_found[0][2])
    if (material_number_equal(label_number, cert_number)
            and material_number_equal(label_number, declared_s)
            and material_number_equal(cert_number, declared_s)):
        results.append(req(
            "three_way_number", RequirementStatus.SATISFIED, "three_way_consistent",
            f"包装、证明首页与商品申报的材料编号三方一致（{declared_s!r}）", []))
        return decide(results, plan=plan)
    results.append(req(
        "three_way_number", RequirementStatus.VIOLATED, "three_way_inconsistent",
        f"三方材料编号不一致（包装 {label_number!r}，证明 {cert_number!r}，"
        f"申报 {declared_s!r}）；仅任意两方一致不能满足本条", []))
    return decide(results, plan=plan)


# ---------------------------------------------------------------------------
# R050 套装组件逐项对应
# ---------------------------------------------------------------------------


def _declared_component_map(task: AuditInput) -> dict[str, str] | None:
    """Resolve R050 declared component mapping from either dict form
    (``declared_component_map``) or public list-of-dicts form
    (``declared_components: [{"component_id": ..., "model": ...}]``)."""
    raw_map = metadata_value(task, "declared_component_map")
    if isinstance(raw_map, dict) and raw_map:
        return {str(k): str(v) for k, v in raw_map.items()}
    raw_list = metadata_value(task, "declared_components")
    if isinstance(raw_list, list) and raw_list:
        out: dict[str, str] = {}
        for item in raw_list:
            if not isinstance(item, dict):
                return None
            cid = item.get("component_id")
            model = item.get("model")
            if cid is None or model is None:
                return None
            out[str(cid)] = str(model)
        return out if out else None
    return None


def evaluate_r050(task: AuditInput, plan: F09Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    declared_norm = _declared_component_map(task)
    if not declared_norm:
        return decide([req(
            "component_pairs", RequirementStatus.UNKNOWN, "declared_map_missing",
            "任务未提供申报组件清单（component_id 与型号配对），无法逐项对应", [])],
            plan=plan)
    found = _certs_with(facts, "component_model_map")
    if not found:
        return decide([req(
            "component_pairs", RequirementStatus.UNKNOWN, "component_table_unobserved",
            "未观察到证明文件组件表，证据不足", [])], plan=plan)
    _e, status, value, obs = found[0]
    if status != ObservationStatus.READABLE or not isinstance(value, dict):
        return decide([req(
            "component_pairs", RequirementStatus.UNKNOWN, "component_table_unreadable",
            "证明文件组件表读不清，按证据不足处理", ev(obs, "组件表不可读", quote))],
            plan=plan)
    # Keyed correspondence: component_id -> model must pair up exactly
    # (dict_correspondence, NOT independent set equality of ids or models).
    printed_norm = {str(k): str(v) for k, v in value.items()}
    pairs_match = dict_correspondence(declared_norm, printed_norm,
                                      value=lambda a, b: model_equal(a, b))
    if pairs_match:
        return decide([req(
            "component_pairs", RequirementStatus.SATISFIED, "component_pairs_match",
            "证明组件表的 component_id 与型号配对和申报清单逐项一致",
            ev(obs, f"组件表 {printed_norm}", quote))], plan=plan)
    same_model_set = (set(declared_norm.values()) == set(printed_norm.values())
                      and set(declared_norm.keys()) == set(printed_norm.keys()))
    reason = ("型号集合相同但对应到不同组件 ID，仍不符合"
              if same_model_set else
              f"组件配对不一致（申报 {declared_norm!r}，证明 {printed_norm!r}）")
    return decide([req(
        "component_pairs", RequirementStatus.VIOLATED, "component_pairs_mismatch",
        f"证明文件组件表与申报清单不是逐项一致的配对：{reason}",
        ev(obs, f"组件表 {printed_norm}", quote))], plan=plan)


# ---------------------------------------------------------------------------
# R051 批次号在允许区间内
# ---------------------------------------------------------------------------


def evaluate_r051(task: AuditInput, plan: F09Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    batch_found = [t for t in entities_of_kind_with(facts, EntityKind.LABEL, "batch_number")
                   if t[1] == ObservationStatus.READABLE]
    interval_found = _certs_with(facts, "batch_interval")
    results: list = []
    if not batch_found:
        results.append(req(
            "batch_in_interval", RequirementStatus.UNKNOWN, "batch_unobserved",
            "未观察到包装批次号，证据不足", []))
        return decide(results, plan=plan)
    if not interval_found:
        results.append(req(
            "batch_in_interval", RequirementStatus.UNKNOWN, "interval_unobserved",
            "未观察到证明给出的批次号允许区间，证据不足", []))
        return decide(results, plan=plan)

    batch_raw = batch_found[0][2]
    if isinstance(batch_raw, bool) or not isinstance(batch_raw, (int, str)):
        results.append(req(
            "batch_in_interval", RequirementStatus.VIOLATED, "batch_not_integer",
            f"批次号 {batch_raw!r} 不是非负整数（仅允许非负整数）", []))
        return decide(results, plan=plan)
    try:
        # 前导零不影响本条数值比较: numeric comparison via Decimal/int.
        batch = decimal_of(str(batch_raw).strip())
        if batch != batch.to_integral_value():
            results.append(req(
                "batch_in_interval", RequirementStatus.VIOLATED, "batch_not_integer",
                f"批次号 {batch_raw!r} 不是非负整数", []))
            return decide(results, plan=plan)
        batch = int(batch)
    except ValueError:
        results.append(req(
            "batch_in_interval", RequirementStatus.VIOLATED, "batch_not_integer",
            f"批次号 {batch_raw!r} 不是非负整数", []))
        return decide(results, plan=plan)
    if batch < 0:
        results.append(req(
            "batch_in_interval", RequirementStatus.VIOLATED, "batch_negative",
            f"批次号 {batch} 为负数，仅允许非负整数", []))
        return decide(results, plan=plan)

    _e, status, value, obs = interval_found[0]
    if status != ObservationStatus.READABLE or not isinstance(value, (list, tuple)) \
            or len(value) != 2:
        results.append(req(
            "batch_in_interval", RequirementStatus.UNKNOWN, "interval_unreadable",
            "证明给出的批次号区间读不清，按证据不足处理", ev(obs, "区间不可读", quote)))
        return decide(results, plan=plan)
    try:
        lo = decimal_of(value[0])
        hi = decimal_of(value[1])
    except ValueError:
        results.append(req(
            "batch_in_interval", RequirementStatus.UNKNOWN, "interval_invalid",
            f"区间端点 {value!r} 非法，无法比较", ev(obs, f"区间 {value}", quote)))
        return decide(results, plan=plan)
    if in_interval(Decimal(batch), lo, hi):
        results.append(req(
            "batch_in_interval", RequirementStatus.SATISFIED, "batch_in_interval",
            f"包装批次号 {batch} 位于证明给出的闭区间 [{lo}, {hi}] 内", []))
        return decide(results, plan=plan)
    results.append(req(
        "batch_in_interval", RequirementStatus.VIOLATED, "batch_out_of_interval",
        f"包装批次号 {batch} 不在证明给出的闭区间 [{lo}, {hi}] 内",
        ev(obs, f"区间 [{lo}, {hi}]", quote)))
    return decide(results, plan=plan)


# ---------------------------------------------------------------------------
# R052 包装规格与证明规格对应
# ---------------------------------------------------------------------------


def evaluate_r052(task: AuditInput, plan: F09Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    declared = metadata_value(task, "declared_model")
    if declared is None:
        return decide([req(
            "spec_pairing", RequirementStatus.UNKNOWN, "declared_model_missing",
            "申报型号缺失，无法核验规格对应", [])], plan=plan)
    declared_s = str(declared)
    spec_found = [t for t in entities_of_kind_with(facts, EntityKind.LABEL, "package_spec")
                  if t[1] == ObservationStatus.READABLE]
    map_found = _certs_with(facts, "model_spec_map")
    results: list = []
    if not spec_found:
        results.append(req(
            "spec_pairing", RequirementStatus.UNKNOWN, "package_spec_unobserved",
            "未观察到包装规格，证据不足", []))
        return decide(results, plan=plan)
    if not map_found:
        results.append(req(
            "spec_pairing", RequirementStatus.UNKNOWN, "spec_map_unobserved",
            "未观察到证明文件的型号-规格对应，证据不足", []))
        return decide(results, plan=plan)
    package_spec = str(spec_found[0][2])
    _e, status, value, obs = map_found[0]
    if status != ObservationStatus.READABLE or not isinstance(value, dict):
        results.append(req(
            "spec_pairing", RequirementStatus.UNKNOWN, "spec_map_unreadable",
            "证明文件的型号-规格对应读不清，按证据不足处理", ev(obs, "对应不可读", quote)))
        return decide(results, plan=plan)
    # Keyed association: declared_model -> its OWN spec (never borrowed).
    own = next((str(v) for k, v in value.items() if model_equal(str(k), declared_s)), None)
    if own is None:
        results.append(req(
            "spec_pairing", RequirementStatus.VIOLATED, "spec_not_listed_for_model",
            f"证明文件未给出申报型号 {declared_s!r} 对应的规格；"
            "不得借用同文件另一型号的规格", ev(obs, f"对应 {value}", quote)))
        return decide(results, plan=plan)
    if annotation_nfkc_strip(own) == annotation_nfkc_strip(package_spec):
        results.append(req(
            "spec_pairing", RequirementStatus.SATISFIED, "spec_matches_own_model",
            f"申报型号 {declared_s!r} 在证明文件中对应的规格与包装规格一致"
            f"（{package_spec!r}）", []))
        return decide(results, plan=plan)
    results.append(req(
        "spec_pairing", RequirementStatus.VIOLATED, "spec_mismatch",
        f"证明文件中 {declared_s!r} 对应规格为 {own!r}，与包装规格 {package_spec!r} 不一致",
        ev(obs, f"对应 {value}", quote)))
    return decide(results, plan=plan)


# ---------------------------------------------------------------------------
# R053 证明附图文字锚点匹配
# ---------------------------------------------------------------------------


def evaluate_r053(task: AuditInput, plan: F09Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    pkg = [t for t in entities_of_kind_with(facts, EntityKind.LABEL, "material_number")
           if t[1] == ObservationStatus.READABLE]
    figure = _certs_with(facts, "figure_model")
    if not pkg or not figure:
        return decide([req(
            "figure_anchor", RequirementStatus.UNKNOWN, "anchor_unobserved",
            "包装主型号/材料编号或证明附图可见字段尚未全部观察到，证据不足", [])],
            plan=plan)
    pkg_number = str(pkg[0][2])
    _ms, mv, _mo = read(facts, pkg[0][0].entity_id, "model")
    pkg_model = str(mv) if _ms == ObservationStatus.READABLE else None
    fig_e = figure[0][0]
    fs, fv, fobs = read(facts, fig_e.entity_id, "figure_model")
    ns, nv, nobs = read(facts, fig_e.entity_id, "figure_material_number")
    if fs != ObservationStatus.READABLE or ns != ObservationStatus.READABLE:
        return decide([req(
            "figure_anchor", RequirementStatus.UNKNOWN, "figure_fields_unreadable",
            "证明附图中的可见字段读不清，按证据不足处理", ev(fobs or nobs, "附图字段不可读", quote))],
            plan=plan)
    # 图案/颜色/版式相似不算: 仅文字锚点匹配有效 (explicit in rule text).
    model_ok = pkg_model is not None and model_equal(str(fv), pkg_model)
    number_ok = material_number_equal(str(nv), pkg_number)
    if model_ok and number_ok:
        return decide([req(
            "figure_anchor", RequirementStatus.SATISFIED, "figure_anchor_match",
            "包装实拍图的主型号和材料编号同时与证明附图可见字段匹配", [])], plan=plan)
    return decide([req(
        "figure_anchor", RequirementStatus.VIOLATED, "figure_anchor_mismatch",
        f"附图文字锚点不匹配（附图型号 {fv!r} / 编号 {nv!r}，包装型号 {pkg_model!r} / "
        f"编号 {pkg_number!r}）；只有图案、颜色或版式相似不满足要求",
        ev(fobs, "附图字段", quote))], plan=plan)


# ---------------------------------------------------------------------------
# R054 共同编号不同主体冲突
# ---------------------------------------------------------------------------


def evaluate_r054(task: AuditInput, plan: F09Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    all_certs = _certs_with(facts, "material_number")
    certs = [t for t in all_certs if t[1] == ObservationStatus.READABLE]
    unreadable = [t for t in all_certs
                  if t[1] in (ObservationStatus.UNREADABLE, ObservationStatus.INCOMPLETE_SCOPE,
                              ObservationStatus.IDENTITY_UNKNOWN)]
    results: list = []
    if not certs:
        code = "certificate_number_unreadable" if unreadable else "certificates_unobserved"
        results.append(req(
            "contact_conflict", RequirementStatus.UNKNOWN, code,
            "未观察到材料编号可读的证明文件，无法判断同号多主体冲突" if unreadable
            else "未观察到任何证明文件材料编号，证据不足", []))
        return decide(results, plan=plan)
    if len(certs) == 1:
        if unreadable:
            # 另一份文件编号不可读：不能排除同号冲突，按证据不足处理.
            results.append(req(
                "contact_conflict", RequirementStatus.UNKNOWN, "certificate_number_unreadable",
                "观察到一份编号可读的证明文件，但另一份编号不可读，无法排除同号多主体冲突",
                ev(unreadable[0][3], "编号不可读", quote)))
            return decide(results, plan=plan)
        results.append(req(
            "contact_conflict", RequirementStatus.SATISFIED, "single_active_certificate",
            "仅观察到一份材料编号可读且无其他编号不可读的证明文件，不存在同号多主体问题", []))
        return decide(results, plan=plan)

    active: list[tuple[str, str, Any]] = []
    for e, _s, number, nobs in certs:
        vs, vv, _o = read(facts, e.entity_id, "void_marked")
        if vs == ObservationStatus.READABLE and vv is True:
            continue  # 未作废 documents only
        if vs != ObservationStatus.READABLE and vs != ObservationStatus.CONFIRMED_MISSING:
            results.append(req(
                f"void_status:{e.entity_id}", RequirementStatus.UNKNOWN,
                "void_status_unreadable",
                "某证明文件作废状态不可读，无法判断冲突范围", []))
            return decide(results, plan=plan)
        active.append((e.entity_id, str(number), nobs))
    if len(active) < 2:
        results.append(req(
            "contact_conflict", RequirementStatus.SATISFIED, "single_active_certificate",
            "有效（未作废）证明文件不足两份，不存在同号多主体问题", []))
        return decide(results, plan=plan)

    contacts: dict[str, list[tuple[str, str, Any]]] = {}
    for eid, number, nobs in active:
        cs, cv, cobs = read(facts, eid, "contact_info")
        if cs != ObservationStatus.READABLE:
            results.append(req(
                f"contact:{eid}", RequirementStatus.UNKNOWN, "contact_unreadable",
                "联系方名称读不清，无法判断是否冲突", ev(cobs, "联系方不可读", quote)))
            return decide(results, plan=plan)
        contacts.setdefault(number, []).append((eid, str(cv), cobs or nobs))

    for number, entries in contacts.items():
        names = {annotation_nfkc_strip(n) for _e, n, _o in entries}
        if len(names) > 1:
            # 共同编号不同联系方 -> ambiguous，不自行选择最新上传文件.
            pair = entries[0], entries[1]
            results.append(req(
                "contact_conflict", RequirementStatus.CONFLICT, "same_number_contact_conflict",
                f"两份均未作废且材料编号相同的证明文件（{number!r}）联系方名称不同"
                f"（{pair[0][1]!r} 与 {pair[1][1]!r}），应判为 ambiguous 并指出冲突",
                ev(pair[0][2], f"联系方 {pair[0][1]}", quote)
                + ev(pair[1][2], f"联系方 {pair[1][1]}", quote)))
            return decide(results, plan=plan)
    results.append(req(
        "contact_conflict", RequirementStatus.SATISFIED, "no_contact_conflict",
        "未作废同号证明文件的联系方名称一致（或编号各不相同），无本条冲突", []))
    return decide(results, plan=plan)


PLAN_CLASS = F09Plan

EVALUATORS: dict[str, Any] = {
    "R049": evaluate_r049,
    "R050": evaluate_r050,
    "R051": evaluate_r051,
    "R052": evaluate_r052,
    "R053": evaluate_r053,
    "R054": evaluate_r054,
}

__all__ = ["F09Plan", "PLAN_CLASS", "APPLICABILITY", "EVALUATORS"]
