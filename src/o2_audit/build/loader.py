"""Runtime artifact loader: read-only frozen BUILD_DIR + tamper/hash binding.

Verifies, before ANY rule executes:

1. ``artifact_manifest.json`` matches the exact public schema with unique
   ``(rule_id, rule_version)`` entries, safe relative paths, no root
   self-reference, and every entry ``sha256`` equal to
   ``o2_public_contracts.artifact_hash.digest(build_dir / entry.path)``;
2. each rule bundle's capability bindings match ``engine/capabilities.json``
   (digest of every declared capability entry) — rule bundle and capability
   table must come from the same build;
3. every bound capability's CURRENT runtime module source hash matches the
   frozen value — a rule can never silently execute modified code without a
   rebuild (:class:`ArtifactIntegrityError`).

The loaded artifact wires :class:`~o2_audit.runtime.controller.AuditController`
through a :class:`FrozenRegistry` that serves ONLY the manifest's rule plans
(no generic fallback) and stamps every output with the exact loaded
``artifact_version`` from the manifest entry.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from o2_public_contracts import artifact_hash
from o2_public_contracts import contracts as public_contracts

from o2_audit.build.capabilities import module_source_sha256
from o2_audit.protocol.jsonio import dump_json_strict
from o2_audit.rules.base import RulePlan, UnsupportedRuleError
from o2_audit.rules.b001.plan import B001Plan
from o2_audit.rules.registry import RuleRegistry
from o2_audit.runtime.controller import AuditController

MANIFEST_NAME = "artifact_manifest.json"
_CAPABILITIES_REL = "engine/capabilities.json"
_ENTRY_FIELDS = {"rule_id", "rule_version", "artifact_version", "path", "sha256"}
_VERSION_RE = r"^\d+\.\d+$"


class ArtifactIntegrityError(RuntimeError):
    """Frozen artifact is tampered, malformed, or bound to code that changed."""


@dataclass(frozen=True)
class LoadedArtifact:
    """A verified frozen artifact (read-only view)."""

    build_dir: Path
    manifest: dict[str, Any]
    capabilities: dict[str, dict[str, Any]]
    bundles: dict[tuple[str, str], dict[str, Any]]
    plans: dict[tuple[str, str], RulePlan]

    @property
    def entries(self) -> list[dict[str, Any]]:
        return self.manifest["entries"]

    @property
    def pairs(self) -> set[tuple[str, str]]:
        return set(self.bundles)

    def artifact_version_for(self, rule_id: str, rule_version: str) -> str:
        key = (rule_id, rule_version)
        if key not in self.bundles:
            raise UnsupportedRuleError(rule_id, rule_version)
        return self._entry(key)["artifact_version"]

    def _entry(self, key: tuple[str, str]) -> dict[str, Any]:
        for entry in self.entries:
            if (entry["rule_id"], entry["rule_version"]) == key:
                return entry
        raise UnsupportedRuleError(*key)  # pragma: no cover - guarded by bundles

    def plan_for(self, rule_id: str, rule_version: str) -> RulePlan:
        key = (rule_id, rule_version)
        if key not in self.plans:
            raise UnsupportedRuleError(rule_id, rule_version)
        return self.plans[key]

    def registry(self) -> "FrozenRegistry":
        return FrozenRegistry(self.plans)

    def controller(self, **kwargs: Any) -> AuditController:
        """An :class:`AuditController` bound to the frozen plans; outputs are
        stamped with the manifest entry's exact ``artifact_version``."""
        versions = {e["artifact_version"] for e in self.entries}
        if len(versions) != 1:
            raise ArtifactIntegrityError(
                f"mixed artifact_versions in one manifest: {sorted(versions)}")
        kwargs.setdefault("registry", self.registry())
        kwargs.setdefault("artifact_version", versions.pop())
        return AuditController(**kwargs)


class FrozenRegistry(RuleRegistry):
    """Registry serving ONLY the frozen manifest plans.

    Evaluators/applicability functions still come from the runtime registry
    (they are code, hash-bound via capability bindings); plans are the frozen
    data. Any (rule_id, rule_version) NOT in the manifest raises
    :class:`UnsupportedRuleError` — no generic fallback.
    """

    def __init__(self, frozen_plans: dict[tuple[str, str], RulePlan]) -> None:
        super().__init__()  # loads evaluators/applicability from resources
        self._plans = dict(frozen_plans)  # replace: only frozen plans resolvable


def verify_build(
    build_dir: str | Path,
    *,
    check_capability_bindings: bool = True,
    runtime_overrides: dict[str, str] | None = None,
) -> LoadedArtifact:
    """Fully verify a frozen BUILD_DIR and load it read-only.

    ``runtime_overrides`` (TEST/SIMULATION hook) replaces the computed
    current source hash of a capability — a simulated code change that must
    fail binding verification until the artifact is rebuilt with the same
    override.
    """
    root = Path(build_dir)
    if root.is_symlink() or not root.is_dir():
        raise ArtifactIntegrityError(f"build dir missing or symbolic: {root}")

    manifest = _load_manifest(root)
    capabilities = _load_capabilities(root)

    bundles: dict[tuple[str, str], dict[str, Any]] = {}
    plans: dict[tuple[str, str], RulePlan] = {}
    for entry in manifest["entries"]:
        key = (entry["rule_id"], entry["rule_version"])
        bundle_path = root / entry["path"]
        bundle = _strict_json(bundle_path, "rule bundle")
        if bundle.get("rule_id") != entry["rule_id"] or \
                bundle.get("rule_version") != entry["rule_version"]:
            raise ArtifactIntegrityError(
                f"{entry['path']}: bundle ids {bundle.get('rule_id')}@"
                f"{bundle.get('rule_version')} disagree with manifest entry")
        if check_capability_bindings:
            _verify_capability_bindings(bundle, capabilities, runtime_overrides)
        bundles[key] = bundle
        plans[key] = _construct_plan(bundle)

    return LoadedArtifact(build_dir=root, manifest=manifest, capabilities=capabilities,
                          bundles=bundles, plans=plans)


def verify_manifest_only(build_dir: str | Path) -> LoadedArtifact:
    """Manifest/entry-digest verification WITHOUT runtime capability binding
    (used by lifecycle rebuilds, where stale bindings are the premise)."""
    return verify_build(build_dir, check_capability_bindings=False)


# ---------------------------------------------------------------------------
# manifest validation
# ---------------------------------------------------------------------------

def _load_manifest(root: Path) -> dict[str, Any]:
    manifest = _strict_json(root / MANIFEST_NAME, "artifact_manifest.json")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ArtifactIntegrityError("manifest entries must be a non-empty list")
    seen: dict[tuple[str, str], str] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != _ENTRY_FIELDS:
            raise ArtifactIntegrityError(
                f"manifest entry fields must be exactly {sorted(_ENTRY_FIELDS)}: {entry!r}")
        rid, ver, av = entry["rule_id"], entry["rule_version"], entry["artifact_version"]
        rel, sha = entry["path"], entry["sha256"]
        for name, value in (("rule_id", rid), ("rule_version", ver),
                            ("artifact_version", av), ("path", rel), ("sha256", sha)):
            if not isinstance(value, str) or not value.strip():
                raise ArtifactIntegrityError(f"manifest entry field {name!r} must be a non-empty string")
        if not re.fullmatch(_VERSION_RE, ver):
            raise ArtifactIntegrityError(f"manifest rule_version invalid: {ver!r}")
        if not public_contracts.safe_relative(rel):
            raise ArtifactIntegrityError(f"manifest path is not safe-relative: {rel!r}")
        if rel == "." or rel == MANIFEST_NAME or rel == _CAPABILITIES_REL:
            raise ArtifactIntegrityError(
                f"manifest entry path {rel!r} is a root/self reference")
        target = root / rel
        if target.is_symlink() or not target.exists():
            raise ArtifactIntegrityError(f"manifest entry target missing: {rel!r}")
        resolved = target.resolve()
        if resolved == root.resolve() or resolved == (root / MANIFEST_NAME).resolve():
            raise ArtifactIntegrityError(
                f"manifest entry path {rel!r} resolves to root/manifest self reference")
        key = (rid, ver)
        if key in seen:
            raise ArtifactIntegrityError(
                f"duplicate manifest entry for {rid}@{ver} (paths {seen[key]!r}, {rel!r})")
        seen[key] = rel
        actual = artifact_hash.path_digest(target)
        if actual != sha:
            raise ArtifactIntegrityError(
                f"entry {rid}@{ver}: sha256 mismatch for {rel!r} "
                f"(manifest {sha}, actual {actual}) — artifact tampered or stale")
    return manifest


def _load_capabilities(root: Path) -> dict[str, dict[str, Any]]:
    caps_doc = _strict_json(root / _CAPABILITIES_REL, "capabilities.json")
    capabilities = caps_doc.get("capabilities")
    if not isinstance(capabilities, dict) or not capabilities:
        raise ArtifactIntegrityError("capabilities.json must carry a capabilities object")
    for name, entry in capabilities.items():
        if not isinstance(entry, dict) or entry.get("name") != name:
            raise ArtifactIntegrityError(f"capability entry name mismatch: {name!r}")
        modules = entry.get("modules")
        if not isinstance(modules, list) or not modules:
            raise ArtifactIntegrityError(f"capability {name!r} has no modules")
        for mod in modules:
            if not isinstance(mod, dict) or not mod.get("module") or not mod.get("sha256"):
                raise ArtifactIntegrityError(f"capability {name!r} has an invalid module entry")
    return capabilities


# ---------------------------------------------------------------------------
# capability binding
# ---------------------------------------------------------------------------

def _verify_capability_bindings(bundle: dict[str, Any],
                               capabilities: dict[str, dict[str, Any]],
                               runtime_overrides: dict[str, str] | None) -> None:
    deps = bundle.get("capability_deps")
    hashes = bundle.get("capability_hashes")
    if not isinstance(deps, list) or not isinstance(hashes, dict) or set(deps) != set(hashes):
        raise ArtifactIntegrityError(
            f"{bundle.get('rule_id')}@{bundle.get('rule_version')}: "
            "bundle capability_deps/capability_hashes disagree")
    for cap in deps:
        entry = capabilities.get(cap)
        if entry is None:
            raise ArtifactIntegrityError(
                f"capability {cap!r} not in frozen capabilities.json")
        frozen_digest = _canonical_digest(entry)
        if hashes[cap] != frozen_digest:
            raise ArtifactIntegrityError(
                f"capability {cap!r} digest disagrees with the frozen rule bundle "
                f"(bundle {hashes[cap]}, table {frozen_digest}) — rebuild required")
        # Every implementing module's current runtime hash must match the frozen value.
        for idx, mod_entry in enumerate(entry["modules"]):
            mod_name = mod_entry["module"]
            if runtime_overrides and mod_name in runtime_overrides:
                current = runtime_overrides[mod_name]
            elif runtime_overrides and cap in runtime_overrides and idx == 0:
                current = runtime_overrides[cap]
            else:
                current = module_source_sha256(mod_name)[1]
            frozen_sha = mod_entry["sha256"]
            if current != frozen_sha:
                raise ArtifactIntegrityError(
                    f"capability {cap!r} module {mod_name!r} runtime source hash disagrees "
                    f"with the frozen artifact (frozen {frozen_sha}, current {current}) — "
                    "rebuild required")


def _canonical_digest(obj: Any) -> str:
    return artifact_hash.digest_obj(obj)


def _strict_json(path: Path, what: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ArtifactIntegrityError(f"{what} missing: {path}")
    try:
        raw = public_contracts.regular_bytes(path)
    except public_contracts.ContractError as e:
        raise ArtifactIntegrityError(f"{what} unreadable: {e}") from e
    try:
        obj = public_contracts.strict_loads(raw.decode("utf-8"))
    except (public_contracts.ContractError, UnicodeDecodeError) as e:
        raise ArtifactIntegrityError(f"{what} is not strict JSON: {e}") from e
    if not isinstance(obj, dict):
        raise ArtifactIntegrityError(f"{what} must be a JSON object")
    return obj


def _construct_plan(bundle: dict[str, Any]) -> RulePlan:
    plan_json = bundle.get("plan")
    if not isinstance(plan_json, dict):
        raise ArtifactIntegrityError(
            f"{bundle.get('rule_id')}@{bundle.get('rule_version')}: bundle has no plan object")
    rid = bundle["rule_id"]
    try:
        if rid == "B001":
            return B001Plan.model_validate(plan_json)
        from o2_audit.rules.advanced import FAMILY_BY_RULE

        family = FAMILY_BY_RULE[rid]
        return family.PLAN_CLASS.model_validate(plan_json)
    except Exception as e:
        raise ArtifactIntegrityError(
            f"{rid}@{bundle.get('rule_version')}: frozen plan failed typed "
            f"validation: {e}") from e


__all__ = [
    "ArtifactIntegrityError",
    "FrozenRegistry",
    "LoadedArtifact",
    "verify_build",
    "verify_manifest_only",
]
