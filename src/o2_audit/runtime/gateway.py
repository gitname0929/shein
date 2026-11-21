"""Observation gateway protocol (execution layer boundary).

The gateway is the ONLY component allowed to inspect materials. It returns
typed observations/entities exclusively — never final labels, compliance flags
or authoritative decisions. Rules and the controller decide; the gateway only
reports what was seen (AGENTS.md: "Model outputs observations, not
authoritative final decisions").

``StaticObservationGateway`` is a deterministic TEST-ONLY helper. It is not a
visual recognition implementation and must never be wired into a real
inference path.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from o2_audit.domain.models import (
    Entity,
    ExecutionError,
    NeedEvidence,
    Observation,
)


class ObservationBatch(BaseModel):
    """Typed result of one gateway round: entities + observations ONLY."""

    model_config = ConfigDict(extra="forbid")

    entities: list[Entity] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)


@runtime_checkable
class ObservationGateway(Protocol):
    """Capability boundary toward material inspection.

    Implementations raise :class:`ExecutionError` on tool/network/timeout
    failure (which the control layer converts into a protocol-legal
    ``execution_status="error"`` record) and otherwise return an
    :class:`ObservationBatch`.
    """

    def observe(self, request: NeedEvidence) -> ObservationBatch:
        """Inspect the materials named by ``request`` and return observations.

        Must not return labels/compliance flags; violations of that contract
        are programming errors, not data.
        """
        ...


class StaticObservationGateway:
    """TEST-ONLY deterministic gateway.

    Returns pre-built :class:`ObservationBatch` objects in order, one per
    ``observe`` call. When the queue is exhausted it returns an empty batch
    (or raises ``ExecutionError`` if ``error_on_exhausted`` is set). This class
    performs no image or OCR work whatsoever and exists so tests can drive the
    controller deterministically; it must never be presented as actual visual
    recognition.
    """

    def __init__(self, batches: list[ObservationBatch], error_on_exhausted: bool = False,
                 error_code: str = "gateway_exhausted") -> None:
        self._batches = list(batches)
        self.calls: list[NeedEvidence] = []
        self.error_on_exhausted = error_on_exhausted
        self.error_code = error_code

    def observe(self, request: NeedEvidence) -> ObservationBatch:
        self.calls.append(request)
        if not self._batches:
            if self.error_on_exhausted:
                raise ExecutionError(self.error_code, "static test gateway exhausted")
            return ObservationBatch()
        return self._batches.pop(0)


__all__ = [
    "ExecutionError",
    "ObservationBatch",
    "ObservationGateway",
    "StaticObservationGateway",
]
