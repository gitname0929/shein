"""Stage 02b tests: rules JSONL importer, frozen artifact compiler, tamper/
capability-hash binding loader, transitive impact, selective incremental
rebuild and the version lifecycle (activate/rollback/manual overrides).

All builds run into ``tmp_path``; SQLite state lives in ``tmp_path`` too
(never inside frozen artifacts). No reference labels are read anywhere.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from o2_public_contracts import artifact_hash
from o2_public_contracts import contracts as public_contracts

from o2_audit.build.compiler import BuildError, compile_build
from o2_audit.build.importer import import_rules_jsonl
from o2_audit.build.loader import ArtifactIntegrityError, verify_build, verify_manifest_only
from o2_audit.build.lifecycle import (
    activate_version,
    affected_rule_pairs,
    compare_versions,
    rebuild_affected,
    record_override,
    record_version,
    rollback_to,
    transitive_dependents,
    version_status,
)
from o2_audit.cli import main as cli_main
from o2_audit.resources import rules_dir
from o2_audit.storage.store import TaskStore

from conftest import make_task, pass_batch
from o2_audit.runtime.gateway import StaticObservationGateway

ARTIFACT_VERSION = "stage02b-test-v1"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def combined_jsonl(tmp_path: Path) -> Path:
    """The 62-rule source: B001@3.0/3.1 + R001..R060@3.0 concatenated."""
    out = tmp_path / "combined_rules.jsonl"
    parts = []
    for name in ("基础规则版本.jsonl", "进阶规则60条.jsonl"):
        parts.append((rules_dir() / name).read_text(encoding="utf-8").rstrip("\n"))
    out.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return out


@pytest.fixture()
def store(tmp_path: Path) -> TaskStore:
    s = TaskStore(tmp_path / "workspace" / "state.sqlite3")
    yield s
    s.close()


def build(tmp_path: Path, store: TaskStore, rules: Path, name: str = "baseline",
          version: str = ARTIFACT_VERSION, out_name: str = "build") -> Path:
    out = tmp_path / out_name
    result = compile_build(
        rules_jsonl=rules, config={"artifact_version": version}, out_dir=out,
        artifact_version=version, store=store, version_name=name)
    return result.build_dir


# ---------------------------------------------------------------------------
# importer
# ---------------------------------------------------------------------------

def test_importer_62_valid_rules(combined_jsonl: Path):
    result = import_rules_jsonl(combined_jsonl)
    assert len(result.valid) == 62
    assert not result.issues and not result.duplicates
    keys = result.keys
    assert ("B001", "3.0") in keys and ("B001", "3.1") in keys
    assert sum(1 for rid, ver in keys if rid.startswith("R") and ver == "3.0") == 60
    assert result.source_file_sha256 == artifact_hash.digest_bytes(
        combined_jsonl.read_bytes())
    for record in result.valid:
        assert record.source_line > 0 and len(record.source_line_sha256) == 64
        assert record.rule_text.strip()


def test_importer_per_record_isolation_with_line_numbers(tmp_path: Path):
    lines = [
        json.dumps({"rule_id": "R001", "version": "3.0", "title": "t",
                    "rule_text": "规则文本", "material_role_candidates": ["package_photos"]}),
        '{"rule_id": "R002", "version": "3.0", broken',            # line 2: malformed
        '{"rule_id": "R003", "version": "3.0", "rule_text": "x", "dup": 1, "dup": 2}',  # line 3: dup key
        '{"rule_id": "R004", "version": "3.0", "rule_text": NaN}',  # line 4: NaN
        json.dumps({"rule_id": "B001", "version": "3.0", "title": "b",
                    "rule_text": "B001 文本"}),
        json.dumps({"rule_id": "R001", "version": "3.0", "title": "t2",
                    "rule_text": "重复键"}),                        # line 6: duplicate key row
        json.dumps({"rule_id": "R099", "version": "9.9", "rule_text": "?"}),  # line 7: invalid id
    ]
    path = tmp_path / "messy.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = import_rules_jsonl(path)
    # valid rows still imported despite surrounding failures
    assert {(r.rule_id, r.rule_version) for r in result.valid} == {("R001", "3.0"), ("B001", "3.0")}
    codes = {i["error_code"] for i in result.issues}
    assert {"malformed_json", "duplicate_json_key", "non_finite_constant",
            "invalid_rule_id"} <= codes
    by_line = {i["line_number"]: i for i in result.issues}
    assert set(by_line) == {2, 3, 4, 7}
    assert by_line[4]["error_code"] == "non_finite_constant"
    assert all(set(i) >= {"line_number", "error_code", "message", "next_action"}
               for i in result.issues)
    # duplicate (rule_id, rule_version) row: idempotent dedup, first wins
    assert result.duplicates == [{
        "line_number": 6, "rule_id": "R001", "rule_version": "3.0", "kept_line": 1,
        "next_action": "ignored (idempotent dedup by rule_id+rule_version)"}]
    assert result.record_by_key("R001", "3.0").title == "t"


def test_importer_idempotent_reimport(combined_jsonl: Path):
    first = import_rules_jsonl(combined_jsonl)
    second = import_rules_jsonl(combined_jsonl)
    assert first.keys == second.keys
    assert first.source_file_sha256 == second.source_file_sha256
    assert not second.duplicates and not second.issues


# ---------------------------------------------------------------------------
# build: manifest / SOPs / ledger / root digest
# ---------------------------------------------------------------------------

def test_build_full_manifest_sops_ledger(tmp_path: Path, store: TaskStore,
                                         combined_jsonl: Path):
    build_dir = build(tmp_path, store, combined_jsonl)

    # -- manifest: strict JSON, exact public schema, 62 unique entries
    manifest = public_contracts.strict_loads(
        (build_dir / "artifact_manifest.json").read_text(encoding="utf-8"))
    entries = manifest["entries"]
    assert set(manifest) == {"entries"} and len(entries) == 62
    seen = set()
    for e in entries:
        assert set(e) == {"rule_id", "rule_version", "artifact_version", "path", "sha256"}
        assert e["artifact_version"] == ARTIFACT_VERSION
        assert (e["rule_id"], e["rule_version"]) not in seen
        seen.add((e["rule_id"], e["rule_version"]))
        # no root self-reference: no entry points at "." or the manifest itself
        assert e["path"] not in (".", "artifact_manifest.json")
        assert public_contracts.safe_relative(e["path"])
        assert e["path"].startswith("engine/rules/")
        # sha256 computed via vendored artifact_hash digest of the real file
        assert e["sha256"] == artifact_hash.path_digest(build_dir / e["path"])
    assert ("B001", "3.0") in seen and ("R060", "3.0") in seen

    # -- capability table is present and referenced by every rule bundle
    caps = public_contracts.strict_loads(
        (build_dir / "engine" / "capabilities.json").read_text(encoding="utf-8"))
    assert {"inspect_material", "rules.b001", "rules.f01", "operators.text"} <= set(caps["capabilities"])
    for e in entries:
        bundle = public_contracts.strict_loads(
            (build_dir / e["path"]).read_text(encoding="utf-8"))
        assert bundle["capability_deps"] and set(bundle["capability_hashes"]) == set(bundle["capability_deps"])
        for cap, digest in bundle["capability_hashes"].items():
            assert digest == artifact_hash.digest_obj(caps["capabilities"][cap])

    # -- SOP/policy JSONs: B001 3.0 & 3.1 present, non-template, genuine quotes
    for version, threshold in (("3.0", "100"), ("3.1", "80")):
        sop = public_contracts.strict_loads(
            (build_dir / "engine" / "sop" / f"B001_{version}.json").read_text(encoding="utf-8"))
        assert set(sop) >= {"template_only", "rule_id", "rule_version",
                            "source_quotes", "sop", "build_policy", "artifact"}
        assert sop["template_only"] is False
        assert sop["rule_id"] == "B001" and sop["rule_version"] == version
        assert sop["source_quotes"], "source_quotes must be non-empty"
        assert all(q["quote"].strip() for q in sop["source_quotes"])
        assert sop["sop"]["applicability"]["small_tag_threshold_mm"] == threshold
        assert len(sop["sop"]["decision_order"]) == 5
        assert sop["build_policy"]["atomic_capabilities"]
        assert sop["artifact"]["version"] == ARTIFACT_VERSION
        assert sop["artifact"]["source_hash"]
    assert len(list((build_dir / "engine" / "sop").glob("*.json"))) == 62

    # -- advanced 60-rule ledger: exact template headers, 60 unique rows,
    #    blank unmeasured metrics
    import csv

    with (build_dir / "ledger" / "advanced_60_ledger.csv").open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    template = list(csv.reader(
        (Path(rules_dir()).parent / "protocol" / "60条建设台账模板.csv")
        .open(encoding="utf-8", newline="")))
    assert rows[0] == template[0]
    data = rows[1:]
    assert len(data) == 60
    ids = [(r[0], r[1]) for r in data]
    assert len(set(ids)) == 60 and ids == [(f"R{i:03d}", "3.0") for i in range(1, 61)]
    for row in data:
        assert row[2] == "built" and row[3] == ARTIFACT_VERSION
        assert row[4] == artifact_hash.path_digest(build_dir / row[5])
        # never fabricated: accuracy / machine_review_rate stay blank
        assert row[9] == "" and row[10] == ""
        assert row[6], "shared_capabilities recorded"

    # -- cleanliness + reproducible whole-directory digest
    names = [p.name for p in build_dir.rglob("*")]
    assert not any("__pycache__" in n or n == ".DS_Store" for n in names)
    assert not any(p.suffix in (".pyc", ".sqlite3", ".log") for p in build_dir.rglob("*"))
    root1 = artifact_hash.path_digest(build_dir)
    root2 = artifact_hash.tree_digest(build_dir)
    assert root1 == root2
    # the root digest was recorded in the store, never written back inside
    assert store.get_artifact_version("baseline")["root_sha256"] == root1
    assert root1.encode() not in (build_dir / "artifact_manifest.json").read_bytes()


def test_build_is_byte_reproducible(tmp_path: Path, combined_jsonl: Path):
    """Same inputs + same artifact_version => identical whole-directory digest
    (file mtimes/permissions never enter the digest)."""
    out_a = compile_build(rules_jsonl=combined_jsonl, config={}, out_dir=tmp_path / "a",
                          artifact_version=ARTIFACT_VERSION)
    out_b = compile_build(rules_jsonl=combined_jsonl, config={}, out_dir=tmp_path / "b",
                          artifact_version=ARTIFACT_VERSION)
    assert out_a.root_sha256 == out_b.root_sha256


def test_build_subset_marks_remaining_advanced_rules_not_built(tmp_path: Path, store: TaskStore):
    b001_only = rules_dir() / "基础规则版本.jsonl"
    build_dir = build(tmp_path, store, b001_only, name="b001-baseline")
    manifest = json.loads((build_dir / "artifact_manifest.json").read_text(encoding="utf-8"))
    assert {(e["rule_id"], e["rule_version"]) for e in manifest["entries"]} == \
        {("B001", "3.0"), ("B001", "3.1")}
    import csv

    with (build_dir / "ledger" / "advanced_60_ledger.csv").open(encoding="utf-8", newline="") as fh:
        data = list(csv.reader(fh))[1:]
    assert len(data) == 60 and len({(r[0], r[1]) for r in data}) == 60
    assert all(row[2] == "not_built" and row[3] == "" and row[4] == "" for row in data)


def test_version_record_is_immutable(tmp_path: Path, store: TaskStore, combined_jsonl: Path):
    build_dir = build(tmp_path, store, combined_jsonl)
    record = store.get_artifact_version("baseline")
    # same identity: idempotent
    record_version(store, "baseline", build_dir, record["artifact_version"])
    # different digest under the same name: refused
    with pytest.raises(ValueError, match="immutable"):
        store.record_artifact_version(name="baseline", build_dir=str(build_dir),
                                      artifact_version=record["artifact_version"],
                                      root_sha256="0" * 64)


# ---------------------------------------------------------------------------
# tamper & capability-hash binding
# ---------------------------------------------------------------------------

def test_tampered_rule_file_fails_verification(tmp_path: Path, store: TaskStore,
                                               combined_jsonl: Path):
    build_dir = build(tmp_path, store, combined_jsonl)
    verify_build(build_dir)  # clean artifact verifies
    target = build_dir / "engine" / "rules" / "R001_3.0.json"
    original = target.read_bytes()
    target.write_bytes(original.replace(b"R001", b"R0XX", 1))
    with pytest.raises(ArtifactIntegrityError, match="sha256 mismatch"):
        verify_build(build_dir)
    # also fails with only manifest-level verification (rebuild precondition)
    with pytest.raises(ArtifactIntegrityError, match="sha256 mismatch"):
        verify_manifest_only(build_dir)


def test_capability_hash_change_requires_rebuild(tmp_path: Path, store: TaskStore,
                                                 combined_jsonl: Path):
    build_dir = build(tmp_path, store, combined_jsonl)
    simulated = {"rules.f01": "a" * 64}
    # frozen artifact + changed capability code => refuse execution
    with pytest.raises(ArtifactIntegrityError, match="rules.f01"):
        verify_build(build_dir, runtime_overrides=simulated)
    # multi-module capability: modifying the SECOND implementing module also fails
    with pytest.raises(ArtifactIntegrityError, match="o2_audit.rules.b001.plan"):
        verify_build(build_dir, runtime_overrides={"o2_audit.rules.b001.plan": "d" * 64})
    # transitive capability change on a single-rule subset build is also caught
    r001_line = (rules_dir() / "进阶规则60条.jsonl").read_text(encoding="utf-8").splitlines()[0]
    r001_jsonl = tmp_path / "r001_only.jsonl"
    r001_jsonl.write_text(r001_line + "\n", encoding="utf-8")
    r001_dir = build(tmp_path, store, r001_jsonl, name="r001-only", out_name="r001_build")
    with pytest.raises(ArtifactIntegrityError, match="operators.numbers"):
        verify_build(r001_dir, runtime_overrides={"operators.numbers": "e" * 64})
    # unchanged code verifies fine (no overrides)
    verify_build(build_dir)

    cand = rebuild_affected(
        build_dir, tmp_path / "cand", rules_jsonl=combined_jsonl, config={},
        artifact_version="cand-v2", changed_capabilities=("rules.f01",),
        runtime_overrides=simulated, store=store, version_name="candidate")
    # the rebuilt candidate binds the NEW simulated hash: verifies in the
    # simulated world, but the OLD artifact still refuses
    verify_build(cand.out_dir, runtime_overrides=simulated)
    with pytest.raises(ArtifactIntegrityError, match="rules.f01"):
        verify_build(build_dir, runtime_overrides=simulated)


def test_loader_rejects_unknown_or_malformed_manifest(tmp_path: Path, store: TaskStore,
                                                      combined_jsonl: Path):
    build_dir = build(tmp_path, store, combined_jsonl)
    manifest_path = build_dir / "artifact_manifest.json"
    original = manifest_path.read_text(encoding="utf-8")

    # duplicate manifest keys are rejected (strict JSON)
    manifest_path.write_text('{"entries": [], "entries": []}', encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError):
        verify_build(build_dir)
    # empty entries rejected
    manifest_path.write_text('{"entries": []}', encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError, match="non-empty"):
        verify_build(build_dir)
    # root self-reference rejected
    manifest = json.loads(original)
    manifest["entries"].append({"rule_id": "ROOT", "rule_version": "3.0",
                                "artifact_version": "x", "path": ".", "sha256": "0" * 64})
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError, match="self reference|not safe-relative"):
        verify_build(build_dir)
    # duplicate (rule_id, rule_version) rejected
    manifest = json.loads(original)
    manifest["entries"].append(dict(manifest["entries"][0], path="engine/rules/R002_3.0.json"))
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError, match="duplicate manifest entry"):
        verify_build(build_dir)


def test_controller_stamps_loaded_artifact_version(tmp_path: Path, store: TaskStore,
                                                   combined_jsonl: Path):
    build_dir = build(tmp_path, store, combined_jsonl)
    loaded = verify_build(build_dir)
    controller = loaded.controller(gateway=StaticObservationGateway([pass_batch()]))
    task = make_task()  # B001@3.0
    row = controller.run(task)
    expected = next(e["artifact_version"] for e in loaded.entries
                    if (e["rule_id"], e["rule_version"]) == ("B001", "3.0"))
    assert row["artifact_version"] == expected == ARTIFACT_VERSION
    # the loaded plan is the frozen one (B001 3.0 small-tag threshold)
    assert loaded.plan_for("B001", "3.0").small_tag_threshold_mm == 100
    assert loaded.plan_for("B001", "3.1").small_tag_threshold_mm == 80
    # a rule NOT in the manifest is unusable through the frozen registry
    subset_dir = build(tmp_path, store, rules_dir() / "基础规则版本.jsonl",
                       name="b001-sub", out_name="b001_build")
    subset = verify_build(subset_dir)
    from o2_audit.rules.base import UnsupportedRuleError

    with pytest.raises(UnsupportedRuleError):
        subset.plan_for("R001", "3.0")
    with pytest.raises(UnsupportedRuleError):
        subset.controller(gateway=StaticObservationGateway([]))._resolve_plan(
            make_task(rule_id="R001"))


def test_loaded_advanced_rule_executes_end_to_end(tmp_path: Path, store: TaskStore,
                                                  combined_jsonl: Path):
    from o2_audit.domain.models import (
        Entity, EntityKind, MaterialRef, Observation, ObservationSource,
        ObservationStatus,
    )
    from o2_audit.runtime.gateway import ObservationBatch

    build_dir = build(tmp_path, store, combined_jsonl)
    loaded = verify_build(build_dir)
    # R001 (包装主视图完整性): package main view with all four boundaries
    # visible -> pass; frozen plan drives the evaluation.
    task = make_task(
        rule_id="R001", rule_version="3.0",
        metadata={"has_outer_package": True},
        materials=[MaterialRef(material_id="m1", role="package_photos",
                               relative_path="assets/m1.png", media_type="image/png",
                               page_index=1)],
    )
    batch = ObservationBatch(
        entities=[Entity(entity_id="PKG1", kind=EntityKind.PACKAGE)],
        observations=[Observation(
            entity_id="PKG1", predicate="boundary_visible",
            status=ObservationStatus.READABLE, raw_value=True, normalized_value=True,
            scope_complete=True,
            source=ObservationSource(material_id="m1", page_index=1))],
    )
    controller = loaded.controller(gateway=StaticObservationGateway([batch]))
    row = controller.run(task)
    assert row["execution_status"] == "completed"
    assert row["rule_id"] == "R001" and row["rule_version"] == "3.0"
    assert row["label"] == "pass"
    assert row["artifact_version"] == ARTIFACT_VERSION


# ---------------------------------------------------------------------------
# transitive impact & selective incremental rebuild
# ---------------------------------------------------------------------------

def test_transitive_impact_analysis(combined_jsonl: Path):
    # operators.numbers is transitively needed by every family (advanced_base)
    # and by B001 -> ALL 62 pairs are affected.
    affected = affected_rule_pairs(("operators.numbers",))
    assert len(affected) == 62
    # rules.f01 is only needed by R001..R006 (no reverse deps).
    affected = affected_rule_pairs(("rules.f01",))
    assert affected == {(f"R{i:03d}", "3.0") for i in range(1, 7)}
    # transitive: advanced_base depends on operators.numbers -> changing
    # numbers impacts everything; inspect_material impacts everything too.
    assert "rules.f10" in transitive_dependents(("operators.numbers",))
    assert len(affected_rule_pairs(("inspect_material",))) == 62
    # unknown module names are rejected
    with pytest.raises(BuildError):
        affected_rule_pairs(changed_modules=("o2_audit.not.a.capability",))


def test_selective_rebuild_retains_unaffected_sha256(tmp_path: Path, store: TaskStore,
                                                     combined_jsonl: Path):
    base = build(tmp_path, store, combined_jsonl)
    base_root = artifact_hash.tree_digest(base)
    base_entries = {(e["rule_id"], e["rule_version"]): e["sha256"]
                    for e in json.loads((base / "artifact_manifest.json")
                                        .read_text(encoding="utf-8"))["entries"]}

    result = rebuild_affected(
        base, tmp_path / "cand", rules_jsonl=combined_jsonl, config={},
        artifact_version="cand-v2", changed_capabilities=("rules.f01",),
        runtime_overrides={"rules.f01": "b" * 64}, store=store, version_name="candidate")
    assert set(result.affected_pairs) == {
        (f"R{i:03d}", "3.0") for i in range(1, 7)}
    # at least one unaffected rule GENUINELY retains its identical sha256
    assert ("B001", "3.0") in result.unchanged_pairs
    assert ("R007", "3.0") in result.retained_identical
    cand_manifest = json.loads((result.out_dir / "artifact_manifest.json")
                               .read_text(encoding="utf-8"))
    cand_entries = {(e["rule_id"], e["rule_version"]): e for e in cand_manifest["entries"]}
    assert cand_entries[("R007", "3.0")]["sha256"] == base_entries[("R007", "3.0")]
    assert cand_entries[("B001", "3.1")]["sha256"] == base_entries[("B001", "3.1")]
    # affected entries actually changed
    assert cand_entries[("R001", "3.0")]["sha256"] != base_entries[("R001", "3.0")]
    assert cand_entries[("R001", "3.0")]["artifact_version"] == "cand-v2"
    # the new manifest fully verifies and the candidate is parented on baseline
    verify_manifest_only(result.out_dir)
    cand_record = store.get_artifact_version("candidate")
    assert cand_record["parent_name"] == "baseline"
    assert cand_record["root_sha256"] == result.root_sha256
    # baseline directory untouched (byte-identical root digest)
    assert artifact_hash.tree_digest(base) == base_root
    # comparison reports the expected diff
    diff = compare_versions(store, "baseline", "candidate")
    changed = [d for d in diff["entries"] if d["change"] == "changed"]
    unchanged = [d for d in diff["entries"] if d["change"] == "unchanged"]
    assert len(changed) == 6 and len(unchanged) == 56
    assert diff["root_sha256_equal"] is False


def test_rebuild_rejects_tampered_base(tmp_path: Path, store: TaskStore,
                                       combined_jsonl: Path):
    base = build(tmp_path, store, combined_jsonl)
    target = base / "engine" / "rules" / "R010_3.0.json"
    original = target.read_bytes()
    target.write_bytes(original.replace(b"R010", b"R0XX", 1))
    with pytest.raises(ArtifactIntegrityError):
        rebuild_affected(base, tmp_path / "cand", rules_jsonl=combined_jsonl,
                         config={}, artifact_version="cand-v2",
                         changed_capabilities=("rules.f02",))


# ---------------------------------------------------------------------------
# lifecycle: activate / rollback / manual overrides
# ---------------------------------------------------------------------------

def test_activate_rollback_restores_exact_previous_version(tmp_path: Path, store: TaskStore,
                                                           combined_jsonl: Path):
    base = build(tmp_path, store, combined_jsonl)
    result = rebuild_affected(
        base, tmp_path / "cand", rules_jsonl=combined_jsonl, config={},
        artifact_version="cand-v2", changed_capabilities=("rules.f03",),
        runtime_overrides={"rules.f03": "c" * 64}, store=store, version_name="candidate")

    assert store.active_version_name() is None
    activated = activate_version(store, "candidate")
    assert activated["active"] == "candidate"
    assert store.active_version_name() == "candidate"
    assert activated["root_sha256"] == result.root_sha256

    rolled = rollback_to(store, "baseline")
    assert rolled["active"] == "baseline"
    assert rolled["rolled_back_from"] == "candidate"
    assert store.active_version_name() == "baseline"
    assert rolled["root_sha256"] == store.get_artifact_version("baseline")["root_sha256"]
    status = version_status(store)
    assert status["active"] == "baseline" and len(status["versions"]) == 2

    # a tampered candidate directory can never be activated
    victim = result.out_dir / "engine" / "rules" / "R007_3.0.json"
    original = victim.read_bytes()
    victim.write_bytes(original.replace(b"R007", b"R0XX", 1))
    with pytest.raises(ArtifactIntegrityError, match="SHA256 mismatch"):
        activate_version(store, "candidate")
    assert store.active_version_name() == "baseline"


def test_manual_override_diff_recorded(tmp_path: Path, store: TaskStore,
                                       combined_jsonl: Path):
    build(tmp_path, store, combined_jsonl)
    override_id = record_override(
        store, "baseline", who="reviewer-7", reason="口径补充: 附签阈值按 3.1 执行",
        field="B001.small_tag_threshold_mm", old_value="100", new_value="80")
    overrides = store.manual_overrides_for("baseline")
    assert len(overrides) == 1 and overrides[0]["override_id"] == override_id
    assert overrides[0]["who"] == "reviewer-7"
    assert overrides[0]["reason"].startswith("口径补充")
    assert overrides[0]["field"] == "B001.small_tag_threshold_mm"
    assert overrides[0]["old_value"] == "100" and overrides[0]["new_value"] == "80"
    events = store.version_events_for("baseline")
    assert any(ev["event"] == "manual_override" for ev in events)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_build_status_activate_rollback(tmp_path: Path, capsys,
                                            combined_jsonl: Path):
    ws = tmp_path / "ws"
    out = tmp_path / "cli_build"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"artifact_version": "cli-v1",
                                  "description": "cli test"}), encoding="utf-8")
    rc = cli_main(["--workspace", str(ws), "build",
                   "--rules", str(combined_jsonl), "--config", str(config),
                   "--out", str(out)])
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["built_rules"] == 62 and len(summary["root_sha256"]) == 64
    assert (out / "artifact_manifest.json").is_file()

    # status lists the recorded version (named by artifact_version)
    assert cli_main(["--workspace", str(ws), "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert [v["name"] for v in status["versions"]] == ["cli-v1"]

    # activate + rollback through the CLI
    assert cli_main(["--workspace", str(ws), "activate", "--name", "cli-v1"]) == 0
    assert json.loads(capsys.readouterr().out)["active"] == "cli-v1"
    assert cli_main(["--workspace", str(ws), "rollback", "--to", "cli-v1"]) == 0
    assert json.loads(capsys.readouterr().out)["active"] == "cli-v1"

    # non-zero exit on failures (missing config field / bad version name)
    bad_config = tmp_path / "bad.json"
    bad_config.write_text("{}", encoding="utf-8")
    assert cli_main(["--workspace", str(ws), "build", "--rules", str(combined_jsonl),
                     "--config", str(bad_config), "--out", str(tmp_path / "b2")]) == 1
    assert cli_main(["--workspace", str(ws), "activate", "--name", "nope"]) == 1


def test_cli_invalid_iterate_exits_nonzero_without_writing_out(tmp_path: Path):
    # All five protocol 1.3.1 commands (build/infer/annotate/iterate/evaluate)
    # are implemented; invalid iterate inputs fail non-zero and never write out.
    out_dir = tmp_path / "never_written"
    rc = cli_main([
        "iterate", "--base", str(tmp_path / "missing_base"),
        "--feedback", str(tmp_path / "missing_fb.json"),
        "--input", str(tmp_path / "missing_in.jsonl"),
        "--out", str(out_dir),
    ])
    assert rc != 0
    assert not out_dir.exists()


def test_console_script_entry_available():
    """The installed ``o2-audit`` console script resolves and prints help."""
    proc = subprocess.run([sys.executable, "-m", "o2_audit.cli", "--help"],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0
    assert "build" in proc.stdout and "rollback" in proc.stdout
