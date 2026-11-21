# O2 AI 商品审核官 — Ontology-Driven Product Review Agent

Challenge implementation for **O2 AI 商品审核官** (Protocol 1.3.1): a single-process, ontology-driven Python 3.12 + Pydantic + SQLite runtime covering all 62 fictional competition rule/version pairs (`B001@3.0`, `B001@3.1`, `R001–R060@3.0`), all five official CLI adapters, isolated evaluation plugins, safe feedback iteration, and a persistent FastAPI + Jinja2 operational product.

All rules are fictional competition rules (练习规则); nothing here performs real regulatory certification.

## Architecture & Capabilities

- **Four-layer ontology-driven core** (inspired by `yql210/ontology-driven-agent`, pinned in `docs/upstream_references.md`):
  - **Intent / Planning**: rule-versioned plans (`B001@3.0`, `B001@3.1`, `R001–R060@3.0` across families `F01–F10`).
  - **Constraint / Control**: deterministic `AuditController` loop (`NeedEvidence` → permitted evidence action → validated `Observation` → reevaluation → deterministic decision). Vision/LLM models produce structured observations only — never authoritative compliance labels.
  - **Capability Execution**: shared rule-ID-independent operators (`text`, `dates`, `numbers`, `sets`, `geometry`), safe image/PDF/crop loader (`pypdfium2` + `Pillow`), credential-free `ModelConfig`, Anthropic-Messages `/v1/messages` client (`ZCodeObservationGateway`), and content-addressed workspace SQLite extraction cache.
  - **Semantic / Persistence Layer**: scope-bound `Entity`/`Observation`/`Evidence` models, immutable frozen artifact bundles, and workspace SQLite state (`state.sqlite3`) strictly outside frozen artifacts.
- **Five official Protocol 1.3.1 CLI adapters** (`o2-audit`):
  - `o2-audit build` — strict per-record rules JSONL importer, frozen `BUILD_DIR` compiler, capability-hash binding, 60-rule `advanced_60_ledger.csv`, and `artifact_manifest.json` using vendored `o2_public_contracts.artifact_hash`.
  - `o2-audit infer` — resumable batch inference with exact-1-row-per-unit accounting, strict protocol validation, and honest `execution_status="error"` / `label=null` on tool/network failures.
  - `o2-audit annotate` — target-directed transcription (NFKC + strip only, preserving internal case/punctuation), whole-unit rule labels, and `<out>.errors.jsonl` failure reporting.
  - `o2-audit iterate` — strict `feedback.schema.json` validation, whole-directory `tree_digest` hash binding before creating `--out`, rule-cited rejection of adversarial/incompatible proposals, bounded `extraction_guidance` updates (affecting both VLM prompts and cache fingerprints) with byte-identical preservation of unaffected bundles, baseline immutability, and non-auto-activation (`official_promotion: false`).
  - `o2-audit evaluate` — isolated subprocess plugin runner (`o2.conclusion`, `o2.evidence`, `o2.version_compare`), read-only input mutation checks, per-plugin timeouts, crash isolation, and AST-verified label isolation (`L-STRUCT-04`).
- **Operational Product & Lifecycle**:
  - `o2-audit serve` — single-process FastAPI + Jinja2 operational workbench backed by SQLite `JobRunner` covering all 16 product tasks (`P-TASK-01..16`, `P-UX-01..04`): rule import & deduplication, single/bulk builds, audit/annotate jobs pinned to frozen artifacts, result/rule-text/bbox evidence viewer (with path-traversal & symlink defense), safe iteration, version activation/rollback/diff, baseline-vs-candidate comparison with explicit scope disclaimers, plugin toggles & isolated evaluation, idempotent retry (`parent_job_id`), and restart recovery / checkpoint resume.
  - `o2-audit status | activate | rollback | compare` — atomic pointer management with whole-directory SHA256 verification.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12 (any `>=3.11` works; `3.12` is locked in `uv.lock`).

```bash
uv sync --python 3.12
uv run pytest
```

## CLI Usage

```bash
# 1. Build a frozen artifact directory (SQLite state stays in ./workspace, never inside BUILD_DIR)
uv run o2-audit build --rules resources/rules/基础规则版本.jsonl --config workspace/build_config.json --out workspace/builds/b001_v1

# 2. Run batch audit inference (never reads reference/gold labels)
uv run o2-audit infer --input path/to/inputs.jsonl --artifacts workspace/builds/b001_v1 --out workspace/outputs/predictions.jsonl

# 3. Run target-directed annotation
uv run o2-audit annotate --input path/to/inputs.jsonl --targets path/to/targets.jsonl --artifacts workspace/builds/b001_v1 --out workspace/outputs/annotations.jsonl

# 4. Run safe feedback iteration into a NEW immutable candidate directory
uv run o2-audit iterate --base workspace/builds/b001_v1 --feedback path/to/feedback.json --input path/to/repair.jsonl --out workspace/builds/b001_cand

# 5. Run isolated evaluation plugins (references stay strictly inside evaluate)
uv run o2-audit evaluate --plugin conclusion --inputs path/to/inputs.jsonl --outputs workspace/outputs/predictions.jsonl --references path/to/refs.jsonl --out workspace/outputs/report.json

# Version lifecycle & local operational Web UI
uv run o2-audit status
uv run o2-audit activate --name my-v1
uv run o2-audit rollback --to baseline
uv run o2-audit compare --a baseline --b candidate
uv run o2-audit serve --host 127.0.0.1 --port 8000 --workspace ./workspace
```

## Configuration

Environment variable names are documented in `.env.example` (no secrets or keys are stored in the repository). Model endpoints and API keys are read strictly from environment variables (e.g. `O2_BASE_URL`/`O2_API_KEY` or `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`) and never persisted into artifacts, logs, or SQLite.

## Repository Layout

```text
src/o2_audit/
  domain/                Protocol-1.3.1 models, entities, observations, evidence, vocabulary
  protocol/              Strict JSON/JSONL parsing, semantic checks, safe material paths
  operators/             Rule-ID-independent operators (text, dates, numbers, sets, geometry)
  rules/                 RuleRegistry, B001 (3.0/3.1), advanced families F01..F10 (R001..R060)
  build/                 Rules importer, frozen artifact compiler, tamper-checking loader, lifecycle
  materials/             Safe PNG/JPEG/WEBP/PDF loader and crop<->source bbox mapping
  model_gateway/         Credential-free config, Anthropic-Messages client, prompts, parsers, gateway
  runtime/               FactView, AuditController, batch infer & target-directed annotate
  evaluation/            Isolated subprocess runner, o2.conclusion, o2.evidence, o2.version_compare
  iterate/               Safe feedback validator, rule-cited proposal classifier, candidate builder
  jobs/                  Persistent SQLite background JobRunner (idempotent retry, checkpoint resume)
  web/                   FastAPI + Jinja2 operational product (HTML workbench + JSON APIs)
  storage/store.py       Single-process SQLite store (outside frozen artifacts)
  cli.py                 o2-audit CLI entrypoint
src/o2_public_contracts/ Vendored public validator helpers (SHA256 byte-identical; see README)
resources/               Self-contained read-only copies of rules, protocol schemas, public dev data
tests/                   Full pytest suite across all 5 stages (511 tests)
docs/                    architecture.md, evaluation.md, observation_vocabulary.md, upstream_references.md
```

## Scope and Honest Limitations

- `resources/` contains read-only public competition materials only; the repository runs standalone without its parent folder.
- `build`, `infer`, `annotate`, and `iterate` never read reference/gold labels; evaluation runs in isolated subprocesses. Local subprocess isolation does not claim OS/container network sandboxing.
- Formal hidden-set / protection-set / final scoring and official promotion gates remain organizer-owned; candidate artifacts always carry `official_promotion: false` and are never auto-activated.
- Unit/integration tests use `httpx.MockTransport` for deterministic CI repeatability (clearly separate from live VLM visual accuracy). Live ZCode (`zcode/GLM-5.3-Flash`) smoke tests were run separately and surfaced upstream rate/risk-control responses honestly as `execution_status="error"` / `label=null`.
- Package dimensions are read exclusively from declared metadata (`package_longest_edge_mm`), never measured from photos; review dates come strictly from task `review_date`, never the machine clock.
