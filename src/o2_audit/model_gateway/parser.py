"""Strict parser for model observation output (Stage 03a).

Converts model text into a validated :class:`ObservationBatch`. Contract:

- Handles optional ```json fenced blocks; parses via the vendored public
  ``strict_loads`` (duplicate keys / NaN / Infinity rejected).
- NEVER accepts a model-emitted compliance label as authoritative: forbidden
  fields (``label``, ``verdict``, ... anything outside the entity/observation
  schemas) cause the offending item to be DROPPED and flagged, not honored.
- ``entities`` and ``observations`` are validated strictly against the typed
  domain models; unknown predicates are rejected (free-form predicate strings
  are a programming error per the vocabulary contract).
- Source membership: every observation's ``source.material_id`` /
  ``source.page_index`` must belong to the materials actually supplied in the
  current request. Unknown material citations drop the observation; omitted
  ``material_id`` binds to the single supplied material when unambiguous;
  mismatched page indices are corrected to the legal source page; invalid /
  inverted / out-of-range bboxes are discarded (bbox=None) — coordinates are
  never fabricated.
- ``raw_value`` is preserved verbatim; ``normalized_value`` is recomputed with
  the shared operators (model normalization for model predicates, NFKC+strip
  for other strings) — never taken from the model.
- ``confirmed_missing`` with ``scope_complete=False`` is downgraded to
  ``unreadable`` so a false absence can never slip through.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict

from o2_audit.domain.models import (
    Entity,
    ExecutionError,
    MaterialRef,
    Observation,
    ObservationSource,
    ObservationStatus,
    validate_bbox_tuple,
)
from o2_audit.model_gateway.prompts import KNOWN_PREDICATES
from o2_audit.operators.text import annotation_nfkc_strip, model_normalize
from o2_audit.runtime.gateway import ObservationBatch
from o2_public_contracts import contracts as public_contracts

#: Predicates whose string values get business model normalization.
_MODEL_PREDICATES = frozenset({
    "model", "main_model", "old_model", "figure_model",
})
#: Predicates whose list elements are model strings.
_MODEL_LIST_PREDICATES = frozenset({
    "compatible_models", "covered_models", "component_models",
})

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

#: Callable applied after bbox validation to map crop-local coordinates into
#: full-source coordinates. Returns None to signal "discard this bbox".
BboxRemapper = Callable[[str, tuple[float, float, float, float]],
                        tuple[float, float, float, float] | None]

_ANNOTATION_STATUSES = ("value", "unreadable", "invalid_value")
_ALLOWED_OBS_KEYS = frozenset({
    "entity_id",
    "predicate",
    "status",
    "raw_value",
    "normalized_value",
    "scope_complete",
    "source",
})
_ALLOWED_TOP_KEYS = frozenset({"entities", "observations"})
_ALLOWED_ANN_KEYS = frozenset({"text", "status", "bbox", "material_id", "page_index"})


class AnnotationResult(BaseModel):
    """Stage 03b target-directed extraction result (annotations, not labels)."""

    model_config = ConfigDict(extra="forbid")

    text: str | None = None
    status: str  # value | unreadable | invalid_value (validated in parse_annotation)
    bbox: tuple[float, float, float, float] | None = None
    source: ObservationSource | None = None


@dataclass
class ParseReport:
    """Diagnostics of one parse round (dropped/flagged/corrected items)."""

    dropped: list[dict[str, Any]] = field(default_factory=list)
    corrections: list[str] = field(default_factory=list)

    def drop(self, reason: str, item: Any) -> None:
        self.dropped.append({"reason": reason, "item": item})

    def correct(self, message: str) -> None:
        self.corrections.append(message)

    @property
    def clean(self) -> bool:
        return not self.dropped and not self.corrections


def extract_json_text(text: str) -> str:
    """Strip an optional ```json fence; fall back to the outermost braces."""
    s = text.strip()
    m = _FENCE_RE.search(s)
    if m:
        return m.group(1).strip()
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end > start:
        return s[start:end + 1]
    return s


def _normalize_value(predicate: str, raw: Any) -> Any:
    """Business normalization with the SHARED operators only (never the
    model's own normalized output; ``raw_value`` is never mutated)."""
    if raw is None:
        return None
    if isinstance(raw, str):
        if predicate in _MODEL_PREDICATES:
            return model_normalize(raw)
        return annotation_nfkc_strip(raw)
    if isinstance(raw, list):
        if predicate in _MODEL_LIST_PREDICATES:
            return [
                model_normalize(item) if isinstance(item, str) else item
                for item in raw
            ]
        return [_normalize_value(predicate, item) for item in raw]
    # bool/int/float/dict pass through unchanged (strict JSON types).
    return raw


def _parse_source(
    source: Any,
    allowed_materials: dict[str, MaterialRef],
    report: ParseReport,
) -> tuple[ObservationSource | None, bool]:
    """Bind provenance to the materials actually supplied.

    Returns ``(source, ok)``. ``ok=False`` means the observation must be
    dropped (unknown material citation).

    - material omitted + exactly one supplied material -> bind to it;
    - material omitted + several supplied -> source stays None (never guess);
    - page_index is corrected to the legal source page of that material.
    """
    if source is None:
        if len(allowed_materials) == 1:
            ref = next(iter(allowed_materials.values()))
            return ObservationSource(
                material_id=ref.material_id, page_index=ref.page_index), True
        return None, True
    if not isinstance(source, dict):
        report.drop("source must be an object", source)
        return None, False
    material_id = source.get("material_id")
    if material_id is None:
        if len(allowed_materials) == 1:
            ref = next(iter(allowed_materials.values()))
            return ObservationSource(
                material_id=ref.material_id, page_index=ref.page_index), True
        return None, True
    if not isinstance(material_id, str) or material_id not in allowed_materials:
        report.drop(
            f"observation cites unknown material_id {material_id!r} "
            "(not among the materials supplied in this request)", source)
        return None, False
    ref = allowed_materials[material_id]
    declared_page = source.get("page_index")
    if declared_page is not None and declared_page != ref.page_index:
        report.correct(
            f"source page_index {declared_page!r} for material {material_id!r} corrected "
            f"to the legal source page {ref.page_index}")
    return ObservationSource(material_id=ref.material_id, page_index=ref.page_index), True


def _parse_bbox(
    raw_bbox: Any,
    material_id: str | None,
    remapper: BboxRemapper | None,
    report: ParseReport,
) -> tuple[float, float, float, float] | None:
    """Validate a model bbox (or discard it — coordinates are never invented)."""
    if raw_bbox is None:
        return None
    try:
        bbox = validate_bbox_tuple(raw_bbox)
    except ValueError as e:
        report.drop(f"invalid bbox discarded ({e})", raw_bbox)
        return None
    if remapper is not None:
        bbox = remapper(material_id or "", bbox)
        if bbox is None:
            report.drop("bbox discarded by crop remapping", raw_bbox)
            return None
    return bbox


def parse_observation_batch(
    text: str,
    allowed_materials: dict[str, MaterialRef],
    *,
    bbox_remapper: BboxRemapper | None = None,
) -> tuple[ObservationBatch, ParseReport]:
    """Parse model text into a validated batch + diagnostics report.

    Raises ``ExecutionError("gateway_parse_error")`` for structurally broken
    output (not JSON / not an object / wrong container types).
    """
    try:
        data = public_contracts.strict_loads(extract_json_text(text))
    except public_contracts.ContractError as e:
        raise ExecutionError("gateway_parse_error", f"model output is not strict JSON: {e}") from e
    if not isinstance(data, dict):
        raise ExecutionError("gateway_parse_error", "model output must be a JSON object")
    entities_raw = data.get("entities", [])
    observations_raw = data.get("observations", [])
    if not isinstance(entities_raw, list) or not isinstance(observations_raw, list):
        raise ExecutionError(
            "gateway_parse_error", "model output entities/observations must be JSON arrays")

    report = ParseReport()

    extra_top = sorted(set(data) - _ALLOWED_TOP_KEYS)
    if extra_top:
        report.drop(f"forbidden extra top-level fields ignored: {extra_top}", {k: data[k] for k in extra_top})

    # -- entities: typed validation; forbidden fields (label/verdict/...)
    #    violate extra="forbid" and drop the whole entity — never honored.
    entities: list[Entity] = []
    entity_ids: set[str] = set()
    for item in entities_raw:
        try:
            entity = Entity.model_validate(item)
        except Exception as e:
            report.drop(f"invalid entity dropped ({e})", item)
            continue
        entity_ids.add(entity.entity_id)
        entities.append(entity)

    # -- observations -----------------------------------------------------------
    observations: list[Observation] = []
    for item in observations_raw:
        if not isinstance(item, dict):
            report.drop("observation must be an object", item)
            continue
        extra_keys = sorted(set(item) - _ALLOWED_OBS_KEYS)
        if extra_keys:
            report.drop(
                f"forbidden extra fields on observation (extra_forbid): {extra_keys}",
                item,
            )
            continue
        predicate = item.get("predicate")
        if not isinstance(predicate, str) or predicate not in KNOWN_PREDICATES:
            report.drop(f"unknown predicate {predicate!r} (free-form strings are rejected)", item)
            continue
        entity_id = item.get("entity_id")
        if not isinstance(entity_id, str) or entity_id not in entity_ids:
            report.drop(f"observation cites unknown entity_id {entity_id!r}", item)
            continue
        status_raw = item.get("status")
        try:
            status = ObservationStatus(status_raw)
        except ValueError:
            report.drop(f"unknown observation status {status_raw!r}", item)
            continue
        source, ok = _parse_source(item.get("source"), allowed_materials, report)
        if not ok:
            continue
        scope_complete = item.get("scope_complete", False)
        if not isinstance(scope_complete, bool):
            report.drop("scope_complete must be a boolean", item)
            continue
        raw_value = item.get("raw_value")
        normalized_value = _normalize_value(predicate, raw_value)
        if status == ObservationStatus.CONFIRMED_MISSING and not scope_complete:
            # Downgrade: a claimed absence without a complete clear scope is
            # never accepted (it would fabricate absence evidence).
            report.correct(
                f"confirmed_missing without scope_complete downgraded to unreadable "
                f"(entity={entity_id}, predicate={predicate})")
            status = ObservationStatus.UNREADABLE
            raw_value = None
            normalized_value = None
        raw_source = item.get("source")
        raw_bbox = raw_source.get("bbox") if isinstance(raw_source, dict) else None
        bbox = _parse_bbox(
            raw_bbox,
            source.material_id if source is not None else None,
            bbox_remapper, report)
        model_source = ObservationSource(
            material_id=source.material_id,
            page_index=source.page_index,
            bbox=bbox,
        ) if source is not None else None
        try:
            observation = Observation(
                entity_id=entity_id,
                predicate=predicate,
                status=status,
                raw_value=raw_value,
                normalized_value=normalized_value,
                scope_complete=scope_complete,
                source=model_source,
            )
        except Exception as e:
            report.drop(f"observation failed typed validation ({e})", item)
            continue
        observations.append(observation)

    return ObservationBatch(entities=entities, observations=observations), report


def parse_annotation(
    text: str,
    allowed_materials: dict[str, MaterialRef],
    *,
    bbox_remapper: BboxRemapper | None = None,
) -> tuple[AnnotationResult, ParseReport]:
    """Parse Stage 03b target-directed annotation output.

    ``status`` must be exactly ``value`` | ``unreadable`` | ``invalid_value``;
    the verbatim ``text`` keeps NFKC+strip as its ONLY normalization (internal
    case/punctuation/spaces preserved). Invalid bboxes become None — never
    fabricated.
    """
    try:
        data = public_contracts.strict_loads(extract_json_text(text))
    except public_contracts.ContractError as e:
        raise ExecutionError("gateway_parse_error", f"annotation output is not strict JSON: {e}") from e
    if not isinstance(data, dict):
        raise ExecutionError("gateway_parse_error", "annotation output must be a JSON object")
    report = ParseReport()
    extra_ann = sorted(set(data) - _ALLOWED_ANN_KEYS)
    if extra_ann:
        report.drop(f"forbidden extra fields in annotation ignored: {extra_ann}", {k: data[k] for k in extra_ann})
    status_raw = data.get("status")
    if status_raw not in _ANNOTATION_STATUSES:
        raise ExecutionError(
            "gateway_parse_error",
            f"annotation status must be value|unreadable|invalid_value, got {status_raw!r}")
    raw_text = data.get("text")
    if raw_text is not None and not isinstance(raw_text, str):
        report.drop("annotation text must be a string or null", raw_text)
        raw_text = None
    text_value = annotation_nfkc_strip(raw_text) if raw_text is not None else None
    if status_raw == "unreadable" and text_value is not None:
        report.correct("unreadable annotation had non-null text; coerced to None")
        text_value = None
    material_id = data.get("material_id")
    source: ObservationSource | None = None
    if material_id is not None:
        if not isinstance(material_id, str) or material_id not in allowed_materials:
            raise ExecutionError(
                "gateway_parse_error",
                f"annotation cites unknown material_id {material_id!r} "
                "(not among the materials supplied in this request)")
        ref = allowed_materials[material_id]
        source = ObservationSource(material_id=ref.material_id, page_index=ref.page_index)
    elif len(allowed_materials) == 1:
        ref = next(iter(allowed_materials.values()))
        source = ObservationSource(material_id=ref.material_id, page_index=ref.page_index)
    bbox: tuple[float, float, float, float] | None = None
    if source is not None:
        bbox = _parse_bbox(data.get("bbox"), source.material_id, bbox_remapper, report)
        source = ObservationSource(
            material_id=source.material_id, page_index=source.page_index, bbox=bbox)
    result = AnnotationResult(
        text=text_value,
        status=status_raw,
        bbox=bbox,
        source=source,
    )
    return result, report


__all__ = [
    "AnnotationResult",
    "BboxRemapper",
    "ParseReport",
    "extract_json_text",
    "parse_annotation",
    "parse_observation_batch",
]
