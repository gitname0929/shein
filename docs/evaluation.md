# Evaluation Plugins, Evidence Semantics, and Version Comparison

This document describes the isolated evaluation subsystem (`src/o2_audit/evaluation/`), corresponding to Protocol `1.3.1` and acceptance criteria `L-FUNC-01..16` and `L-STRUCT-01..04`.

## 1. Isolation & Label Boundary

- Reference / gold labels (`references`) are **only** loaded inside `o2_audit.evaluation` and the `o2-audit evaluate` CLI adapter (and evaluation unit tests).
- Production `build`, `infer`, `annotate`, `rules`, `model_gateway`, and `runtime` modules never import `o2_audit.evaluation` or read reference files.
- Each enabled plugin executes sequentially in an independent subprocess (`python -B -m o2_audit.evaluation.worker`) with no shell interpolation:
  - Inputs (`inputs`, `outputs`, `references`, `context`) are transferred as serialized read-only JSON copies over stdin.
  - The worker hashes canonical JSON snapshots before and after invoking `evaluate(inputs, outputs, references, context)` and aborts with `plugin_mutated_inputs` if any input object was mutated.
  - Per-plugin timeouts (`--timeout`, `PLUGIN_TIMEOUT_SECONDS`, or manifest `timeout_seconds`) terminate stalled plugins with `plugin_timeout`.
  - Strict finite JSON validation rejects `NaN` / `Infinity` and schema-invalid reports with `plugin_invalid_output`.
  - Disabled plugins (`enabled: false`) are never spawned (`invoked: false`, `status: "disabled"`).
  - One plugin's crash, timeout, mutation, or invalid output is isolated as a structured report in `errors` and never modifies predictions or blocks remaining plugins.

> **Honest isolation boundary**: Local subprocess execution enforces process separation, per-plugin timeout, and read-only input mutation detection. It does **not** claim OS-kernel or network container sandboxing in local development mode.

## 2. Registered Plugins & CLI Usage

Default manifest: `src/o2_audit/evaluation/plugins_config.json`.

| Plugin ID | Aliases | Entry | Purpose |
| --- | --- | --- | --- |
| `o2.conclusion` | `conclusion` | `o2_audit.evaluation.conclusion:evaluate` | Joint `(label, applicability)` conclusion metrics (`A`, `U`, `Q`, `M`, frozen denominator, per-rule/version groups, confusion matrix, optional contrasts) |
| `o2.evidence` | `evidence` | `o2_audit.evaluation.evidence:evaluate` | Separates reference structure validation from deterministic semantic content grounding |
| `o2.version_compare` | `version_compare`, `compare` | `o2_audit.evaluation.compare:evaluate` | Rule/version-aligned baseline vs candidate comparison |

CLI entrypoint (exact Protocol 1.3.1 flags):

```bash
o2-audit evaluate \
  --plugin conclusion \
  --inputs INPUT_JSONL \
  --outputs PREDICTIONS_JSONL \
  --references REFERENCES_JSONL \
  --out REPORT_JSON
```

Direct `module:function` entries (e.g. `--plugin my_pkg.my_plugin:evaluate`) are also supported without modifying main pipeline code.

## 3. Conclusion Quality Plugin (`o2.conclusion`)

- Delegates core scoring directly to vendored `o2_public_contracts.quality.metric` (and `quality.contrasts` **only** when `context["contrast_groups"]` is explicitly provided).
- Preserves the frozen denominator: missing outputs, duplicate outputs, execution errors, invalid labels, wrong rule versions, wrong `artifact_version`, invalid bboxes, and boolean `page_index` remain counted in `all`.
- Extraneous units are reported in `extraneous` without inflating `all`.
- Empty reference sets emit `null` for all core metrics (`accuracy`, `machine_review_rate`, `machine_accuracy`, `quality`, etc.) — never `0` or `1`.
- All-ambiguous truth sets satisfy `U == A`.
- Never runs full 60-rule `quality_parts` on partial public dev subsets.

## 4. Evidence Quality Plugin (`o2.evidence`)

Strictly separates **reference structure validity** from **semantic content grounding**:

1. **Reference structure checks** (`structure_valid`, `structure_issues`):
   - Runs unit-level `o2_public_contracts.contracts.prediction_errors`.
   - Distinguishes `material_not_found` (missing or foreign `material_id`), `page_index_invalid` (bool / non-int / `< 1`), `wrong_source_page`, `bbox_invalid` (non-finite / bool / out-of-bounds / unordered; never silently fixed), `fact_empty`, and `duplicate_evidence`.
2. **Semantic content checks** (`grounded: true | false | null`):
   - Deterministically checks claims against structured reference `visible_facts` (and `cut_coords`) when provided in `references`:
     - Exact/normalized field text & key-value matching (models, `QLAB-` codes, ISO dates, Chinese/English field aliases).
     - Contradicted field values (`value_contradicted`).
     - Fabricated values on regions marked `readable: false` (`unreadable_fact_fabricated`).
     - Contradicted absence claims when the field is readable or scope is cropped (`absence_claim_contradicted`, `absence_claimed_on_incomplete_scope`, `unreadable_conflated_with_missing`).
     - Unreadable / incomplete-scope consistency (`unreadable_claim_contradicted`, `incomplete_scope_contradicted`).
     - Region alignment (`bbox_region_mismatch` when `iou < bbox_iou_min`, default `0.3`).
     - Wrong source page/material (`wrong_source_page_or_material`).
     - Claims absent from a non-empty reference `visible_facts` list (`claim_not_in_reference_visible_facts`).
   - Supports an optional pluggable `VisualEvidenceVerifier` protocol (`context["visual_verifier"]`) for visual crop re-verification.
   - **Honest limitation**: When neither reference `visible_facts` nor a `VisualEvidenceVerifier` is available, `grounded` is explicitly `null` (`semantic_status: "unverifiable"`). The plugin **never** fabricates `grounded: true` merely because a material file or `material_id` exists.

## 5. Version Comparison & Protection Gate (`o2.version_compare`)

- Aligns units strictly by `(audit_unit_id, rule_id, rule_version)`; different rule versions are never mixed.
- **With references**: computes `repaired_ids`, `new_error_ids`, `unchanged_correct_ids`, `unchanged_error_ids`, and `new_execution_failure_ids` from `quality.metric` joint-correct sets.
- **Without references**: runs in `prediction_diff_only` mode, reporting only `changed_prediction_ids`, `unchanged_prediction_ids`, and `error_delta` (`repaired_ids` and `new_error_ids` are `null`).
- `evaluate_protection_gate(...)` delegates directly to `o2_public_contracts.quality.gate(...)` for explicit local/public evaluation vectors and always tags `official: false` (`scope: "local_evaluation_only"`). Formal hidden-set promotion approval remains organizer-owned.
