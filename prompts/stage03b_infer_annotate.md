# Stage 03b: inference & annotation pipelines, CLI (`infer`/`annotate`) & live ZCode visual smoke

Read `AGENTS.md`, `docs/supervisor_acceptance.md`, `resources/protocol/执行与评分协议.md`, `resources/protocol/标注任务与样例.md`, `resources/protocol/annotation.schema.json`, `resources/protocol/annotation_task.schema.json`, and the completed Stage 01–03a code under `src/o2_audit/`. Extend existing interfaces; do NOT modify `TODO.md`, `AGENTS.md`, `IMPLEMENTATION_STATUS.md`, `tools/run_glm_stage.py`, or the 7 vendored files in `src/o2_public_contracts/`. No commits, no push, no nested agents.

Efficiency note: batch independent Read / Edit / Write calls in the same turn whenever possible.

## Deliverables

1. **Batch inference runner (`src/o2_audit/runtime/infer.py`)**:
   - Load frozen `BUILD_DIR` read-only via `verify_build` (manifest + capability hash binding; never write inside `BUILD_DIR`).
   - Read `INPUT_JSONL` strictly via `o2_public_contracts.contracts.read_rows` (enforcing duplicate keys, NaN/Infinity, 64 MiB / 512 KiB / 100k record limits) and parse each row into `AuditInput`.
   - For each unit, run `AuditController` bound to the frozen artifact + `ZCodeObservationGateway` (or an explicitly injected test-only `ObservationGateway` in tests; NEVER default to a fake offline gateway!).
   - Persist execution state/observations/traces/usage/timing into workspace SQLite & trace JSONL OUTSIDE `BUILD_DIR` (resumable without producing duplicate final rows).
   - Output `PREDICTIONS_JSONL` with:
     - Exact 1 output row per input `audit_unit_id` (unique, preserving order, never dropping failed tasks — tool/network/missing-config failures emit `execution_status="error"`, `label=None`).
     - Exact loaded `artifact_version` stamped on every row.
     - Validated against `o2_public_contracts.contracts.prediction_errors` and `o2_audit.protocol.checks.prediction_semantic_errors`.
   - Never read any reference/gold file in production inference code.

2. **Target-directed annotation runner (`src/o2_audit/runtime/annotate.py`)**:
   - Read `INPUT_JSONL` (`AuditInput`) and `TARGETS_JSONL` (`annotation_task.schema.json`: `annotation_id`, `audit_unit_id`, `material_id`, `page_index`, `target`, `value_type`, `normalization`, `bbox_format`).
   - Run the SAME inference pipeline on the referenced `audit_unit_id`s (deduplicated per unit so multiple targets on one unit share one inference run) to obtain the whole-unit rule `label` (`pass` / `reject` / `ambiguous`) — NEVER equate `label` to whether the single text field is valid!
   - Run target-directed extraction via `gateway.annotate_target(...)` on the exact `(material_id, page_index)`:
     - Return verbatim text normalized ONLY with `annotation_nfkc_strip` (Unicode NFKC + `.strip()`), preserving internal case, punctuation, and spaces (NEVER apply `model_normalize` to annotation `value`!).
     - `status`: `"value"` (readable & valid for this target), `"unreadable"` (`value` MUST be `None`), or `"invalid_value"` (readable target itself violates format/value convention; another field's violation does NOT turn this target into `invalid_value`).
     - `bbox`: normalized `[x1, y1, x2, y2]` (`0 <= x1 < x2 <= 1`, `0 <= y1 < y2 <= 1`, finite non-bool floats) covering the target line/object or its occluded region.
     - Exact 8 fields of `annotation.schema.json`, preserving `annotation_id`, `audit_unit_id`, `material_id`, `page_index` verbatim.
   - If inference fails (`execution_status="error"` / `label is None`) OR target extraction fails, write structured error details to a sibling `<out>.errors.jsonl` file and do NOT fabricate `unreadable` or fake answers in `ANNOTATIONS_JSONL`.
   - Validate emitted rows with `o2_public_contracts.contracts.annotation_errors`.

3. **CLI integration (`src/o2_audit/cli.py`)**:
   - Wire exact official protocol 1.3.1 commands:
     - `o2-audit infer --input INPUT_JSONL --artifacts BUILD_DIR --out PREDICTIONS_JSONL` (optional `--data-root`, `--model-config`, `--workspace`, `--run-id`; default `--data-root` inferred from `INPUT_JSONL` parent dir containing `assets/`).
     - `o2-audit annotate --input INPUT_JSONL --targets TARGETS_JSONL --artifacts BUILD_DIR --out ANNOTATIONS_JSONL` (optional `--data-root`, `--model-config`, `--workspace`, `--run-id`, `--errors-out`).
   - Keep `iterate` and `evaluate` reserved with explicit non-zero exit (`not yet implemented in Stage 03b`).

4. **Tests (`tests/test_infer_and_annotate.py`)**:
   - End-to-end `infer` CLI & API tests with injected test-only gateway / mocked HTTP transport:
     - Full protocol validation (`prediction_errors == []`, `prediction_semantic_errors == []`).
     - Missing model config -> `execution_status="error"`, `label=None`, row retained in denominator.
     - Resume after partial run -> zero duplicate `audit_unit_id` rows, completed units not re-called.
     - Tampered frozen `BUILD_DIR` -> refused cleanly with non-zero exit.
     - Frozen `BUILD_DIR` digest byte-identical before and after `infer` / `annotate` (zero writes inside artifacts).
   - End-to-end `annotate` CLI & API tests:
     - Raw NFKC+strip vs model normalization preserved (`"型号：ＡＢ-１"` -> `"型号:AB-1"`, internal case/hyphen preserved, `"AB-1"` never lowercased to `"ab1"`).
     - Whole-unit `label` comes from unit rule evaluation even when the annotated target itself has `status="value"`.
     - `unreadable` enforces `value=None` and valid occlusion `bbox`.
     - `invalid_value` for bad material number while valid model target on another field stays `status="value"`.
     - Tool/inference failure written to `.errors.jsonl` and never fabricated as `unreadable`.
     - Validated with `o2_public_contracts.contracts.annotation_errors == []`.

5. **Real live ZCode visual smoke test (under `workspace/smoke_stage03b/`)**:
   - Build a frozen artifact into `workspace/smoke_stage03b/build_v1`.
   - Run `o2-audit infer` using the real configured ZCode endpoint (`zcode/GLM-5.3-Flash`, credentials inherited from environment, NEVER logged or saved) on 3–4 varied PUBLIC dev tasks from `resources/public_dev/公开开发集/inputs/` (excluding gold from inference!).
   - Run `o2-audit annotate` using the same real ZCode endpoint on the 3 public annotation sample targets (`resources/public_dev/公开开发集/标注样例/inputs/tasks.jsonl` + `targets.jsonl`).
   - Separately run the official vendored `resources/protocol/校验工具/validate_submission.py` and `validate_annotations.py` against the outputs to inspect honest schema legality and quality metrics; fix any principled prompt/parser defects discovered (NEVER ID-specific workarounds!).
   - Update `README.md` with factual `infer` / `annotate` CLI usage, ZCode environment variable setup (no secrets), workspace/trace layout, and honest remaining limitations.

## Verification

Run:
- `uv run pytest tests/test_infer_and_annotate.py -v`
- `uv run pytest -q`
- Real ZCode smoke commands and official validators (`validate_submission.py`, `validate_annotations.py`) on `workspace/smoke_stage03b/`.

Report exact files added/changed, exact commands run, exact pytest counts, model used, real API call count, exercised public tasks/targets, and honest validation/quality outcomes. Stop after this stage.
