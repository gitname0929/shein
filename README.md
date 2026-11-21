# O2 AI 商品审核官 — core repository (Stage 02)

Challenge implementation for the O2 product-review agent: protocol-1.3.1
inputs, typed observations, all 62 deterministic rule-version evaluators,
frozen artifact building/verification, version lifecycle, and a four-layer
runtime. All rules are fictional competition rules (练习规则); nothing here
performs real regulatory certification.

**Stage 02 status: foundation + 62 deterministic rules + frozen artifact build/lifecycle CLI.**
Implemented:
- protocol/JSON strictness, domain models, unified observation vocabulary, shared operators (`text`/`numbers`/`dates`/`sets`/`geometry`), SQLite task & version store;
- all 62 rule-version plans and deterministic evaluators (`B001@3.0`, `B001@3.1`, `R001..R060@3.0`) wired through `RuleRegistry` and `AuditController`;
- strict per-record rules JSONL importer, frozen `BUILD_DIR` compiler (`artifact_manifest.json` using vendored `o2_public_contracts.artifact_hash`, capability SHA256 bindings, non-template SOP/policy JSONs, 60-rule ledger CSV), tamper-checking loader, transitive dependency impact analysis, selective incremental rebuild, version activate/rollback, and `o2-audit build` / `status` / `activate` / `rollback` / `compare` CLI.

**Not yet implemented** (later stages):
- Stage 03: real image/PDF visual & OCR observation gateway and CLI `infer` / `annotate`;
- Stage 04: isolated evaluation plugins and CLI `evaluate`;
- Stage 05: feedback-driven iteration (`iterate`), persistent job runner, and FastAPI UI.
The reserved commands `infer`, `annotate`, `iterate`, and `evaluate` exit non-zero cleanly until their stages are implemented. The test gateway is a deterministic TEST-ONLY stub, not visual recognition.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12 (any >=3.11 works;
3.12 is locked).

```bash
uv sync --python 3.12
uv run pytest            # run the test suite
uv run pytest -v         # verbose
```

## Build & lifecycle CLI

```bash
# Compile a frozen artifact directory (SQLite state stays in ./workspace, outside BUILD_DIR)
uv run o2-audit build --rules resources/rules/基础规则版本.jsonl --config workspace/build_config.json --out workspace/builds/b001_v1

# Inspect, activate, compare, or roll back recorded versions (verifies whole-directory SHA256)
uv run o2-audit status
uv run o2-audit activate --name my-v1
uv run o2-audit rollback --to baseline
uv run o2-audit compare --a baseline --b candidate
```

No GPU, model weights or external services are needed for Stage 02.

## Layout

```
src/o2_audit/            package core
  domain/                typed protocol-1.3.1 models, observations, evidence, vocabulary
  protocol/              strict JSON/JSONL, semantic checks, safe material paths
  operators/             shared rule-ID-independent operators (text/dates/numbers/sets/geometry)
  runtime/               FactView, observation gateway protocol, four-layer controller
  rules/                 RuleRegistry, B001 (3.0/3.1), advanced families F01..F10 (R001..R060)
  build/                 rules importer, frozen artifact compiler, tamper-checking loader, lifecycle
  storage/store.py       single-process SQLite store (runs/steps/observations/artifact versions)
  cli.py                 o2-audit CLI entrypoint
  resources.py           repo-local resource paths
src/o2_public_contracts/ vendored public validator helpers (byte-identical, see README there)
resources/               read-only copies of rules/protocol/public dev data (see resources/README.md)
tests/                   pytest suite (synthetic fact graphs, build/tamper/lifecycle/CLI tests)
docs/                    architecture.md, observation_vocabulary.md, upstream_references.md
workspace/               writable state (SQLite & scratch builds live here, never inside frozen artifacts)
```

## Resource policy

`resources/` is read-only reference data copied from the competition package;
the repository works without the parent folder. `resources/public_dev`
contains PUBLIC development data only — its reference labels are never loaded
by inference paths (evaluation runs separately). See `resources/README.md`.

## Configuration

Environment variable names are listed in `.env.example` (no secrets, no
values). Stage 01 needs none of them at runtime; they are reserved for later
model/OCR integration stages.

## Scope and limitations

- B001 3.0/3.1 semantics are implemented against synthetic fact graphs;
  "observation" objects are produced by tests, not by an image model.
- Package size is read from the declared metadata value only, never measured
  from photos (规则原文约定).
- No review date is taken from the machine clock; only the task's
  `review_date` is used.
- The controller loops are bounded (`max_rounds`); gateway failures become
  protocol-legal `execution_status="error"` rows with `label=null`.
