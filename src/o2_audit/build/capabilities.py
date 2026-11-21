"""Capability registry: which Python modules implement each executable
capability, and which capabilities each one depends on.

A *capability* is the smallest unit whose source code is hash-bound into a
frozen artifact (``engine/capabilities.json``). Rule plans declare capability
dependencies (``RulePlan.capability_deps``); the loader verifies, at load
time, that the CURRENT runtime module bytes still match the hashes frozen at
build time — so a rule can never silently execute modified code without a
rebuild.

Dependency edges are derived from the actual ``from o2_audit...`` imports of
the implementing modules (see :func:`capability_dependencies`), not asserted
by hand:

- ``o2_audit.operators.*``      -> ``operators.<name>`` capabilities
- ``o2_audit.rules.base``       -> ``rules.base`` (shared plan types)
- ``o2_audit.rules.advanced.base`` -> ``rules.advanced_base``
- ``o2_audit.rules.b001.*``     -> ``rules.b001``
- ``o2_audit.rules.advanced.fNN_*`` -> ``rules.fNN``
- ``o2_audit.runtime.facts``    -> ``runtime.facts``
- ``o2_audit.runtime.controller`` / ``gateway`` -> ``inspect_material``

Domain/protocol modules (``o2_audit.domain.*``, ``o2_audit.protocol.*``) are
the shared foundation layer; they are typed and separately tested, and are not
per-rule versioned capabilities in Stage 02b.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+(o2_audit[a-z_0-9.]*)", re.MULTILINE)

# Capability name -> dotted module names that implement it (real source files
# whose SHA256 is frozen at build time and re-verified at load time).
CAPABILITY_MODULES: dict[str, tuple[str, ...]] = {
    "operators.text": ("o2_audit.operators.text",),
    "operators.numbers": ("o2_audit.operators.numbers",),
    "operators.dates": ("o2_audit.operators.dates",),
    "operators.sets": ("o2_audit.operators.sets",),
    "operators.geometry": ("o2_audit.operators.geometry",),
    "runtime.facts": ("o2_audit.runtime.facts",),
    "rules.base": ("o2_audit.rules.base",),
    "rules.advanced_base": ("o2_audit.rules.advanced.base",),
    "rules.b001": ("o2_audit.rules.b001.evaluate", "o2_audit.rules.b001.plan"),
    "rules.f01": ("o2_audit.rules.advanced.f01_evidence",),
    "rules.f02": ("o2_audit.rules.advanced.f02_identity",),
    "rules.f03": ("o2_audit.rules.advanced.f03_fields",),
    "rules.f04": ("o2_audit.rules.advanced.f04_placement",),
    "rules.f05": ("o2_audit.rules.advanced.f05_visual",),
    "rules.f06": ("o2_audit.rules.advanced.f06_warnings",),
    "rules.f07": ("o2_audit.rules.advanced.f07_numeric",),
    "rules.f08": ("o2_audit.rules.advanced.f08_documents",),
    "rules.f09": ("o2_audit.rules.advanced.f09_cross_material",),
    "rules.f10": ("o2_audit.rules.advanced.f10_boundary",),
    "inspect_material": ("o2_audit.runtime.controller", "o2_audit.runtime.gateway"),
}

# dotted module -> owning capability (inverse of CAPABILITY_MODULES).
MODULE_TO_CAPABILITY: dict[str, str] = {
    mod: cap for cap, mods in CAPABILITY_MODULES.items() for mod in mods
}

_FAMILIES = ("f01", "f02", "f03", "f04", "f05", "f06", "f07", "f08", "f09", "f10")


def capability_dependencies(capability: str) -> tuple[str, ...]:
    """Capabilities ``capability`` directly depends on, derived from the real
    ``from o2_audit...`` import lines of its implementing module sources.

    Unknown ``o2_audit`` modules (domain/protocol foundation layer) are not
    capabilities and are ignored. Self-references are excluded.
    """
    deps: set[str] = set()
    for mod in CAPABILITY_MODULES[capability]:
        source = _module_source_path(mod)
        if source is None or not source.is_file():
            continue
        for match in _IMPORT_RE.finditer(source.read_text(encoding="utf-8")):
            target = MODULE_TO_CAPABILITY.get(match.group(1))
            if target is not None and target != capability:
                deps.add(target)
    return tuple(sorted(deps))


def transitive_dependencies(capabilities: tuple[str, ...] | set[str] | list[str]) -> tuple[str, ...]:
    """Full forward transitive dependency closure (inclusive of ``capabilities``)."""
    visited: set[str] = set(capabilities)
    frontier = set(capabilities)
    while frontier:
        next_frontier: set[str] = set()
        for cap in frontier:
            for dep in capability_dependencies(cap):
                if dep not in visited:
                    visited.add(dep)
                    next_frontier.add(dep)
        frontier = next_frontier
    return tuple(sorted(visited))


def _module_source_path(dotted_module: str) -> Path | None:
    import importlib

    try:
        mod = importlib.import_module(dotted_module)
    except Exception:  # pragma: no cover - only if the module is broken
        return None
    file_attr = getattr(mod, "__file__", None)
    return Path(file_attr).resolve() if file_attr else None


def module_source_sha256(dotted_module: str) -> tuple[Path, str]:
    """Real on-disk source file of a module plus its SHA256."""
    import hashlib

    path = _module_source_path(dotted_module)
    if path is None or not path.is_file():
        raise FileNotFoundError(f"capability module source not found: {dotted_module}")
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def current_capability_hashes(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """capability -> its (first) implementing module's current source SHA256.

    ``overrides`` is an explicit TEST/SIMULATION hook: it replaces the
    computed current hash for a capability so callers can model "this
    capability's code changed" without editing repository sources. It is
    never used to fake a successful verification — a simulated change still
    fails binding checks until the artifact is rebuilt with the same override.
    """
    hashes: dict[str, str] = {}
    for cap, mods in CAPABILITY_MODULES.items():
        if overrides and cap in overrides:
            hashes[cap] = overrides[cap]
        else:
            hashes[cap] = module_source_sha256(mods[0])[1]
    return hashes


def build_capability_entries(overrides: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """Canonical capability entries frozen into ``engine/capabilities.json``.

    Each entry carries the REAL source-file SHA256 of every implementing
    module plus its derived dependency capability names. The whole entry dict
    is canon-serialized, so ``artifact_hash.digest_obj(entry)`` is the stable
    binding hash referenced inside each rule's frozen bundle.
    """
    entries: dict[str, dict[str, Any]] = {}
    for cap in sorted(CAPABILITY_MODULES):
        modules = []
        for idx, mod in enumerate(CAPABILITY_MODULES[cap]):
            if overrides and mod in overrides:
                sha = overrides[mod]
                path = _module_source_path(mod)
            elif overrides and cap in overrides and idx == 0:
                sha = overrides[cap]
                path = _module_source_path(mod)
            else:
                path, sha = module_source_sha256(mod)
            modules.append({
                "module": mod,
                # Informational path relative to the repo root when possible;
                # hash binding always re-resolves the module at load time.
                "source_path": _repo_relative(path) if path else mod,
                "sha256": sha,
            })
        entries[cap] = {
            "name": cap,
            "modules": modules,
            "depends_on": list(capability_dependencies(cap)),
        }
    return entries


def _repo_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(_repo_root()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


__all__ = [
    "CAPABILITY_MODULES",
    "MODULE_TO_CAPABILITY",
    "build_capability_entries",
    "capability_dependencies",
    "current_capability_hashes",
    "module_source_sha256",
]
