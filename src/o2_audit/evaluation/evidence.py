"""Evidence quality evaluation plugin (Protocol 1.3.1).

Explicitly separates two independent verification layers:

1. **Reference structure validity** (``structure_valid``, ``structure_issues``):
   - Unit-level protocol compliance via ``o2_public_contracts.contracts.prediction_errors``.
   - Per-evidence-item checks that explicitly distinguish:
     * ``material_not_found`` (missing or foreign ``material_id`` not belonging to the unit)
     * ``page_index_invalid`` (bool, non-int, <1) / ``wrong_source_page`` (page does not match material's ``page_index``)
     * ``bbox_invalid`` (non-finite, bool, out of ``[0, 1]``, unordered ``x1 >= x2`` or ``y1 >= y2``) — NEVER silently fixed
     * ``fact_empty`` / ``duplicate_evidence``

2. **Semantic content grounding** (``grounded: True | False | None``):
   - Checks claimed evidence ``fact`` and optional ``bbox`` against structured
     reference ``visible_facts`` (from public/evaluation ``references``) OR an
     optional pluggable ``VisualEvidenceVerifier`` hook.
   - Detects:
     * contradicted field values (e.g. claiming a wrong model, material number, or date)
     * fabricated values when reference marks the region ``readable=false``
     * contradicted absence claims (claiming field missing when reference shows it readable)
     * region/bbox mismatch (``iou < bbox_iou_min`` against the matched reference fact)
     * wrong source page/material (fact actually belongs to a different material/page)
     * claims absent from a complete reference ``visible_facts`` list
   - When NEITHER reference ``visible_facts`` NOR a visual verifier is available,
     returns ``grounded=None`` with explicit ``unverifiable_reason`` — NEVER
     reports ``grounded=True`` merely because a file or ``material_id`` exists!
"""
from __future__ import annotations

import math
import re
import unicodedata
from typing import Any, Protocol, runtime_checkable

from o2_public_contracts import contracts as public_contracts

PLUGIN_ID = "o2.evidence"
PLUGIN_VERSION = "1.0.0"
INPUT_SCHEMA_VERSION = "1.3.1"

DEFAULT_BBOX_IOU_MIN = 0.3

_QLAB_RE = re.compile(r"\bQLAB-[0-9A-Za-z]+\b")
_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_ABSENCE_MARKERS = (
    "缺失",
    "缺少",
    "缺型号",
    "缺材料编号",
    "未见",
    "无此字段",
    "没有型号",
    "没有材料编号",
    "confirmed_missing",
    "missing",
    "absent",
)
_UNREADABLE_MARKERS = (
    "不可读",
    "模糊",
    "遮挡",
    "无法辨认",
    "unreadable",
    "illegible",
    "blurred",
    "obstructed",
)
_INCOMPLETE_MARKERS = (
    "裁切",
    "截断",
    "范围不完整",
    "incomplete_scope",
    "cropped",
    "truncated",
)
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "model": ("型号", "覆盖型号", "model", "covered_models"),
    "material_number": ("材料编号", "编号", "material_number", "qlab"),
    "valid_from": ("有效起始日", "起始日", "valid_from"),
    "valid_until": ("有效截止日", "截止日", "有效期至", "valid_until"),
    "issue_date": ("签发日", "签发日期", "issue_date"),
    "withdrawal_date": ("撤回日", "撤回日期", "withdrawal_date"),
}


@runtime_checkable
class VisualEvidenceVerifier(Protocol):
    """Pluggable hook for optional visual re-verification of evidence crops."""

    def verify(
        self,
        task: dict[str, Any],
        evidence_item: dict[str, Any],
    ) -> dict[str, Any]:
        """Return ``{"grounded": True|False|None, "reason": str, "detail": ...}``."""
        ...


def _norm_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text).strip()


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", _norm_text(text)).lower()


def _parse_kv(text: str) -> tuple[str | None, str | None]:
    norm = _norm_text(text)
    for sep in (":", "="):
        if sep in norm:
            k, v = norm.split(sep, 1)
            k_s, v_s = k.strip(), v.strip()
            if k_s and v_s:
                return k_s, v_s
    return None, None


def _canonical_field(label: str) -> str | None:
    low = _compact(label)
    for canonical, aliases in _FIELD_ALIASES.items():
        for alias in aliases:
            if _compact(alias) in low:
                return canonical
    return None


def _has_any_marker(text: str, markers: tuple[str, ...]) -> bool:
    low = _norm_text(text).lower()
    return any(m in low for m in markers)


def _check_evidence_item_structure(
    ev: Any,
    mats_by_id: dict[str, dict[str, Any]],
) -> list[str]:
    """Detailed per-item structural validation separating missing material vs bbox vs page."""
    issues: list[str] = []
    if not isinstance(ev, dict):
        return ["evidence_item_not_object"]

    mid = ev.get("material_id")
    mat = mats_by_id.get(mid) if isinstance(mid, str) else None
    if not isinstance(mid, str) or not mid.strip() or mat is None:
        issues.append("material_not_found")

    page = ev.get("page_index")
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        issues.append("page_index_invalid")
    elif mat is not None and page != mat.get("page_index"):
        issues.append("wrong_source_page")

    fact = ev.get("fact")
    if not isinstance(fact, str) or not fact.strip() or len(fact) > 2000:
        issues.append("fact_empty")

    if "bbox" in ev and ev["bbox"] is not None:
        bbox = ev["bbox"]
        if not isinstance(bbox, list) or len(bbox) != 4:
            issues.append("bbox_invalid")
        else:
            bad = False
            for val in bbox:
                if isinstance(val, bool) or not isinstance(val, (int, float)):
                    bad = True
                    break
                fval = float(val)
                if not math.isfinite(fval) or fval < 0.0 or fval > 1.0:
                    bad = True
                    break
            if bad or not (bbox[0] < bbox[2] and bbox[1] < bbox[3]):
                issues.append("bbox_invalid")

    if "rule_quote" in ev and ev["rule_quote"] is not None:
        rq = ev["rule_quote"]
        if not isinstance(rq, str) or not rq.strip() or len(rq) > 2000:
            issues.append("rule_quote_invalid")

    return issues


def _extract_ref_facts_for_unit(
    ref: dict[str, Any] | None,
) -> tuple[dict[tuple[str, int], dict[str, Any]], list[tuple[str, int, dict[str, Any]]]]:
    """Index reference evidence by ``(material_id, page_index)`` and flat list."""
    by_mp: dict[tuple[str, int], dict[str, Any]] = {}
    flat: list[tuple[str, int, dict[str, Any]]] = []
    if not isinstance(ref, dict):
        return by_mp, flat
    ref_ev = ref.get("evidence")
    if not isinstance(ref_ev, list):
        return by_mp, flat
    for entry in ref_ev:
        if not isinstance(entry, dict):
            continue
        mid = entry.get("material_id")
        pg = entry.get("page_index")
        if not isinstance(mid, str) or isinstance(pg, bool) or not isinstance(pg, int):
            continue
        vf_raw = entry.get("visible_facts")
        if not isinstance(vf_raw, list):
            continue
        by_mp[(mid, pg)] = entry
        for vf in vf_raw:
            if isinstance(vf, dict):
                flat.append((mid, pg, vf))
    return by_mp, flat


def _fact_matches_visible_fact(claim_fact: str, vf_text: str) -> bool:
    """True when claimed fact matches a reference ``visible_facts`` entry."""
    c_norm = _compact(claim_fact)
    v_norm = _compact(vf_text)
    if not c_norm or not v_norm:
        return False
    if c_norm == v_norm or v_norm in c_norm or c_norm in v_norm:
        return True
    ck, cv = _parse_kv(claim_fact)
    vk, vv = _parse_kv(vf_text)
    if ck and vk and cv and vv:
        c_field = _canonical_field(ck)
        v_field = _canonical_field(vk)
        if c_field and c_field == v_field and _compact(cv) == _compact(vv):
            return True
    return False


def _verify_against_reference(
    ev: dict[str, Any],
    ref_entry: dict[str, Any] | None,
    all_ref_facts: list[tuple[str, int, dict[str, Any]]],
    bbox_iou_min: float,
) -> dict[str, Any]:
    """Deterministic semantic content check against reference ``visible_facts``."""
    fact_str = _norm_text(str(ev.get("fact", "")))
    pred_bbox = ev.get("bbox")
    mid = ev["material_id"]
    page = ev["page_index"]

    if ref_entry is None:
        # Reference has no visible_facts for this (material_id, page_index).
        # Check if the claimed text actually lives on ANOTHER material/page in reference!
        if all_ref_facts:
            for other_mid, other_pg, vf in all_ref_facts:
                vf_text = str(vf.get("text") or "")
                if vf_text and _fact_matches_visible_fact(fact_str, vf_text):
                    return {
                        "grounded": False,
                        "semantic_status": "contradicted",
                        "semantic_issues": ["wrong_source_page_or_material"],
                        "reason": (
                            f"claimed fact matches reference on ({other_mid}, page {other_pg}), "
                            f"not ({mid}, page {page})"
                        ),
                    }
        return {
            "grounded": None,
            "semantic_status": "unverifiable",
            "semantic_issues": [],
            "unverifiable_reason": "no_reference_visible_facts_for_material",
        }

    visible_facts: list[dict[str, Any]] = [
        vf for vf in ref_entry.get("visible_facts", []) if isinstance(vf, dict)
    ]
    cut_coords = ref_entry.get("cut_coords")
    shape = ref_entry.get("shape") if isinstance(ref_entry.get("shape"), dict) else {}

    # 0. Structured matrix_cells support (e.g. R050 component-model matrices)
    matrix_cells = shape.get("matrix_cells") if isinstance(shape.get("matrix_cells"), dict) else None
    if matrix_cells:
        c_compact = _compact(fact_str)
        matched_cells = [
            cell
            for cell in matrix_cells.values()
            if isinstance(cell, dict)
            and isinstance(cell.get("component_id"), str)
            and isinstance(cell.get("model"), str)
            and _compact(cell["component_id"]) in c_compact
            and _compact(cell["model"]) in c_compact
        ]
        if matched_cells:
            cell = matched_cells[0]
            cell_status = cell.get("status")
            cell_val = cell.get("value")
            cell_bbox = cell.get("bbox")
            if _has_any_marker(fact_str, _UNREADABLE_MARKERS):
                if cell_status != "unreadable":
                    return {
                        "grounded": False,
                        "semantic_status": "contradicted",
                        "semantic_issues": ["unreadable_claim_contradicted"],
                        "reason": f"matrix cell ({cell['component_id']}, {cell['model']}) is readable in reference",
                    }
            else:
                if cell_status == "unreadable":
                    return {
                        "grounded": False,
                        "semantic_status": "contradicted",
                        "semantic_issues": ["unreadable_fact_fabricated"],
                        "reason": f"matrix cell ({cell['component_id']}, {cell['model']}) is unreadable in reference",
                    }
                claims_present = any(tok in fact_str for tok in ("有记录", "黑点", "存在", "配对"))
                claims_absent = any(tok in fact_str for tok in ("无记录", "空白", "没有黑点"))
                if claims_present and not claims_absent and cell_val == "无记录":
                    return {
                        "grounded": False,
                        "semantic_status": "contradicted",
                        "semantic_issues": ["value_contradicted"],
                        "reason": f"claimed pairing in ({cell['component_id']}, {cell['model']}), but reference cell is 无记录",
                    }
                if claims_absent and not claims_present and cell_val == "有记录":
                    return {
                        "grounded": False,
                        "semantic_status": "contradicted",
                        "semantic_issues": ["value_contradicted"],
                        "reason": f"claimed empty cell in ({cell['component_id']}, {cell['model']}), but reference cell is 有记录",
                    }
            if pred_bbox is not None and isinstance(cell_bbox, list) and len(cell_bbox) == 4:
                score_iou = public_contracts.iou(pred_bbox, cell_bbox)
                if score_iou < bbox_iou_min:
                    return {
                        "grounded": False,
                        "semantic_status": "contradicted",
                        "semantic_issues": ["bbox_region_mismatch"],
                        "reason": f"matrix cell bbox IoU {score_iou:.3f} < {bbox_iou_min}",
                    }
            return {
                "grounded": True,
                "semantic_status": "supported",
                "semantic_issues": [],
                "reason": "supported by reference matrix_cells",
            }

    # 1. Explicit unreadable claim
    if _has_any_marker(fact_str, _UNREADABLE_MARKERS):
        unreadable_vfs = [vf for vf in visible_facts if vf.get("readable") is False]
        if unreadable_vfs:
            if pred_bbox is not None:
                best_iou = max(
                    (
                        public_contracts.iou(pred_bbox, vf["bbox"])
                        for vf in unreadable_vfs
                        if isinstance(vf.get("bbox"), list) and len(vf["bbox"]) == 4
                    ),
                    default=1.0,
                )
                if best_iou < bbox_iou_min:
                    return {
                        "grounded": False,
                        "semantic_status": "contradicted",
                        "semantic_issues": ["bbox_region_mismatch"],
                        "reason": f"unreadable claim bbox IoU {best_iou:.3f} < {bbox_iou_min}",
                    }
            return {
                "grounded": True,
                "semantic_status": "supported",
                "semantic_issues": [],
                "reason": "unreadable claim matches reference readable=false fact",
            }
        if visible_facts and all(vf.get("readable") is True for vf in visible_facts):
            return {
                "grounded": False,
                "semantic_status": "contradicted",
                "semantic_issues": ["unreadable_claim_contradicted"],
                "reason": "claimed unreadable, but all reference visible_facts on this page are readable=true",
            }
        return {
            "grounded": None,
            "semantic_status": "unverifiable",
            "semantic_issues": [],
            "unverifiable_reason": "reference_has_no_readability_facts",
        }

    # 2. Explicit incomplete-scope / cut claim
    if _has_any_marker(fact_str, _INCOMPLETE_MARKERS):
        if cut_coords is not None:
            return {
                "grounded": True,
                "semantic_status": "supported",
                "semantic_issues": [],
                "reason": "incomplete-scope claim matches reference cut_coords",
            }
        return {
            "grounded": False,
            "semantic_status": "contradicted",
            "semantic_issues": ["incomplete_scope_contradicted"],
            "reason": "claimed cropped/incomplete scope, but reference cut_coords is null",
        }

    # 3. Explicit absence claim (e.g. "缺型号" / "confirmed_missing")
    if _has_any_marker(fact_str, _ABSENCE_MARKERS):
        if cut_coords is not None:
            return {
                "grounded": False,
                "semantic_status": "contradicted",
                "semantic_issues": ["absence_claimed_on_incomplete_scope"],
                "reason": "cannot establish confirmed absence when reference shows cropped/incomplete scope",
            }
        claimed_field = _canonical_field(fact_str)
        if claimed_field is not None:
            for vf in visible_facts:
                vk, _ = _parse_kv(str(vf.get("text") or ""))
                if vk and _canonical_field(vk) == claimed_field:
                    if vf.get("readable") is True:
                        return {
                            "grounded": False,
                            "semantic_status": "contradicted",
                            "semantic_issues": ["absence_claim_contradicted"],
                            "reason": f"claimed {claimed_field} missing, but reference shows '{vf.get('text')}'",
                        }
                    return {
                        "grounded": False,
                        "semantic_status": "contradicted",
                        "semantic_issues": ["unreadable_conflated_with_missing"],
                        "reason": f"claimed {claimed_field} missing, but reference marks it unreadable",
                    }
            return {
                "grounded": True,
                "semantic_status": "supported",
                "semantic_issues": [],
                "reason": f"confirmed absence of {claimed_field} is consistent with complete reference visible_facts",
            }
        return {
            "grounded": None,
            "semantic_status": "unverifiable",
            "semantic_issues": [],
            "unverifiable_reason": "absence_claim_does_not_name_verifiable_field",
        }

    # 4. Positive factual claim: match against reference visible_facts on this page
    matched_vfs: list[dict[str, Any]] = []
    for vf in visible_facts:
        vf_text = str(vf.get("text") or "")
        if vf_text and _fact_matches_visible_fact(fact_str, vf_text):
            matched_vfs.append(vf)

    if matched_vfs:
        readable_matches = [vf for vf in matched_vfs if vf.get("readable") is not False]
        if not readable_matches:
            return {
                "grounded": False,
                "semantic_status": "contradicted",
                "semantic_issues": ["unreadable_fact_fabricated"],
                "reason": "claimed readable text on a region marked readable=false in reference",
            }
        if pred_bbox is not None:
            ref_boxes = [
                vf["bbox"]
                for vf in readable_matches
                if isinstance(vf.get("bbox"), list) and len(vf["bbox"]) == 4
            ]
            if ref_boxes:
                best_iou = max(public_contracts.iou(pred_bbox, rb) for rb in ref_boxes)
                if best_iou < bbox_iou_min:
                    return {
                        "grounded": False,
                        "semantic_status": "contradicted",
                        "semantic_issues": ["bbox_region_mismatch"],
                        "reason": f"claimed bbox IoU {best_iou:.3f} < {bbox_iou_min} against matched reference fact",
                    }
        return {
            "grounded": True,
            "semantic_status": "supported",
            "semantic_issues": [],
            "reason": "supported by reference visible_facts",
        }

    # 5. Contradiction checks when no exact match:
    # (a) Same canonical field key present with a DIFFERENT value
    ck, cv = _parse_kv(fact_str)
    if ck and cv:
        c_field = _canonical_field(ck)
        if c_field is not None:
            for vf in visible_facts:
                vk, vv = _parse_kv(str(vf.get("text") or ""))
                if vk and vv and _canonical_field(vk) == c_field:
                    if vf.get("readable") is False:
                        return {
                            "grounded": False,
                            "semantic_status": "contradicted",
                            "semantic_issues": ["unreadable_fact_fabricated"],
                            "reason": f"claimed {c_field}={cv!r}, but reference marks field unreadable",
                        }
                    if _compact(cv) != _compact(vv):
                        return {
                            "grounded": False,
                            "semantic_status": "contradicted",
                            "semantic_issues": ["value_contradicted"],
                            "reason": f"claimed {c_field}={cv!r} contradicts reference {vv!r}",
                        }

    # (b) Specific QLAB code or ISO date claimed that contradicts reference visible_facts
    claimed_qlabs = set(_QLAB_RE.findall(fact_str))
    claimed_dates = set(_DATE_RE.findall(fact_str))
    ref_all_text = " ".join(
        str(vf.get("text") or "") for vf in visible_facts if vf.get("readable") is not False
    )
    if claimed_qlabs:
        ref_qlabs = set(_QLAB_RE.findall(ref_all_text))
        if ref_qlabs and not (claimed_qlabs & ref_qlabs):
            return {
                "grounded": False,
                "semantic_status": "contradicted",
                "semantic_issues": ["value_contradicted"],
                "reason": f"claimed material number {sorted(claimed_qlabs)} contradicts reference {sorted(ref_qlabs)}",
            }
    if claimed_dates:
        ref_dates = set(_DATE_RE.findall(ref_all_text))
        if ref_dates and not (claimed_dates & ref_dates):
            return {
                "grounded": False,
                "semantic_status": "contradicted",
                "semantic_issues": ["value_contradicted"],
                "reason": f"claimed date {sorted(claimed_dates)} contradicts reference {sorted(ref_dates)}",
            }

    # (c) Fact actually appears on a DIFFERENT material/page in the unit's reference
    for other_mid, other_pg, vf in all_ref_facts:
        if (other_mid, other_pg) == (mid, page):
            continue
        vf_text = str(vf.get("text") or "")
        if vf_text and _fact_matches_visible_fact(fact_str, vf_text):
            return {
                "grounded": False,
                "semantic_status": "contradicted",
                "semantic_issues": ["wrong_source_page_or_material"],
                "reason": (
                    f"claimed fact belongs to ({other_mid}, page {other_pg}), "
                    f"not cited ({mid}, page {page})"
                ),
            }

    # (d) Reference has concrete visible_facts on this page and claim doesn't match any of them
    if visible_facts:
        return {
            "grounded": False,
            "semantic_status": "unsupported",
            "semantic_issues": ["claim_not_in_reference_visible_facts"],
            "reason": "claimed fact is not supported by reference visible_facts on this source page",
        }

    return {
        "grounded": None,
        "semantic_status": "unverifiable",
        "semantic_issues": [],
        "unverifiable_reason": "reference_visible_facts_empty",
    }


def evaluate(
    inputs: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    references: list[dict[str, Any]],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Protocol 1.3.1 evidence-quality plugin ``evaluate(inputs, outputs, references, context)``."""
    ctx = context if isinstance(context, dict) else {}
    bbox_iou_min = float(ctx.get("bbox_iou_min", DEFAULT_BBOX_IOU_MIN))
    visual_verifier = ctx.get("visual_verifier")
    if visual_verifier is not None and not isinstance(visual_verifier, VisualEvidenceVerifier):
        visual_verifier = None

    tasks_by_id = {
        t["audit_unit_id"]: t
        for t in (inputs or [])
        if isinstance(t, dict) and isinstance(t.get("audit_unit_id"), str)
    }
    refs_by_id = {
        r["audit_unit_id"]: r
        for r in (references or [])
        if isinstance(r, dict) and isinstance(r.get("audit_unit_id"), str)
    }

    diagnostics: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    total_units = 0
    structurally_valid_units = 0
    total_items = 0
    structurally_valid_items = 0
    structurally_invalid_items = 0
    supported_items = 0
    unsupported_items = 0
    unverifiable_items = 0

    for pred in outputs or []:
        if not isinstance(pred, dict):
            errors.append({"code": "prediction_not_object", "detail": "non-dict prediction entry"})
            continue
        uid = pred.get("audit_unit_id")
        task = tasks_by_id.get(uid) if isinstance(uid, str) else None
        if task is None:
            diagnostics.append({
                "audit_unit_id": uid,
                "structure_valid": False,
                "unit_structure_issues": ["unknown_audit_unit_id"],
                "evidence_checks": [],
            })
            total_units += 1
            continue

        total_units += 1
        unit_issues = public_contracts.prediction_errors(pred, task)
        mats_by_id = {
            m["material_id"]: m
            for m in task.get("materials", [])
            if isinstance(m, dict) and isinstance(m.get("material_id"), str)
        }
        ref = refs_by_id.get(uid)
        ref_by_mp, all_ref_facts = _extract_ref_facts_for_unit(ref)

        ev_list = pred.get("evidence")
        ev_checks: list[dict[str, Any]] = []
        seen_keys: set[tuple[Any, ...]] = set()
        any_item_struct_bad = False

        if isinstance(ev_list, list):
            for idx, ev in enumerate(ev_list):
                total_items += 1
                item_issues = _check_evidence_item_structure(ev, mats_by_id)
                if isinstance(ev, dict) and not item_issues:
                    dedup_key = (
                        ev.get("material_id"),
                        ev.get("page_index"),
                        _compact(str(ev.get("fact", ""))),
                        tuple(ev["bbox"]) if isinstance(ev.get("bbox"), list) else None,
                    )
                    if dedup_key in seen_keys:
                        item_issues.append("duplicate_evidence")
                    else:
                        seen_keys.add(dedup_key)

                struct_ok = len(item_issues) == 0
                if struct_ok:
                    structurally_valid_items += 1
                else:
                    structurally_invalid_items += 1
                    any_item_struct_bad = True

                # Semantic verification runs ONLY on structurally valid items.
                if not struct_ok:
                    sem = {
                        "grounded": False,
                        "semantic_status": "invalid_reference",
                        "semantic_issues": list(item_issues),
                        "reason": "structurally invalid evidence reference cannot be grounded",
                    }
                    unsupported_items += 1
                else:
                    ref_entry = ref_by_mp.get((ev["material_id"], ev["page_index"]))
                    sem = _verify_against_reference(
                        ev, ref_entry, all_ref_facts, bbox_iou_min
                    )
                    if sem["grounded"] is None and visual_verifier is not None:
                        sem = visual_verifier.verify(task, ev)

                    if sem["grounded"] is True:
                        supported_items += 1
                    elif sem["grounded"] is False:
                        unsupported_items += 1
                    else:
                        unverifiable_items += 1

                ev_checks.append({
                    "index": idx,
                    "material_id": ev.get("material_id") if isinstance(ev, dict) else None,
                    "page_index": ev.get("page_index") if isinstance(ev, dict) else None,
                    "structure_valid": struct_ok,
                    "structure_issues": item_issues,
                    **sem,
                })

        unit_struct_ok = (len(unit_issues) == 0) and (not any_item_struct_bad)
        if unit_struct_ok:
            structurally_valid_units += 1

        diagnostics.append({
            "audit_unit_id": uid,
            "rule_id": task.get("rule_id"),
            "rule_version": task.get("rule_version"),
            "structure_valid": unit_struct_ok,
            "unit_structure_issues": unit_issues,
            "evidence_checks": ev_checks,
        })

    verifiable_total = supported_items + unsupported_items
    metrics = {
        "total_units": total_units,
        "structurally_valid_units": structurally_valid_units,
        "structurally_invalid_units": total_units - structurally_valid_units,
        "total_evidence_items": total_items,
        "structurally_valid_items": structurally_valid_items,
        "structurally_invalid_items": structurally_invalid_items,
        "semantically_supported_items": supported_items,
        "semantically_unsupported_items": unsupported_items,
        "semantically_unverifiable_items": unverifiable_items,
        "structure_validity_rate": (
            structurally_valid_items / total_items if total_items else None
        ),
        "semantic_grounding_rate": (
            supported_items / verifiable_total if verifiable_total else None
        ),
    }

    return {
        "plugin_id": PLUGIN_ID,
        "plugin_version": PLUGIN_VERSION,
        "input_schema_version": INPUT_SCHEMA_VERSION,
        "metrics": metrics,
        "diagnostics": diagnostics,
        "errors": errors,
        "official": False,
        "limitation": (
            "Semantic grounding verifies claims deterministically against structured "
            "reference visible_facts (or an attached VisualEvidenceVerifier). When "
            "neither is provided, grounded is explicitly null — never fabricated as true."
        ),
    }
