"""F06 警示与说明内容 (R031-R036).

R031 固定警示完整出现, R032 限制性文案不得自相矛盾, R033 步骤顺序明确,
R034 例外场景说明, R035 语言声明与关键句一致, R036 否定词完整保留.
Fixed texts and step sequences are plan parameters; comparisons use shared
text operators (verbatim with allowed line breaks; case-sensitive English).
"""
from __future__ import annotations

from typing import Any

from o2_audit.domain.models import (
    AuditInput,
    EntityKind,
    ObservationStatus,
    RequirementStatus,
)
from o2_audit.operators.text import (
    annotation_nfkc_strip,
    contains_verbatim_linebreaks,
    exact_case_match,
    ordered_steps_match,
    verbatim_match_linebreaks,
)
from o2_audit.runtime.facts import FactView
from o2_audit.rules.advanced.base import (
    decide,
    entities_of_kind_with,
    ev,
    field_check,
    metadata_value,
    req,
)
from o2_audit.rules.base import RuleOutcome, RulePlan


class F06Plan(RulePlan):
    """F06 typed plans."""

    #: R031: the fixed six-character warning, allowed to wrap across lines.
    fixed_warning_text: str = "仅供练习使用"
    #: R033: exact ordered assembly steps.
    step_texts: tuple[str, ...] = ("连接底座", "固定主体", "检查连接")
    #: R034: the two required clauses (both must appear verbatim).
    disassembly_clauses: tuple[str, ...] = ("使用时不可拆卸", "收纳时可拆卸")
    #: R035: language_mode -> required sentence.
    language_sentences: dict[str, str] = {"中文": "保持干燥", "英文": "Keep dry"}


def _applicability_r034(task: AuditInput, plan: F06Plan) -> tuple[str, list[str]]:
    v = metadata_value(task, "storage_disassemblable")
    if v is None:
        return "unknown", ["storage_disassemblable"]
    if v is True:
        return "applicable", []
    return "not_applicable", []


def _applicability_r035(task: AuditInput, plan: F06Plan) -> tuple[str, list[str]]:
    mode = metadata_value(task, "language_mode")
    if mode is None:
        return "unknown", ["language_mode"]
    if annotation_nfkc_strip(str(mode)) in plan.language_sentences or \
            annotation_nfkc_strip(str(mode)) == "双语":
        return "applicable", []
    return "not_applicable", []


APPLICABILITY: dict[str, Any] = {
    "R031": lambda task, plan: ("applicable", []),
    "R032": lambda task, plan: ("applicable", []),
    "R033": lambda task, plan: ("applicable", []),
    "R034": _applicability_r034,
    "R035": _applicability_r035,
    "R036": lambda task, plan: ("applicable", []),
}


def _verbatim(facts: FactView, kind: EntityKind = EntityKind.LABEL):
    return entities_of_kind_with(facts, kind, "verbatim_text")


# ---------------------------------------------------------------------------
# R031 固定警示完整出现
# ---------------------------------------------------------------------------


def evaluate_r031(task: AuditInput, plan: F06Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    expected = plan.fixed_warning_text

    def check(value: Any) -> tuple[bool, str]:
        if isinstance(value, str) and verbatim_match_linebreaks(value, expected):
            return True, ""
        return False, (f"包装固定警示为 {value!r}，须完整出现“{expected}”六字"
                       "（允许字符间换行，不允许缺字、同音替换或只写“练习用”）")

    result = field_check(facts, EntityKind.LABEL, "verbatim_text", "fixed_warning",
                         check=check, quote=quote,
                         missing_reason="完整清晰范围内确认固定警示文字缺失",
                         unreadable_reason="警示文字读不清，按证据不足处理")
    return decide([result], plan=plan)


# ---------------------------------------------------------------------------
# R032 限制性文案不得自相矛盾
# ---------------------------------------------------------------------------


def evaluate_r032(task: AuditInput, plan: F06Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    indoor = entities_of_kind_with(facts, EntityKind.LABEL, "indoor_only_text")
    outdoor = entities_of_kind_with(facts, EntityKind.LABEL, "outdoor_durable_text")
    if not indoor and not outdoor:
        return decide([req(
            "restriction", RequirementStatus.UNKNOWN, "restriction_text_unobserved",
            "未观察到限制性文案，证据不足", [])], plan=plan)
    indoor_status = indoor[0][1] if indoor else ObservationStatus.CONFIRMED_MISSING
    indoor_value = indoor[0][2] if indoor else None
    if indoor_status == ObservationStatus.CONFIRMED_MISSING or (
            indoor_status == ObservationStatus.READABLE and indoor_value is False):
        # 完整清晰范围内确认未出现“仅供室内使用”：无自相矛盾可言，直接 satisfied
        # （即使“适合室外长期放置”可见也不构成矛盾）。
        return decide([req(
            "restriction", RequirementStatus.SATISFIED, "no_indoor_restriction",
            "完整清晰范围内确认未出现“仅供室内使用”，不存在自相矛盾的限制性文案",
            ev(indoor[0][3] if indoor else None, "确认无室内限制文案", quote))],
            plan=plan)
    if indoor_status != ObservationStatus.READABLE:
        return decide([req(
            "restriction", RequirementStatus.UNKNOWN, "restriction_text_unverifiable",
            "“仅供室内使用”是否出现无法确认，按证据不足处理",
            ev(indoor[0][3] if indoor else None, "文案不可读", quote))], plan=plan)
    if not outdoor:
        return decide([req(
            "restriction", RequirementStatus.SATISFIED, "no_contradiction",
            "出现“仅供室内使用”且未观察到“适合室外长期放置”", [])], plan=plan)
    out_status, out_value, out_obs = outdoor[0][1], outdoor[0][2], outdoor[0][3]
    if out_status != ObservationStatus.READABLE:
        return decide([req(
            "restriction", RequirementStatus.UNKNOWN, "contradiction_unverifiable",
            "“适合室外长期放置”是否出现无法确认，按证据不足处理",
            ev(out_obs, "文案不可读", quote))], plan=plan)
    if out_value is True:
        # 两句均明确可见即判为 reject，不以字号大小决定优先级 (explicit).
        return decide([req(
            "restriction", RequirementStatus.VIOLATED, "contradictory_restrictions",
            "同一包装同时明确出现“仅供室内使用”和“适合室外长期放置”，自相矛盾",
            ev(out_obs, "两句同时可见", quote))], plan=plan)
    return decide([req(
        "restriction", RequirementStatus.SATISFIED, "no_contradiction",
        "“仅供室内使用”出现且未出现“适合室外长期放置”", [])], plan=plan)


# ---------------------------------------------------------------------------
# R033 步骤顺序明确
# ---------------------------------------------------------------------------


def evaluate_r033(task: AuditInput, plan: F06Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    expected = list(plan.step_texts)
    found = entities_of_kind_with(facts, EntityKind.DOCUMENT, "step_list") or \
        entities_of_kind_with(facts, EntityKind.LABEL, "step_list")
    if not found:
        return decide([req(
            "steps", RequirementStatus.UNKNOWN, "steps_unobserved",
            "未观察到组装说明步骤，证据不足", [])], plan=plan)
    _e, status, value, obs = found[0]
    if status == ObservationStatus.CONFIRMED_MISSING:
        return decide([req(
            "steps", RequirementStatus.VIOLATED, "steps_missing",
            f"完整清晰范围内确认组装步骤缺失（须为 {expected}）",
            ev(obs, "步骤缺失", quote))], plan=plan)
    if status != ObservationStatus.READABLE or not isinstance(value, list):
        return decide([req(
            "steps", RequirementStatus.UNKNOWN, "steps_unreadable",
            "组装步骤读不清，按证据不足处理", ev(obs, "步骤不可读", quote))],
            plan=plan)
    if ordered_steps_match(value, expected):
        return decide([req(
            "steps", RequirementStatus.SATISFIED, "steps_in_order",
            f"组装说明按顺序标明 {expected}", ev(obs, "步骤完整有序", quote))],
            plan=plan)
    return decide([req(
        "steps", RequirementStatus.VIOLATED, "steps_invalid",
        f"组装步骤 {value!r} 与要求的顺序 {expected} 不符"
        "（步骤缺失、重复编号或次序颠倒均不符合）", ev(obs, "步骤不符", quote))],
        plan=plan)


# ---------------------------------------------------------------------------
# R034 例外场景说明
# ---------------------------------------------------------------------------


def evaluate_r034(task: AuditInput, plan: F06Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    expected = list(plan.disassembly_clauses)
    found = entities_of_kind_with(facts, EntityKind.LABEL, "disassembly_clauses")
    if not found:
        return decide([req(
            "exception_clauses", RequirementStatus.UNKNOWN, "clauses_unobserved",
            "未观察到例外场景说明，证据不足", [])], plan=plan)
    _e, status, value, obs = found[0]
    if status == ObservationStatus.CONFIRMED_MISSING:
        return decide([req(
            "exception_clauses", RequirementStatus.VIOLATED, "clauses_missing",
            f"完整清晰范围内确认说明缺失（须同时给出 {expected}）",
            ev(obs, "说明缺失", quote))], plan=plan)
    if status != ObservationStatus.READABLE or not isinstance(value, list):
        return decide([req(
            "exception_clauses", RequirementStatus.UNKNOWN, "clauses_unreadable",
            "例外场景说明读不清，按证据不足处理", ev(obs, "说明不可读", quote))],
            plan=plan)
    present = {annotation_nfkc_strip(str(x)) for x in value}
    if all(annotation_nfkc_strip(c) in present for c in expected):
        return decide([req(
            "exception_clauses", RequirementStatus.SATISFIED, "clauses_complete",
            f"“{expected[0]}”和“{expected[1]}”同时给出（缺失任一或互换均不符合）",
            ev(obs, "两条件齐备", quote))], plan=plan)
    return decide([req(
        "exception_clauses", RequirementStatus.VIOLATED, "clauses_invalid",
        f"例外场景说明 {sorted(present)} 未同时给出 {expected}（或将两条件互换）",
        ev(obs, "说明不符", quote))], plan=plan)


# ---------------------------------------------------------------------------
# R035 语言声明与关键句一致
# ---------------------------------------------------------------------------


def evaluate_r035(task: AuditInput, plan: F06Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    mode = annotation_nfkc_strip(str(metadata_value(task, "language_mode")))
    required: list[tuple[str, str]] = []
    if mode == "中文":
        required.append(("中文", plan.language_sentences["中文"]))
    elif mode == "英文":
        required.append(("英文", plan.language_sentences["英文"]))
    elif mode == "双语":
        required.append(("中文", plan.language_sentences["中文"]))
        required.append(("英文", plan.language_sentences["英文"]))
    else:
        return decide([req(
            "language_sentence", RequirementStatus.UNKNOWN, "language_mode_invalid",
            f"language_mode {mode!r} 不是中文/英文/双语，无法确定关键句", [])],
            plan=plan)

    found = entities_of_kind_with(facts, EntityKind.LABEL, "language_text")
    if not found:
        return decide([req(
            "language_sentence", RequirementStatus.UNKNOWN, "language_text_unobserved",
            "未观察到语言文本内容，证据不足", [])], plan=plan)
    _e, status, value, obs = found[0]
    if status == ObservationStatus.CONFIRMED_MISSING:
        return decide([req(
            "language_sentence", RequirementStatus.VIOLATED, "required_sentence_missing",
            f"完整清晰范围内确认语言文本缺失（须有 {required}）",
            ev(obs, "文本缺失", quote))], plan=plan)
    if status != ObservationStatus.READABLE or not isinstance(value, list):
        return decide([req(
            "language_sentence", RequirementStatus.UNKNOWN, "language_text_unreadable",
            "语言文本读不清，按证据不足处理", ev(obs, "文本不可读", quote))],
            plan=plan)
    texts = [str(x) for x in value]
    for lang, sentence in required:
        hit = any(exact_case_match(t, sentence) for t in texts)
        if not hit:
            # 英文大小写按原文精确比较 (explicit in rule text).
            return decide([req(
                "language_sentence", RequirementStatus.VIOLATED,
                "required_sentence_missing",
                f"language_mode={mode} 须有“{sentence}”（{lang}），实际文本 {texts!r}；"
                "英文大小写按原文精确比较", ev(obs, f"文本 {texts}", quote))],
                plan=plan)
    return decide([req(
        "language_sentence", RequirementStatus.SATISFIED, "sentences_present",
        f"language_mode={mode} 所需关键句 {required} 均按原文精确出现", [])],
        plan=plan)


# ---------------------------------------------------------------------------
# R036 否定词完整保留
# ---------------------------------------------------------------------------


def evaluate_r036(task: AuditInput, plan: F06Plan, facts: FactView) -> RuleOutcome:
    quote = plan.quote("rule_text")
    expected = "请勿倒置"
    found = _verbatim(facts)
    if not found:
        return decide([req(
            "negation", RequirementStatus.UNKNOWN, "warning_unobserved",
            "未观察到倒置警示文字，证据不足", [])], plan=plan)
    _e, status, value, obs = found[0]
    if status == ObservationStatus.CONFIRMED_MISSING:
        return decide([req(
            "negation", RequirementStatus.VIOLATED, "warning_missing",
            f"完整清晰范围内确认未出现“{expected}”警示", ev(obs, "警示缺失", quote))],
            plan=plan)
    if status != ObservationStatus.READABLE or not isinstance(value, str):
        # 否定词被遮挡导致无法辨认 -> ambiguous (explicit in rule text).
        return decide([req(
            "negation", RequirementStatus.UNKNOWN, "negation_unreadable",
            "否定词被遮挡或不可辨认，按证据不足处理", ev(obs, "文字不可读", quote))],
            plan=plan)
    text = annotation_nfkc_strip(value)
    if contains_verbatim_linebreaks(text, expected):
        return decide([req(
            "negation", RequirementStatus.SATISFIED, "negation_intact",
            f"警示完整出现“{expected}”，否定词未被省略", ev(obs, f"警示为 {text}", quote))],
            plan=plan)
    if contains_verbatim_linebreaks(text, "倒置"):
        # 不能仅显示“请倒置”或“倒置” (explicit in rule text).
        return decide([req(
            "negation", RequirementStatus.VIOLATED, "negation_dropped",
            f"警示仅显示 {text!r}，明确缺少否定词“勿”，不满足要求",
            ev(obs, f"警示为 {text}", quote))], plan=plan)
    return decide([req(
        "negation", RequirementStatus.VIOLATED, "warning_missing",
        f"完整清晰范围内未出现“{expected}”警示（实际 {text!r}）",
        ev(obs, f"警示为 {text}", quote))], plan=plan)


PLAN_CLASS = F06Plan

EVALUATORS: dict[str, Any] = {
    "R031": evaluate_r031,
    "R032": evaluate_r032,
    "R033": evaluate_r033,
    "R034": evaluate_r034,
    "R035": evaluate_r035,
    "R036": evaluate_r036,
}

__all__ = ["F06Plan", "PLAN_CLASS", "APPLICABILITY", "EVALUATORS"]
