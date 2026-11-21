"""Frozen BUILD_DIR compiler (Stage 02b).

Compiles imported rules + a build config JSON into an immutable artifact
directory:

- ``engine/rules/<rule_id>_<rule_version>.json``  executable rule bundle:
  the typed plan JSON plus capability bindings (each rule references the
  canonical digest of every capability entry it depends on);
- ``engine/capabilities.json``  shared capability table: REAL source-file
  SHA256 of every implementing module under ``src/o2_audit/operators/`` and
  ``src/o2_audit/rules/`` (+ the runtime inspection loop), frozen at build
  time;
- ``engine/sop/<rule_id>_<rule_version>.json``  full SOP & build-policy JSON
  following ``resources/protocol/SOP与建设policy空模板.json`` with
  ``template_only: false`` and genuine verbatim source quotes;
- ``ledger/advanced_60_ledger.csv``  the advanced 60-rule construction ledger
  with the exact template headers, exactly 60 unique rows (R001..R060@3.0),
  blank unmeasured metrics (accuracy / machine_review_rate are NEVER
  fabricated);
- ``artifact_manifest.json``  exact public schema
  ``{"entries":[{rule_id, rule_version, artifact_version, path, sha256}]}``;
  every ``sha256`` is computed via vendored
  ``o2_public_contracts.artifact_hash.digest(...)`` and NO entry path points
  at the root ``.`` (which would self-reference the manifest).

After the manifest is written, the whole-directory root digest is computed
with ``artifact_hash.digest(BUILD_DIR)`` and recorded ONLY in the TaskStore
(never written back inside BUILD_DIR — that would invalidate the root hash).
The build directory contains no ``__pycache__``, ``.DS_Store``, SQLite DB or
runtime logs.
"""
from __future__ import annotations

import csv
import io
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from o2_public_contracts import artifact_hash
from o2_public_contracts.contracts import safe_relative

from o2_audit.build.capabilities import (
    build_capability_entries,
    transitive_dependencies,
)
from o2_audit.build.importer import ImportResult, RuleRecord, import_rules_jsonl
from o2_audit.protocol.jsonio import dump_json_strict
from o2_audit.rules.base import RulePlan, UnsupportedRuleError
from o2_audit.rules.b001.plan import B001Plan
from o2_audit.rules.registry import RuleRegistry, default_registry
from o2_audit.storage.store import TaskStore

ADVANCED_RULE_IDS = tuple(f"R{i:03d}" for i in range(1, 61))
LEDGER_HEADERS = [
    "rule_id", "rule_version", "build_status", "artifact_version",
    "artifact_sha256", "trace_path", "shared_capabilities", "build_seconds",
    "manual_edit_count", "accuracy", "machine_review_rate",
]
_FORBIDDEN_NAMES = {"__pycache__", ".DS_Store"}
_FORBIDDEN_SUFFIXES = (".pyc", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".log")


class BuildError(RuntimeError):
    """Raised when the build cannot produce a frozen artifact directory."""


@dataclass
class BuildResult:
    build_dir: Path
    artifact_version: str
    root_sha256: str
    manifest: dict[str, Any]
    built: list[tuple[str, str]]
    import_result: ImportResult
    capability_entries: dict[str, dict[str, Any]]
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "build_dir": str(self.build_dir),
            "artifact_version": self.artifact_version,
            "root_sha256": self.root_sha256,
            "built_rules": len(self.built),
            "import": self.import_result.summary(),
            "warnings": self.warnings,
        }


def rule_bundle_capability_deps(rule_id: str) -> tuple[str, ...]:
    """Full transitive capability dependency closure of one rule (its
    implementing capability, all transitively imported capabilities, and the
    runtime inspection loop)."""
    impl = _impl_capability(rule_id)
    return transitive_dependencies((impl, "inspect_material"))


def _impl_capability(rule_id: str) -> str:
    if rule_id == "B001":
        return "rules.b001"
    from o2_audit.build.capabilities import MODULE_TO_CAPABILITY
    from o2_audit.rules.advanced import FAMILY_BY_RULE

    if rule_id in FAMILY_BY_RULE:
        module_name = FAMILY_BY_RULE[rule_id].__name__
        cap = MODULE_TO_CAPABILITY.get(module_name)
        if cap is not None:
            return cap
    raise BuildError(f"unknown rule id {rule_id!r}: no capability module mapping")


def compile_build(
    rules_jsonl: str | Path,
    config: dict[str, Any],
    out_dir: str | Path,
    *,
    artifact_version: str,
    store: TaskStore | None = None,
    version_name: str | None = None,
    parent_name: str | None = None,
    overwrite: bool = False,
    registry: RuleRegistry | None = None,
    runtime_overrides: dict[str, str] | None = None,
) -> BuildResult:
    """Compile a frozen build directory; returns the build result including
    the whole-directory root SHA256 (recorded only in the store, if given).

    ``runtime_overrides`` is a TEST/SIMULATION hook that replaces the current
    source hash of a capability, modelling "this capability's code changed".
    """
    out = Path(out_dir)
    if out.resolve() == Path.cwd().resolve():  # basic sanity, never build into CWD
        raise BuildError("refusing to build into the current working directory")
    if out.exists() and any(out.iterdir()):
        if not overwrite:
            raise BuildError(f"build dir already exists and is not empty: {out}")
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    reg = registry if registry is not None else default_registry()
    imported = import_rules_jsonl(rules_jsonl)
    if not imported.valid:
        raise BuildError(
            f"no valid rule rows in {rules_jsonl}: "
            f"{len(imported.issues)} issue(s), first: "
            f"{imported.issues[0] if imported.issues else 'none'}")

    warnings: list[str] = [f"{len(imported.issues)} invalid row(s) skipped with per-record errors"
                           for _ in (0,) if imported.issues]
    warnings += [f"{len(imported.duplicates)} duplicate (rule_id, rule_version) row(s) ignored"
                 for _ in (0,) if imported.duplicates]

    # -- resolve buildable rules (per-record isolation for unsupported pairs)
    buildable: list[tuple[RuleRecord, RulePlan]] = []
    for record in imported.valid:
        try:
            plan = reg.get_plan(record.rule_id, record.rule_version)
        except UnsupportedRuleError:
            warnings.append(
                f"{record.rule_id}@{record.rule_version} (source line "
                f"{record.source_line}) has no runtime plan; recorded as not built")
            continue
        # The frozen bundle must carry the ACTUAL rule text of this source row.
        if plan.raw_rule_text and plan.raw_rule_text != record.rule_text:
            raise BuildError(
                f"{record.rule_id}@{record.rule_version}: source JSONL rule_text "
                f"differs from the registered plan text (line {record.source_line})")
        buildable.append((record, plan))
    if not buildable:
        raise BuildError("no rule row could be resolved to a runtime plan")

    capability_entries = build_capability_entries(runtime_overrides)
    artifact_version = str(artifact_version)
    if not artifact_version.strip():
        raise BuildError("artifact_version must be a non-empty string")

    # -- engine/rules/<id>_<ver>.json + engine/capabilities.json
    (out / "engine" / "rules").mkdir(parents=True, exist_ok=True)
    _write_json(out / "engine" / "capabilities.json", {
        "capability_schema": "o2-capability-table/1",
        "capabilities": capability_entries,
    })
    built: list[tuple[str, str]] = []
    for record, plan in sorted(buildable, key=lambda rp: (rp[0].rule_id, rp[0].rule_version)):
        deps = rule_bundle_capability_deps(record.rule_id)
        missing = [d for d in deps if d not in capability_entries]
        if missing:
            raise BuildError(f"{record.rule_id}: capability deps not in table: {missing}")
        bundle = {
            "bundle_schema": "o2-rule-bundle/1",
            "rule_id": record.rule_id,
            "rule_version": record.rule_version,
            "source_row_sha256": record.source_line_sha256,
            "source_file_sha256": record.source_file_sha256,
            "capability_deps": list(deps),
            "capability_hashes": {
                d: artifact_hash.digest_obj(capability_entries[d]) for d in deps
            },
            "plan": plan.model_dump(mode="json"),
        }
        _write_json(out / "engine" / "rules" / f"{record.rule_id}_{record.rule_version}.json",
                    bundle)
        built.append((record.rule_id, record.rule_version))

    # -- engine/sop/<id>_<ver>.json
    sop_dir = out / "engine" / "sop"
    sop_dir.mkdir(parents=True, exist_ok=True)
    for record, plan in buildable:
        _write_json(
            sop_dir / f"{record.rule_id}_{record.rule_version}.json",
            build_sop(record, plan, rule_bundle_capability_deps(record.rule_id),
                      artifact_version))

    # -- ledger/advanced_60_ledger.csv
    ledger_path = _write_ledger(out, built, artifact_version)

    # -- artifact_manifest.json (public schema; entries point at rule
    #    sub-artifacts, never at the root ".")
    entries = []
    for rid, ver in sorted(built):
        rel = f"engine/rules/{rid}_{ver}.json"
        if not safe_relative(rel) or rel == "artifact_manifest.json":
            raise BuildError(f"unsafe manifest path {rel!r}")
        entries.append({
            "rule_id": rid,
            "rule_version": ver,
            "artifact_version": artifact_version,
            "path": rel,
            "sha256": artifact_hash.path_digest(out / rel),
        })
    manifest = {"entries": entries}
    manifest_path = out / "artifact_manifest.json"
    manifest_path.write_bytes(
        dump_json_strict(manifest).encode("utf-8"))

    _assert_clean(out)

    # Whole-directory root digest AFTER the manifest exists. Recorded only in
    # the store — never written back inside BUILD_DIR.
    root_sha256 = artifact_hash.tree_digest(out)

    if store is not None:
        name = version_name or artifact_version
        for record, plan in buildable:
            store.register_rule(record.rule_id, record.rule_version,
                                plan.model_dump(mode="json"), record.source_line_sha256)
        store.create_run(f"build:{name}", "build", artifact_version, dict(config))
        parent = store.get_artifact_version(parent_name) if parent_name else None
        store.record_artifact_version(
            name=name, build_dir=str(out), artifact_version=artifact_version,
            root_sha256=root_sha256, parent_name=parent_name,
            parent_root_sha256=parent["root_sha256"] if parent else None)
        store.record_version_event(
            "build", name,
            {"artifact_version": artifact_version, "root_sha256": root_sha256,
             "built_rules": len(built), "ledger": str(ledger_path)})

    return BuildResult(
        build_dir=out, artifact_version=artifact_version, root_sha256=root_sha256,
        manifest=manifest, built=built, import_result=imported,
        capability_entries=capability_entries, warnings=warnings)


# ---------------------------------------------------------------------------
# SOP & build policy
# ---------------------------------------------------------------------------

_DECISION_ORDER = [
    "applicability（适用性；不可用即短路，不调用检查）",
    "explicit_exception_or_conflict（明确的例外/同级别冲突优先）",
    "proven_violation（已证实的违反 -> reject）",
    "consequential_unknown（影响结论的未知 -> ambiguous）",
    "satisfied（全部适用要求满足 -> pass）",
]


def build_sop(record: RuleRecord, plan: RulePlan, capability_deps: tuple[str, ...],
              artifact_version: str) -> dict[str, Any]:
    """Full SOP + build-policy JSON following the protocol template with
    ``template_only: false``; quotes are verbatim from the rule source."""
    quotes = [{"key": k, "quote": v} for k, v in sorted(plan.quotes.items())] or \
        [{"key": "rule_text", "quote": record.rule_text}]
    if isinstance(plan, B001Plan):
        applicability = {
            "market": plan.applicable_market,
            "category": plan.applicable_category,
            "power_mode": plan.applicable_power_mode,
            "small_tag_threshold_mm": str(plan.small_tag_threshold_mm),
        }
        normalization = [
            "型号经总则规定的型号归一化后与申报型号比对（归一化保留 O/0、I/1）",
            "材料编号应匹配 " + plan.material_number_pattern,
        ]
    else:
        applicability = {
            "rule_family_id": plan.rule_family_id,
            "material_roles": list(plan.material_roles),
            "metadata_precheck": "适用性由元数据级 precheck 判定；条件以规则原文为准",
        }
        normalization = [
            "字符串值按 NFKC 归一化并去首尾空白后比较；JSON 类型严格区分",
            "数值比较使用 Decimal，严格区分 JSON 整数与布尔",
        ]
    sop = {
        "applicability": applicability,
        "facts_to_check": [q["quote"] for q in quotes],
        "evidence_requirements": [
            "每条证据含 material_id、page_index 与非空 fact",
            "引用必须属于该审核单元的输入材料且源页码一致",
            "需要视觉判断的完成输出至少有一条合法图像引用",
            "bbox 须满足 0≤x1<x2≤1、0≤y1<y2≤1；不给 null/越界/反向框",
        ],
        "normalization": normalization,
        "decision_order": _DECISION_ORDER,
        "label_conditions": {
            "pass": [
                "适用性明确为 applicable 且全部适用要求满足",
                "明确不适用时 label=pass 且 reason_code=rule_not_applicable",
            ],
            "reject": ["存在已证实的违反项（proven violation）"],
            "ambiguous": [
                "适用性无法确定（列出缺失或非法条件）",
                "存在同级别冲突（conflict 优先于普通违反）",
                "关键证据不足（证据不足判 ambiguous；工具失败记 error，不是模糊）",
            ],
        },
        "clarifications": [note for note in [record.raw.get("material_note")] if note],
    }
    build_policy = {
        "atomic_capabilities": list(capability_deps),
        "reuse_decisions": [
            "共享 operators（text/numbers/dates/sets/geometry）与规则 ID 无关",
            "规则特定参数（阈值/模式/授权换算）只存在于版本化 plan 中",
        ],
        "data_requirements": [
            "任务须提供 audit_unit_id/sample_id/rule_id/rule_version/review_date/metadata/materials",
            "材料路径仅允许数据根下安全相对路径",
            "涉及的输入材料角色: " + (", ".join(record.material_roles) or "按规则原文确定"),
        ],
        "labeling_plan": {
            "decision_order": "见 sop.decision_order",
            "unknown_policy": "结论性未知判 ambiguous 并驱动 NeedEvidence 检查动作",
        },
        "model_and_tool_config": {
            "vision_model": "Stage 03 接入（Stage 02b 尚无视觉/OCR 实现）",
            "observation_gateway": "ObservationGateway 协议；观测不产出最终结论",
        },
        "evaluation_plugins": [],
        "quality_gates": {
            "manifest": "artifact_manifest.json 全部 sha256 通过 artifact_hash 校验",
            "capability_binding": "运行时能力模块哈希与冻结值一致，否则拒绝执行",
            "sop": "template_only=false，引用为规则原文",
        },
        "resource_budget": {},
        "promotion_conditions": [
            "激活前整个产物根目录 SHA256 与登记值一致",
            "候选不回写 baseline；基线保持只读",
        ],
        "rollback_conditions": [
            "激活目标目录摘要与登记不符时拒绝激活",
            "回滚恢复先前活动指针并再次校验其根目录 SHA256",
        ],
    }
    return {
        "template_only": False,
        "rule_id": record.rule_id,
        "rule_version": record.rule_version,
        "source_quotes": quotes,
        "sop": sop,
        "build_policy": build_policy,
        "artifact": {
            "version": artifact_version,
            "source_hash": record.source_line_sha256,
            "dependencies": list(capability_deps),
        },
    }


# ---------------------------------------------------------------------------
# ledger CSV
# ---------------------------------------------------------------------------

def _write_ledger(out: Path, built: list[tuple[str, str]],
                  artifact_version: str) -> Path:
    """Exactly 60 unique advanced rows (R001..R060@3.0): built rows carry the
    real artifact path/hash; the rest are explicit ``not_built`` rows.
    accuracy / machine_review_rate stay blank until actually evaluated."""
    ledger_dir = out / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    built_map = {(rid, ver): (rid, ver) for rid, ver in built}
    caps_by_rule = {rid: rule_bundle_capability_deps(rid)
                    for rid, _ in built if rid.startswith("R")}
    rows = []
    for rid in ADVANCED_RULE_IDS:
        key = (rid, "3.0")
        if key in built_map:
            trace = f"engine/rules/{rid}_3.0.json"
            rows.append([
                rid, "3.0", "built", artifact_version,
                artifact_hash.path_digest(out / trace), trace,
                ";".join(caps_by_rule[rid]), "", "0", "", "",
            ])
        else:
            rows.append([rid, "3.0", "not_built", "", "", "", "", "", "", "", ""])
    return _write_ledger_csv(ledger_dir / "advanced_60_ledger.csv", rows, LEDGER_HEADERS)


def _write_ledger_csv(path: Path, rows: list[list[Any]], headers: list[str]) -> Path:
    """Deterministic CSV writer with the exact template headers; enforces
    unique (rule_id, rule_version) rows."""
    keys = [(row[0], row[1]) for row in rows]
    if len(set(keys)) != len(keys):
        raise BuildError(f"duplicate (rule_id, rule_version) ledger rows: {keys}")
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(buffer.getvalue(), encoding="utf-8", newline="")
    return path


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dump_json_strict(obj).encode("utf-8"))


def _assert_clean(build_dir: Path) -> None:
    """Zero __pycache__/.DS_Store/SQLite/logs inside the frozen build dir."""
    for p in build_dir.rglob("*"):
        if p.name in _FORBIDDEN_NAMES or p.suffix in _FORBIDDEN_SUFFIXES:
            raise BuildError(f"forbidden artifact inside frozen build dir: {p}")
        if p.is_dir() and p.name == "__pycache__":
            raise BuildError(f"forbidden directory inside frozen build dir: {p}")


def plan_json_fingerprint(build_dir: str | Path, rule_id: str, rule_version: str) -> dict[str, Any]:
    """Parse one frozen rule bundle (strict JSON) without full verification."""
    path = Path(build_dir) / "engine" / "rules" / f"{rule_id}_{rule_version}.json"
    return json.loads(path.read_text(encoding="utf-8"))


__all__ = [
    "ADVANCED_RULE_IDS",
    "BuildError",
    "BuildResult",
    "LEDGER_HEADERS",
    "compile_build",
    "build_sop",
    "plan_json_fingerprint",
    "rule_bundle_capability_deps",
]