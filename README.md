# O2 AI 商品审核官 — core repository (Stage 01)

Challenge implementation for the O2 product-review agent: protocol-1.3.1
inputs, typed observations, rule evaluation and a four-layer runtime. All
rules are fictional competition rules (练习规则); nothing here performs real
regulatory certification.

**Stage 01 status: foundation + vertical prototype only.** Implemented:
protocol/JSON strictness, domain models, shared operators, SQLite task store,
a typed B001 plan/evaluator and a minimal four-layer controller with a
gateway protocol. **Not yet implemented** (later stages): vision/OCR image
understanding, the 60 advanced rules (R001–R060), the five CLI commands
(`build`/`infer`/`annotate`/`iterate`/evaluate), the review UI, artifact
sealing/publishing and real model integration. The test gateway is a
deterministic TEST-ONLY stub, not visual recognition.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12 (any >=3.11 works;
3.12 is locked).

```bash
uv sync --python 3.12
uv run pytest            # run the test suite
uv run pytest -v         # verbose
```

No GPU, model weights or external services are needed for Stage 01.

## Layout

```
src/o2_audit/            package core
  domain/models.py       typed protocol-1.3.1 models, observations, evidence
  protocol/              strict JSON/JSONL, semantic checks, safe material paths
  operators/             shared rule-ID-independent operators (text/dates/numbers/sets/geometry)
  runtime/               FactView, observation gateway protocol, four-layer controller
  rules/b001/            B001 typed plans (3.0/3.1) + evaluator
  storage/store.py       single-process SQLite store (runs/steps/attempts/observations)
  resources.py           repo-local resource paths
src/o2_public_contracts/ vendored public validator helpers (byte-identical, see README there)
resources/               read-only copies of rules/protocol/public dev data (see resources/README.md)
tests/                   pytest suite (synthetic fact graphs, test-only gateway)
docs/                    architecture.md, upstream_references.md
workspace/               writable state (SQLite lives here, never inside artifacts)
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
