"""Typed domain models for the audit prototype.

Design points fixed for later stages:

- Strict JSON types: page indices and bbox coordinates reject ``True``/``False``
  and floats (JSON ``1.0`` is not a page number). Pydantic strict int/float
  validation is used and re-checked in tests.
- Raw transcription vs business normalization are separate fields on
  :class:`Observation`.
- Inspection statuses distinguish readable, confirmed-missing (only legal when
  the check scope was complete and clear), unreadable, incomplete inspection
  scope and unknown identity.
- Metadata keys that are not modelled stay unknown; missing and ``null`` are
  distinct from a present value.
"""
from __future__ import annotations

import math
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Field-status vocabulary
# ---------------------------------------------------------------------------


class ObservationStatus(StrEnum):
    """Status of one predicate on one entity.

    ``confirmed_missing`` requires ``scope_complete=True``: the field may only
    be declared absent when the full, clear required check range was visible.
    An empty OCR result never proves absence.
    """

    READABLE = "readable"
    CONFIRMED_MISSING = "confirmed_missing"
    UNREADABLE = "unreadable"
    INCOMPLETE_SCOPE = "incomplete_scope"
    IDENTITY_UNKNOWN = "identity_unknown"


class EntityKind(StrEnum):
    LABEL = "label"
    PACKAGE = "package"
    CERTIFICATE = "certificate"
    DOCUMENT = "document"
    OBJECT = "object"


class LabelCarrier(StrEnum):
    """Where the audited label lives (B001 item 2)."""

    OUTER_PACKAGE = "outer_package"
    ATTACHED_TAG = "attached_tag"


# ---------------------------------------------------------------------------
# Strict scalar helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Materials / input
# ---------------------------------------------------------------------------


def _strict_page(v: Any, minimum: int = 0) -> int:
    """Protocol page index: a JSON integer (bool/float rejected), >= minimum.

    Source-file pages are 0-based per input.schema.json conventions; the
    public contract requires >= 1 for material/evidence rows, which its own
    validators enforce on serialized output.
    """
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("page_index must be a JSON integer (bool/float rejected)")
    if v < minimum:
        raise ValueError(f"page_index must be >= {minimum}")
    return v


class MaterialRef(BaseModel):
    """One material entry of an audit input (mirrors input.schema.json)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    material_id: str = Field(min_length=1, max_length=128)
    role: str = Field(min_length=1, max_length=128)
    relative_path: str = Field(min_length=1)
    media_type: Literal["image/png", "image/jpeg", "application/pdf"]
    page_index: int

    @field_validator("page_index", mode="before")
    @classmethod
    def _strict_page(cls, v: Any) -> Any:
        return _strict_page(v)


class Metadata(BaseModel):
    """Free-form task metadata; unknown keys are preserved verbatim.

    Missing key and explicit ``None`` both mean "unknown" for business logic;
    neither is coerced to ``False``/``0``. Typed accessors live in
    :mod:`o2_audit.runtime.facts`.

    Accepts both shapes:

    - flat protocol shape: ``Metadata(**{"market": "Q", ...})`` /
      ``AuditInput.model_validate({"metadata": {"market": "Q", ...}, ...})``
      (all unknown keys are collected into ``values`` instead of leaking into
      ``model_extra``);
    - wrapped shape: ``Metadata(values={"market": "Q"})``.
    """

    model_config = ConfigDict(extra="allow")

    values: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _collect_flat_keys(cls, v: Any) -> Any:
        if isinstance(v, dict) and "values" not in v:
            # Flat protocol dict: every key becomes a metadata value entry.
            return {"values": dict(v)}
        return v

    @field_validator("values", mode="before")
    @classmethod
    def _wrap(cls, v: Any) -> Any:
        return v if isinstance(v, dict) else {}

    def raw(self, key: str) -> Any:
        return self.values.get(key)

    def has(self, key: str) -> bool:
        return key in self.values


class AuditInput(BaseModel):
    """One audit task unit (protocol 1.3.1 input)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    audit_unit_id: str = Field(min_length=1, max_length=256)
    sample_id: str = Field(min_length=1, max_length=256)
    rule_id: str = Field(min_length=1, max_length=256)
    rule_version: str = Field(min_length=1, max_length=256)
    review_date: str = Field(min_length=1, max_length=256)
    metadata: Metadata
    materials: list[MaterialRef]


# ---------------------------------------------------------------------------
# Entities and observations
# ---------------------------------------------------------------------------


class Entity(BaseModel):
    """A scope-bound object inside one audit unit.

    Entities are never merged across units; two separate physical labels are
    two distinct ``kind=label`` entities even when they appear in one photo.
    ``container_entity_id`` links a label to the package it sits on (or the
    tag to the package it is attached to).
    """

    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(min_length=1, max_length=256)
    kind: EntityKind
    container_entity_id: str | None = None


class ObservationSource(BaseModel):
    """Where an observation came from (material + source page)."""

    model_config = ConfigDict(extra="forbid")

    material_id: str = Field(min_length=1, max_length=128)
    page_index: int
    bbox: tuple[float, float, float, float] | None = None

    @field_validator("page_index", mode="before")
    @classmethod
    def _strict_page(cls, v: Any) -> Any:
        return _strict_page(v)

    @field_validator("bbox", mode="before")
    @classmethod
    def _strict_bbox(cls, v: Any) -> Any:
        if v is None:
            return None
        return validate_bbox_tuple(v)


class Observation(BaseModel):
    """One observed predicate on one entity.

    ``raw_value`` keeps the original transcription; ``normalized_value`` is the
    business comparison value produced by shared operators. Both are ``None``
    unless ``status`` allows a value.
    """

    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(min_length=1, max_length=256)
    predicate: str = Field(min_length=1, max_length=256)
    status: ObservationStatus
    raw_value: Any = None
    normalized_value: Any = None
    scope_complete: bool = False
    source: ObservationSource | None = None

    @model_validator(mode="after")
    def _missing_requires_complete_scope(self) -> "Observation":
        if self.status == ObservationStatus.CONFIRMED_MISSING and not self.scope_complete:
            raise ValueError(
                "confirmed_missing requires scope_complete=True "
                "(absence may only be claimed when the full required check range was visible)"
            )
        return self


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def validate_bbox_tuple(v: Any) -> tuple[float, float, float, float]:
    """Strict bbox: 4 finite numbers, 0<=x1<x2<=1, 0<=y1<y2<=1; bool rejected.

    Mirrors the public ``contracts.bbox_ok`` semantics as a typed validator.
    """
    if not isinstance(v, (list, tuple)) or len(v) != 4:
        raise ValueError("bbox must have exactly 4 numbers")
    for x in v:
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
            raise ValueError("bbox coordinates must be finite numbers (bool rejected)")
        if x < 0 or x > 1:
            raise ValueError("bbox coordinates must be within [0, 1]")
    x1, y1, x2, y2 = float(v[0]), float(v[1]), float(v[2]), float(v[3])
    if not (x1 < x2 and y1 < y2):
        raise ValueError("bbox must be ordered (x1<x2, y1<y2)")
    return (x1, y1, x2, y2)


class Evidence(BaseModel):
    """Evidence row of a prediction (mirrors output.schema.json evidence)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    material_id: str = Field(min_length=1, max_length=128)
    page_index: int
    fact: str = Field(min_length=1)
    bbox: tuple[float, float, float, float] | None = None
    rule_quote: str | None = None

    @field_validator("page_index", mode="before")
    @classmethod
    def _strict_page(cls, v: Any) -> Any:
        return _strict_page(v)

    @field_validator("bbox", mode="before")
    @classmethod
    def _strict_bbox(cls, v: Any) -> Any:
        if v is None:
            return None
        return validate_bbox_tuple(v)

    @field_validator("fact", "rule_quote")
    @classmethod
    def _nonblank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("fact/rule_quote must not be blank")
        return v


# ---------------------------------------------------------------------------
# Requirement results, evidence gaps and errors
# ---------------------------------------------------------------------------


class RequirementStatus(StrEnum):
    SATISFIED = "satisfied"
    VIOLATED = "violated"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"


class RequirementResult(BaseModel):
    """Evaluation outcome of one requirement of the active rule plan."""

    model_config = ConfigDict(extra="forbid")

    requirement_id: str = Field(min_length=1)
    status: RequirementStatus
    reason_code: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    evidence: list[Evidence] = Field(default_factory=list)
    blocks_conclusion: bool = False


class AllowedAction(StrEnum):
    """Capability actions the control layer may request from the gateway."""

    INSPECT_MATERIAL = "inspect_material"


class NeedEvidence(BaseModel):
    """Structured evidence gap returned by the control layer to the intent layer.

    The gap names the entity/predicate needed, the candidate materials that may
    carry it and the actions allowed to obtain it.
    """

    model_config = ConfigDict(extra="forbid")

    entity_kind: EntityKind
    predicate: str
    candidate_material_ids: list[str] = Field(default_factory=list)
    allowed_actions: list[AllowedAction] = Field(default_factory=list)
    reason: str = Field(min_length=1)


class ExecutionError(Exception):
    """Tool/network/timeout failure: execution_status=error, label=null.

    This is deliberately not an ambiguous verdict.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


__all__ = [
    "AllowedAction",
    "AuditInput",
    "Entity",
    "EntityKind",
    "Evidence",
    "ExecutionError",
    "LabelCarrier",
    "MaterialRef",
    "Metadata",
    "NeedEvidence",
    "Observation",
    "ObservationSource",
    "ObservationStatus",
    "RequirementResult",
    "RequirementStatus",
    "validate_bbox_tuple",
]
