"""Strict JSON/JSONL reading.

The actual parsing/limit logic is the vendored public ``contracts.py``
(duplicate-key rejection, NaN/Infinity rejection, 64 MiB file / 512 KiB line /
100000 record limits). This wrapper adds typed errors and dict conversion;
no limit or semantic was reimplemented.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from o2_public_contracts import contracts as public_contracts


class ProtocolError(ValueError):
    pass


MAX_FILE_BYTES = public_contracts.MAX_FILE_BYTES
MAX_LINE_BYTES = public_contracts.MAX_LINE_BYTES
MAX_RECORDS = public_contracts.MAX_RECORDS


def read_json_strict(path: str | Path) -> dict[str, Any]:
    """Read a single strict JSON object file (UTF-8)."""
    raw = public_contracts.regular_bytes(Path(path))
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ProtocolError(f"{Path(path).name}: JSON must be UTF-8") from e
    try:
        obj = public_contracts.strict_loads(text)
    except public_contracts.ContractError as e:
        raise ProtocolError(f"{Path(path).name}: {e}") from e
    if not isinstance(obj, dict):
        raise ProtocolError(f"{Path(path).name}: top-level JSON must be an object")
    return obj


def read_jsonl_strict(path: str | Path, missing_ok: bool = False) -> list[dict[str, Any]]:
    """Read strict JSONL rows via the vendored public reader.

    Rejects duplicate JSON keys, NaN/Infinity, oversized lines/files, too many
    records and non-UTF-8 input. Rows that are not JSON objects are returned
    as-is for the semantic layer to diagnose.
    """
    try:
        rows: list[Any] = public_contracts.read_rows(Path(path), missing_ok=missing_ok)
    except public_contracts.ContractError as e:
        raise ProtocolError(str(e)) from e
    return rows


def dump_json_strict(obj: Any) -> str:
    """Canonical strict dump (no NaN, compact); used for digests."""
    try:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except ValueError as e:
        raise ProtocolError(f"value not strictly serializable: {e}") from e


def write_json_atomic(path: str | Path, obj: Any, *, indent: int = 2) -> Path:
    """Atomically write strictly finite UTF-8 JSON (temp file + replace)."""
    import os
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        text = json.dumps(obj, ensure_ascii=False, indent=indent, allow_nan=False) + "\n"
    except ValueError as e:
        raise ProtocolError(f"value not strictly serializable: {e}") from e
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
    return target
