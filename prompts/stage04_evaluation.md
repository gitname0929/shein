# Stage 04: isolated evaluation plugins, evidence semantics, version comparison & `o2-audit evaluate` CLI

Read `AGENTS.md`, `docs/supervisor_acceptance.md`, `resources/protocol/插件接口与运行约定.md`, `resources/protocol/插件输出契约.json`, `resources/protocol/执行与评分协议.md`, `resources/protocol/验收项目与评分细则.md`, `src/o2_public_contracts/quality.py`, `src/o2_public_contracts/contracts.py`, and `src/o2_audit/cli.py`. Extend existing interfaces; do NOT modify `TODO.md`, `AGENTS.md`, `IMPLEMENTATION_STATUS.md`, `tools/run_glm_stage.py`, or the 7 vendored files in `src/o2_public_contracts/`. No commits, no push, no nested agents.

Efficiency note: batch independent Read / Edit / Write calls in the same turn whenever possible.

## Isolation boundary

Reference/gold labels may ONLY be loaded inside `src/o2_audit/evaluation/**`, `o2-audit evaluate`, and evaluation tests (`tests/test_evaluation.py`). NEVER import or reference evaluation/gold code from `build`, `infer`, `annotate`, `rules`, `model_gateway`, or `runtime`.

## Deliverables

1. **Plugin manifest, contract & subprocess runner (`src/o2_audit/evaluation/runner.py`, `src/o2_audit/evaluation/manifest.py`, `src/o2_audit/evaluation/plugins_config.json`)**:
   - Standard Python signature: `evaluate(inputs, outputs, references, context) -> dict`, where `context` carries at least `schema_version="1.3.1"` and `purpose`.
   - Declarative plugin manifest mapping registered plugin IDs (`conclusion`, `evidence`, `version_compare`) AND direct `module:function` entry specs to callable targets, with `enabled: bool`, `version`, `description`, `timeout_seconds` (configurable via `PLUGIN_TIMEOUT_SECONDS` env or CLI `--timeout`).
   - Independent subprocess execution (`python -m o2_audit.evaluation.worker`, no shell interpolation, read-only serialized JSON copies via stdin):
     - Detects read-only input mutation inside the worker and fails cleanly with structured error (`plugin_mutated_inputs`).
     - Enforces per-plugin timeout (`plugin_timeout`).
     - Rejects non-finite JSON (`NaN`/`Infinity`) and schema-invalid reports (`plugin_invalid_output`).
     - Required report fields: `plugin_id`, `plugin_version`, `input_schema_version`, `metrics`, `diagnostics`, `errors`.
     - Disabled plugin is NEVER invoked (returns explicit `disabled` status/record in batch runner).
     - Batch runner (`run_plugins`) runs enabled plugins sequentially/independently: one plugin's crash/timeout/invalid output NEVER modifies predictions/inputs or blocks other plugins.
     - Be honest in code/docs: subprocess isolation provides process isolation, timeout, and input-mutation detection — do NOT claim full OS/network container sandbox locally.

2. **Conclusion quality plugin (`src/o2_audit/evaluation/conclusion.py`)**:
   - Faithfully delegates core A / U / Q / M / joint-correct / frozen-denominator accounting to vendored `o2_public_contracts.quality.metric(references, outputs, inputs, expected_artifact_version=...)` — NEVER duplicate scoring inconsistently!
   - Preserves frozen denominator: duplicates, invalid rows, missing rows, extra rows, execution errors, wrong `artifact_version`, invalid bbox, bool `page_index`, foreign material refs are all faithfully reflected.
   - Empty reference groups emit `null` metrics (never `1.0` / full score).
   - All-ambiguous truth groups satisfy `U == A`.
   - Provides per-`(rule_id, rule_version)` grouped metrics, confusion matrix diagnostics (`truth_label -> pred_label`), applicability diagnostics, and optional contrast-group metrics ONLY when `contrast_groups` are explicitly supplied in `context`. Do NOT run full 60-rule `quality_parts` on public dev data lacking 50 rules.

3. **Evidence quality plugin (`src/o2_audit/evaluation/evidence.py`)**:
   - Clearly separates:
     a. **Reference structure validity** (delegating to `o2_public_contracts.contracts.prediction_errors`, bbox order/bounds/finiteness, strict int `page_index`, unit material ownership, duplicate evidence detection);
     b. **Actual semantic content support** (checking evidence claims against structured reference `visible_facts` / expected field facts when available in `references` or `context`, plus an explicit pluggable `VisualEvidenceVerifier` protocol hook for optional visual re-verification).
   - Explicitly detects:
     - unsupported claims (`grounded=False`, e.g., claiming a field value/date/number contradicted by reference visible facts or claiming `confirmed_missing` when reference shows the field is present or scope is incomplete),
     - contradictory bbox/page citations (e.g., citing a page/region inconsistent with reference evidence bbox/page),
     - unverifiable claims when neither reference visible facts nor a visual verifier is available -> returns `grounded=None` with explicit `unverifiable_reason` (NEVER fabricates `grounded=True` merely because a `material_id` exists!).
   - Distinguishes clear field absence vs unreadable vs scope-incomplete vs wrong source page.
   - Documents exact deterministic claim forms checked and honest limitations in `docs/evaluation.md`.

4. **Version comparison & candidate protection gate (`src/o2_audit/evaluation/compare.py`)**:
   - Aligns strictly by `(audit_unit_id, rule_id, rule_version)` — never mixes different rules/versions.
   - With reference truth: computes `repaired_ids`, `new_error_ids`, `unchanged_correct_ids`, `unchanged_error_ids` using official `o2_public_contracts.quality.metric` joint-correct unit sets (`label` + `applicability` both correct and legal).
   - Without reference truth: reports ONLY `changed_predictions` / `unchanged_predictions` / `error_delta` (NEVER calls an unverified prediction change a "repaired error"!).
   - Exposes `evaluate_protection_gate(...)` delegating directly to `o2_public_contracts.quality.gate(...)` over the four required evaluation vectors (baseline/candidate × repair/protected): all four complete & legal, zero new protected joint errors, repair accuracy non-decreasing, zero new errors/invalid/missing. Clearly notes that formal hidden-set gate approval remains organizer-owned.

5. **CLI integration (`src/o2_audit/cli.py`)**:
   - Wire exact official protocol 1.3.1 command:
     `o2-audit evaluate --plugin PLUGIN_ID --inputs INPUT_JSONL --outputs PREDICTIONS_JSONL --references REFERENCES_JSONL --out REPORT_JSON`
     (optional `--timeout`, `--purpose`, `--expected-artifact-version`, `--config`; `--plugin` accepts registered IDs `conclusion` / `evidence` / `version_compare` OR `module:function`; `--references` optional only for reference-free diagnostic modes where explicit `null` metrics are reported).
   - Keep `iterate` reserved with explicit non-zero exit (`not yet implemented in Stage 04`).
   - Update `tests/test_build_and_lifecycle.py` so `evaluate` is no longer in the reserved-only test loop (only `iterate` remains reserved).

6. **Documentation (`docs/evaluation.md`) & Comprehensive Tests (`tests/test_evaluation.py`)**:
   - Cover every locally feasible L-FUNC-01..16 and L-STRUCT-01..04 acceptance criterion:
     - Frozen denominator, duplicates, wrong `artifact_version`, execution errors, missing/extra units, empty truth (`null` metrics), all-ambiguous truth (`U == A`).
     - Invalid bbox, bool `page_index`, foreign material citation.
     - Evidence semantics: structurally valid + semantically supported (`grounded=True`), structurally valid + semantically contradicted (`grounded=False`), structurally valid + no ground-truth facts (`grounded=None`, never fake `True`), structurally invalid.
     - Subprocess runner isolation: disabled plugin not invoked, crashing plugin isolated, timed-out plugin isolated, input-mutating plugin caught, non-finite JSON output rejected, one failed plugin does not block others.
     - Version comparison with truth (`repaired` / `new_error` / `unchanged`) and without truth (`changed_predictions` only), plus `evaluate_protection_gate` pass/fail cases (new protected error -> blocked, repair regression -> blocked, new execution error -> blocked, clean improvement -> pass).
     - Live CLI `o2-audit evaluate` smoke tests on the public dev dataset (`resources/public_dev/公开开发集/inputs/audit_inputs.jsonl`, `reference_predictions.jsonl`, `references/audit_gold.jsonl`) verifying exact protocol JSON output!

## Verification

Run:
- `uv run pytest tests/test_evaluation.py -v`
- `uv run pytest -q`
- Live `o2-audit evaluate --plugin conclusion ...` and `o2-audit evaluate --plugin evidence ...` against public dev `reference_predictions.jsonl` and `references/audit_gold.jsonl` writing into `workspace/smoke_stage04/`.

Report exact files added/changed, exact commands run, exact pytest counts, and honest public evaluation report metrics. Stop after this stage.
