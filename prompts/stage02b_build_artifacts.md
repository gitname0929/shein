# Stage 02b: rule builder, frozen artifact manifests, capability hash binding, version lifecycle & CLI

Read `AGENTS.md`, `docs/supervisor_acceptance.md`, `resources/protocol/产物目录哈希规范.md`, `resources/protocol/SOP与建设policy空模板.json`, `resources/protocol/60条建设台账模板.csv`, `resources/protocol/执行与评分协议.md`, and the completed Stage 01 + Stage 02a code under `src/o2_audit/`. Extend existing interfaces; do NOT replace the four-layer architecture or modify `TODO.md`, `AGENTS.md`, `IMPLEMENTATION_STATUS.md`, `tools/run_glm_stage.py`, or the 7 vendored files in `src/o2_public_contracts/`. No commits, no push, no nested agents.

Efficiency note: batch independent Read / Edit / Write calls in the same turn whenever possible.

## Deliverables

1. **Rule JSONL importer (`src/o2_audit/build/importer.py`)**:
   - Parse rules JSONL strictly (rejecting duplicate JSON keys, NaN/Infinity, malformed lines) with **per-record isolation**: valid rows are imported while invalid rows produce structured line-numbered errors (`line_number`, `rule_id` if extractable, `error_code`, `message`, `next_action`) without aborting the entire batch.
   - Deduplicate by `(rule_id, rule_version)` idempotently and record source SHA256 hashes.
   - Support reading both `基础规则版本.jsonl` (B001@3.0, B001@3.1) and `进阶规则60条.jsonl` (R001..R060@3.0), or a combined/custom rules JSONL.

2. **Artifact compiler & frozen build directory (`src/o2_audit/build/compiler.py`)**:
   - Compile requested rules + config JSON into a frozen `BUILD_DIR`:
     - Executable rule plans + capability dependency metadata under `engine/rules/<rule_id>_<rule_version>.json` and `engine/capabilities.json` (each capability records its real source-file SHA256 computed from the actual Python module(s) implementing it under `src/o2_audit/operators/` and `src/o2_audit/rules/`).
     - Full SOP & build policy JSONs (matching `resources/protocol/SOP与建设policy空模板.json` with `template_only: false`, genuine `source_quotes`, `sop`, `build_policy`, `artifact`) for `B001@3.0`, `B001@3.1`, and all built rules under `engine/sop/`.
     - Advanced 60-rule construction ledger CSV (`ledger/advanced_60_ledger.csv`) following the exact column headers of `resources/protocol/60条建设台账模板.csv` with **exactly 60 unique rows** (`R001..R060@3.0`) when all 60 advanced rules are built (or the built subset plus explicit `not_built` rows for remaining advanced rules when building a subset; never duplicate rule IDs and never fabricate accuracy/machine_review_rate — leave metrics blank until evaluated).
     - `artifact_manifest.json` using the exact public schema `{"entries":[{"rule_id":...,"rule_version":...,"artifact_version":...,"path":...,"sha256":...}]}` where every `sha256` is computed via vendored `o2_public_contracts.artifact_hash.digest(...)`.
     - **Critical hash invariant**: NO entry path may point at the root directory `.` (which contains `artifact_manifest.json`, causing self-reference). Each rule entry points to its own sub-artifact path under `engine/` (e.g. `engine/rules/B001_3.0` or `engine/rules/R001_3.0`), and shared capabilities live under `engine/capabilities.json` (whose hash is referenced inside each rule's frozen plan/bundle). After writing `artifact_manifest.json`, compute and record the whole-directory root hash via `o2_public_contracts.artifact_hash.digest(BUILD_DIR)` in `TaskStore` (NEVER written back inside `BUILD_DIR` after hashing, which would invalidate the root hash!).
     - Zero `__pycache__`, `.DS_Store`, SQLite DB, or runtime logs inside `BUILD_DIR`.

3. **Runtime artifact loader & tamper/hash binding (`src/o2_audit/build/loader.py`)**:
   - Load a frozen `BUILD_DIR` read-only, validate `artifact_manifest.json` strictly (unique `(rule_id, rule_version)`, safe relative paths, no root self-reference, every entry `sha256` matches `o2_public_contracts.artifact_hash.digest(build_dir / entry.path)`).
   - Verify capability dependency hashes against current runtime capability module hashes; if a plan file OR a bound capability implementation hash disagrees with the frozen artifact, raise `ArtifactIntegrityError` so rules cannot silently execute modified code without a rebuild!
   - Wire `AuditController` / loaded plans so outputs record the exact loaded `artifact_version` from the manifest entry.

4. **Version registry, transitive dependency impact & selective incremental rebuild (`src/o2_audit/build/lifecycle.py`)**:
   - Maintain immutable version records in `TaskStore` (baseline, candidates, active pointer, parent artifact identity, root directory SHA256, manual override diffs).
   - Transitive dependency graph: capability -> dependent capabilities/rules; given a changed capability or rule source, compute all directly and transitively affected `(rule_id, rule_version)` pairs.
   - Selective incremental rebuild (`rebuild_affected`): rebuild ONLY affected rules/capabilities into a new immutable `OUT_DIR` while copying unaffected rule sub-artifacts byte-for-byte so at least one unaffected rule genuinely retains its identical `entry.sha256`.
   - Record manual diffs/overrides explicitly (who/reason/field diff) and support `compare_versions`, `activate`, and `rollback` (restoring the previous active pointer and verifying its exact directory SHA256 before activation).

5. **CLI entrypoint (`src/o2_audit/cli.py` + `pyproject.toml` `[project.scripts]` `o2-audit = "o2_audit.cli:main"`)**:
   - Exact public adapter signatures:
     - `o2-audit build --rules RULES_JSONL --config CONFIG_JSON --out BUILD_DIR`
     - Developer/lifecycle subcommands: `o2-audit status`, `o2-audit activate --name NAME`, `o2-audit rollback --to NAME` (using `--workspace` / `O2_AUDIT_WORKSPACE_DIR` outside frozen artifacts).
     - Reserve exact CLI names `infer`, `annotate`, `iterate`, `evaluate` with explicit non-zero exit + clear `"not yet implemented in Stage 02b"` message (NEVER fake exit 0).

6. **Comprehensive pytest suite (`tests/test_build_and_lifecycle.py`)**:
   - Importer tests: valid 62-rule import, malformed JSONL line / duplicate key isolated with line numbers while valid rows succeed, idempotent re-import.
   - Build tests: run `o2-audit build` via CLI and Python API on repo-local rules JSONL; verify `artifact_manifest.json` passes vendored `o2_public_contracts.contracts` / `artifact_hash.digest`, unique 62 entries, no root self-reference, B001 3.0 & 3.1 SOP/policy JSONs present and non-template, advanced 60-rule ledger has exactly 60 unique advanced rules with blank unmeasured metrics, and whole-directory digest is reproducible.
   - Tamper & binding tests: modifying a frozen rule file fails verification; modifying a simulated capability hash fails verification unless rebuilt; `AuditController` bound to loaded artifact stamps exact `artifact_version`.
   - Impact & incremental rebuild tests: changing one capability computes transitive affected rules; selective rebuild updates affected rule hashes while an unaffected rule retains its exact `sha256`!
   - Lifecycle tests: baseline remains untouched, activate/rollback restores exact previous version & SHA256, manual override diff recorded, unimplemented CLI commands (`infer`/`annotate`/`iterate`/`evaluate`) exit non-zero cleanly.
   - Update `README.md` with factual `o2-audit build` usage and Stage 02 limitations (do NOT claim vision/infer/annotate/UI yet).

## Verification

Run:
- `uv sync --python 3.12`
- `uv run pytest -v`
- `uv run o2-audit build --rules ... --config ... --out ...` (into a temporary dir under `workspace/` and inspect output, then clean up or keep under ignored `workspace/`).

Report exact files changed/added, exact commands run, exact pytest counts, and honest remaining limitations. Stop after this stage.
