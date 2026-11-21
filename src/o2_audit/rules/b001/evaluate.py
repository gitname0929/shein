"""B001 semantic evaluation (prototype implementation of the published rule).

Decision order implemented here (B001 items 5-6 + 规则总则 §2):

1. Applicability (market Q AND category 桌面灯 AND power_mode 可充电).
   A known-false condition settles not_applicable even when other conditions
   are unknown (short circuit per 总则 §1).
2. Special material conflicts (item 5) among *non-void* labels / certificates
   override ordinary violations -> ambiguous. Conflicts are checked across ALL
   non-void labels (different material numbers) and ALL non-void certificates
   (same number, different validity periods).
3. Proven violations (confirmed missing in a complete clear scope, mismatch,
   position/format/validity failures, voided-only evidence) -> reject.
   Voided-only label/certificate evidence is a VIOLATION per item 5 ("清楚标注
   '作废' 的旧标签或旧文件不作为有效证据，也不能单独用于满足要求"), never a
   bare ``pass`` or ``unknown``.
4. Conclusion-affecting unknowns (unreadable necessary info, uncertain tag
   connection) -> ambiguous.
5. All applicable requirements satisfied -> pass.

Unknown/missing/unreadable are never merged. Fields observed on two separate
label entities are never merged to satisfy the same-label requirement (item 2).
This module stays separable from future generic rule evaluation: it consumes a
FactView + typed plan + shared operators only.
"""
from __future__ import annotations

from typing import Any

from o2_audit.domain.models import (
    Entity,
    EntityKind,
    Evidence,
    Observation,
    ObservationStatus,
    RequirementResult,
    RequirementStatus,
)
from o2_audit.operators.dates import parse_date, DateParseError
from o2_audit.operators.numbers import decimal_of, number_strictly_less
from o2_audit.operators.sets import list_contains_model
from o2_audit.operators.text import (
    material_number_equal,
    matches_pattern,
    model_equal,
)
from o2_audit.runtime.facts import FactView, consolidate
from o2_audit.rules.base import RuleOutcome
from o2_audit.rules.b001.plan import B001Plan


class B001Outcome(RuleOutcome):
    """Backward-compatible alias: identical constructor/attributes as Stage 01,
    now shared with every advanced rule via :class:`RuleOutcome`."""



# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _evidence_of(obs: Observation | None, fact: str, rule_quote: str | None = None) -> list[Evidence]:
    if obs is None or obs.source is None:
        return []
    return [Evidence(
        material_id=obs.source.material_id,
        page_index=obs.source.page_index,
        fact=fact,
        bbox=list(obs.source.bbox) if obs.source.bbox else None,
        rule_quote=rule_quote,
    )]


def _req(req_id: str, status: RequirementStatus, reason_code: str, reason: str,
         evidence: list[Evidence]) -> RequirementResult:
    return RequirementResult(
        requirement_id=req_id, status=status, reason_code=reason_code,
        reason=reason, evidence=evidence,
        blocks_conclusion=(status in (RequirementStatus.UNKNOWN, RequirementStatus.CONFLICT)),
    )


def _raw_repr(value: Any) -> str:
    if isinstance(value, list):
        return "、".join(str(v) for v in value)
    return str(value)


# ---------------------------------------------------------------------------
# applicability
# ---------------------------------------------------------------------------


def _evaluate_applicability(task_metadata: dict[str, Any], plan: B001Plan) -> tuple[str, list[str], list[Evidence]]:
    conditions = [
        ("market", plan.applicable_market),
        ("category", plan.applicable_category),
        ("power_mode", plan.applicable_power_mode),
    ]
    missing: list[str] = []
    for key, expected in conditions:
        v = task_metadata.get(key)
        if v is None:
            missing.append(key)
        elif not material_number_equal(str(v), expected):
            # A known-false condition settles applicability immediately
            # (总则 §1: known-false wins over other unknown conditions).
            return "not_applicable", [], []
    if missing:
        return "unknown", missing, []
    return "applicable", [], []


def applicability_precheck(task, plan: B001Plan) -> tuple[str, list[str]]:
    """Semantic-layer applicability check used by the controller so that a
    known-false or unknown applicability short-circuits BEFORE any gateway
    call. Returns (applicability, missing_conditions)."""
    applicability, missing, _ = _evaluate_applicability(task.metadata.values, plan)
    return applicability, missing


# ---------------------------------------------------------------------------
# label-side requirements (items 1-2)
# ---------------------------------------------------------------------------


def _active_labels(facts: FactView) -> tuple[list[Entity], list[Entity], list[RequirementResult]]:
    """Split label entities into (active, clearly_voided, void_unknown_results).

    ``作废`` filtering: labels clearly marked void are not valid evidence and
    cannot satisfy requirements. A label whose void status is unreadable makes
    its usability unknown. No void observation at all is also unknown (never
    assumed non-void).
    """
    active: list[Entity] = []
    clearly_voided: list[Entity] = []
    results: list[RequirementResult] = []
    for label in facts.entities_of_kind(EntityKind.LABEL):
        status, value, obs = consolidate(facts.claims(label.entity_id, "void_marked"))
        if status == ObservationStatus.READABLE and value is True:
            clearly_voided.append(label)
            continue  # clearly voided: excluded, no requirement may use it
        if status in (ObservationStatus.UNREADABLE, ObservationStatus.INCOMPLETE_SCOPE,
                      ObservationStatus.IDENTITY_UNKNOWN):
            results.append(_req(
                f"label_void_status:{label.entity_id}", RequirementStatus.UNKNOWN,
                "void_status_unreadable",
                f"标签 {label.entity_id} 的作废标记不可读，无法确定其是否可作为有效证据",
                _evidence_of(obs, "作废标记状态不可读"),
            ))
            continue
        # READABLE False (explicitly not voided) or CONFIRMED_MISSING (no void
        # marking exists in a fully visible label) -> active.
        active.append(label)
    return active, clearly_voided, results


def _label_field_requirements(facts: FactView, active: list[Entity],
                              declared_model: str | None, plan: B001Plan) -> list[RequirementResult]:
    """Items 1-2 for each label entity INDEPENDENTLY.

    The model and the material number must be readable on the SAME label
    entity; fields observed on two separate labels are never merged (item 2).
    """
    violations: list[RequirementResult] = []
    unknowns: list[RequirementResult] = []
    declared_checked = False
    number_checked = False

    for label in active:
        eid = label.entity_id
        m_status, m_value, m_obs = consolidate(facts.claims(eid, "model"))
        n_status, n_value, n_obs = consolidate(facts.claims(eid, "material_number"))

        if m_status == ObservationStatus.CONFIRMED_MISSING or n_status == ObservationStatus.CONFIRMED_MISSING:
            field = "型号" if m_status == ObservationStatus.CONFIRMED_MISSING else "材料编号"
            violations.append(_req(
                f"label_fields:{eid}", RequirementStatus.VIOLATED,
                "label_field_confirmed_missing",
                f"在完整清晰的标签检查范围内确认{field}字段缺失，且型号与材料编号必须出现在同一张标签上",
                _evidence_of(m_obs if m_status == ObservationStatus.CONFIRMED_MISSING else n_obs,
                             f"标签{field}字段缺失", plan.quote("same_label")),
            ))
            continue
        if m_status != ObservationStatus.READABLE or n_status != ObservationStatus.READABLE:
            unknowns.append(_req(
                f"label_fields:{eid}", RequirementStatus.UNKNOWN,
                "label_field_unreadable",
                "标签型号或材料编号读不清，证据不足",
                _evidence_of(m_obs if m_obs is not None else n_obs, "字段不可读"),
            ))
            continue

        # Both fields readable on the SAME label entity: check them.
        declared_checked = True
        number_checked = True
        if declared_model is not None and not model_equal(str(m_value), declared_model):
            violations.append(_req(
                "label_model_match", RequirementStatus.VIOLATED, "model_mismatch",
                f"标签型号 {_raw_repr(m_value)!r} 与申报型号 {declared_model!r} 经型号归一化后不一致",
                _evidence_of(m_obs, f"标签型号为 {_raw_repr(m_value)}", plan.quote("model_match")),
            ))
        if not matches_pattern(str(n_value), plan.material_number_pattern):
            violations.append(_req(
                f"number_format:{eid}", RequirementStatus.VIOLATED,
                "material_number_format_invalid",
                f"材料编号 {_raw_repr(n_value)!r} 不符合规定格式（前缀须大写 QLAB- 加 6 位十进制数字）",
                _evidence_of(n_obs, f"标签材料编号为 {_raw_repr(n_value)}，不符合规定格式",
                             plan.quote("number_format")),
            ))

    if declared_model is None:
        unknowns.append(_req(
            "label_model_match", RequirementStatus.UNKNOWN, "declared_model_missing",
            "申报型号缺失，无法进行型号一致性比较", [],
        ))
    elif not declared_checked:
        unknowns.append(_req(
            "label_model_match", RequirementStatus.UNKNOWN, "model_unreadable",
            "无有效标签型号可比较", [],
        ))
    if not number_checked:
        unknowns.append(_req(
            "label_number_format", RequirementStatus.UNKNOWN, "material_number_unreadable",
            "无有效标签材料编号可核验", [],
        ))

    # Unknowns are appended unconditionally: the final decision ranks proven
    # violations above consequential unknowns, so keeping both is required.
    results = violations + unknowns
    return results


# ---------------------------------------------------------------------------
# carrier requirement (item 2, separately from tag attachment)
# ---------------------------------------------------------------------------


def _carrier_requirement(facts: FactView, active: list[Entity],
                         edge_mm_raw: Any, plan: B001Plan) -> RequirementResult:
    if edge_mm_raw is None:
        return _req("carrier", RequirementStatus.UNKNOWN, "package_size_unknown",
                    "包装最长边毫米数未知，无法判断标签载体是否符合位置要求", [])
    try:
        edge_mm = decimal_of(edge_mm_raw)
    except ValueError:
        return _req("carrier", RequirementStatus.UNKNOWN, "package_size_invalid",
                    f"申报包装最长边值 {_raw_repr(edge_mm_raw)!r} 非法，无法比较", [])

    small_package = number_strictly_less(edge_mm, plan.small_tag_threshold_mm)
    evidence: list[Evidence] = []
    unknowns: list[RequirementResult] = []
    violations: list[RequirementResult] = []
    compliant = False

    for label in active:
        c_status, c_value, c_obs = consolidate(facts.claims(label.entity_id, "carrier"))
        if c_status != ObservationStatus.READABLE:
            unknowns.append(_req(
                f"carrier:{label.entity_id}", RequirementStatus.UNKNOWN, "carrier_identity_unknown",
                "标签载体（外包装或附签）无法确认", _evidence_of(c_obs, "标签载体不确定"),
            ))
            continue
        if c_value == "outer_package":
            # Acceptable for any declared size.
            compliant = True
            evidence.extend(_evidence_of(c_obs, f"标签位于外包装（申报最长边 {edge_mm} mm）",
                                         plan.quote("carrier_outer_package")))
            continue
        if c_value == "attached_tag":
            if not small_package:
                violations.append(_req(
                    f"carrier:{label.entity_id}", RequirementStatus.VIOLATED, "tag_not_allowed_for_size",
                    f"包装最长边 {edge_mm} mm 不小于阈值 {plan.small_tag_threshold_mm} mm，附签不是合规载体",
                    _evidence_of(c_obs, "申报尺寸下附签不满足位置要求", plan.quote("carrier_outer_package")),
                ))
                continue
            conn_status, conn_value, conn_obs = consolidate(
                facts.claims(label.entity_id, "attached_to_package"))
            if conn_status == ObservationStatus.READABLE and conn_value is True:
                compliant = True
                evidence.extend(_evidence_of(c_obs, "小包装附签与包装连接关系已确认",
                                             plan.quote("attached_tag")))
                continue
            if conn_status == ObservationStatus.CONFIRMED_MISSING:
                violations.append(_req(
                    f"carrier:{label.entity_id}", RequirementStatus.VIOLATED, "tag_attachment_absent",
                    "完整清晰画面中确认附签未与包装连接", _evidence_of(conn_obs, "确认无连接关系",
                                                                 plan.quote("tag_attachment")),
                ))
                continue
            unknowns.append(_req(
                f"carrier:{label.entity_id}", RequirementStatus.UNKNOWN, "tag_connection_uncertain",
                "包装与附签连接关系不确定，不能仅凭单独附签照片确认符合",
                _evidence_of(conn_obs, "连接关系不确定", plan.quote("tag_attachment")),
            ))
            continue
        unknowns.append(_req(
            f"carrier:{label.entity_id}", RequirementStatus.UNKNOWN, "carrier_identity_unknown",
            f"未知载体类型 {_raw_repr(c_value)!r}", [],
        ))

    if compliant:
        # At least one active label satisfies item 2 (outer package, or an
        # attached tag on a small package with confirmed connection); other
        # non-conflicting carriers do not turn this into a rejection.
        return _req("carrier", RequirementStatus.SATISFIED, "carrier_ok",
                    "存在满足位置要求的标签载体（外包装标签或小包装上已确认连接的附签）",
                    evidence[:5])
    if violations:
        return violations[0]
    if unknowns:
        return unknowns[0]
    if not active:
        return _req("carrier", RequirementStatus.UNKNOWN, "no_active_label",
                    "无有效标签可核验载体位置", [])
    return _req("carrier", RequirementStatus.SATISFIED, "carrier_ok",
                "标签载体符合位置要求", evidence[:5])


# ---------------------------------------------------------------------------
# certificate requirements (items 3-4)
# ---------------------------------------------------------------------------


def _certificate_requirements(facts: FactView, task_material_roles: set[str],
                              declared_model: str | None,
                              review_date_raw: str,
                              active_label_numbers: list[str],
                              plan: B001Plan) -> tuple[list[RequirementResult], list[Entity]]:
    """Certificate-side items 3-5.

    Returns (results, valid_certs): ``valid_certs`` are the certificates not
    clearly marked void (they may still be unusable if fields are unreadable,
    but they are the population the conflict check must scan).
    """
    results: list[RequirementResult] = []
    certs = facts.entities_of_kind(EntityKind.CERTIFICATE)

    if not certs:
        if "certificate_pages" not in task_material_roles:
            return [_req("certificate_document", RequirementStatus.VIOLATED,
                         "certificate_not_submitted",
                         "规则要求提交关联证明文件，任务未提交任何证明文件材料", [])], []
        return [_req("certificate_document", RequirementStatus.UNKNOWN,
                     "certificate_uninspected",
                     "证明文件材料尚未完成检查，证据不足", [])], []

    valid_certs: list[Entity] = []
    clearly_voided_certs: list[Entity] = []
    void_unknowns = 0
    for cert in certs:
        v_status, v_value, v_obs = consolidate(facts.claims(cert.entity_id, "void_marked"))
        if v_status == ObservationStatus.READABLE and v_value is True:
            clearly_voided_certs.append(cert)
            continue  # clearly voided certificate excluded
        if v_status in (ObservationStatus.UNREADABLE, ObservationStatus.INCOMPLETE_SCOPE,
                        ObservationStatus.IDENTITY_UNKNOWN):
            results.append(_req(
                f"cert_void_status:{cert.entity_id}", RequirementStatus.UNKNOWN,
                "certificate_void_status_unreadable",
                "证明文件作废标记不可读，无法确定是否可作为有效证据",
                _evidence_of(v_obs, "证明作废标记不可读"),
            ))
            void_unknowns += 1
            continue
        valid_certs.append(cert)

    if not valid_certs and clearly_voided_certs and void_unknowns == 0:
        # All observed certificates are clearly voided: item 5 forbids using
        # them to satisfy the requirement -> proven violation, never pass.
        first = clearly_voided_certs[0].entity_id
        _, _, v_obs = consolidate(facts.claims(first, "void_marked"))
        results.append(_req(
            "certificate_document", RequirementStatus.VIOLATED, "voided_certificate_only",
            "观察到的证明文件均清楚标注作废，不能单独用于满足要求",
            _evidence_of(v_obs, "证明文件标注作废", plan.quote("voided_only")),
        ))
        return results, []

    field_violations: list[RequirementResult] = []
    unknowns: list[RequirementResult] = []

    for cert in valid_certs:
        n_status, n_value, n_obs = consolidate(facts.claims(cert.entity_id, "material_number"))
        c_status, c_models, c_models_obs = consolidate(facts.claims(cert.entity_id, "covered_models"))
        f_status, f_from, f_from_obs = consolidate(facts.claims(cert.entity_id, "valid_from"))
        u_status, u_until, u_until_obs = consolidate(facts.claims(cert.entity_id, "valid_until"))

        statuses = (n_status, c_status, f_status, u_status)
        if all(s == ObservationStatus.CONFIRMED_MISSING for s in statuses):
            field_violations.append(_req(
                f"cert_fields:{cert.entity_id}", RequirementStatus.VIOLATED,
                "certificate_field_confirmed_missing",
                "完整清晰的证明文件检查范围内确认规定字段缺失",
                _evidence_of(n_obs, "证明文件规定字段缺失", plan.quote("cert_fields")),
            ))
            continue
        if any(s == ObservationStatus.CONFIRMED_MISSING for s in statuses):
            field_violations.append(_req(
                f"cert_fields:{cert.entity_id}", RequirementStatus.VIOLATED,
                "certificate_field_confirmed_missing",
                "完整清晰的证明文件检查范围内确认某规定字段缺失",
                _evidence_of(n_obs if n_status == ObservationStatus.CONFIRMED_MISSING
                             else (c_models_obs if c_status == ObservationStatus.CONFIRMED_MISSING
                                   else (f_from_obs if f_status == ObservationStatus.CONFIRMED_MISSING else u_until_obs)),
                             "证明文件某规定字段缺失", plan.quote("cert_fields")),
            ))
            continue
        if any(s in (ObservationStatus.UNREADABLE, ObservationStatus.IDENTITY_UNKNOWN,
                     ObservationStatus.INCOMPLETE_SCOPE) for s in statuses):
            unknowns.append(_req(
                f"cert_fields:{cert.entity_id}", RequirementStatus.UNKNOWN,
                "certificate_field_unreadable",
                "证明文件必要字段读不清，按证据不足处理",
                _evidence_of(n_obs or c_models_obs or f_from_obs or u_until_obs, "证明字段不可读",
                             plan.quote("cert_fields")),
            ))
            continue

        # number match against label/package number
        matched = any(material_number_equal(str(n_value), ln) for ln in active_label_numbers)
        if active_label_numbers and not matched:
            field_violations.append(_req(
                f"cert_number_match:{cert.entity_id}", RequirementStatus.VIOLATED,
                "certificate_number_mismatch",
                f"证明文件材料编号 {_raw_repr(n_value)!r} 与包装或合规附签编号不一致",
                _evidence_of(n_obs, f"证明编号为 {_raw_repr(n_value)}", plan.quote("cert_number_match")),
            ))
        elif not active_label_numbers:
            unknowns.append(_req(
                f"cert_number_match:{cert.entity_id}", RequirementStatus.UNKNOWN,
                "label_number_unreadable",
                "标签编号不可读，无法跨源核对证明编号", [],
            ))

        # coverage
        if declared_model is None:
            unknowns.append(_req(
                f"cert_coverage:{cert.entity_id}", RequirementStatus.UNKNOWN,
                "declared_model_missing", "申报型号缺失，无法核对覆盖型号", []))
        elif isinstance(c_models, list) and c_models:
            if not list_contains_model([str(m) for m in c_models], declared_model):
                field_violations.append(_req(
                    f"cert_coverage:{cert.entity_id}", RequirementStatus.VIOLATED,
                    "certificate_coverage_mismatch",
                    f"覆盖型号清单 {_raw_repr(c_models)!r} 不包含申报型号 {declared_model!r}"
                    "（不接受通配符、系列简称或外观相似替代）",
                    _evidence_of(c_models_obs, f"证明覆盖型号为 {_raw_repr(c_models)}",
                                 plan.quote("cert_coverage")),
                ))
        else:
            unknowns.append(_req(
                f"cert_coverage:{cert.entity_id}", RequirementStatus.UNKNOWN,
                "certificate_coverage_unreadable",
                "覆盖型号清单不可读", _evidence_of(c_models_obs, "覆盖型号不可读",
                                                 plan.quote("cert_coverage")),
            ))

        # dates
        date_results = _validity_requirement(cert.entity_id, f_from, f_from_obs,
                                             u_until, u_until_obs, review_date_raw, plan)
        field_violations.extend(r for r in date_results if r.status == RequirementStatus.VIOLATED)
        unknowns.extend(r for r in date_results if r.status == RequirementStatus.UNKNOWN)
        results.extend(r for r in date_results if r.status == RequirementStatus.SATISFIED)

    results.extend(field_violations)
    results.extend(unknowns)
    return results, valid_certs


def _validity_requirement(entity_id: str, f_from: Any, f_from_obs: Observation | None,
                          u_until: Any, u_until_obs: Observation | None,
                          review_date_raw: str, plan: B001Plan) -> list[RequirementResult]:
    out: list[RequirementResult] = []
    quote = plan.quote("validity")
    if f_from is None and u_until is None:
        return [_req(f"validity:{entity_id}", RequirementStatus.VIOLATED, "validity_date_missing",
                     "证明文件有效起始日或截止日缺失，属于不符合要求",
                     _evidence_of(f_from_obs or u_until_obs, "有效期字段缺失", quote))]
    if f_from is None or u_until is None:
        # one side present, one missing
        return [_req(f"validity:{entity_id}", RequirementStatus.VIOLATED, "validity_date_missing",
                     "证明文件有效期字段缺失，属于不符合要求",
                     _evidence_of(f_from_obs or u_until_obs, "有效期字段缺失", quote))]
    try:
        start = parse_date(str(f_from))
    except DateParseError:
        return [_req(f"validity:{entity_id}", RequirementStatus.VIOLATED, "validity_date_invalid",
                     f"有效起始日 {_raw_repr(f_from)!r} 不是合法日期",
                     _evidence_of(f_from_obs, f"起始日为 {_raw_repr(f_from)}", quote))]
    try:
        end = parse_date(str(u_until))
    except DateParseError:
        return [_req(f"validity:{entity_id}", RequirementStatus.VIOLATED, "validity_date_invalid",
                     f"有效截止日 {_raw_repr(u_until)!r} 不是合法日期",
                     _evidence_of(u_until_obs, f"截止日为 {_raw_repr(u_until)}", quote))]
    try:
        review = parse_date(review_date_raw)
    except DateParseError:
        return [_req(f"validity:{entity_id}", RequirementStatus.UNKNOWN, "review_date_invalid",
                     f"任务审核基准日期 {review_date_raw!r} 非法，无法核验有效期", [])]
    if start > end:
        return [_req(f"validity:{entity_id}", RequirementStatus.VIOLATED, "validity_period_reversed",
                     f"有效起始日 {start} 晚于有效截止日 {end}",
                     _evidence_of(f_from_obs, f"有效期为 {start} 至 {end}", quote))]
    if review < start or review > end:
        return [_req(f"validity:{entity_id}", RequirementStatus.VIOLATED, "validity_period_exceeded",
                     f"审核基准日期 {review} 不在有效期 [{start}, {end}] 内（闭区间）",
                     _evidence_of(f_from_obs, f"有效期为 {start} 至 {end}", quote))]
    return [_req(f"validity:{entity_id}", RequirementStatus.SATISFIED, "validity_ok",
                 f"审核基准日期 {review} 在有效期 [{start}, {end}] 内（含起止日）",
                 _evidence_of(f_from_obs, f"有效期为 {start} 至 {end}", quote))]


# ---------------------------------------------------------------------------
# special conflicts (item 5) - override ordinary violations
# ---------------------------------------------------------------------------


def _conflict_results(facts: FactView, active_labels: list[Entity],
                      valid_certs: list[Entity], plan: B001Plan) -> list[RequirementResult]:
    """Item 5 special conflicts, scanned across ALL non-void labels and ALL
    non-void certificates.

    - L1: multiple non-void labels of the same packaging with DIFFERENT
      material numbers -> conflict. Same-number labels are NOT a conflict.
    - C1: two non-void certificates with the same number but DIFFERENT
      validity periods -> conflict.
    """
    results: list[RequirementResult] = []

    # L1: group active-label numbers by material-number equality.
    groups: list[tuple[str, list[tuple[str, str, Observation | None]]]] = []
    for label in active_labels:
        status, value, obs = consolidate(facts.claims(label.entity_id, "material_number"))
        if status != ObservationStatus.READABLE or value is None:
            continue
        entry = (label.entity_id, str(value), obs)
        for g in groups:
            if material_number_equal(g[0], str(value)):
                g[1].append(entry)
                break
        else:
            groups.append((str(value), [entry]))
    if len(groups) > 1:
        canon_a, members_a = groups[0]
        canon_b, members_b = groups[1]
        ea, va, oa = members_a[0]
        eb, vb, ob = members_b[0]
        results.append(_req(
            "label_number_conflict", RequirementStatus.CONFLICT, "label_number_conflict",
            f"同一包装的多份未作废标签存在不同材料编号（{va} 与 {vb}），视为材料冲突，判为 ambiguous",
            (_evidence_of(oa, f"标签 {ea} 编号 {va}", plan.quote("conflict_label_number"))
             + _evidence_of(ob, f"标签 {eb} 编号 {vb}", plan.quote("conflict_label_number"))),
        ))

    # C1: group non-void certificate periods by material-number equality.
    periods: list[tuple[str, tuple[str, str], Observation | None]] = []
    for cert in valid_certs:
        n_status, n_value, n_obs = consolidate(facts.claims(cert.entity_id, "material_number"))
        f_status, f_from, f_from_obs = consolidate(facts.claims(cert.entity_id, "valid_from"))
        u_status, u_until, u_until_obs = consolidate(facts.claims(cert.entity_id, "valid_until"))
        if not all(s == ObservationStatus.READABLE for s in (n_status, f_status, u_status)):
            continue
        try:
            parse_date(str(f_from))
            parse_date(str(u_until))
        except DateParseError:
            continue
        period = (str(f_from), str(u_until))
        existing = next((p for p in periods if material_number_equal(p[0], str(n_value))), None)
        if existing is None:
            periods.append((str(n_value), period, n_obs))
            continue
        _, first_period, first_obs = existing
        if first_period != period:
            results.append(_req(
                "certificate_period_conflict", RequirementStatus.CONFLICT,
                "certificate_period_conflict",
                f"两份未标注作废的同号证明文件有效期不同（{first_period} 与 {period}），视为材料冲突，判为 ambiguous",
                (_evidence_of(first_obs, f"证明文件有效期 {first_period}", plan.quote("conflict_cert_period"))
                 + _evidence_of(n_obs or u_until_obs, f"同号证明文件有效期 {period}",
                                plan.quote("conflict_cert_period"))),
            ))
    return results


# ---------------------------------------------------------------------------
# top-level entry
# ---------------------------------------------------------------------------


def evaluate_b001(task, plan: B001Plan, facts: FactView) -> B001Outcome:
    metadata = task.metadata.values
    applicability, missing_conditions, _ = _evaluate_applicability(metadata, plan)
    if applicability == "not_applicable":
        return B001Outcome(
            applicability="not_applicable", label="pass", reason_code="rule_not_applicable",
            reason="申报条件明确不在规则适用范围内",
            results=[_req("applicability", RequirementStatus.SATISFIED, "rule_not_applicable",
                          "存在明确不满足适用条件的申报字段", [])],
            evidence=[],
        )
    if applicability == "unknown":
        return B001Outcome(
            applicability="unknown", label="ambiguous", reason_code="applicability_unknown",
            reason=f"适用条件无法确定，缺失条件: {', '.join(missing_conditions)}",
            results=[_req("applicability", RequirementStatus.UNKNOWN, "applicability_unknown",
                          "适用条件未知", [])],
            evidence=[], missing_applicability_conditions=missing_conditions,
        )

    declared_model = metadata.get("declared_model")
    edge_mm_raw = metadata.get("package_longest_edge_mm")
    review_date_raw = task.review_date
    task_material_roles = {m.role for m in task.materials}

    label_entities = facts.entities_of_kind(EntityKind.LABEL)
    active, clearly_voided, void_unknowns = _active_labels(facts)
    results: list[RequirementResult] = list(void_unknowns)

    # Voided-only label evidence: item 5 makes this a proven violation.
    if label_entities and not active and len(clearly_voided) == len(label_entities):
        _, _, v_obs = consolidate(facts.claims(clearly_voided[0].entity_id, "void_marked"))
        results.append(_req(
            "label_voided_only", RequirementStatus.VIOLATED, "voided_label_only",
            "观察到的标签均清楚标注作废，不能作为有效证据，也不能单独用于满足要求",
            _evidence_of(v_obs, "标签标注作废", plan.quote("voided_only")),
        ))

    active_label_numbers: list[str] = []
    for label in active:
        _s, v, _o = consolidate(facts.claims(label.entity_id, "material_number"))
        if v is not None:
            active_label_numbers.append(str(v))

    results.extend(_label_field_requirements(facts, active, declared_model, plan))
    results.append(_carrier_requirement(facts, active, edge_mm_raw, plan))

    cert_results, valid_certs = _certificate_requirements(
        facts, task_material_roles, declared_model, review_date_raw, active_label_numbers, plan)
    results.extend(cert_results)

    results.extend(_conflict_results(facts, active, valid_certs, plan))

    conflicts = [r for r in results if r.status == RequirementStatus.CONFLICT]
    if conflicts:
        return B001Outcome(
            applicability="applicable", label="ambiguous", reason_code="material_conflict",
            reason="存在 B001 第 5 项定义的材料冲突，优先判为 ambiguous，不从冲突材料中任意选择一份",
            results=results,
            evidence=conflicts[0].evidence,
        )
    violations = [r for r in results if r.status == RequirementStatus.VIOLATED]
    if violations:
        return B001Outcome(
            applicability="applicable", label="reject", reason_code="proven_violation",
            reason="；".join(v.reason for v in violations[:3]),
            results=results, evidence=[e for v in violations[:3] for e in v.evidence],
        )
    unknowns = [r for r in results if r.status == RequirementStatus.UNKNOWN]
    if unknowns:
        return B001Outcome(
            applicability="applicable", label="ambiguous", reason_code="insufficient_evidence",
            reason="；".join(u.reason for u in unknowns[:3]),
            results=results, evidence=[e for u in unknowns[:3] for e in u.evidence],
        )
    return B001Outcome(
        applicability="applicable", label="pass", reason_code="all_requirements_satisfied",
        reason="全部适用要求满足",
        results=results, evidence=[e for r in results for e in r.evidence][:10],
    )


__all__ = ["B001Outcome", "applicability_precheck", "evaluate_b001"]
