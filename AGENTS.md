# Implementation and review contract

## Goal

Implement the complete O2 product-review challenge described by TODO.md. Use OntoAgent's four-layer division as architectural inspiration; implement a small challenge-specific runtime, not its codebase-governance semantics. Source rules and protocols are in the parent directory until copied into resources/. The submitted repository must eventually work without its parent folder.

## Delegation

The supervising assistant assigns bounded stages to a ZCode GLM-5.3-Flash coding worker. A worker implements only its assigned stage, reports changed files, checks actually run, failures and limitations. Never fabricate results or mark TODO items complete. Supervisor reviews, tests and checks off verified work. Workers must not commit, push, change Git configuration, overwrite another stage's work, or modify this contract/TODO without an explicit task.

## Runtime invariants

- Five CLI commands and protocol 1.3.1 must match the public specification.
- build/infer/annotate/iterate cannot load reference labels. Evaluation runs separately.
- Domain entities, current-unit observations and evidence are typed and scope-bound.
- Keep original transcription separate from business normalization.
- Missing, unreadable, incomplete inspection, identity uncertainty, conflicting claims and execution failure are distinct.
- Rules apply only to explicitly requested ID/version. B001 small-tag thresholds are <100 for 3.0 and <80 for 3.1. Never infer review date from machine clock.
- Decision order: applicability; explicit exception/conflict priority; proven violation; consequential unknown; satisfied.
- Never infer absence from an empty OCR result. Never merge fields across distinct labels.
- Rules/artifacts actually constrain execution. Model outputs observations, not authoritative final decisions.
- Tools/network errors are execution_status=error and label=null, not ambiguous.
- No answer lookup by sample IDs, filenames, decorative numbers, ordering or public reference predictions.
- Every expected unit remains in outputs/accounting. Retrying cannot select a better answer or duplicate final IDs.
- Frozen artifacts/inputs are read-only. Running output, caches and SQLite live outside artifacts.
- Keep baseline/candidate separate. No hidden protection/final labels, no forged organizer records.
- Strict JSON types: bool is not an integer/page; invalid bbox is rejected; nonfinite/duplicate keys are rejected.
- Shared operators are independent of rule IDs; rule-specific parameters live in versioned plans.

## Scope and tools

Edit only this repository unless a task explicitly says otherwise. Original parent materials are read-only. Do not expose credentials or print environment secrets. Never bypass permission denials. Install only dependencies needed by assigned work; use uv with Python 3.12 (available locally), locking versions. Prefer standard library + Pydantic, Pillow, jsonschema, FastAPI/Jinja2 and pytest. Keep a single process/SQLite architecture initially. If using local ZCode, credentials come from environment and must not be persisted. Do not start nested agents or unrelated tasks.

## Quality

Add meaningful tests for semantics and fault handling. Run appropriate checks and report exact outcome. Mock gateways are for tests and must never be presented as actual visual recognition. Preserve third-party sources/licenses when vendoring public validation helpers. Keep modules focused and APIs documented. A clean clone must have clear CLI, model/OCR configuration and UI startup instructions.
