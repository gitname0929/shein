"""Version registry, transitive dependency impact and selective incremental
rebuild (Stage 02b).

- Immutable version records live in the TaskStore (name, build_dir,
  artifact_version, whole-directory root SHA256, parent artifact identity,
  manual override diffs, event history) — NEVER inside the frozen artifact.
- Impact analysis: capability -> dependent capabilities/rules; given a
  changed capability (or module), compute ALL directly and transitively
  affected ``(rule_id, rule_version)`` pairs.
- ``rebuild_affected``: rebuild ONLY affected rules into a NEW immutable
  OUT_DIR while unaffected rule sub-artifacts are copied byte-for-byte, so
  unaffected rules genuinely retain their identical ``entry.sha256``.
- ``activate`` / ``rollback`` restore the active pointer after verifying the
  target's exact directory SHA256.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from o2_public_contracts import artifact_hash

from o2_audit.build.capabilities import CAPABILITY_MODULES, build_capability_entries
from o2_audit.build.compiler import (
    BuildError,
    _assert_clean,
    _write_json,
    build_sop,
    rule_bundle_capability_deps,
)
from o2_audit.build.importer import import_rules_jsonl
from o2_audit.build.loader import (
    ArtifactIntegrityError,
    LoadedArtifact,
    verify_build,
    verify_manifest_only,
)
from o2_audit.protocol.jsonio import dump_json_strict
from o2_audit.rules.registry import RuleRegistry, default_registry
from o2_audit.storage.store import TaskStore


# ---------------------------------------------------------------------------
# dependency graph / impact analysis
# ---------------------------------------------------------------------------

def capability_graph() -> dict[str, tuple[str, ...]]:
    """capability -> capabilities it directly depends on (real import-derived
    edges from :mod:`o2_audit.build.capabilities`)."""
    from o2_audit.build.capabilities import capability_dependencies

    return {cap: capability_dependencies(cap) for cap in CAPABILITY_MODULES}


def rule_capability_deps(rule_pairs: Iterable[tuple[str, str]] | None = None,
                         registry: RuleRegistry | None = None,
                         ) -> dict[tuple[str, str], tuple[str, ...]]:
    """(rule_id, rule_version) -> full capability dependency set."""
    reg = registry if registry is not None else default_registry()
    pairs = rule_pairs if rule_pairs is not None else reg.keys()
    return {pair: rule_bundle_capability_deps(pair[0]) for pair in pairs}


def transitive_dependents(
    changed: Iterable[str],
    graph: dict[str, tuple[str, ...]] | None = None,
) -> set[str]:
    """All capabilities DIRECTLY or TRANSITIVELY affected by ``changed``.

    ``changed`` holds capability names whose implementation changed; every
    capability that (transitively) depends on any of them is affected.
    """
    graph = graph if graph is not None else capability_graph()
    changed_set = set(changed)
    affected = set(changed_set)
    frontier = set(changed_set)
    while frontier:
        dependents = {
            cap for cap, deps in graph.items()
            if cap not in affected and affected.intersection(deps)
        }
        if not dependents:
            break
        affected |= dependents
        frontier = dependents
    return affected


def affected_rule_pairs(
    changed_capabilities: Iterable[str] = (),
    changed_modules: Iterable[str] = (),
    rule_pairs: Iterable[tuple[str, str]] | None = None,
    registry: RuleRegistry | None = None,
) -> set[tuple[str, str]]:
    """All directly and transitively affected (rule_id, rule_version) pairs.

    ``changed_modules`` holds dotted module paths (mapped to their owning
    capability via ``MODULE_TO_CAPABILITY``).
    """
    from o2_audit.build.capabilities import MODULE_TO_CAPABILITY

    changed = set(changed_capabilities)
    for mod in changed_modules:
        cap = MODULE_TO_CAPABILITY.get(mod)
        if cap is None:
            raise BuildError(f"module {mod!r} is not a registered capability module")
        changed.add(cap)
    if not changed:
        return set()
    impacted = transitive_dependents(changed)
    deps = rule_capability_deps(rule_pairs, registry)
    return {pair for pair, cap_deps in deps.items() if impacted.intersection(cap_deps)}


# ---------------------------------------------------------------------------
# version records / activation / rollback
# ---------------------------------------------------------------------------

def record_version(store: TaskStore, name: str, build_dir: str | Path,
                   artifact_version: str, parent_name: str | None = None) -> str:
    """Compute the whole-directory root SHA256 of a finished build dir and
    register it as an immutable version record. The digest is computed over
    the CURRENT bytes — never written back inside the artifact."""
    root = Path(build_dir)
    if root.is_symlink() or not root.is_dir():
        raise ArtifactIntegrityError(f"build dir missing or symbolic: {root}")
    root_sha256 = artifact_hash.tree_digest(root)
    parent = store.get_artifact_version(parent_name) if parent_name else None
    store.record_artifact_version(
        name=name, build_dir=str(root), artifact_version=artifact_version,
        root_sha256=root_sha256,
        parent_name=parent_name,
        parent_root_sha256=parent["root_sha256"] if parent else None,
    )
    return root_sha256


def _verify_recorded_digest(store: TaskStore, name: str) -> dict[str, Any]:
    record = store.get_artifact_version(name)
    if record is None:
        raise KeyError(f"artifact version not registered: {name!r}")
    build_dir = Path(record["build_dir"])
    if build_dir.is_symlink() or not build_dir.is_dir():
        raise ArtifactIntegrityError(
            f"version {name!r}: build dir missing or symbolic: {build_dir}")
    actual = artifact_hash.tree_digest(build_dir)
    if actual != record["root_sha256"]:
        raise ArtifactIntegrityError(
            f"version {name!r}: directory SHA256 mismatch "
            f"(registered {record['root_sha256']}, actual {actual}) — "
            "frozen artifact was modified after registration")
    return record


def activate_version(store: TaskStore, name: str) -> dict[str, Any]:
    """Verify the recorded directory SHA256, then move the active pointer."""
    record = _verify_recorded_digest(store, name)
    store.set_active_version(name)
    store.record_version_event("activate", name, {
        "artifact_version": record["artifact_version"],
        "root_sha256": record["root_sha256"],
    })
    return {"active": name, "artifact_version": record["artifact_version"],
            "root_sha256": record["root_sha256"]}


def rollback_to(store: TaskStore, name: str) -> dict[str, Any]:
    """Restore the previous active pointer after verifying the exact
    directory SHA256 of the rollback target."""
    record = _verify_recorded_digest(store, name)
    previous = store.active_version_name()
    store.set_active_version(name)
    store.record_version_event("rollback", name, {
        "restored_from": previous,
        "artifact_version": record["artifact_version"],
        "root_sha256": record["root_sha256"],
    })
    return {"active": name, "rolled_back_from": previous,
            "artifact_version": record["artifact_version"],
            "root_sha256": record["root_sha256"]}


def record_override(store: TaskStore, version_name: str, who: str, reason: str,
                    field: str, old_value: Any, new_value: Any) -> int:
    """Record an explicit manual diff/override (who/reason/field diff)."""
    override_id = store.record_manual_override(version_name, who, reason, field,
                                               old_value, new_value)
    store.record_version_event("manual_override", version_name,
                               {"override_id": override_id, "field": field,
                                "who": who, "reason": reason})
    return override_id


def version_status(store: TaskStore) -> dict[str, Any]:
    versions = store.list_artifact_versions()
    return {"active": store.active_version_name(), "versions": versions}


# ---------------------------------------------------------------------------
# version comparison
# ---------------------------------------------------------------------------

def compare_versions(store: TaskStore, name_a: str, name_b: str) -> dict[str, Any]:
    """Compare two REGISTERED versions: verify both directories against their
    recorded digests, then diff their manifests entry by entry."""
    rec_a = _verify_recorded_digest(store, name_a)
    rec_b = _verify_recorded_digest(store, name_b)
    art_a = verify_manifest_only(rec_a["build_dir"])
    art_b = verify_manifest_only(rec_b["build_dir"])
    entries_a = {(e["rule_id"], e["rule_version"]): e for e in art_a.entries}
    entries_b = {(e["rule_id"], e["rule_version"]): e for e in art_b.entries}
    keys = sorted(set(entries_a) | set(entries_b))
    diff = []
    for key in keys:
        ea, eb = entries_a.get(key), entries_b.get(key)
        if ea is None:
            diff.append({"rule": f"{key[0]}@{key[1]}", "change": "added"})
        elif eb is None:
            diff.append({"rule": f"{key[0]}@{key[1]}", "change": "removed"})
        elif ea["sha256"] != eb["sha256"]:
            diff.append({"rule": f"{key[0]}@{key[1]}", "change": "changed",
                         "old_sha256": ea["sha256"], "new_sha256": eb["sha256"]})
        else:
            diff.append({"rule": f"{key[0]}@{key[1]}", "change": "unchanged",
                         "sha256": ea["sha256"]})
    return {
        "a": {"name": name_a, "artifact_version": rec_a["artifact_version"],
              "root_sha256": rec_a["root_sha256"]},
        "b": {"name": name_b, "artifact_version": rec_b["artifact_version"],
              "root_sha256": rec_b["root_sha256"]},
        "root_sha256_equal": rec_a["root_sha256"] == rec_b["root_sha256"],
        "entries": diff,
    }


# ---------------------------------------------------------------------------
# selective incremental rebuild
# ---------------------------------------------------------------------------

@dataclass
class RebuildResult:
    out_dir: Path
    artifact_version: str
    root_sha256: str
    base_root_sha256: str
    affected_pairs: list[tuple[str, str]]
    unchanged_pairs: list[tuple[str, str]]
    retained_identical: list[tuple[str, str]] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "out_dir": str(self.out_dir),
            "artifact_version": self.artifact_version,
            "root_sha256": self.root_sha256,
            "base_root_sha256": self.base_root_sha256,
            "affected": [f"{r}@{v}" for r, v in self.affected_pairs],
            "unchanged": [f"{r}@{v}" for r, v in self.unchanged_pairs],
            "retained_identical_sha256": [f"{r}@{v}" for r, v in self.retained_identical],
        }


def rebuild_affected(
    base_dir: str | Path,
    out_dir: str | Path,
    *,
    rules_jsonl: str | Path,
    config: dict[str, Any],
    artifact_version: str,
    changed_capabilities: Iterable[str] = (),
    changed_modules: Iterable[str] = (),
    runtime_overrides: dict[str, str] | None = None,
    store: TaskStore | None = None,
    version_name: str | None = None,
    registry: RuleRegistry | None = None,
) -> RebuildResult:
    """Selective incremental rebuild into a NEW immutable OUT_DIR.

    Only rules whose capability dependencies (direct or transitive) intersect
    the change set are recompiled; unaffected rule sub-artifacts are copied
    byte-for-byte and MUST retain their identical ``entry.sha256``.
    """
    base = Path(base_dir)
    out = Path(out_dir)
    if out.resolve() == base.resolve():
        raise BuildError("rebuild out dir must differ from the base build dir (immutable)")
    if out.exists() and any(out.iterdir()):
        raise BuildError(f"rebuild out dir already exists and is not empty: {out}")

    base_art = verify_manifest_only(base)  # entry digests verified; stale
    # capability bindings are exactly the premise of a rebuild.
    base_manifest = base_art.manifest
    base_entries = {(e["rule_id"], e["rule_version"]): e for e in base_manifest["entries"]}
    base_root_sha256 = artifact_hash.tree_digest(base)

    changed = set(changed_capabilities) | set((runtime_overrides or {}).keys())
    for mod in changed_modules:
        from o2_audit.build.capabilities import MODULE_TO_CAPABILITY

        cap = MODULE_TO_CAPABILITY.get(mod)
        if cap is None:
            raise BuildError(f"module {mod!r} is not a registered capability module")
        changed.add(cap)
    impacted_caps = transitive_dependents(changed) if changed else set()
    affected = {
        key for key, bundle in base_art.bundles.items()
        if impacted_caps.intersection(bundle["capability_deps"])
    }
    unchanged = set(base_art.bundles) - affected

    # 1. Copy the base build byte-for-byte; overwrite only affected parts.
    shutil.copytree(base, out)

    # 2. Recompute the capability table (real current hashes + overrides).
    capability_entries = build_capability_entries(runtime_overrides)
    _write_json(out / "engine" / "capabilities.json", {
        "capability_schema": "o2-capability-table/1",
        "capabilities": capability_entries,
    })

    reg = registry if registry is not None else default_registry()
    imported = import_rules_jsonl(rules_jsonl)
    rebuilt: dict[tuple[str, str], str] = {}
    for key in sorted(affected):
        rid, ver = key
        record = imported.record_by_key(rid, ver)
        if record is None:
            raise BuildError(
                f"cannot rebuild {rid}@{ver}: rule row missing from {rules_jsonl}")
        plan = reg.get_plan(rid, ver)
        deps = rule_bundle_capability_deps(rid)
        bundle = {
            "bundle_schema": "o2-rule-bundle/1",
            "rule_id": rid,
            "rule_version": ver,
            "source_row_sha256": record.source_line_sha256,
            "source_file_sha256": record.source_file_sha256,
            "capability_deps": list(deps),
            "capability_hashes": {
                d: artifact_hash.digest_obj(capability_entries[d]) for d in deps
            },
            "plan": plan.model_dump(mode="json"),
        }
        rel = f"engine/rules/{rid}_{ver}.json"
        _write_json(out / rel, bundle)
        rebuilt[key] = rel
        # SOP regenerated for the rebuilt rule (new artifact_version).
        _write_json(out / "engine" / "sop" / f"{rid}_{ver}.json",
                    build_sop(record, plan, deps, artifact_version))

    # 3. Ledger: rebuilt rows carry the new artifact_version/hash; unaffected
    #    rows keep the base values (their artifacts did not change).
    _rewrite_ledger(out, base_entries, set(rebuilt), artifact_version)

    # 4. Manifest: same entry set; rebuilt entries recomputed, unaffected
    #    entries copied byte-for-byte semantics (identical sha256).
    entries = []
    for key in sorted(base_entries):
        rid, ver = key
        rel = base_entries[key]["path"]
        sha = artifact_hash.path_digest(out / rel)
        if key in unchanged and sha != base_entries[key]["sha256"]:
            raise BuildError(
                f"unaffected rule {rid}@{ver} changed during rebuild — refusing")
        if key in unchanged:
            entries.append(dict(base_entries[key], artifact_version=artifact_version))
        else:
            entries.append({
                "rule_id": rid, "rule_version": ver,
                "artifact_version": artifact_version,
                "path": rel, "sha256": sha,
            })
    manifest = {"entries": entries}
    (out / "artifact_manifest.json").write_bytes(
        dump_json_strict(manifest).encode("utf-8"))
    _assert_clean(out)
    root_sha256 = artifact_hash.tree_digest(out)

    retained = [
        key for key in sorted(unchanged)
        if artifact_hash.path_digest(out / base_entries[key]["path"])
        == base_entries[key]["sha256"]
    ]

    if store is not None:
        name = version_name or artifact_version
        parent_name = _find_parent_name(store, base_root_sha256)
        record_version(store, name, out, artifact_version, parent_name=parent_name)
        store.record_version_event(
            "rebuild_affected", name,
            {"base_root_sha256": base_root_sha256, "root_sha256": root_sha256,
             "affected": [f"{r}@{v}" for r, v in sorted(affected)],
             "changed_capabilities": sorted(changed)})

    return RebuildResult(
        out_dir=out, artifact_version=artifact_version, root_sha256=root_sha256,
        base_root_sha256=base_root_sha256, affected_pairs=sorted(affected),
        unchanged_pairs=sorted(unchanged), retained_identical=retained,
        manifest=manifest)


def _find_parent_name(store: TaskStore, base_root_sha256: str) -> str | None:
    for record in store.list_artifact_versions():
        if record["root_sha256"] == base_root_sha256:
            return record["name"]
    return None


def _rewrite_ledger(out: Path, base_entries: dict[tuple[str, str], dict[str, Any]],
                    rebuilt: set[tuple[str, str]], artifact_version: str) -> None:
    from o2_audit.build.compiler import ADVANCED_RULE_IDS, LEDGER_HEADERS, _write_ledger_csv

    _write_ledger_csv(
        out / "ledger" / "advanced_60_ledger.csv",
        rows=[
            (rid, "3.0",
             "built" if (rid, "3.0") in base_entries else "not_built",
             artifact_version if (rid, "3.0") in rebuilt else
             (base_entries[(rid, "3.0")]["artifact_version"]
              if (rid, "3.0") in base_entries else ""),
             (artifact_hash.path_digest(out / base_entries[(rid, "3.0")]["path"])
              if (rid, "3.0") in base_entries else ""),
             (base_entries[(rid, "3.0")]["path"] if (rid, "3.0") in base_entries else ""),
             (";".join(rule_bundle_capability_deps(rid)) if (rid, "3.0") in base_entries else ""),
             "", "0" if (rid, "3.0") in base_entries else "", "", "")
            for rid in ADVANCED_RULE_IDS
        ],
        headers=LEDGER_HEADERS,
    )


__all__ = [
    "RebuildResult",
    "activate_version",
    "affected_rule_pairs",
    "capability_graph",
    "compare_versions",
    "rebuild_affected",
    "record_override",
    "record_version",
    "rollback_to",
    "rule_capability_deps",
    "transitive_dependents",
    "version_status",
]
