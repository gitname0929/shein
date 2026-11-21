"""F08 证明文件结构与时效 (R043-R048).

R043 证明文件编号必填, R044 证明有效期覆盖, R045 覆盖型号明确列出,
R046 作废状态优先, R047 文件版本与申报一致, R048 撤回日期边界.
R046: 清晰“作废”标记一律 reject（不能用于满足要求）; R048: 撤回日早于签发日
判为 ambiguous，审核基准日期在撤回日当天及之后判为 reject。
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
from o2_audit.operators.text import annotation_nfkc_strip, material_number_equal, model_equal, text_equal
from o2_audit.runtime.facts import FactView
from o2_audit.rules.advanced.base import (
    decide,
    entities_of_kind_with,
    ev,
    field_check,
    metadata_value,
    read,
    req,
)
from o2_audit.rules.base import RuleOutcome, RulePlan


class F08Plan(RulePlan):
    """F08 typed plans: R045's forbidden coverage shortcuts are a rule
    parameter (通配符、系列名和“全部型号”均不接受)."""

    forbidden_coverage_patterns: tuple[str, ...] = ("*", "?", "？", "×", "全部型号", "系列")


APPLICABILITY: dict[str, Any] = {
    rid: (lambda task, plan: ("applicable", []))
    for rid in ("R043", "R044", "R045", "R046", "R047", "R048")
}


def _cert(facts: FactView, predicate: str):
    found = entities_of_kind_with(facts, EntityKind.CERTIFICATE, predicate)
    if not found:
        return None
    return found[0]


def _no_certificate(plan: RulePlan) -> RuleOutcome:
    return decide([req(
        "certificate", RequirementStatus.VIOLATED, "certificate_not_submitted",
        "规则要求核验证明文件，任务未提交任何证明文件材料", [])],
        plan=plan, reject_reason="未提交证明文件")


# ---------------------------------------------------------------------------
# R043 证明文件编号必填
# ---------------------------------------------------------------------------


def evaluate_r043(task: AuditInput, plan: F08Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    found = _cert(facts, "material_number")
    if found is None:
        if not any(m.role == "certificate_pages" for m in task.materials):
            return _no_certificate(plan)
        return decide([req(
            "cert_number", RequirementStatus.UNKNOWN, "certificate_uninspected",
            "证明文件材料尚未记录观察，证据不足", [])], plan=plan)
    _e, status, value, obs = found
    if status == ObservationStatus.CONFIRMED_MISSING:
        return decide([req(
            "cert_number", RequirementStatus.VIOLATED, "cert_number_missing",
            "完整清晰范围内确认证明文件首页无“材料编号”字段；"
            "文件名、二维码或页脚流水号不能替代首页字段",
            ev(obs, "首页编号字段缺失", quote))], plan=plan)
    if status != ObservationStatus.READABLE:
        return decide([req(
            "cert_number", RequirementStatus.UNKNOWN, "cert_number_unreadable",
            "证明文件首页材料编号读不清，按证据不足处理",
            ev(obs, "编号不可读", quote))], plan=plan)
    if isinstance(value, str) and annotation_nfkc_strip(value) != "":
        return decide([req(
            "cert_number", RequirementStatus.SATISFIED, "cert_number_present",
            f"证明文件首页存在非空“材料编号”字段（{value!r}）", [])], plan=plan)
    return decide([req(
        "cert_number", RequirementStatus.VIOLATED, "cert_number_empty",
        "证明文件首页“材料编号”字段为空，不满足必填要求",
        ev(obs, f"编号为 {value!r}", quote))], plan=plan)


# ---------------------------------------------------------------------------
# R044 证明有效期覆盖
# ---------------------------------------------------------------------------


def evaluate_r044(task: AuditInput, plan: F08Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    found = _cert(facts, "valid_from")
    if found is None:
        if not any(m.role == "certificate_pages" for m in task.materials):
            return _no_certificate(plan)
        return decide([req(
            "validity", RequirementStatus.UNKNOWN, "validity_unobserved",
            "证明文件有效期尚未记录观察，证据不足", [])], plan=plan)
    e = found[0]
    f_status, f_value, f_obs = read(facts, e.entity_id, "valid_from")
    u_status, u_value, u_obs = read(facts, e.entity_id, "valid_until")
    results: list = []
    if ObservationStatus.CONFIRMED_MISSING in (f_status, u_status):
        results.append(req(
            "validity", RequirementStatus.VIOLATED, "validity_date_missing",
            "完整清晰范围内确认有效起始日或截止日缺失", ev(f_obs or u_obs, "日期缺失", quote)))
        return decide(results, plan=plan)
    if f_status != ObservationStatus.READABLE or u_status != ObservationStatus.READABLE:
        return decide([req(
            "validity", RequirementStatus.UNKNOWN, "validity_unreadable",
            "有效期日期读不清，按证据不足处理", ev(f_obs or u_obs, "日期不可读", quote))],
            plan=plan)
    try:
        start = parse_date(str(f_value))
    except DateParseError:
        return decide([req(
            "validity", RequirementStatus.VIOLATED, "validity_date_invalid",
            f"有效起始日 {f_value!r} 不是合法日期", ev(f_obs, f"起始日 {f_value}", quote))],
            plan=plan)
    try:
        end = parse_date(str(u_value))
    except DateParseError:
        return decide([req(
            "validity", RequirementStatus.VIOLATED, "validity_date_invalid",
            f"有效截止日 {u_value!r} 不是合法日期", ev(u_obs, f"截止日 {u_value}", quote))],
            plan=plan)
    if start > end:
        return decide([req(
            "validity", RequirementStatus.VIOLATED, "validity_reversed",
            f"有效起始日 {start} 晚于截止日 {end}", ev(f_obs, f"有效期 {start}-{end}", quote))],
            plan=plan)
    try:
        review = parse_date(task.review_date)
    except DateParseError:
        return decide([req(
            "validity", RequirementStatus.UNKNOWN, "review_date_invalid",
            f"审核基准日期 {task.review_date!r} 非法，无法核验", [])], plan=plan)
    if review < start or review > end:
        return decide([req(
            "validity", RequirementStatus.VIOLATED, "validity_not_covering",
            f"审核基准日期 {review} 不在有效期 [{start}, {end}] 内", [])], plan=plan)
    # 到期日当天仍满足本条 (closed interval, explicit in rule text).
    return decide([req(
        "validity", RequirementStatus.SATISFIED, "validity_covers_review",
        f"审核基准日期 {review} 在有效期 [{start}, {end}] 闭区间内（到期日当天仍满足）",
        [])], plan=plan)


# ---------------------------------------------------------------------------
# R045 覆盖型号明确列出
# ---------------------------------------------------------------------------


def evaluate_r045(task: AuditInput, plan: F08Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    declared = metadata_value(task, "declared_model")
    if declared is None:
        return decide([req(
            "coverage", RequirementStatus.UNKNOWN, "declared_model_missing",
            "申报型号缺失，无法核对覆盖型号", [])], plan=plan)
    declared_s = str(declared)
    found = _cert(facts, "covered_models")
    if found is None:
        if not any(m.role == "certificate_pages" for m in task.materials):
            return _no_certificate(plan)
        return decide([req(
            "coverage", RequirementStatus.UNKNOWN, "coverage_unobserved",
            "证明文件覆盖型号尚未记录观察，证据不足", [])], plan=plan)
    _e, status, value, obs = found
    if status == ObservationStatus.CONFIRMED_MISSING:
        return decide([req(
            "coverage", RequirementStatus.VIOLATED, "coverage_missing",
            "完整清晰范围内确认“覆盖型号”字段缺失", ev(obs, "覆盖型号缺失", quote))],
            plan=plan)
    if status != ObservationStatus.READABLE or not isinstance(value, list):
        return decide([req(
            "coverage", RequirementStatus.UNKNOWN, "coverage_unreadable",
            "覆盖型号清单读不清，按证据不足处理", ev(obs, "清单不可读", quote))],
            plan=plan)
    models = [str(m) for m in value]
    for m in models:
        norm = annotation_nfkc_strip(m)
        for pat in plan.forbidden_coverage_patterns:
            if pat in norm:
                # 通配符、系列名和“全部型号”不满足要求 (explicit in rule text).
                return decide([req(
                    "coverage", RequirementStatus.VIOLATED, "coverage_shortcut_not_accepted",
                    f"覆盖型号条目 {m!r} 含通配符/系列名/“全部型号”，不接受；"
                    "须逐项列出完整型号", ev(obs, f"条目 {m}", quote))], plan=plan)
    if any(model_equal(m, declared_s) for m in models):
        return decide([req(
            "coverage", RequirementStatus.SATISFIED, "coverage_contains_declared",
            f"覆盖型号逐项列出完整型号且包含申报型号 {declared_s!r}", [])], plan=plan)
    return decide([req(
        "coverage", RequirementStatus.VIOLATED, "coverage_missing_declared",
        f"覆盖型号清单 {models!r} 未包含申报型号 {declared_s!r}",
        ev(obs, f"覆盖型号 {models}", quote))], plan=plan)


# ---------------------------------------------------------------------------
# R046 作废状态优先
# ---------------------------------------------------------------------------


def evaluate_r046(task: AuditInput, plan: F08Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    found = _cert(facts, "void_marked")
    if found is None:
        if not any(m.role == "certificate_pages" for m in task.materials):
            return _no_certificate(plan)
        return decide([req(
            "void_status", RequirementStatus.UNKNOWN, "void_status_unobserved",
            "证明文件作废状态尚未记录观察，证据不足", [])], plan=plan)
    _e, status, value, obs = found
    if status == ObservationStatus.READABLE and value is True:
        # 清晰作废标记一律 reject，不论有效期是否覆盖审核日 (explicit).
        return decide([req(
            "void_status", RequirementStatus.VIOLATED, "void_marked_reject",
            "证明文件出现清晰“作废”标记，不论有效期如何均判为不符合",
            ev(obs, "证明标注作废", quote))], plan=plan)
    if status == ObservationStatus.READABLE and value is False:
        return decide([req(
            "void_status", RequirementStatus.SATISFIED, "not_voided",
            "完整清晰范围内确认证明文件无作废标记", ev(obs, "确认未作废", quote))],
            plan=plan)
    if status == ObservationStatus.CONFIRMED_MISSING:
        return decide([req(
            "void_status", RequirementStatus.SATISFIED, "not_voided",
            "完整清晰范围内确认证明文件无作废标记", ev(obs, "确认未作废", quote))],
            plan=plan)
    # 模糊到无法确认是否为作废标记 -> ambiguous (explicit in rule text).
    return decide([req(
        "void_status", RequirementStatus.UNKNOWN, "void_mark_unclear",
        "作废标记模糊无法确认，按证据不足处理", ev(obs, "作废标记不清晰", quote))],
        plan=plan)


# ---------------------------------------------------------------------------
# R047 文件版本与申报一致
# ---------------------------------------------------------------------------


def evaluate_r047(task: AuditInput, plan: F08Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    required = metadata_value(task, "required_document_version")
    if required is None:
        return decide([req(
            "document_version", RequirementStatus.UNKNOWN, "required_version_missing",
            "任务未提供 required_document_version，无法核验", [])], plan=plan)
    result = field_check(
        facts, EntityKind.CERTIFICATE, "document_version", "document_version",
        check=lambda v: ((True, "") if text_equal(str(v), str(required))
                         else (False, f"证明文件版本 {v!r} 与要求版本 {required!r} "
                               "不完全一致；版本高低不代表可替代，不允许用更新版本自行覆盖")),
        quote=quote,
        missing_reason="完整清晰范围内确认版本字段缺失",
        unreadable_reason="版本字段读不清，按证据不足处理")
    return decide([result], plan=plan)


# ---------------------------------------------------------------------------
# R048 撤回日期边界
# ---------------------------------------------------------------------------


def evaluate_r048(task: AuditInput, plan: F08Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    found = _cert(facts, "withdrawal_date")
    meta_withdrawal = metadata_value(task, "withdrawal_date")
    w_obs = None
    if found is not None:
        e = found[0]
        w_status, w_value, w_obs = read(facts, e.entity_id, "withdrawal_date")
        if w_status == ObservationStatus.CONFIRMED_MISSING:
            if meta_withdrawal is not None:
                w_status, w_value = ObservationStatus.READABLE, meta_withdrawal
            else:
                # 任务提供撤回日期的证明文件才适用本条；确认无撤回日期 -> 不适用.
                return decide([req(
                    "withdrawal", RequirementStatus.SATISFIED, "no_withdrawal_date",
                    "完整清晰范围内确认该证明文件无撤回日期，本条边界不触发",
                    ev(w_obs, "确认无撤回日期", quote))], plan=plan)
        if w_status != ObservationStatus.READABLE:
            return decide([req(
                "withdrawal", RequirementStatus.UNKNOWN, "withdrawal_date_unreadable",
                "撤回日期读不清，按证据不足处理", ev(w_obs, "撤回日不可读", quote))],
                plan=plan)
    elif meta_withdrawal is not None:
        # 撤回日由任务元数据提供；仍需从证明文件实体观察签发日与有效期。
        cert_found = _cert(facts, "issue_date") or _cert(facts, "valid_from")
        if cert_found is None:
            if not any(m.role == "certificate_pages" for m in task.materials):
                return _no_certificate(plan)
            return decide([req(
                "withdrawal", RequirementStatus.UNKNOWN, "withdrawal_unobserved",
                "证明文件签发日与有效期尚未记录观察，证据不足", [])], plan=plan)
        e = cert_found[0]
        w_value = meta_withdrawal
    else:
        if not any(m.role == "certificate_pages" for m in task.materials):
            return _no_certificate(plan)
        return decide([req(
            "withdrawal", RequirementStatus.UNKNOWN, "withdrawal_unobserved",
            "证明文件撤回信息尚未记录观察，证据不足", [])], plan=plan)
    i_status, i_value, i_obs = read(facts, e.entity_id, "issue_date")
    if i_status != ObservationStatus.READABLE:
        return decide([req(
            "withdrawal", RequirementStatus.UNKNOWN, "issue_date_unreadable",
            "签发日读不清，无法核对撤回日合法性，按证据不足处理",
            ev(i_obs, "签发日不可读", quote))], plan=plan)
    try:
        withdrawal = parse_date(str(w_value))
    except DateParseError:
        return decide([req(
            "withdrawal", RequirementStatus.VIOLATED, "withdrawal_date_invalid",
            f"撤回日期 {w_value!r} 不是合法日期", ev(w_obs, f"撤回日 {w_value}", quote))],
            plan=plan)
    try:
        issued = parse_date(str(i_value))
    except DateParseError:
        return decide([req(
            "withdrawal", RequirementStatus.VIOLATED, "issue_date_invalid",
            f"签发日 {i_value!r} 不是合法日期", ev(i_obs, f"签发日 {i_value}", quote))],
            plan=plan)
    if withdrawal < issued:
        # 撤回日合法但早于签发日则判为 ambiguous (explicit in rule text).
        return decide([req(
            "withdrawal", RequirementStatus.UNKNOWN, "withdrawal_before_issue",
            f"撤回日 {withdrawal} 早于签发日 {issued}，日期关系矛盾，判为 ambiguous",
            ev(w_obs, f"撤回 {withdrawal} / 签发 {issued}", quote))], plan=plan)
    try:
        review = parse_date(task.review_date)
    except DateParseError:
        return decide([req(
            "withdrawal", RequirementStatus.UNKNOWN, "review_date_invalid",
            f"审核基准日期 {task.review_date!r} 非法，无法核验", [])], plan=plan)
    if review >= withdrawal:
        # 在撤回日当天及之后判为 reject (explicit in rule text).
        return decide([req(
            "withdrawal", RequirementStatus.VIOLATED, "review_on_or_after_withdrawal",
            f"审核基准日期 {review} 在撤回日 {withdrawal} 当天及之后，判为不符合",
            ev(w_obs, f"撤回日 {withdrawal}", quote))], plan=plan)
    # 审核基准日期早于撤回日：继续按有效期判断 (closed interval).
    f_status, f_value, f_obs = read(facts, e.entity_id, "valid_from")
    u_status, u_value, u_obs = read(facts, e.entity_id, "valid_until")
    if f_status != ObservationStatus.READABLE or u_status != ObservationStatus.READABLE:
        return decide([req(
            "validity", RequirementStatus.UNKNOWN, "validity_unreadable",
            "审核基准日期早于撤回日，但有效期日期不可读，按证据不足处理",
            ev(f_obs or u_obs, "有效期不可读", quote))], plan=plan)
    try:
        start = parse_date(str(f_value))
        end = parse_date(str(u_value))
    except DateParseError:
        return decide([req(
            "validity", RequirementStatus.VIOLATED, "validity_date_invalid",
            "有效起始日/截止日不是合法日期", [])], plan=plan)
    if start <= review <= end:
        return decide([req(
            "validity", RequirementStatus.SATISFIED, "validity_covers_review",
            f"审核基准日期 {review} 早于撤回日 {withdrawal}，且在有效期 "
            f"[{start}, {end}] 闭区间内", [])], plan=plan)
    return decide([req(
        "validity", RequirementStatus.VIOLATED, "validity_not_covering",
        f"审核基准日期 {review} 早于撤回日 {withdrawal}，但不在有效期 "
        f"[{start}, {end}] 内", [])], plan=plan)


PLAN_CLASS = F08Plan

EVALUATORS: dict[str, Any] = {
    "R043": evaluate_r043,
    "R044": evaluate_r044,
    "R045": evaluate_r045,
    "R046": evaluate_r046,
    "R047": evaluate_r047,
    "R048": evaluate_r048,
}

__all__ = ["F08Plan", "PLAN_CLASS", "APPLICABILITY", "EVALUATORS"]
