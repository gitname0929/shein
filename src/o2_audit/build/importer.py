"""Strict rules JSONL importer with per-record isolation.

Reads a rules JSONL (``基础规则版本.jsonl`` B001@3.0/3.1, ``进阶规则60条.jsonl``
R001..R060@3.0, or any combined/custom rules JSONL) and returns:

- every VALID row as a :class:`RuleRecord` (deduplicated by
  ``(rule_id, rule_version)`` — first occurrence wins, later duplicates are
  reported idempotently and never abort the batch);
- every INVALID row as a structured issue with ``line_number``, ``rule_id``
  (when extractable), ``error_code``, ``message`` and ``next_action`` — the
  rest of the batch still imports.

Parsing strictness comes from the vendored public ``contracts.strict_loads``
(duplicate JSON keys, NaN/Infinity and malformed JSON are all rejected);
file-level size/record limits come from ``contracts.regular_bytes``. No
parsing semantic is reimplemented.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from o2_public_contracts import contracts as public_contracts

_RULE_ID_RE = r"^(B001|R0(?:0[1-9]|[1-5][0-9]|60))$"
_VERSION_RE = r"^\d+\.\d+$"


@dataclass(frozen=True)
class RuleRecord:
    """One valid, deduplicated rule row from the source JSONL."""

    rule_id: str
    rule_version: str
    title: str
    rule_text: str
    rule_family_id: str
    material_roles: tuple[str, ...]
    raw: dict[str, Any]
    source_line: int
    source_line_sha256: str  # SHA256 of the raw source line (UTF-8 bytes)
    source_file_sha256: str  # SHA256 of the whole source file


@dataclass
class ImportResult:
    valid: list[RuleRecord] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    duplicates: list[dict[str, Any]] = field(default_factory=list)
    source_path: str = ""
    source_file_sha256: str = ""

    @property
    def keys(self) -> set[tuple[str, str]]:
        return {(r.rule_id, r.rule_version) for r in self.valid}

    def record_by_key(self, rule_id: str, rule_version: str) -> RuleRecord | None:
        for r in self.valid:
            if (r.rule_id, r.rule_version) == (rule_id, rule_version):
                return r
        return None

    def summary(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "source_file_sha256": self.source_file_sha256,
            "valid": len(self.valid),
            "issues": len(self.issues),
            "duplicates": len(self.duplicates),
        }


def _issue(line_number: int, rule_id: str | None, error_code: str,
           message: str, next_action: str) -> dict[str, Any]:
    return {
        "line_number": line_number,
        "rule_id": rule_id,
        "error_code": error_code,
        "message": message,
        "next_action": next_action,
    }


def import_rules_jsonl(path: str | Path) -> ImportResult:
    """Import a rules JSONL with per-record isolation (never aborts on one
    bad line; file-level protection limits still apply)."""
    src = Path(path)
    raw = public_contracts.regular_bytes(src)  # symlink/size guards + 64MiB cap
    source_sha = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"{src.name}: rules JSONL must be UTF-8") from e

    result = ImportResult(source_path=str(src), source_file_sha256=source_sha)
    seen: dict[tuple[str, str], int] = {}

    for line_number, line in enumerate(text.split("\n"), 1):
        line = line.rstrip("\r")
        if not line.strip():
            continue
        if len(line.encode("utf-8")) > public_contracts.MAX_LINE_BYTES:
            result.issues.append(_issue(
                line_number, None, "oversized_line",
                f"line exceeds {public_contracts.MAX_LINE_BYTES} bytes",
                "split or shorten the record; row skipped"))
            continue

        try:
            row = public_contracts.strict_loads(line)
        except public_contracts.ContractError as e:
            code = _classify_parse_error(str(e))
            result.issues.append(_issue(
                line_number, None, code, f"line {line_number}: {e}",
                "fix the JSON syntax and re-import; valid rows keep importing"))
            continue
        if not isinstance(row, dict):
            result.issues.append(_issue(
                line_number, None, "not_an_object",
                f"line {line_number}: record is {type(row).__name__}, not a JSON object",
                "wrap the record as an object; row skipped"))
            continue

        rule_id = row.get("rule_id") if isinstance(row.get("rule_id"), str) else None
        problem = _validate_fields(row)
        if problem is not None:
            result.issues.append(_issue(line_number, rule_id, *problem))
            continue

        key = (row["rule_id"], row["version"])
        if key in seen:
            result.duplicates.append({
                "line_number": line_number,
                "rule_id": key[0],
                "rule_version": key[1],
                "kept_line": seen[key],
                "next_action": "ignored (idempotent dedup by rule_id+rule_version)",
            })
            continue

        seen[key] = line_number
        result.valid.append(RuleRecord(
            rule_id=key[0],
            rule_version=key[1],
            title=str(row.get("title", "")),
            rule_text=str(row.get("rule_text", "")),
            rule_family_id=str(row.get("rule_family_id", "")),
            material_roles=tuple(
                str(r) for r in row.get("material_role_candidates", ()) or ()
                if isinstance(r, str)),
            raw=row,
            source_line=line_number,
            source_line_sha256=hashlib.sha256(line.encode("utf-8")).hexdigest(),
            source_file_sha256=source_sha,
        ))
    return result


def _classify_parse_error(message: str) -> str:
    if message.startswith("Duplicate JSON key"):
        return "duplicate_json_key"
    if message.startswith("Non-finite"):
        return "non_finite_constant"
    if "Oversized" in message:
        return "oversized_line"
    return "malformed_json"


def _validate_fields(row: dict[str, Any]) -> tuple[str, str, str] | None:
    """Return (error_code, message, next_action) or None when the row is fine."""
    import re

    for name in ("rule_id", "version"):
        value = row.get(name)
        if not isinstance(value, str) or not value.strip():
            return ("missing_field", f"required field {name!r} missing or blank",
                    "add the field and re-import; row skipped")
    if not re.fullmatch(_RULE_ID_RE, row["rule_id"]):
        return ("invalid_rule_id",
                f"rule_id {row['rule_id']!r} is not B001 or R001..R060",
                "correct the rule_id; row skipped")
    if not re.fullmatch(_VERSION_RE, row["version"]):
        return ("invalid_rule_version",
                f"version {row['version']!r} must look like '3.0'",
                "correct the version; row skipped")
    if not isinstance(row.get("rule_text"), str) or not row["rule_text"].strip():
        return ("missing_field", "required field 'rule_text' missing or blank",
                "add the rule text; row skipped")
    return None


__all__ = ["ImportResult", "RuleRecord", "import_rules_jsonl"]
