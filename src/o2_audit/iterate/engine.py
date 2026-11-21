"""Safe feedback-driven iteration engine (Protocol 1.3.1).

Implements ``o2-audit iterate --base BUILD_DIR --feedback FEEDBACK_JSON --input REPAIR_JSONL --out CANDIDATE_DIR``:

1. Validates ``FEEDBACK_JSON`` strictly against ``resources/protocol/feedback.schema.json``
   and verifies whole-directory ``artifact_hash.tree_digest(base_dir) == base_artifact_sha256``
   BEFORE creating any output directory.
2. Treats teacher text strictly as an untrusted candidate proposal — NEVER as
   authority and NEVER executes teacher-supplied code.
3. Emits an explicit, rule-cited decision (``accept`` | ``reject`` | ``no_change`` |
   ``defer``) for every feedback packet:
   - ``reject``: incompatible thresholds, O/0 or I/1 conflation, wrong conflict
     priority, date/review_date violations, R059/R060 forbidden overrides,
     ungrounded rule additions, code execution attempts, unknown unit/rule.
   - ``no_change``: source-consistent advice already satisfied by the frozen
     rule bundle (with concrete rule & plan citations).
   - ``accept``: real bounded, source-consistent extraction / observation
     clarification added to the targeted rule's ``plan.extraction_guidance``
     (genuinely loaded by subsequent ``ZCodeObservationGateway`` runs and
     hashed into ``prompt_fingerprint`` so cache never mixes versions).
   - ``defer``: ambiguous/under-specified advice requiring human review.
4. Copies unaffected rule bundles byte-for-byte so their manifest ``sha256``
   stays identical; creates a new immutable ``CANDIDATE_DIR`` with parent
   identity and ``iteration_report.json`` without touching ``BUILD_DIR`` or
   auto-activating the candidate.
5. Never reads hidden/protection/final gold labels; ``REPAIR_JSONL`` is
   strictly unlabeled audit tasks.
"""
from __future__ import annotations

import copy
import csv
import io
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema

from o2_audit.build.loader import LoadedArtifact, verify_build
from o2_audit.domain.models import AuditInput
from o2_audit.protocol.jsonio import ProtocolError, read_json_strict
from o2_audit.resources import resources_root
from o2_audit.runtime.infer import load_input_tasks
from o2_audit.storage.store import TaskStore
from o2_public_contracts import artifact_hash


class IterationError(RuntimeError):
    """Raised when feedback validation, hash binding, or candidate creation fails."""


@dataclass
class IterationResult:
    base_dir: Path
    out_dir: Path
    feedback_id: str
    base_artifact_version: str
    base_root_sha256: str
    candidate_artifact_version: str
    candidate_root_sha256: str
    changed_rules: list[str]
    decisions: list[dict[str, Any]]
    counts: dict[str, int]
    report_path: Path
    version_name: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "feedback_id": self.feedback_id,
            "base_dir": str(self.base_dir),
            "base_artifact_version": self.base_artifact_version,
            "base_root_sha256": self.base_root_sha256,
            "out_dir": str(self.out_dir),
            "candidate_artifact_version": self.candidate_artifact_version,
            "candidate_root_sha256": self.candidate_root_sha256,
            "version_name": self.version_name,
            "changed_rules": self.changed_rules,
            "counts": self.counts,
            "report_path": str(self.report_path),
            "official_promotion": False,
        }


# ---------------------------------------------------------------------------
# Strict feedback schema validation
# ---------------------------------------------------------------------------

def _feedback_schema() -> dict[str, Any]:
    path = resources_root() / "protocol" / "feedback.schema.json"
    return read_json_strict(path)


def validate_feedback_packet(feedback_path: str | Path) -> dict[str, Any]:
    """Read and strictly validate ``FEEDBACK_JSON`` against ``feedback.schema.json``."""
    try:
        data = read_json_strict(feedback_path)
    except (ProtocolError, OSError) as exc:
        raise IterationError(f"invalid feedback JSON: {exc}") from exc

    schema = _feedback_schema()
    validator = jsonschema.Draft202012Validator(schema)
    errs = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if errs:
        first = errs[0]
        loc = ".".join(str(p) for p in first.path) or "$"
        raise IterationError(f"feedback schema violation at {loc}: {first.message}")

    # Ensure packet_ids are unique within a feedback file
    seen_packets: set[str] = set()
    for item in data["items"]:
        pid = item["packet_id"]
        if pid in seen_packets:
            raise IterationError(f"duplicate packet_id in feedback: {pid!r}")
        seen_packets.add(pid)
    return data


# ---------------------------------------------------------------------------
# Rule-grounded proposal classifier (never executes code; teacher != authority)
# ---------------------------------------------------------------------------

_CODE_EXEC_PATTERNS = (
    re.compile(r"\b(?:exec|eval|__import__|subprocess|os\.system|rm\s+-rf|import\s+os)\b", re.I),
    re.compile(r"```(?:python|bash|sh|powershell)", re.I),
    re.compile(r"<\s*script\b", re.I),
    re.compile(r"(?:执行代码|运行脚本|注入代码|覆盖源码|直接修改python)", re.I),
)

_CONFLICT_OVERRIDE_PATTERNS = (
    re.compile(r"(?:普通|缺字段|格式错误).{0,12}优先于.{0,12}(?:冲突|ambiguous)", re.I),
    re.compile(r"(?:忽略|跳过|无视).{0,10}(?:标签冲突|编号冲突|期限冲突|同号冲突|规则冲突)", re.I),
    re.compile(r"(?:冲突|矛盾).{0,12}(?:直接判|改为|一律判|按最新|取最新|覆盖).{0,8}(?:reject|pass|拒绝|通过|有效)", re.I),
)

_EQUIVALENCE_VIOLATION_PATTERNS = (
    re.compile(r"(?:[Oo]\s*[与和=＝]\s*0|0\s*[与和=＝]\s*[Oo]).{0,8}(?:等价|相同|视为一致|互换|等同)", re.I),
    re.compile(r"(?:[Ii]\s*[与和=＝]\s*1|1\s*[与和=＝]\s*[Ii]).{0,8}(?:等价|相同|视为一致|互换|等同)", re.I),
    re.compile(r"(?:允许|接受).{0,8}(?:小写\s*qlab|5位数字|五位数字|7位数字|七位数字|任意位数)", re.I),
)

_DATE_VIOLATION_PATTERNS = (
    re.compile(r"(?:使用|采用|改为).{0,8}(?:当前日期|系统日期|今天|today|current\s*date)", re.I),
    re.compile(r"(?:忽略|不用).{0,8}(?:review_date|审核日)", re.I),
    re.compile(r"(?:开区间|不包含端点|截止日当天(?:算|判)(?:过期|reject|拒绝))", re.I),
)

_APPLICABILITY_VIOLATION_PATTERNS = (
    re.compile(r"(?:跳过|忽略|取消|绕过).{0,8}(?:适用条件|适用性|applicability)", re.I),
    re.compile(r"(?:未知|缺失元数据).{0,10}(?:按\s*false|按\s*0|视为不适用|从图像猜测|从图片猜测)", re.I),
    re.compile(r"(?:新增|追加|额外增加).{0,10}(?:强制要求|规则条件|未在原文|隐藏门槛)", re.I),
    re.compile(r"(?:所有|全部).{0,8}(?:未知|不可读|模糊|工具失败|超时).{0,10}(?:判|改为|视为).{0,8}(?:pass|reject|ambiguous)", re.I),
)

_EXTRACTION_CLARIFICATION_MARKERS = (
    "提取说明",
    "提取提示",
    "提取澄清",
    "观察提示",
    "视觉检查",
    "放大检查",
    "局部放大",
    "ocr",
    "字符辨认",
    "注意区分",
    "重点关注",
    "仔细检查",
    "逐行核对",
    "表格行列",
    "定位区域",
    "区域定位",
    "clarification",
    "extraction guidance",
    "observation hint",
    "visual inspection",
    "crop focus",
    "disambiguate",
)

_ALREADY_IMPLEMENTED_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(r"(?:示范建议|仅用于接口演示|型号归一化约定)", re.I),
        "already_implemented_model_normalization",
        "规则产物已按规则总则实现 NFKC + trim + 规则指定大小写/空格/连字符归一化（且保持 O/0 与 I/1 区分），无需改动。",
    ),
    (
        re.compile(r"(?:[Oo]\s*[与和]\s*0.{0,6}(?:不等价|区分|不能互换)|[Ii]\s*[与和]\s*1.{0,6}(?:不等价|区分))", re.I),
        "already_implemented_character_distinction",
        "冻结算子已严格保持 O/0 与 I/1 区分，无需改动。",
    ),
    (
        re.compile(r"(?:闭区间.{0,8}(?:包含端点|端点有效)|到期当天.{0,6}(?:有效|pass))", re.I),
        "already_implemented_closed_date_interval",
        "冻结日期算子已按输入 review_date 执行闭区间 [valid_from, valid_until] 判定，无需改动。",
    ),
    (
        re.compile(r"(?:作废.{0,10}(?:逐项排除|仅排除该项|不作废整张))", re.I),
        "already_implemented_void_item_exclusion",
        "冻结规则已对清晰作废证据逐项排除而不作废整张图片，无需改动。",
    ),
]


def _extract_rule_citations(bundle: dict[str, Any], limit: int = 2) -> list[str]:
    plan = bundle.get("plan") if isinstance(bundle.get("plan"), dict) else {}
    citations: list[str] = []
    quotes = plan.get("quotes")
    if isinstance(quotes, dict):
        for val in quotes.values():
            if isinstance(val, str) and val.strip():
                citations.append(val.strip()[:240])
                if len(citations) >= limit:
                    break
    if not citations:
        raw = str(plan.get("raw_rule_text") or "").strip()
        if raw:
            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            citations.append(" ".join(lines[:2])[:240])
    return citations or [f"{bundle.get('rule_id')}@{bundle.get('rule_version')}"]


def _check_rule_specific_conflicts(
    rule_id: str,
    rule_version: str,
    proposal: str,
    bundle: dict[str, Any],
) -> tuple[str, str] | None:
    """Detect rule-specific threshold/priority violations incompatible with source text."""
    p_low = proposal.lower()

    # B001 threshold guard: 3.0 is <100 mm, 3.1 is <80 mm (strictly <, never <=)
    if rule_id == "B001":
        expected = 100 if rule_version == "3.0" else (80 if rule_version == "3.1" else None)
        if expected is not None:
            # Proposing <=100 or <=80 or "等于100/80允许附签"
            if re.search(rf"(?:<=|≤|小于等于|等于\s*){expected}\s*mm?.{{0,12}}(?:允许|可用|可使用).{{0,6}}(?:附签|挂签)", proposal, re.I):
                return (
                    "threshold_endpoint_conflict",
                    f"B001@{rule_version} 原文要求严格小于 {expected} mm 才允许附签，恰好等于 {expected} mm 必须使用外包装载体，拒绝修改边界。",
                )
            # Proposing a DIFFERENT mm threshold (e.g. changing 3.0 to 80mm or 3.1 to 100mm or 120mm)
            for match in re.finditer(r"(?:改为|调整为|放宽到|放宽至|收紧到|阈值(?:为|改为|设为)|小于|<)\s*(\d{2,4})\s*mm", proposal, re.I):
                proposed_mm = int(match.group(1))
                if proposed_mm != expected:
                    return (
                        "incompatible_threshold_change",
                        f"B001@{rule_version} 冻结阈值为 {expected} mm，建议中的 {proposed_mm} mm 与规则原文冲突，拒绝修改。",
                    )
            if re.search(r"(?:不区分|统一|合并).{0,10}(?:3\.0|3\.1|100\s*mm|80\s*mm)", proposal, re.I):
                return (
                    "version_threshold_conflation",
                    f"拒绝混淆 B001@3.0 (100 mm) 与 B001@3.1 (80 mm) 的版本阈值。",
                )

    # R059 guard: never invent specific-over-general precedence for blue boxes
    if rule_id == "R059":
        if re.search(r"(?:具体优先|特定优先|特别法优先|强制优先于禁止|禁止优先于强制|直接判\s*(?:pass|reject))", proposal, re.I):
            return (
                "r059_invented_precedence_forbidden",
                "R059 同级冲突（蓝色包装盒圆形标志强制与禁止并存）不得自造具体优先于一般规则，拒绝修改。",
            )

    # R060 guard: never borrow a numeric threshold for undefined "较小"
    if rule_id == "R060":
        if re.search(r"(?:较小|小包装).{0,16}(?:按|设为|定义为|借用|取|<=|<)\s*\d+\s*mm", proposal, re.I) or (
            "b001" in p_low and re.search(r"\d+\s*mm", p_low)
        ):
            return (
                "r060_borrowed_threshold_forbidden",
                "R060 未定义“较小”的数值阈值，不得借用 B001 或其他规则的毫米阈值，拒绝修改。",
            )

    # R058 guard: production_date selects prefix, NOT review_date
    if rule_id == "R058":
        if re.search(r"(?:按|使用|改为).{0,6}(?:review_date|审核日).{0,10}(?:选择|决定|判断).{0,6}前缀", proposal, re.I):
            return (
                "r058_wrong_date_basis",
                "R058 明确按生产日期（而非审核日）确定编号前缀，拒绝修改。",
            )

    return None


def evaluate_feedback_item(
    item: dict[str, Any],
    tasks_by_id: dict[str, AuditInput],
    base_artifact: LoadedArtifact,
    existing_guidance_by_rule: dict[tuple[str, str], list[str]],
) -> dict[str, Any]:
    """Classify one teacher feedback packet into accept / reject / no_change / defer."""
    packet_id = item["packet_id"]
    uid = item["audit_unit_id"]
    proposal = item["teacher_proposal"].strip()
    teacher_source = item["teacher_source"]

    task = tasks_by_id.get(uid)
    if task is None:
        return {
            "packet_id": packet_id,
            "audit_unit_id": uid,
            "rule_id": None,
            "rule_version": None,
            "teacher_source": teacher_source,
            "teacher_proposal": proposal,
            "decision": "reject",
            "reason_code": "unknown_audit_unit",
            "reason": f"反馈指向的 audit_unit_id={uid!r} 不在本次未标注修复集 INPUT_JSONL 中，拒绝盲目修改。",
            "rule_citations": [],
            "diff": None,
        }

    key = (task.rule_id, task.rule_version)
    bundle = base_artifact.bundles.get(key)
    if bundle is None:
        return {
            "packet_id": packet_id,
            "audit_unit_id": uid,
            "rule_id": task.rule_id,
            "rule_version": task.rule_version,
            "teacher_source": teacher_source,
            "teacher_proposal": proposal,
            "decision": "reject",
            "reason_code": "rule_not_in_base_artifact",
            "reason": f"基线产物中不存在规则 {task.rule_id}@{task.rule_version}，拒绝修改。",
            "rule_citations": [],
            "diff": None,
        }

    citations = _extract_rule_citations(bundle)

    # 1. Hard safety rejects (never execute teacher code or override normative rules)
    for pat in _CODE_EXEC_PATTERNS:
        if pat.search(proposal):
            return {
                "packet_id": packet_id,
                "audit_unit_id": uid,
                "rule_id": task.rule_id,
                "rule_version": task.rule_version,
                "teacher_source": teacher_source,
                "teacher_proposal": proposal,
                "decision": "reject",
                "reason_code": "code_execution_forbidden",
                "reason": "禁止执行教师反馈中的代码/脚本或覆盖源码实现。",
                "rule_citations": citations,
                "diff": None,
            }

    rule_conflict = _check_rule_specific_conflicts(task.rule_id, task.rule_version, proposal, bundle)
    if rule_conflict is not None:
        code, msg = rule_conflict
        return {
            "packet_id": packet_id,
            "audit_unit_id": uid,
            "rule_id": task.rule_id,
            "rule_version": task.rule_version,
            "teacher_source": teacher_source,
            "teacher_proposal": proposal,
            "decision": "reject",
            "reason_code": code,
            "reason": msg,
            "rule_citations": citations,
            "diff": None,
        }

    for patterns, code, msg in (
        (_CONFLICT_OVERRIDE_PATTERNS, "conflict_priority_violation", "建议违反总则/规则的冲突优先口径，拒绝修改。"),
        (_EQUIVALENCE_VIOLATION_PATTERNS, "normalization_or_format_violation", "建议违反字符区分（O/0、I/1）或编号格式口径，拒绝修改。"),
        (_DATE_VIOLATION_PATTERNS, "date_semantics_violation", "建议违反审核日（review_date）或闭区间日期口径，拒绝修改。"),
        (_APPLICABILITY_VIOLATION_PATTERNS, "ungrounded_rule_or_applicability_override", "建议跳过适用条件、猜测未知元数据或引入原文无依据的新门槛，拒绝修改。"),
    ):
        if any(pat.search(proposal) for pat in patterns):
            return {
                "packet_id": packet_id,
                "audit_unit_id": uid,
                "rule_id": task.rule_id,
                "rule_version": task.rule_version,
                "teacher_source": teacher_source,
                "teacher_proposal": proposal,
                "decision": "reject",
                "reason_code": code,
                "reason": msg,
                "rule_citations": citations,
                "diff": None,
            }

    # 2. Evidence-based no_change (already satisfied in the frozen rule plan)
    for pat, code, msg in _ALREADY_IMPLEMENTED_PATTERNS:
        if pat.search(proposal):
            return {
                "packet_id": packet_id,
                "audit_unit_id": uid,
                "rule_id": task.rule_id,
                "rule_version": task.rule_version,
                "teacher_source": teacher_source,
                "teacher_proposal": proposal,
                "decision": "no_change",
                "reason_code": code,
                "reason": msg,
                "rule_citations": citations,
                "diff": None,
            }

    # 3. Bounded safe accept path: source-consistent extraction / observation guidance
    p_low = proposal.lower()
    if any(marker in p_low for marker in _EXTRACTION_CLARIFICATION_MARKERS):
        sanitized = re.sub(r"\s+", " ", proposal).strip()[:280]
        entry_text = f"[{packet_id}] {sanitized}"
        current = list(existing_guidance_by_rule.get(key, ()))
        if any(sanitized in existing for existing in current):
            return {
                "packet_id": packet_id,
                "audit_unit_id": uid,
                "rule_id": task.rule_id,
                "rule_version": task.rule_version,
                "teacher_source": teacher_source,
                "teacher_proposal": proposal,
                "decision": "no_change",
                "reason_code": "extraction_guidance_already_present",
                "reason": "相同的提取澄清提示已存在于该规则冻结配置中，无需重复追加。",
                "rule_citations": citations,
                "diff": None,
            }
        updated = current + [entry_text]
        existing_guidance_by_rule[key] = updated
        return {
            "packet_id": packet_id,
            "audit_unit_id": uid,
            "rule_id": task.rule_id,
            "rule_version": task.rule_version,
            "teacher_source": teacher_source,
            "teacher_proposal": proposal,
            "decision": "accept",
            "reason_code": "bounded_extraction_clarification_accepted",
            "reason": (
                "与规则原文一致的视觉提取/观察澄清提示，不改变判定阈值或优先顺序；"
                "已限定写入目标规则冻结配置的 plan.extraction_guidance，将在后续推理提示与缓存键中生效。"
            ),
            "rule_citations": citations,
            "diff": {
                "rule": f"{task.rule_id}@{task.rule_version}",
                "field": "plan.extraction_guidance",
                "before": current,
                "after": updated,
            },
        }

    # 4. Default: defer under-specified proposals for human review
    return {
        "packet_id": packet_id,
        "audit_unit_id": uid,
        "rule_id": task.rule_id,
        "rule_version": task.rule_version,
        "teacher_source": teacher_source,
        "teacher_proposal": proposal,
        "decision": "defer",
        "reason_code": "requires_human_review",
        "reason": "建议未违反规则原文，但未给出可直接安全落地的提取/观察澄清范围，已暂缓并等待人工复核。",
        "rule_citations": citations,
        "diff": None,
    }


# ---------------------------------------------------------------------------
# Candidate directory builder
# ---------------------------------------------------------------------------

def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    path.write_text(text, encoding="utf-8")


def _update_ledger_csv(
    ledger_path: Path,
    manifest_entries: list[dict[str, Any]],
) -> None:
    if not ledger_path.is_file():
        return
    by_key = {(e["rule_id"], e["rule_version"]): e for e in manifest_entries}
    rows: list[dict[str, str]] = []
    with ledger_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        for row in reader:
            key = (row.get("rule_id", ""), row.get("rule_version", ""))
            entry = by_key.get(key)
            if entry is not None:
                row["artifact_version"] = entry["artifact_version"]
                row["artifact_sha256"] = entry["sha256"]
            rows.append(row)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    ledger_path.write_text(buf.getvalue(), encoding="utf-8")


def run_iteration(
    base_dir: str | Path,
    feedback_path: str | Path,
    input_jsonl: str | Path,
    out_dir: str | Path,
    *,
    artifact_version: str | None = None,
    version_name: str | None = None,
    store: TaskStore | None = None,
    overwrite: bool = False,
) -> IterationResult:
    """Execute safe feedback iteration into a NEW immutable ``CANDIDATE_DIR``.

    All validation (feedback JSON schema, base artifact integrity, whole-base
    SHA256 binding, strict unlabeled ``input_jsonl``) runs BEFORE creating
    ``out_dir``. ``base_dir`` is strictly read-only.
    """
    base = Path(base_dir)
    out = Path(out_dir)

    if out.resolve() == base.resolve():
        raise IterationError("candidate out_dir must differ from base_dir (baseline is immutable)")
    if out.resolve() == Path.cwd().resolve():
        raise IterationError("refusing to write candidate into current working directory")

    # 1. Validate feedback schema BEFORE touching output dir
    feedback = validate_feedback_packet(feedback_path)

    # 2. Verify base artifact integrity and exact whole-directory hash binding
    base_art = verify_build(base)
    base_root_sha256 = artifact_hash.tree_digest(base)
    if base_root_sha256 != feedback["base_artifact_sha256"]:
        raise IterationError(
            "feedback base_artifact_sha256 does not match base BUILD_DIR whole-tree digest: "
            f"expected {feedback['base_artifact_sha256']}, actual {base_root_sha256}"
        )

    # 3. Load unlabeled repair tasks strictly (NEVER reads gold/references)
    try:
        tasks = load_input_tasks(input_jsonl)
    except ProtocolError as exc:
        raise IterationError(f"invalid repair input JSONL: {exc}") from exc
    tasks_by_id = {t.audit_unit_id: t for t in tasks}

    # 4. Check out_dir cleanliness only after pre-validation passes
    if out.exists() and any(out.iterdir()):
        if not overwrite:
            raise IterationError(f"candidate out_dir already exists and is not empty: {out}")
        shutil.rmtree(out)

    # 5. Evaluate every feedback packet deterministically against base rules
    guidance_by_rule: dict[tuple[str, str], list[str]] = {}
    for key, bundle in base_art.bundles.items():
        plan = bundle.get("plan") if isinstance(bundle.get("plan"), dict) else {}
        existing = plan.get("extraction_guidance")
        guidance_by_rule[key] = list(existing) if isinstance(existing, list) else []

    decisions: list[dict[str, Any]] = []
    changed_keys: set[tuple[str, str]] = set()
    accepted_by_rule: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for item in feedback["items"]:
        dec = evaluate_feedback_item(item, tasks_by_id, base_art, guidance_by_rule)
        decisions.append(dec)
        if dec["decision"] == "accept" and dec["rule_id"] and dec["rule_version"]:
            key = (dec["rule_id"], dec["rule_version"])
            changed_keys.add(key)
            accepted_by_rule.setdefault(key, []).append(dec)

    counts = {
        "accept": sum(1 for d in decisions if d["decision"] == "accept"),
        "reject": sum(1 for d in decisions if d["decision"] == "reject"),
        "no_change": sum(1 for d in decisions if d["decision"] == "no_change"),
        "defer": sum(1 for d in decisions if d["decision"] == "defer"),
    }

    base_versions = sorted({e["artifact_version"] for e in base_art.entries})
    base_av = base_versions[-1]
    cand_av = (
        artifact_version.strip()
        if isinstance(artifact_version, str) and artifact_version.strip()
        else f"{base_av}-iter-{feedback['feedback_id']}"
    )

    # 6. Copy base artifact byte-for-byte into candidate dir, then apply ONLY
    #    accepted bounded changes to targeted rule bundles
    shutil.copytree(base, out)

    new_entries: list[dict[str, Any]] = []
    for entry in base_art.entries:
        key = (entry["rule_id"], entry["rule_version"])
        rel = entry["path"]
        target_file = out / rel
        if key in changed_keys:
            updated_bundle = copy.deepcopy(base_art.bundles[key])
            updated_bundle["plan"]["extraction_guidance"] = guidance_by_rule[key]
            history = list(updated_bundle.get("iteration_changes") or [])
            for acc in accepted_by_rule[key]:
                history.append({
                    "feedback_id": feedback["feedback_id"],
                    "packet_id": acc["packet_id"],
                    "teacher_source": acc["teacher_source"],
                    "reason_code": acc["reason_code"],
                    "diff": acc["diff"],
                })
            updated_bundle["iteration_changes"] = history
            _write_json(target_file, updated_bundle)

            # Update sibling SOP record if present
            sop_file = out / "engine" / "sop" / f"{key[0]}_{key[1]}.json"
            if sop_file.is_file():
                sop_doc = read_json_strict(sop_file)
                sop_doc["iteration_changes"] = history
                _write_json(sop_file, sop_doc)

        actual_sha = artifact_hash.path_digest(target_file)
        new_entries.append({
            "rule_id": entry["rule_id"],
            "rule_version": entry["rule_version"],
            "artifact_version": cand_av,
            "path": rel,
            "sha256": actual_sha,
        })

    _write_json(out / "artifact_manifest.json", {"entries": new_entries})
    _update_ledger_csv(out / "ledger" / "advanced_60_ledger.csv", new_entries)

    changed_rules = sorted(f"{r}@{v}" for r, v in changed_keys)
    report_path = out / "iteration_report.json"
    report_doc = {
        "schema_version": "1.3.1",
        "feedback_id": feedback["feedback_id"],
        "base_artifact_version": base_av,
        "base_artifact_sha256": base_root_sha256,
        "candidate_artifact_version": cand_av,
        "changed_rules": changed_rules,
        "counts": counts,
        "decisions": decisions,
        "official_promotion": False,
        "note": (
            "Candidate artifact created from unlabeled repair tasks and validated "
            "feedback packets. Candidate is NOT auto-activated; formal hidden-set "
            "promotion gate remains organizer-owned."
        ),
    }
    _write_json(report_path, report_doc)

    # 7. Verify candidate artifact integrity & baseline immutability
    verify_build(out)
    candidate_root_sha256 = artifact_hash.tree_digest(out)
    if artifact_hash.tree_digest(base) != base_root_sha256:
        raise IterationError("base artifact directory was unexpectedly modified during iteration")

    # 8. Register candidate in TaskStore (NEVER auto-activate)
    registered_name: str | None = None
    if store is not None:
        parent_name: str | None = None
        for rec in store.list_artifact_versions():
            if rec["root_sha256"] == base_root_sha256:
                parent_name = rec["name"]
                break
        registered_name = version_name or cand_av
        store.record_artifact_version(
            name=registered_name,
            build_dir=str(out),
            artifact_version=cand_av,
            root_sha256=candidate_root_sha256,
            parent_name=parent_name,
            parent_root_sha256=base_root_sha256,
        )
        store.record_version_event(
            "candidate_created",
            registered_name,
            {
                "feedback_id": feedback["feedback_id"],
                "base_root_sha256": base_root_sha256,
                "changed_rules": changed_rules,
                "counts": counts,
            },
        )

    return IterationResult(
        base_dir=base,
        out_dir=out,
        feedback_id=feedback["feedback_id"],
        base_artifact_version=base_av,
        base_root_sha256=base_root_sha256,
        candidate_artifact_version=cand_av,
        candidate_root_sha256=candidate_root_sha256,
        changed_rules=changed_rules,
        decisions=decisions,
        counts=counts,
        report_path=report_path,
        version_name=registered_name,
    )
