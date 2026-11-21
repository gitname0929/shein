"""Repo-local resource paths.

The submitted repository must work without the original competition package:
resources live under ``<repo>/resources`` (copied, read-only). Discovery is
independent of the parent folder: the repo root is derived from this file's
location (``src/o2_audit/resources.py`` -> parents[2]) and can be overridden
with the ``O2_AUDIT_RESOURCES_DIR`` environment variable.

Layout (see resources/README.md for provenance):

- ``resources/rules``      public rule texts and versioned rule JSONs
- ``resources/protocol``   schemas, protocol docs, vendored-validator copies, hash fixture
- ``resources/public_dev`` PUBLIC development inputs/assets and reference outputs
- ``resources/学生赛题说明.md`` root student specification copy
"""
from __future__ import annotations

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_RESOURCES = _REPO_ROOT / "resources"


def resources_root() -> Path:
    """Root directory of the repo-local resource tree."""
    override = os.environ.get("O2_AUDIT_RESOURCES_DIR")
    return Path(override) if override else _DEFAULT_RESOURCES


def rules_dir() -> Path:
    """Public rule texts and versioned rule JSONs (read-only copies)."""
    return resources_root() / "rules"


def protocol_dir() -> Path:
    """Protocol schemas, docs and validator material copies (read-only)."""
    return resources_root() / "protocol"


def public_dev_dir() -> Path:
    """PUBLIC development data only.

    Reference labels under this directory must never be loaded by
    build/infer/annotate/iterate; evaluation runs separately (AGENTS.md).
    """
    return resources_root() / "public_dev"


def student_spec_path() -> Path:
    """Repo-local copy of the root student specification."""
    return resources_root() / "学生赛题说明.md"


__all__ = [
    "public_dev_dir",
    "protocol_dir",
    "resources_root",
    "rules_dir",
    "student_spec_path",
]
