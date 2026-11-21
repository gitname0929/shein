# Stage 02a: all 62 executable rules (B001@3.0/3.1 + R001..R060@3.0) and observation vocabulary

Read `AGENTS.md`, `docs/supervisor_acceptance.md`, `resources/rules/规则总则.md`, `resources/rules/规则口径补充说明.md`, `resources/rules/进阶规则60条.jsonl`, `resources/rules/基础规则版本.jsonl`, and existing `src/o2_audit/` modules. Extend existing Stage 01 interfaces without replacing the four-layer architecture. Do NOT depend on the parent folder; read only repo-local `resources/`. Do NOT modify `TODO.md`, `AGENTS.md`, `IMPLEMENTATION_STATUS.md`, `tools/run_glm_stage.py`, or the 7 vendored files in `src/o2_public_contracts/`. No commits, no push, no nested agents.

Efficiency note: batch independent Read / Write / Edit tool calls in the same turn whenever possible. Group the 60 advanced rules cleanly into the 10 official families (`F01`..`F10`, 6 rules each) so the codebase stays modular, crisp, and completes well within budget.

## Deliverables

1. **Unified rule plan & outcome interface (`src/o2_audit/rules/base.py` & `registry.py`)**:
   - Define `RuleOutcome` (compatible with existing `B001Outcome`, which can subclass or alias it) and `RulePlan` base (with `rule_id`, `rule_version`, `rule_family_id`, `title`, `raw_rule_text`, `quotes`, `required_predicates`, `material_roles`, `capability_deps`).
   - Keep `B001Plan` (`plan_3_0`, `plan_3_1`) and `evaluate_b001` fully backward-compatible so all 96 existing Stage 01 tests continue to pass untouched.
   - Implement `RuleRegistry` loading all **62** `(rule_id, rule_version)` pairs (`B001@3.0`, `B001@3.1`, `R001@3.0`..`R060@3.0`) backed by actual typed plans populated with raw rule text and citations from `resources/rules/`.
   - **No silent fallback**: requesting an unknown `(rule_id, rule_version)` must raise `KeyError` / `UnsupportedRuleError` (never default to `pass`).

2. **Structured observation vocabulary (`src/o2_audit/domain/vocabulary.py` & `docs/observation_vocabulary.md`)**:
   - Define and document the common structured observation predicates & payload conventions required across all 62 rules so Stage 03's visual gateway produces facts directly consumable by all rule evaluators without rule-ID branching in the gateway:
     - text/field observations (raw transcription vs normalized value, status, role, entity_id, container/face/label scope, bbox),
     - object/boundary/face coverage & obstruction (`boundary_visible`, `interior_obstructed`, `visible_faces`, `printed_page_number`, `total_printed_pages`, `material_authenticity`, `supplement_field_identity`, `same_object_match`),
     - visual marks & geometry (`shape_type`, `is_filled`, `is_closed`, `color_mode`, `mark_color`, `mark_count`, `mark_bbox`, `label_bbox`, `package_bbox`, `line_crosses_field`, `view_type`, `relative_position`, `height_mm`, `width_mm`, `area_ratio`),
     - warning/instructions (`verbatim_text`, `step_list`, `language_text`),
     - numeric/unit pairs (`measured_value`, `measured_unit`, `declared_value`, `declared_unit`),
     - documents/tables/certificates (`void_marked`, `issue_date`, `withdrawal_date`, `valid_from`, `valid_until`, `covered_models`, `component_model_map`, `model_spec_map`, `contact_info`, `document_number`, `alternative_sets`).
   - Preserve provenance (`source.material_id`, `source.page_index`, `source.bbox`), entity separation (distinct labels/pages/units), and distinct statuses (`readable`, `confirmed_missing` requiring `scope_complete=True`, `unreadable`, `incomplete_scope`, `identity_unknown`).

3. **Shared rule-ID-independent operators (`src/o2_audit/operators/`)**:
   - Extend shared operators ONLY with generic, rule-ID-independent primitives (no hardcoded `R037` or `B001` IDs inside `operators/`):
     - `numbers.py`: exact unit-conversion helper taking an explicit conversion table / factor from the rule plan (`convert_exact(value, from_unit, to_unit, factors)`), exact tolerance check (`within_tolerance(actual, expected, abs_tol=None, rel_tol=None)`), closed/open numeric interval checks.
     - `text.py`: exact verbatim text match with allowed line-break normalization (for R031), ordered step-sequence match (for R033), exact case-sensitive English match (for R035).
     - `geometry.py`: bbox containment, relative position (`above`/`below`/`left`/`right`/`adjacent`), edge/height/area ratios (exact Decimal or bounded float per rule plan).
     - `sets.py`: keyed dictionary/matrix correspondence check (for R050, R052), unique-item check (for R009), one-to-one mapping check (for R011).

4. **Deterministic evaluators for all 60 advanced rules (`src/o2_audit/rules/advanced/`)**:
   - Organize cleanly into 10 family modules:
     - `f01_evidence.py` (R001–R006)
     - `f02_identity.py` (R007–R012)
     - `f03_fields.py` (R013–R018)
     - `f04_placement.py` (R019–R024)
     - `f05_visual.py` (R025–R030)
     - `f06_warnings.py` (R031–R036)
     - `f07_numeric.py` (R037–R042)
     - `f08_documents.py` (R043–R048)
     - `f09_cross_material.py` (R049–R054)
     - `f10_boundary.py` (R055–R060)
   - Every single rule MUST read its exact wording from `resources/rules/进阶规则60条.jsonl` + `规则口径补充说明.md` + `规则总则.md` and implement its actual applicability, special conflict/exception priority, proven violations, consequential unknowns, and satisfied branch.
   - Specifically verify ALL supervisor acceptance gates from `docs/supervisor_acceptance.md`:
     - **R001**: boundary visibility vs interior obstruction;
     - **R002**: declared faces metadata vs visible faces;
     - **R003**: printed document page numbers (NOT source `page_index`);
     - **R005**: supplement must match same business field (NOT decorative/archive ID); all views clearly obstructed rejects vs uncertain supplement identity ambiguous;
     - **R006**: design/render-only rejects vs unknown source authenticity ambiguous;
     - **R009**: independently sold units require unique serials;
     - **R010**: main vs compatible model distinction;
     - **R011**: one-to-one replacement mapping;
     - **R019/R020**: location-only checks do NOT require readable field values when mark/field identity & location are clear;
     - **R021**: same-label co-location (never merge separate labels);
     - **R023**: lines crossing target fields (not blank margin);
     - **R024/R030**: specified standard view and authorized equality endpoints;
     - **R025/R026**: real shape/fill/closure properties;
     - **R028**: grayscale photo cannot prove color -> ambiguous;
     - **R029**: count unvoided stars only from complete scope;
     - **R031**: exact text allowing linebreaks;
     - **R033**: exact steps and order;
     - **R035**: exact English case;
     - **R037–R042**: ONLY rule-authorized unit conversions and rule-specific tolerances;
     - **R046**: clear void always rejects (cannot be used to satisfy);
     - **R048**: withdrawal date earlier than issue date -> ambiguous; otherwise review on or after withdrawal date -> reject;
     - **R050**: keyed component/model pairs (including matrices), NOT independent set equality;
     - **R052**: keyed model-to-spec association;
     - **R054**: active same-number documents with differing contact info -> ambiguous;
     - **R056**: invalid quantity applicability semantics per exact rule text;
     - **R057**: one complete alternative (never cherry-pick parts of two incomplete alternatives);
     - **R058**: production date chooses prefix branch (NOT review date);
     - **R059**: same-level normative conflict for blue-box circular mark -> ambiguous, while preserving valid non-conflict branches (e.g. non-blue box or applicability-out);
     - **R060**: "较小" undefined -> ambiguous when branch depends on undefined small-size threshold (do NOT borrow B001's 100/80 mm!), while preserving unaffected branches (e.g. clearly compliant external-package placement).

5. **Controller generalization (`src/o2_audit/runtime/controller.py`)**:
   - Update `AuditController` so it accepts any registered `RulePlan` (via `RuleRegistry` or direct plan + evaluator) while preserving full backward compatibility for `AuditController(plan_3_0(), gateway, ...)`.
   - Applicability short-circuit works generically before invoking the gateway; `NeedEvidence` gap loop works for both B001 and advanced rules.

6. **Comprehensive pytest suite**:
   - `tests/test_advanced_rules.py`:
     - Verify all 62 `(rule_id, rule_version)` pairs are registered, carry non-empty raw rule text/citations from `resources/rules/`, and unknown IDs/versions raise errors.
     - Semantic tests for **every single one of R001..R060**: at least one genuine `pass`, one genuine `reject`, and one `ambiguous` (or `not_applicable` / rule-specific conflict branch where applicable) verifying actual reason codes and decision semantics — NO weak smoke assertions that only check "returns a label".
     - Dedicated targeted tests for every single named edge case above (R001, R002, R003, R005, R006, R009, R010, R011, R019, R020, R021, R023, R024, R025, R026, R028, R029, R030, R031, R033, R035, R037–R042, R046, R048, R050, R052, R054, R056, R057, R058, R059 unaffected + conflict, R060 unaffected + undefined threshold).
     - Controller end-to-end tests running multiple advanced rules through `AuditController` with `StaticObservationGateway` and validating outputs with `prediction_semantic_errors`.

## Verification

Run:
- `uv run pytest -v`

Fix all test/import/typing errors. Report exact files added/changed, exact pytest pass/skip/fail counts, and any honest limitations (Stage 02b will add `o2-audit build`, manifest hashing, and version registry; Stage 03 will add real vision/OCR). Stop after this stage.
