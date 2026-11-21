"""Content-addressed extraction cache for the observation gateway (Stage 03a).

Backed by the workspace :class:`~o2_audit.storage.store.TaskStore` SQLite —
NEVER inside frozen artifacts (AGENTS.md: running output, caches and SQLite
live outside artifacts).

Cache keys are computed from CONTENT only (material content SHA256s, model
name, prompt fingerprint, schema version, scope and an optional plan
identity) — never from material IDs — so two audit units submitting identical
material content share one extraction while the cached result is rebound to
each unit's own legal ``MaterialRef`` on retrieval (see
``ZCodeObservationGateway`` for the rebinding invariant).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from o2_audit.storage.store import TaskStore

SCHEMA_VERSION = "obs-v1"


def cache_key(
    *,
    content_sha256s: list[str],
    model: str,
    prompt_hash: str,
    schema_version: str,
    scope: str,
    plan_identity: str = "",
    crops: dict[str, list[float]] | None = None,
) -> str:
    """Deterministic cache key over content hashes + extraction identity.

    Material IDs are deliberately excluded: identical content under a
    different audit unit's ID must still hit (and then rebind)."""
    payload = {
        "shas": sorted(content_sha256s),
        "model": model,
        "prompt_hash": prompt_hash,
        "schema_version": schema_version,
        "scope": scope,
        "plan_identity": plan_identity,
        "crops": {k: [round(v, 6) for v in box] for k, box in (crops or {}).items()},
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    return digest


class ExtractionCache:
    """Small get/put facade over ``TaskStore.extraction_cache``."""

    def __init__(self, store: TaskStore) -> None:
        self._store = store

    def get(self, key: str) -> dict[str, Any] | None:
        payload = self._store.cache_get(key)
        return payload if isinstance(payload, dict) else None

    def put(self, key: str, payload: dict[str, Any]) -> None:
        self._store.cache_put(key, payload)


def open_workspace_cache(workspace_dir: str | Path) -> ExtractionCache:
    """Cache in its own workspace SQLite file (never under frozen artifacts)."""
    from o2_audit.storage.store import TaskStore as _Store

    return ExtractionCache(_Store(Path(workspace_dir) / "extraction_cache.sqlite3"))


__all__ = ["ExtractionCache", "SCHEMA_VERSION", "cache_key", "open_workspace_cache"]
