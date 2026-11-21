"""F03 字段完整性与格式 (R013-R018).

R013 练习材料编号格式, R014 批次日期与流水号格式, R015 整件数量格式,
R016 支持地址协议格式, R017 日期区间有序, R018 双语字段成对.
Format patterns live in the typed plan; the operators only apply them.
"""
from __future__ import annotations

import re
from typing import Any

from o2_audit.domain.models import (
    AuditInput,
    EntityKind,
    ObservationStatus,
    RequirementStatus,
)
from o2_audit.operators.dates import parse_date, DateParseError
from o2_audit.operators.text import annotation_nfkc_strip, matches_pattern
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


class F03Plan(RulePlan):
    """F03 typed plans: format patterns are rule parameters, not operator
    constants."""

    material_number_pattern: str = r"^QLAB-[0-9]{6}$"
    #: R014: 真实公历日期 YYYYMMDD + 半角连字符 + N 位数字流水号
    batch_serial_digits: int = 3


def _applicability_r018(task: AuditInput, plan: F03Plan) -> tuple[str, list[str]]:
    mode = metadata_value(task, "language_mode")
    if mode is None:
        return "unknown", ["language_mode"]
    if annotation_nfkc_strip(str(mode)) == "双语":
        return "applicable", []
    return "not_applicable", []


APPLICABILITY: dict[str, Any] = {
    "R013": lambda task, plan: ("applicable", []),
    "R014": lambda task, plan: ("applicable", []),
    "R015": lambda task, plan: ("applicable", []),
    "R016": lambda task, plan: ("applicable", []),
    "R017": lambda task, plan: ("applicable", []),
    "R018": _applicability_r018,
}


# ---------------------------------------------------------------------------
# R013 练习材料编号格式
# ---------------------------------------------------------------------------


def evaluate_r013(task: AuditInput, plan: F03Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    pattern = plan.material_number_pattern

    def check(value: Any) -> tuple[bool, str]:
        if matches_pattern(str(value), pattern):
            return True, ""
        return False, (f"包装材料编号 {value!r} 不符合 {pattern} "
                       "（大写 QLAB- 后接恰好 6 位数字，前导零保留）")

    result = field_check(facts, EntityKind.LABEL, "material_number",
                         "number_format", check=check, quote=quote,
                         missing_reason="完整清晰范围内确认材料编号字段缺失",
                         unreadable_reason="材料编号读不清，按证据不足处理")
    return decide([result], plan=plan)


# ---------------------------------------------------------------------------
# R014 批次日期与流水号格式
# ---------------------------------------------------------------------------


def evaluate_r014(task: AuditInput, plan: F03Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    digits = plan.batch_serial_digits
    pattern = re.compile(rf"^(\d{{8}})-(\d{{{digits}}})$")

    def check(value: Any) -> tuple[bool, str]:
        s = annotation_nfkc_strip(str(value))
        m = pattern.fullmatch(s)
        if not m:
            return False, (f"批次号 {value!r} 不符合『YYYYMMDD-流水号』结构"
                           f"（{digits} 位数字流水号）")
        try:
            parse_date(f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}")
        except DateParseError:
            return False, f"批次号 {value!r} 中的日期不是真实公历日期"
        return True, ""

    result = field_check(facts, EntityKind.LABEL, "batch_number", "batch_format",
                         check=check, quote=quote,
                         missing_reason="完整清晰范围内确认批次号字段缺失",
                         unreadable_reason="批次号读不清，按证据不足处理")
    return decide([result], plan=plan)


# ---------------------------------------------------------------------------
# R015 整件数量格式
# ---------------------------------------------------------------------------


def evaluate_r015(task: AuditInput, plan: F03Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    pattern = re.compile(r"^(0|[1-9][0-9]*)件$")

    def check(value: Any) -> tuple[bool, str]:
        s = annotation_nfkc_strip(str(value))
        m = pattern.fullmatch(s)
        if not m:
            return False, (f"件数 {value!r} 不是不带小数点的正整数后接“件”"
                           "（0、负数、小数或仅写“一套”均不满足）")
        if int(m.group(1)) == 0:
            return False, f"件数 {value!r} 为 0，不满足正整数要求"
        return True, ""

    result = field_check(facts, EntityKind.LABEL, "piece_count", "piece_count_format",
                         check=check, quote=quote,
                         missing_reason="完整清晰范围内确认件数字段缺失",
                         unreadable_reason="件数读不清，按证据不足处理")
    return decide([result], plan=plan)


# ---------------------------------------------------------------------------
# R016 支持地址协议格式
# ---------------------------------------------------------------------------


def evaluate_r016(task: AuditInput, plan: F03Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    pattern = re.compile(r"^https://[^\s/]+$")

    def check(value: Any) -> tuple[bool, str]:
        s = annotation_nfkc_strip(str(value))
        if not s.startswith("https://"):
            return False, f"支持地址 {value!r} 未以 https:// 开头"
        host = s[len("https://"):].split("/", 1)[0]
        if not host:
            return False, f"支持地址 {value!r} 主机名为空"
        return True, ""

    found = entities_of_kind_with(facts, EntityKind.LABEL, "support_url")
    if not found:
        # 只印二维码而无可见地址文字 -> 明确不满足 (requires confirmed missing).
        for e in facts.entities_of_kind(EntityKind.LABEL):
            s, _v, obs = read(facts, e.entity_id, "qr_present")
            if s == ObservationStatus.READABLE and _v is True:
                return decide([req(
                    "support_url", RequirementStatus.VIOLATED, "qr_only_no_address_text",
                    "只印二维码而无可见地址文字，不满足本条（不要求联网访问）",
                    ev(obs, "仅有二维码", quote))], plan=plan)
        return decide([req(
            "support_url", RequirementStatus.UNKNOWN, "support_url_unobserved",
            "未观察到支持地址字段，证据不足", [])], plan=plan)

    for _e, status, _v, obs in found:
        if status == ObservationStatus.CONFIRMED_MISSING:
            # Confirmed missing address text: QR-only counts as 不满足 as well.
            s, v, obs_qr = read(facts, _e.entity_id, "qr_present")
            code = "qr_only_no_address_text" if (s == ObservationStatus.READABLE and v is True) \
                else "support_url_confirmed_missing"
            return decide([req(
                "support_url", RequirementStatus.VIOLATED, code,
                "完整清晰范围内确认包装上没有可见的支持地址文字，不满足本条",
                ev(obs, "确认无地址文字", quote))], plan=plan)
    result = field_check(facts, EntityKind.LABEL, "support_url", "support_url",
                         check=check, quote=quote,
                         missing_reason="确认支持地址字段缺失",
                         unreadable_reason="支持地址读不清，按证据不足处理")
    return decide([result], plan=plan)


# ---------------------------------------------------------------------------
# R017 日期区间有序
# ---------------------------------------------------------------------------


def evaluate_r017(task: AuditInput, plan: F03Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    labels = facts.entities_of_kind(EntityKind.LABEL)
    targets = [e for e in labels
               if facts.claims(e.entity_id, "valid_from")
               or facts.claims(e.entity_id, "valid_until")]
    if not targets:
        return decide([req(
            "date_interval", RequirementStatus.UNKNOWN, "interval_unobserved",
            "未观察到启用日/截止日字段，本条仅在标签同时给出两日时适用，证据不足", [])],
            plan=plan)
    e = targets[0]
    f_status, f_value, f_obs = read(facts, e.entity_id, "valid_from")
    u_status, u_value, u_obs = read(facts, e.entity_id, "valid_until")

    def unreadable(name: str, obs) -> RuleOutcome:
        return decide([req(
            "date_interval", RequirementStatus.UNKNOWN, "interval_date_unreadable",
            f"{name}读不清，按证据不足处理", ev(obs, f"{name}不可读", quote))],
            plan=plan)

    if f_status != ObservationStatus.READABLE and f_status != ObservationStatus.CONFIRMED_MISSING:
        return unreadable("启用日", f_obs)
    if u_status != ObservationStatus.READABLE and u_status != ObservationStatus.CONFIRMED_MISSING:
        return unreadable("截止日", u_obs)
    # 只给出一端不满足要求 (explicit in rule text).
    if f_status == ObservationStatus.CONFIRMED_MISSING or u_status == ObservationStatus.CONFIRMED_MISSING:
        return decide([req(
            "date_interval", RequirementStatus.VIOLATED, "interval_one_endpoint_only",
            "启用日与截止日须同时给出，只给出一端不满足要求",
            ev(f_obs if f_status == ObservationStatus.CONFIRMED_MISSING else u_obs,
               "仅有一端日期", quote))], plan=plan)
    try:
        start = parse_date(str(f_value))
    except DateParseError:
        return decide([req(
            "date_interval", RequirementStatus.VIOLATED, "interval_date_invalid",
            f"启用日 {f_value!r} 不是合法日期", ev(f_obs, f"启用日为 {f_value}", quote))],
            plan=plan)
    try:
        end = parse_date(str(u_value))
    except DateParseError:
        return decide([req(
            "date_interval", RequirementStatus.VIOLATED, "interval_date_invalid",
            f"截止日 {u_value!r} 不是合法日期", ev(u_obs, f"截止日为 {u_value}", quote))],
            plan=plan)
    if start > end:
        return decide([req(
            "date_interval", RequirementStatus.VIOLATED, "interval_reversed",
            f"启用日 {start} 晚于截止日 {end}，区间无序", ev(f_obs, f"区间 {start} 至 {end}", quote))],
            plan=plan)
    return decide([req(
        "date_interval", RequirementStatus.SATISFIED, "interval_ordered",
        f"启用日 {start} 不晚于截止日 {end}，两日期均合法", [])], plan=plan)


# ---------------------------------------------------------------------------
# R018 双语字段成对
# ---------------------------------------------------------------------------


def evaluate_r018(task: AuditInput, plan: F03Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    labels = [e for e in facts.entities_of_kind(EntityKind.LABEL)]
    results: list = []
    if not labels:
        return decide([req(
            "bilingual_pair", RequirementStatus.UNKNOWN, "labels_unobserved",
            "未观察到包装标签，证据不足", [])], plan=plan)

    def pair_check(e) -> tuple[bool, str, Any, Any]:
        cn_s, cn_v, _ = read(facts, e.entity_id, "product_name_cn")
        en_s, en_v, _ = read(facts, e.entity_id, "product_name_en")
        if cn_s == ObservationStatus.READABLE and en_s == ObservationStatus.READABLE:
            cn_ok = isinstance(cn_v, str) and annotation_nfkc_strip(cn_v) != ""
            en_ok = isinstance(en_v, str) and annotation_nfkc_strip(en_v) != ""
            return (cn_ok and en_ok), "bilingual_pair_empty_or_missing", (cn_v, en_v)
        if ObservationStatus.CONFIRMED_MISSING in (cn_s, en_s):
            return False, "bilingual_pair_missing", (cn_v, en_v)
        return True, "bilingual_pair_unreadable", (cn_v, en_v)

    for e in labels:
        ok, code, values = pair_check(e)
        if not ok:
            results.append(req(
                f"bilingual_pair:{e.entity_id}", RequirementStatus.VIOLATED, code,
                f"双语版包装须同时提供非空的“产品名称”和“Product Name”字段"
                f"（观察到 {values!r}）", []))
            return decide(results, plan=plan)
        if code == "bilingual_pair_unreadable":
            results.append(req(
                f"bilingual_pair:{e.entity_id}", RequirementStatus.UNKNOWN,
                "bilingual_pair_unreadable",
                "产品名称字段读不清，按证据不足处理", []))
            return decide(results, plan=plan)
    results.append(req(
        "bilingual_pair", RequirementStatus.SATISFIED, "bilingual_pair_present",
        "“产品名称”与“Product Name”两个非空字段同时存在（不要求逐字翻译一致）", []))
    return decide(results, plan=plan)


PLAN_CLASS = F03Plan

EVALUATORS: dict[str, Any] = {
    "R013": evaluate_r013,
    "R014": evaluate_r014,
    "R015": evaluate_r015,
    "R016": evaluate_r016,
    "R017": evaluate_r017,
    "R018": evaluate_r018,
}

__all__ = ["F03Plan", "PLAN_CLASS", "APPLICABILITY", "EVALUATORS"]
