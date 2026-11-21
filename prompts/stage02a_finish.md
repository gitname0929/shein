# Stage 02a Finish: supervisor review fixes, controller generalization, vocabulary docs & 62-rule semantic tests

Read `AGENTS.md`, `docs/supervisor_acceptance.md`, and the Stage 02a code already written under `src/o2_audit/`. Do NOT rewrite files from scratch — apply targeted fixes to the existing Stage 02a files, generalize `AuditController`, write `docs/observation_vocabulary.md`, and add `tests/test_advanced_rules.py`. Do NOT modify `TODO.md`, `AGENTS.md`, `IMPLEMENTATION_STATUS.md`, `tools/run_glm_stage.py`, or the 7 vendored files in `src/o2_public_contracts/`. No commits, no push, no nested agents.

Efficiency note: batch independent Read / Edit / Write calls in the same turn so this stage finishes quickly and cleanly.

## 1. Mandatory supervisor review fixes (all 10 defects must be fixed)

1. **`src/o2_audit/rules/advanced/f03_fields.py`** (`NameError`):
   - `APPLICABILITY` currently references `_applicability_r018` on line 48 before `def _applicability_r018` is defined on line 52. Move `APPLICABILITY` below `_applicability_r018`.

2. **`src/o2_audit/operators/sets.py`** (`dict_correspondence` bug):
   - Currently checks `set(a.keys()) != set(b.keys())` on raw unnormalized keys and calls `key(str(k), str(k))`. Fix it so keys are matched using `key` (or NFKC+strip canonicalization when `key is key_match`, and general pairwise matching with `key` otherwise) so keys like `" C1 "` and `"C1"` match while mismatched key-to-value pairs still fail.

3. **`src/o2_audit/rules/advanced/f01_evidence.py`** (R005 tuple comparison & supplement logic):
   - Line 299 compares 3-tuple `read(facts, e.entity_id, "interior_obstructed")` to 2-tuple `(ObservationStatus.READABLE, True)` — always `False`! Fix to `read(...)[:2] == (ObservationStatus.READABLE, True)`.
   - When `interior_obstructed` is `(ObservationStatus.READABLE, False)` (confirmed NOT obstructed), return `SATISFIED` (`no_obstruction`), not `UNKNOWN`.
   - When obstructed and multiple supplement labels exist, scan ALL supplement candidates: if ANY matching supplement is unobstructed -> `SATISFIED`; else if ANY matching supplement is obstructed -> `VIOLATED` (`supplement_obstructed`); else if ANY supplement has unreadable/unknown identity or target anchor is unreadable -> `UNKNOWN` (`supplement_identity_ambiguous` / `target_anchor_unreadable`); else (all supplements have clearly different business anchor) -> `VIOLATED` (`supplement_different_field`).

4. **`src/o2_audit/rules/advanced/f02_identity.py`** (R012 `_substitute_similar` & applicability):
   - `_substitute_similar` currently loops `("O","0")`, `("0","O")`, `("I","1")`, `("1","I")` sequentially so `O->0` is overwritten by `0->O`. Replace with simultaneous canonicalization (e.g. `model_normalize(s).translate(str.maketrans("OI", "01"))`) so `model_normalize(printed) != model_normalize(declared)` AND `canon(printed) == canon(declared)` cleanly detects O/0 or I/1 substitution (`similar_char_substitution`).
   - Add `_applicability_r012`: if `declared_model` is present and contains neither `"0"` nor `"1"`, return `("not_applicable", [])`; otherwise `("applicable", [])` (keep missing `declared_model` as `applicable` so evaluator returns `ambiguous` with `applicable`).

5. **`src/o2_audit/rules/advanced/f04_placement.py`** (R019 front-view detection):
   - Check `front_views` across BOTH `EntityKind.PACKAGE` and `EntityKind.LABEL` (so a submitted front package view + model label only on back/side returns `VIOLATED` `model_not_on_front`, NOT `ambiguous`).
   - Exclude labels whose `model` observation is `CONFIRMED_MISSING` from `targets` (a confirmed-missing model on front must not count as "has model field on front"). Keep `UNREADABLE` model values on front as `SATISFIED` (per 口径补充说明: R019/R020 do not require reading the exact field value when field identity & carrier location are clear).

6. **`src/o2_audit/rules/advanced/f05_visual.py`** (R026/R027 empty-fact short-circuit & R029 bug):
   - **R026 & R027**: when `facts` has NO `EntityKind.OBJECT` observations at all (empty `FactView` before gateway inspection), return `UNKNOWN` (`unobserved`), NOT `VIOLATED`, so `AuditController` triggers the gateway. Only return `VIOLATED` when `CONFIRMED_MISSING` is observed or when partial/wrong shapes are observed in complete scope.
   - **R029**: fix lines 304–310 where `void_marked == False` did `continue` and skipped checking `is_filled`! For every star entity:
     - Check `void_marked`: if `(READABLE, True)` -> skip (voided star not counted); if non-readable (`UNREADABLE`/`INCOMPLETE_SCOPE`/`IDENTITY_UNKNOWN`) -> return `UNKNOWN` (`star_void_status_unverifiable`).
     - Then check `is_filled`: if `(READABLE, False)` -> skip (hollow star not counted as solid star); if non-readable -> return `UNKNOWN` (`star_fill_unverifiable`).
     - Otherwise count the star as unvoided solid star.
     - Also if `front_fully_shown == True` and `shape_type` is `CONFIRMED_MISSING` on the package (0 stars), return `VIOLATED` (`star_count_wrong`, 0 != 3).

7. **`src/o2_audit/rules/advanced/f06_warnings.py`** (R032 bug):
   - If `indoor_only_text` is `(ObservationStatus.READABLE, False)` or `ObservationStatus.CONFIRMED_MISSING`, return `SATISFIED` (`no_indoor_restriction`) immediately (do NOT treat as `UNKNOWN`, and do NOT reject if `outdoor_durable_text` is `True`).
   - Only when `indoor_only_text` is `(READABLE, True)` AND `outdoor_durable_text` is `(READABLE, True)` -> `VIOLATED` (`contradictory_restrictions`).

8. **`src/o2_audit/rules/advanced/f09_cross_material.py`** (R054 Pydantic crash & 1-cert case):
   - Line 363–364 currently passes `[ev(...), ev(...)]` (a `list[list[Evidence]]`, which crashes `RequirementResult` validation!). Fix to `ev(pair[0][2], f"联系方 {pair[0][1]}", quote) + ev(pair[1][2], f"联系方 {pair[1][1]}", quote)`.
   - Distinguish 0 certificates or unreadable certificate numbers (`UNKNOWN`) vs 1 readable active certificate when no other certificates have unreadable numbers (`SATISFIED`, `single_active_certificate`).

9. **`src/o2_audit/rules/advanced/f10_boundary.py`** (R057 & R059 semantic fixes):
   - **R057**: distinguish confirmed-incomplete `attached_tag` (`attached_to_package` is `CONFIRMED_MISSING` or `(READABLE, False)`, or `material_number` is `CONFIRMED_MISSING`) from uncertain (`UNREADABLE`/`INCOMPLETE_SCOPE`/`IDENTITY_UNKNOWN` or missing observation). When both alternatives are confirmed incomplete (e.g., outer package has `material_number` `CONFIRMED_MISSING` AND attached tag has readable `material_number` but `attached_to_package=False`/`CONFIRMED_MISSING`), return `VIOLATED` (`no_complete_alternative`), NOT `UNKNOWN`!
   - **R059**: for ANY confirmed **blue** box (`is_blue == True`), both same-level clauses ("所有练习盒正面须展示空心圆" and "蓝色练习盒正面不得出现空心圆") apply simultaneously with no authorized precedence -> return `CONFLICT` (`same_level_conflict` -> `label="ambiguous"`), whether a circle is present OR absent! For **non-blue** boxes (`is_blue == False`), Clause 2 does not trigger, so Clause 1 governs cleanly: hollow circle present -> `SATISFIED` (`pass`), confirmed absent -> `VIOLATED` (`reject`), circle presence unknown -> `UNKNOWN` (`ambiguous`).

10. **`src/o2_audit/rules/registry.py`** (consistent applicability precheck):
    - In `RuleRegistry.evaluate(task, facts, plan=None)`, for non-B001 rules run `self.applicability_precheck(task, p)` FIRST and short-circuit with `not_applicable_outcome()` when `applicability == "not_applicable"` or `applicability_unknown_outcome(missing)` when `applicability == "unknown"`, so calling `registry.evaluate(...)` directly and calling `AuditController.run(...)` produce identical applicability semantics.

## 2. Generalize `AuditController` (`src/o2_audit/runtime/controller.py`)

- Keep `AuditController(plan, gateway, store=None, run_id=..., artifact_version=..., max_rounds=3, registry=None)` 100% backward-compatible with all 96 existing Stage 01 tests.
- Accept ANY `RulePlan` (not only `B001Plan`), OR `plan=None` with `registry` (defaulting to `default_registry()`) to resolve `(task.rule_id, task.rule_version)` per task (raising `UnsupportedRuleError` on unknown/mismatched `(rule_id, rule_version)`).
- Reset `self._inspected = set()` at the beginning of each `run(task)` so multi-task reuse of one controller never leaks inspected material IDs across units.
- Run applicability precheck first; if `applicability != "applicable"`, short-circuit BEFORE calling the gateway and finalize with `not_applicable_outcome()` / `applicability_unknown_outcome(missing)` (or B001's outcome for B001).
- Generalize `_need_evidence(outcome, task, active_plan)`:
  - Keep `_GAP_MAP` for B001 backward compatibility.
  - For any other `RequirementResult` with `status == RequirementStatus.UNKNOWN` whose `reason_code` is NOT a non-actionable metadata/semantic block (e.g., `declared_*_missing`, `declared_*_invalid`, `package_size_missing`, `package_size_invalid`, `required_faces_missing`, `total_pages_missing`, `required_version_missing`, `language_mode_invalid`, `review_date_invalid`, `undefined_small_threshold`, `grayscale_photo`, `non_standard_view`, `withdrawal_before_issue`, `same_level_conflict`), ask `active_plan.gap_for(r.reason_code)` and emit a `NeedEvidence` targeting matching `task.materials` (or all `task.materials` if none match the role filter).
- Every produced row MUST continue to pass `prediction_semantic_errors(row, task)`.

## 3. Documentation (`docs/observation_vocabulary.md`)

- Write concise, clear documentation of `src/o2_audit/domain/vocabulary.py`:
  - Predicate groups, expected value types, entity kinds, and `ObservationStatus` semantics (`readable` vs `confirmed_missing` with `scope_complete=True` vs `unreadable` vs `incomplete_scope` vs `identity_unknown`).
  - Provenance (`material_id`, `page_index`, `bbox`), raw vs normalized separation, entity separation (multi-label photos, per-label `void_marked`), and printed page number vs source `page_index`.

## 4. Comprehensive semantic test suite (`tests/test_advanced_rules.py`)

Write thorough tests covering:
1. **Registry completeness & strictness**:
   - Exactly **62** registered `(rule_id, rule_version)` pairs (`B001@3.0`, `B001@3.1`, `R001@3.0`..`R060@3.0`).
   - Every plan has non-empty `title`, `raw_rule_text`, `quotes["rule_text"]`, `rule_family_id`.
   - Unknown rule ID or unknown version raises `UnsupportedRuleError` (which is also a `KeyError`).
2. **Every single rule R001–R060 semantic coverage**:
   - Every rule from `R001` through `R060` MUST have genuine tests asserting at least:
     - a valid `pass` (or valid non-conflict `pass` for R059 / unaffected outer-package `pass` for R060),
     - a genuine `reject` (or `not_applicable` where appropriate, plus `reject` for every rule that can reject),
     - a genuine `ambiguous` (including conflict/undefined-threshold/unknown-applicability/unreadable cases),
     checking BOTH `outcome.label`, `outcome.applicability`, AND meaningful `outcome.reason_code`!
3. **Every single named acceptance gate from `docs/supervisor_acceptance.md`**:
   - R001 boundary visible with interior obstructed (`pass`) vs cropped boundary (`reject`) vs obstructed boundary (`ambiguous`);
   - R002 declared faces covered by multi-face photo (`pass`) vs missing face (`reject`) vs missing `required_faces` (`ambiguous`, `applicability="applicable"`);
   - R003 multiple PNGs with `page_index=1` but distinct `printed_page_number` 1..N (`pass`), missing/out-of-range (`reject`), unreadable printed page (`ambiguous`);
   - R005 same-business-field unobstructed supplement (`pass`), all views obstructed (`reject`), different business anchor (`reject`), uncertain supplement identity (`ambiguous`), not obstructed (`pass`);
   - R006 photo (`pass`), design/render-only (`reject`), unknown source (`ambiguous`);
   - R009 unique serials (`pass`), duplicate or missing serial (`reject`), single-unit `declared_quantity=1` (`not_applicable`), unreadable (`ambiguous`);
   - R010 main model match (`pass`), declared model only in compatible list (`reject`);
   - R011 one-to-one mapping (`pass`), non-one-to-one or missing replacement map (`reject`), no `old_model` (`not_applicable`);
   - R012 O/0 & I/1 substitution rejected (`similar_char_substitution`), indistinguishable (`ambiguous`), model without 0/1 (`not_applicable`);
   - R019/R020 location clear with `model`/`serial_number` value `UNREADABLE` STILL passes (`pass`), wrong carrier/view rejects (`reject`), front view missing -> `ambiguous`;
   - R021 same label (`pass`) vs split across two labels in same photo (`reject`);
   - R022 `<80` fixed tag (`pass`), `==80` tag (`reject`), missing size (`ambiguous`, `applicability="applicable"`);
   - R023 tear line crossing blank margin (`pass`) vs crossing model/material_number (`reject`);
   - R024 upper half & `on_midline` (`pass`), lower half (`reject`), non-standard view (`ambiguous`);
   - R025 hollow closed circle no text (`pass`), filled / unclosed / text inside (`reject`), obstructed (`ambiguous`);
   - R026 closed triangle + 1 dot inside (`pass`), dot outside / 2 dots / unclosed (`reject`), unobserved (`ambiguous`);
   - R027 circle left of square via bbox (`pass`), vertical stack same x (`reject`), unobserved (`ambiguous`);
   - R028 matching color (`pass`), wrong color (`reject`), grayscale photo (`ambiguous`);
   - R029 3 unvoided solid stars + 1 voided star + 1 hollow star (`pass`), 2 or 4 solid stars (`reject`), incomplete front view (`ambiguous`);
   - R030 `height_ratio == 1.5` (`pass`), `< 1.5` (`reject`), non-standard view (`ambiguous`);
   - R031 `"仅供\n练习使用"` (`pass`), `"练习用"` (`reject`);
   - R032 indoor-only without outdoor (`pass`), no indoor-only even with outdoor (`pass`), both indoor & outdoor (`reject`);
   - R033 exact 1-2-3 steps (`pass`), swapped order or duplicate number (`reject`);
   - R035 exact `"Keep dry"` (`pass`), `"keep dry"` wrong case (`reject`), bilingual requires both;
   - R037–R042 authorized exact conversions (L/mL, kg/g, mm/cm, 1% inclusive tolerance on R041, 30 days inclusive on R042) AND unauthorized units/extra error/missing-as-zero rejected;
   - R046 clear void rejects even when validity covers review date; unclear void -> `ambiguous`;
   - R048 withdrawal before issue date -> `ambiguous`, review on withdrawal date -> `reject`, review before withdrawal date within validity -> `pass`;
   - R050 keyed component_id->model correspondence: swapped models with identical sets MUST reject!
   - R052 keyed model->spec correspondence: cannot borrow another model's spec;
   - R054 two active same-number certs with different `contact_info` -> `ambiguous` with valid `Evidence` list (no Pydantic crash!), one voided + one active -> `pass`;
   - R055 category/power short-circuit (`not_applicable` when one conjunct false even if other missing!);
   - R056 `declared_quantity=1` (`not_applicable`), `>1` with contents (`pass`) / missing (`reject`), `0` or `"2"` or `True` or missing (`ambiguous`, `applicability="unknown"`);
   - R057 one complete alternative (`pass`), two confirmed-incomplete alternatives (`reject`, never cherry-pick!), uncertain element (`ambiguous`);
   - R058 production date before `2026-01-01` requires `OLD-` even when `review_date >= 2026-01-01`, exact case (`old-` rejects);
   - R059 blue box with circle (`ambiguous` conflict), blue box without circle (`ambiguous` conflict), non-blue box with circle (`pass`), non-blue box confirmed without circle (`reject`);
   - R060 outer package (`pass` unaffected branch), attached tag even with `package_longest_edge_mm=50` (`ambiguous` undefined `"较小"` threshold — never borrows B001's 100/80 mm!).
4. **Generalized `AuditController` end-to-end tests**:
   - Run multiple advanced rules (e.g., R001, R025, R037, R050, R055 not_applicable short-circuit without calling gateway, R059 conflict, R060 unaffected + undefined, plus multi-task reuse verifying `_inspected` isolation) through `AuditController` with `StaticObservationGateway` and verify every output passes `prediction_semantic_errors(row, task)`.

## Verification

Run:
- `uv run pytest -v`

All existing 96 Stage 01 tests AND all new Stage 02a tests must pass (0 failures). Report exact files modified/added and exact pytest counts. Stop after this stage.
