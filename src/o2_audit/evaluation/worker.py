"""Isolated subprocess worker for evaluation plugins.

Invoked by :mod:`o2_audit.evaluation.runner` via::

    python -B -m o2_audit.evaluation.worker

Reads one JSON request on stdin containing ``entry`` (``module:function``) and
read-only ``inputs``, ``outputs``, ``references``, ``context``. Verifies that
the plugin did NOT mutate any of the four input arguments and prints strictly
finite JSON on stdout.

Note on isolation: this worker provides process isolation, timeout boundaries
(enforced by the parent runner), and read-only input mutation detection. It
does NOT claim full OS/kernel/network container sandboxing in local mode.
"""
from __future__ import annotations

import importlib
import json
import sys
import traceback
from typing import Any

from o2_public_contracts import contracts as public_contracts

_MUTATION_EXIT_CODE = 86


def _canonical_snapshot(obj: Any) -> str:
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )


def _load_callable(entry: str):
    if ":" not in entry:
        raise ValueError(f"invalid plugin entry (expected module:function): {entry!r}")
    module_name, func_name = entry.rsplit(":", 1)
    if not module_name.strip() or not func_name.strip():
        raise ValueError(f"invalid plugin entry: {entry!r}")
    mod = importlib.import_module(module_name.strip())
    fn = getattr(mod, func_name.strip())
    if not callable(fn):
        raise TypeError(f"plugin target {entry!r} is not callable")
    return fn


def run_worker() -> int:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    raw = sys.stdin.read()
    try:
        payload = public_contracts.strict_loads(raw)
    except Exception as exc:
        print(f"invalid worker stdin JSON: {exc}", file=sys.stderr)
        return 2

    if not isinstance(payload, dict) or "entry" not in payload:
        print("worker payload must be an object with 'entry'", file=sys.stderr)
        return 2

    entry = payload["entry"]
    inputs = payload.get("inputs", [])
    outputs = payload.get("outputs", [])
    references = payload.get("references", [])
    context = payload.get("context", {})

    args = [inputs, outputs, references, context]
    before = _canonical_snapshot(args)

    try:
        fn = _load_callable(entry)
        result = fn(inputs, outputs, references, context)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1

    try:
        after = _canonical_snapshot(args)
    except (TypeError, ValueError) as exc:
        print(f"PLUGIN_MUTATED_INPUTS: non-serializable mutation: {exc}", file=sys.stderr)
        return _MUTATION_EXIT_CODE

    if after != before:
        print("PLUGIN_MUTATED_INPUTS: plugin mutated read-only input objects", file=sys.stderr)
        return _MUTATION_EXIT_CODE

    try:
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        print(f"PLUGIN_NON_FINITE_OR_UNSERIALIZABLE_OUTPUT: {exc}", file=sys.stderr)
        return 3

    sys.stdout.write(encoded)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(run_worker())
