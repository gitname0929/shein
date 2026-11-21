# Stage 01 (finish & review fixes): foundation and vertical prototype

You are the ZCode GLM implementation worker. An earlier run created the initial files for Stage 01 (`pyproject.toml`, `uv.lock`, `resources/`, `src/o2_public_contracts/`, `src/o2_audit/domain/models.py`, `src/o2_audit/operators/*`, `src/o2_audit/protocol/*`, `src/o2_audit/storage/store.py`, `src/o2_audit/runtime/facts.py`, `src/o2_audit/rules/b001/{plan,evaluate}.py`) before hitting an API rate limit.

Read `AGENTS.md`, `prompts/stage01_foundation.md`, and the existing files under `src/o2_audit/`. Complete Stage 01 cleanly and fix every supervisor-identified defect listed below. Do NOT modify `TODO.md`, `AGENTS.md`, `IMPLEMENTATION_STATUS.md`, `tools/run_glm_stage.py`, or any files in `src/o2_public_contracts/` (those vendored files are already byte-identical and verified). Do NOT commit, push, or start nested agents.

Efficiency note: batch independent Read / Edit / Write calls in the same turn whenever possible to keep API round-trips low and avoid gateway rate limits.

## 1. Fix supervisor-identified defects in existing files

1. `src/o2_audit/domain/models.py`:
   - `Metadata`: fix flat-dict deserialization so `AuditInput.model_validate({"metadata": {"market": "Q", ...}, ...})` populates `metadata.values` properly (via a `model_validator(mode="before")`) instead of putting keys into `model_extra`, while also accepting `Metadata(values={...})`.
   - `ObservationSource`: add `@field_validator("bbox", mode="before")` using `validate_bbox_tuple` (when not `None`) so invalid/bool/unordered bboxes are rejected on observations as well as `Evidence`.
   - `Observation`: add a `model_validator(mode="after")` enforcing that `status == ObservationStatus.CONFIRMED_MISSING` requires `scope_complete is True` (raise `ValueError` otherwise).
   - Ensure `page_index >= 0` is enforced where required by protocol (`MaterialRef`, `ObservationSource`, `Evidence`).
2. `src/o2_audit/operators/text.py`, `src/o2_audit/operators/numbers.py` & `src/o2_audit/rules/b001/plan.py`:
   - `model_normalize`: per 规则总则 §3 ("忽略拉丁字母大小写、半角空格和半角连字符"), lowercase ASCII Latin letters `A-Z` only (do NOT use `.casefold()`, which alters non-ASCII characters like `ß`).
   - Remove the hardcoded B001 regex `^QLAB-[0-9]{6}$` from shared `operators/text.py`; replace it with a generic `matches_pattern(value: str, pattern: str) -> bool` (NFKC + strip + `re.fullmatch`). Move `material_number_pattern: str = r"^QLAB-[0-9]{6}$"` onto `B001Plan` so shared operators remain rule-ID independent.
   - `operators/numbers.py`: `decimal_of(value)` must also accept `Decimal` instances (rejecting non-finite `Decimal` and `bool`), because `evaluate_b001` passes `Decimal` values to `number_strictly_less`.
   - `runtime/facts.py`: in `consolidate(claims)`, when `status == READABLE` and `normalized_value is None` but `raw_value is not None`, fall back to `raw_value`; when readable claims disagree, still preserve the first conflicting observation (`readable[0]`) instead of dropping source evidence to `None`.
3. `src/o2_audit/storage/store.py`:
   - `activate_artifact(name)`: verify the target `name` exists BEFORE clearing `is_active=1` (or raise `KeyError` inside the transaction when `cur.rowcount != 1` so `ROLLBACK` preserves the previously active pointer).
   - `observations_for(audit_unit_id, run_id: str | None = None)`: include `run_id` in `_observation_digest` and support filtering by `run_id` so separate runs (e.g., baseline vs candidate) never collide or leak observations.
4. `src/o2_audit/rules/b001/evaluate.py`:
   - Fix `NameError` at line 328: `task_roles` -> `task_material_roles`.
   - Fix **inverted** `label_number_conflict` logic in `_conflict_results`: two active (non-void) labels with DIFFERENT material numbers MUST trigger `RequirementStatus.CONFLICT`; two active labels with the SAME material number must NOT trigger a conflict.
   - Fix voided-only label and voided-only certificate handling: per B001 item 5 ("清楚标注‘作废’的旧标签或旧文件不作为有效证据，也不能单独用于满足要求"), when all observed labels are clearly voided (and no non-void label exists in complete scope), or all observed certificates are clearly voided (and no non-void certificate exists), that is a **VIOLATION (reject)**, NEVER a `pass` or bare `unknown`.
   - Multi-label / multi-certificate accuracy:
     - Conflicts are always checked first across ALL non-void labels (different numbers) and ALL non-void certificates (same number, different validity periods) and override ordinary violations.
     - Same-label requirement: a label entity must hold BOTH `model` and `material_number` on the SAME label entity; fields split across two separate label entities must NOT be merged to pass.
     - Carrier check: if at least one active label satisfies both item 1 and item 2 (e.g., an `outer_package` label on a >=100mm package), do not falsely reject solely because another non-conflicting tag exists, while rejecting when the only active label is an `attached_tag` on >=100mm (3.0) or >=80mm (3.1).
     - Remove the dead `if/else` around `cert_void_unknowns`.
     - Populate concise original `rule_quote` strings on `Evidence` items using quotes stored on `B001Plan`.
     - Use `matches_pattern(str(n_value), plan.material_number_pattern)` from `operators.text`.

## 2. Complete remaining Stage 01 deliverables

1. Add clean package `__init__.py` files (`src/o2_audit/__init__.py`, `domain/`, `rules/`, `rules/b001/`, `runtime/`, `storage/`) and `src/o2_audit/resources.py` exposing repo-local paths (`rules_dir()`, `protocol_dir()`, `public_dev_dir()`, `student_spec_path()`) independent of the parent folder. Add `resources/README.md` documenting provenance (read-only copies from competition package, public dev references must never be loaded by build/infer/annotate/iterate).
2. Implement the minimal four-layer vertical controller:
   - `src/o2_audit/runtime/gateway.py`: `ObservationGateway` Protocol returning typed `ObservationBatch(entities, observations)` ONLY (never final labels/compliance flags) and raising `ExecutionError` on tool/network failure. Provide a test-only `StaticObservationGateway` helper (explicitly documented as test-only).
   - `src/o2_audit/runtime/controller.py`: `AuditController` implementing the four-layer cycle:
     - Semantic layer: `FactView` + `B001Plan` + `TaskStore`.
     - Intent/planning layer: checks applicability from metadata first (short-circuits before calling gateway if `not_applicable` or `unknown`), then inspects `NeedEvidence` gaps and emits permitted `AllowedAction` requests to the gateway with a strict max-round bound.
     - Constraint/control layer: calls `evaluate_b001`, detects remaining actionable evidence gaps (`NeedEvidence`) vs terminal verdict, enforces decision order and protocol-1.3.1 output validation via `prediction_semantic_errors`.
     - Execution layer: invokes `ObservationGateway`, records immutable observations & step checkpoints in `TaskStore` so re-running a completed step recovers without duplicating gateway calls or final records, and converts `ExecutionError` into a protocol-legal error record (`execution_status="error"`, `label=None`, `error_code=...`).
3. Add meaningful pytest suite under `tests/`:
   - `tests/test_protocol_and_contracts.py`: strict JSON/JSONL (duplicate keys, NaN/Infinity, bool-as-int/page, float-as-page, invalid bbox), safe material path (reject `..`, absolute, backslash, drive `:`, symlink), vendored `o2_public_contracts` verified against `resources/protocol/hash_fixture` + `hash_fixture_expected.json`.
   - `tests/test_operators.py`: `model_normalize` (NFKC, trim, ASCII Latin case, ASCII space/hyphen, O vs 0, I vs 1, non-ASCII `ß` preserved), `annotation_nfkc_strip` vs `model_normalize`, `material_number_equal`, `matches_pattern`, `parse_date` (endpoints, invalid dates, bool rejected), `decimal_of` / `number_strictly_less` (exact 100/80 thresholds, bool/NaN rejected), `list_contains_model`, `bbox_ok` & `iou`.
   - `tests/test_store.py`: transactions, rule registration idempotency, task idempotency, step checkpoint recovery without duplicate execution, attempt history, run-isolated immutable observations, `activate_artifact` rollback on missing target.
   - `tests/test_b001_and_controller.py`: synthetic fact graphs & injected test-only gateway covering:
     - 90 mm attached-tag allowed in B001 3.0 -> `pass`, rejected in B001 3.1 -> `reject` (with exact boundary tests at 100 mm for 3.0 and 80 mm for 3.1);
     - attached-tag without proven attachment -> `ambiguous`, confirmed missing attachment -> `reject`;
     - same-label prohibition (model on label A, material_number on label B -> `reject`);
     - voided-only label / voided-only certificate -> `reject`;
     - known-false applicability short-circuit (`market="X"` with `category=None` -> `not_applicable`, `pass`, `rule_not_applicable`, zero gateway calls);
     - unknown applicability -> `unknown`, `ambiguous`, zero gateway calls;
     - missing (complete scope) -> `reject` vs unreadable -> `ambiguous` vs `ExecutionError` -> `execution_status="error", label=None`;
     - conflict priority overriding ordinary mismatch (two active labels with different numbers + model mismatch -> `ambiguous`; two active same-number certs with different validity periods + expired/mismatch -> `ambiguous`; two active labels with SAME number -> NOT conflict);
     - certificate date endpoints (exact `valid_from` and exact `valid_until` -> `pass`; one day before/after -> `reject`; reversed dates -> `reject`);
     - `NeedEvidence` multi-round loop (round 1 returns partial observations -> controller emits `NeedEvidence` -> round 2 returns certificate -> `pass`);
     - checkpoint recovery/idempotency (second run reuses completed step without invoking gateway again);
     - every produced output passes `prediction_semantic_errors`.
4. Add `README.md` (setup with `uv sync --python 3.12`, test command `uv run pytest`, resource layout, clear Stage 01 limitations — do NOT claim vision/OCR/60-rule/CLI completion yet), `.env.example` (environment variable names only, no secrets), and `docs/architecture.md` (four-layer architecture mapped from OntoAgent conceptually, explicitly noting clean-room challenge implementation and pinned references in `docs/upstream_references.md`).

## 3. Verification

Run:
- `uv sync --python 3.12`
- `uv run pytest -v`

Fix any real test or typing/import failures. Report exact files changed/added, exact commands run, exact pytest pass/fail counts, and remaining Stage 01 limitations honestly. Stop after this stage.
