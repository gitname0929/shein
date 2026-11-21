"""Shared synthetic builders for Stage 01 tests.

All facts are synthetic; nothing here is a vision/OCR implementation. Public
label IDs are never used as dispatch logic — tests build their own entities.
"""
from __future__ import annotations

from typing import Any

from o2_audit.domain.models import (
    AuditInput,
    Entity,
    EntityKind,
    MaterialRef,
    Metadata,
    Observation,
    ObservationSource,
    ObservationStatus,
)
from o2_audit.runtime.facts import FactView
from o2_audit.runtime.gateway import ObservationBatch


def make_task(
    metadata: dict[str, Any] | None = None,
    materials: list[MaterialRef] | None = None,
    rule_version: str = "3.0",
    review_date: str = "2026-06-15",
    audit_unit_id: str = "u-1",
    rule_id: str = "B001",
) -> AuditInput:
    base_meta = {
        "market": "Q",
        "category": "桌面灯",
        "power_mode": "可充电",
        "declared_model": "LED-100",
        "package_longest_edge_mm": 90,
    }
    if metadata is not None:
        base_meta.update(metadata)
    if materials is None:
        materials = [
            MaterialRef(material_id="m1", role="package_photos",
                        relative_path="assets/m1.png", media_type="image/png", page_index=1),
            MaterialRef(material_id="m2", role="tag_photos",
                        relative_path="assets/m2.png", media_type="image/png", page_index=1),
            MaterialRef(material_id="m3", role="certificate_pages",
                        relative_path="assets/m3.png", media_type="image/png", page_index=1),
        ]
    return AuditInput(
        audit_unit_id=audit_unit_id,
        sample_id=f"s-{audit_unit_id}",
        rule_id=rule_id,
        rule_version=rule_version,
        review_date=review_date,
        metadata=Metadata(values=base_meta),
        materials=materials,
    )


def ent(entity_id: str, kind: str) -> Entity:
    return Entity(entity_id=entity_id, kind=EntityKind(kind))


def obs(
    entity_id: str,
    predicate: str,
    value: Any = None,
    *,
    status: str = "readable",
    raw: Any = None,
    scope_complete: bool = False,
    source: tuple[str, int] = ("m1", 1),
) -> Observation:
    return Observation(
        entity_id=entity_id,
        predicate=predicate,
        status=ObservationStatus(status),
        raw_value=raw,
        normalized_value=value,
        scope_complete=scope_complete,
        source=ObservationSource(material_id=source[0], page_index=source[1]),
    )


def pass_batch(
    *,
    model: str = "LED-100",
    number: str = "QLAB-123456",
    edge_mm: int = 90,
    covered: list[str] | None = None,
    valid_from: str = "2026-01-01",
    valid_until: str = "2026-12-31",
    carrier: str = "attached_tag",
    attached: bool = True,
    label_id: str = "L1",
    cert_id: str = "C1",
) -> ObservationBatch:
    """A synthetic, fully-satisfied B001 fact graph as one gateway batch."""
    covered = ["LED-100"] if covered is None else covered
    label = ent(label_id, "label")
    cert = ent(cert_id, "certificate")
    observations = [
        obs(label_id, "void_marked", False, source=("m1", 1)),
        obs(label_id, "model", model, source=("m1", 1)),
        obs(label_id, "material_number", number, source=("m1", 1)),
        obs(label_id, "carrier", carrier, source=("m2", 1)),
        obs(label_id, "attached_to_package", attached, source=("m2", 1)),
        obs(cert_id, "void_marked", False, source=("m3", 1)),
        obs(cert_id, "material_number", number, source=("m3", 1)),
        obs(cert_id, "covered_models", covered, source=("m3", 1)),
        obs(cert_id, "valid_from", valid_from, source=("m3", 1)),
        obs(cert_id, "valid_until", valid_until, source=("m3", 1)),
    ]
    return ObservationBatch(entities=[label, cert], observations=observations)


def pass_facts(**kwargs: Any) -> FactView:
    batch = pass_batch(**kwargs)
    return FactView(entities=list(batch.entities), observations=list(batch.observations))
