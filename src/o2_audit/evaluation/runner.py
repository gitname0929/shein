"""Host runner for isolated evaluation plugins.

Executes each enabled plugin sequentially in a fresh Python subprocess
(``python -B -m o2_audit.evaluation.worker``) with no shell interpolation:

- Inputs/outputs/references/context are passed as serialized JSON copies over
  stdin; the worker verifies read-only immutability before returning.
- Enforces per-plugin timeouts (``timeout`` arg -> ``spec.timeout_seconds`` ->
  ``PLUGIN_TIMEOUT_SECONDS`` env).
- Validates strict finite JSON output and required protocol 1.3.1 report
  fields: ``plugin_id``, ``plugin_version``, ``input_schema_version``,
  ``metrics``, ``diagnostics``, ``errors``.
- Disabled plugins are NEVER invoked (invocation count = 0).
- One plugin's crash, timeout, input mutation, or invalid output is isolated
  as a structured error report and never modifies predictions or blocks other
  plugins.

Honest boundary: subprocess execution provides process isolation, timeout
enforcement, and read-only input mutation detection locally; it does NOT claim
OS-kernel or network container sandboxing.
"""
from __future__ import annotations

import copy
import json
import math
import os
import subprocess
import sys
from typing import Any

from o2_audit.evaluation.manifest import (
    SCHEMA_VERSION,
    PluginRegistry,
    PluginSpec,
    env_timeout_seconds,
)
from o2_public_contracts import contracts as public_contracts

_MUTATION_EXIT_CODE = 86

ISOLATION_NOTE = (
    "Subprocess isolation enforces process boundary, per-plugin timeout, and "
    "read-only input mutation detection; local development mode does not "
    "claim OS/kernel container or network sandboxing."
)


def _check_finite_json(value: Any, path: str = "$") -> list[str]:
    """Ensure every number nested inside a JSON structure is finite."""
    issues: list[str] = []
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return issues
    if isinstance(value, int):
        return issues
    if isinstance(value, float):
        if not math.isfinite(value):
            issues.append(f"{path}: non-finite float")
        return issues
    if isinstance(value, list):
        for idx, item in enumerate(value):
            issues.extend(_check_finite_json(item, f"{path}[{idx}]"))
        return issues
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                issues.append(f"{path}: non-string key {k!r}")
            else:
                issues.extend(_check_finite_json(v, f"{path}.{k}"))
        return issues
    issues.append(f"{path}: non-JSON value of type {type(value).__name__}")
    return issues


def validate_plugin_report(obj: Any) -> list[str]:
    """Validate protocol 1.3.1 plugin report schema and finite JSON values."""
    issues: list[str] = []
    if not isinstance(obj, dict):
        return ["report must be a JSON object"]
    for key in ("plugin_id", "plugin_version", "input_schema_version"):
        val = obj.get(key)
        if not isinstance(val, str) or not val.strip():
            issues.append(f"missing or invalid string field '{key}'")
    if obj.get("input_schema_version") not in (SCHEMA_VERSION,):
        issues.append(
            f"input_schema_version must be '{SCHEMA_VERSION}', got {obj.get('input_schema_version')!r}"
        )
    if not isinstance(obj.get("metrics"), dict):
        issues.append("field 'metrics' must be a JSON object")
    if not isinstance(obj.get("diagnostics"), list):
        issues.append("field 'diagnostics' must be a JSON array")
    if not isinstance(obj.get("errors"), list):
        issues.append("field 'errors' must be a JSON array")
    issues.extend(_check_finite_json(obj))
    return issues


def _error_report(
    *,
    plugin_id: str,
    plugin_version: str,
    code: str,
    detail: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "detail": detail}
    if extra:
        err.update(extra)
    return {
        "plugin_id": plugin_id,
        "plugin_version": plugin_version,
        "input_schema_version": SCHEMA_VERSION,
        "metrics": {},
        "diagnostics": [],
        "errors": [err],
        "isolation_note": ISOLATION_NOTE,
    }


def _normalize_context(context: dict[str, Any] | None) -> dict[str, Any]:
    ctx = copy.deepcopy(context) if context is not None else {}
    ctx.setdefault("schema_version", SCHEMA_VERSION)
    ctx.setdefault("purpose", "local_evaluation")
    return ctx


def run_plugin(
    plugin: PluginSpec | str,
    inputs: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    references: list[dict[str, Any]] | None,
    context: dict[str, Any] | None = None,
    *,
    timeout: float | None = None,
    extra_pythonpath: list[str] | None = None,
) -> dict[str, Any]:
    """Run one plugin in an isolated subprocess and return its validated report.

    Never mutates caller's ``inputs`` / ``outputs`` / ``references`` / ``context``.
    Disabled plugins are NOT invoked at all.
    """
    if isinstance(plugin, str):
        spec = PluginSpec(
            plugin_id=plugin,
            entry=plugin,
            plugin_version="custom",
            enabled=True,
            timeout_seconds=None,
        )
    else:
        spec = plugin

    if not spec.enabled:
        return {
            "plugin_id": spec.plugin_id,
            "plugin_version": spec.plugin_version,
            "input_schema_version": SCHEMA_VERSION,
            "status": "disabled",
            "invoked": False,
            "metrics": {},
            "diagnostics": [],
            "errors": [],
            "isolation_note": ISOLATION_NOTE,
        }

    effective_timeout = (
        timeout
        if timeout is not None
        else (env_timeout_seconds() if env_timeout_seconds() is not None else spec.timeout_seconds)
    )
    if effective_timeout is not None and effective_timeout <= 0:
        raise ValueError("plugin timeout must be positive")

    ctx = _normalize_context(context)
    payload = {
        "entry": spec.entry,
        "inputs": inputs,
        "outputs": outputs,
        "references": list(references) if references is not None else [],
        "context": ctx,
    }
    try:
        serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        return _error_report(
            plugin_id=spec.plugin_id,
            plugin_version=spec.plugin_version,
            code="plugin_invalid_input",
            detail=f"evaluation payload is not finite JSON: {exc}",
        )

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    if extra_pythonpath:
        existing = env.get("PYTHONPATH", "")
        parts = [p for p in extra_pythonpath if p]
        if existing:
            parts.append(existing)
        env["PYTHONPATH"] = os.pathsep.join(parts)

    try:
        proc = subprocess.run(
            [sys.executable, "-B", "-m", "o2_audit.evaluation.worker"],
            input=serialized,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=effective_timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return _error_report(
            plugin_id=spec.plugin_id,
            plugin_version=spec.plugin_version,
            code="plugin_timeout",
            detail=f"plugin exceeded timeout ({effective_timeout}s)",
        )
    except OSError as exc:
        return _error_report(
            plugin_id=spec.plugin_id,
            plugin_version=spec.plugin_version,
            code="plugin_spawn_error",
            detail=str(exc),
        )

    if proc.returncode == _MUTATION_EXIT_CODE:
        return _error_report(
            plugin_id=spec.plugin_id,
            plugin_version=spec.plugin_version,
            code="plugin_mutated_inputs",
            detail=(proc.stderr or "plugin mutated read-only inputs").strip()[-1000:],
        )

    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "").strip()[-1000:]
        code = (
            "plugin_invalid_output"
            if "PLUGIN_NON_FINITE_OR_UNSERIALIZABLE_OUTPUT" in stderr_tail
            else "plugin_failed"
        )
        return _error_report(
            plugin_id=spec.plugin_id,
            plugin_version=spec.plugin_version,
            code=code,
            detail=stderr_tail or f"plugin exited with status {proc.returncode}",
        )

    try:
        report = public_contracts.strict_loads(proc.stdout)
    except (ValueError, public_contracts.ContractError) as exc:
        return _error_report(
            plugin_id=spec.plugin_id,
            plugin_version=spec.plugin_version,
            code="plugin_invalid_output",
            detail=f"plugin stdout is not valid strict finite JSON: {exc}",
        )

    issues = validate_plugin_report(report)
    if issues:
        return _error_report(
            plugin_id=spec.plugin_id,
            plugin_version=spec.plugin_version,
            code="plugin_invalid_output",
            detail="; ".join(issues),
        )

    report.setdefault("status", "ok")
    report.setdefault("invoked", True)
    report.setdefault("isolation_note", ISOLATION_NOTE)
    return report


def run_plugins(
    registry: PluginRegistry,
    inputs: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    references: list[dict[str, Any]] | None,
    context: dict[str, Any] | None = None,
    *,
    plugin_ids: list[str] | None = None,
    timeout: float | None = None,
    extra_pythonpath: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Run multiple plugins sequentially and independently.

    Disabled plugins are recorded with ``status="disabled", invoked=False`` and
    are never spawned. One plugin's failure never modifies inputs/outputs or
    prevents remaining plugins from running.
    """
    specs = (
        [registry.resolve(pid) for pid in plugin_ids]
        if plugin_ids is not None
        else registry.list_specs()
    )
    reports: list[dict[str, Any]] = []
    for spec in specs:
        reports.append(
            run_plugin(
                spec,
                inputs,
                outputs,
                references,
                context,
                timeout=timeout,
                extra_pythonpath=extra_pythonpath,
            )
        )
    return reports
