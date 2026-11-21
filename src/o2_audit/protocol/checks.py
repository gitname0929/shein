"""Semantic checks delegated to the vendored public contracts.py.

This module is the single place where input/prediction/annotation semantic
validation happens. We do not reimplement any public rule; we only convert
typed domain objects back into protocol JSON and call the public functions.
"""
from __future__ import annotations

from typing import Any

from o2_audit.domain.models import AuditInput
from o2_public_contracts import contracts as public_contracts


def _task_json(task: AuditInput) -> dict[str, Any]:
    d = task.model_dump(mode="json")
    # Metadata keeps the original protocol shape (plain dict with unknown keys).
    d["metadata"] = task.metadata.values
    return d


def input_semantic_errors(task: AuditInput) -> list[str]:
    """Public ``input_errors`` on a typed input."""
    return public_contracts.input_errors(_task_json(task))


def prediction_semantic_errors(
    row: dict[str, Any],
    task: AuditInput,
    expected_artifact_version: str | None = None,
) -> list[str]:
    """Public ``prediction_errors`` (joint status/label/applicability, evidence
    membership, bbox, page types, visual-evidence minimum)."""
    return public_contracts.prediction_errors(row, _task_json(task), expected_artifact_version)


def annotation_semantic_errors(row: dict[str, Any], target: dict[str, Any], task: AuditInput) -> list[str]:
    return public_contracts.annotation_errors(row, target, _task_json(task))


def visual_evidence_required(task: AuditInput, applicability: str) -> bool:
    """Exact public ``visual_evidence_required`` including metadata exceptions.

    Do not widen the no-evidence allowance anywhere else.
    """
    return public_contracts.visual_evidence_required(_task_json(task), applicability)
