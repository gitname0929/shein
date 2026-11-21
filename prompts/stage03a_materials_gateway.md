# Stage 03a: materials image/PDF/crop loader, ZCode multimodal ObservationGateway & extraction cache

Read `AGENTS.md`, `docs/supervisor_acceptance.md`, `docs/observation_vocabulary.md`, `resources/protocol/容器与模型接入.md`, and the existing `src/o2_audit/domain/`, `src/o2_audit/protocol/`, `src/o2_audit/runtime/`, `src/o2_audit/storage/` code. Extend the existing four-layer architecture cleanly; do NOT modify `TODO.md`, `AGENTS.md`, `IMPLEMENTATION_STATUS.md`, `tools/run_glm_stage.py`, or the 7 vendored files in `src/o2_public_contracts/`. No commits, no push, no nested agents.

Efficiency note: batch independent Read / Edit / Write calls in the same turn whenever possible.

## Deliverables

1. **Materials loader, PDF page renderer & crop coordinate mapper (`src/o2_audit/materials/`)**:
   - `src/o2_audit/materials/__init__.py`
   - `src/o2_audit/materials/loader.py`:
     - Safe path resolution anchored strictly under a `data_root` via existing `o2_audit.protocol.paths.resolve_material_path` (reject symlinks, traversal, absolute paths, missing files, oversized files).
     - Load PNG/JPEG/WEBP images with Pillow (`PIL.Image`), validating actual decoded image dimensions and format against declared `media_type` (bounded max bytes and max pixels to prevent decompression bombs).
     - Render PDF pages via `pypdfium2` selecting the EXACT `page_index` (1-based source page index, rejecting bool/out-of-range page numbers cleanly with `ExecutionError`). Record both `page_index` (1-based source PDF page) and total PDF page count; never confuse source `page_index` with printed document page numbers.
     - Compute raw file content SHA256 (`content_sha256`) for deterministic caching.
     - Downscale overly large images for model transmission while preserving the exact full-source width/height metadata so normalized `[0, 1]` coordinates always map to the original source image.
     - Crop helper `crop_region(loaded_image, crop_bbox)` using Pillow, plus exact inverse mapping `map_crop_bbox_to_source(crop_bbox, sub_bbox)` that maps a normalized `[x1, y1, x2, y2]` inside a crop back into full-source normalized `[x1, y1, x2, y2]` (clamping tiny floating-point epsilon while rejecting invalid/inverted/non-finite/bool bboxes).
     - Encode loaded/cropped image cleanly to base64 PNG/JPEG bytes for `/v1/messages` image content blocks.

2. **Model gateway config & ZCode `/v1/messages` multimodal client (`src/o2_audit/model_gateway/`)**:
   - `src/o2_audit/model_gateway/__init__.py`
   - `src/o2_audit/model_gateway/config.py`:
     - `ModelConfig` (Pydantic): `model` (default `"zcode/GLM-5.3-Flash"`), `base_url_env` (default `"O2_BASE_URL"` with fallback `"ANTHROPIC_BASE_URL"`), `api_key_env` (default `"O2_API_KEY"` with fallback `"ANTHROPIC_AUTH_TOKEN"`), `anthropic_version` (default `"2023-06-01"`), `max_tokens` (default `4096`), `timeout_seconds` (default `90.0`), `max_retries` (default `2`), `temperature` (default `0.0`).
     - **Security invariant**: NEVER serialize or log credential values! Config JSON/dicts store ONLY env var names and non-secret parameters. Resolve endpoint/token from `os.environ` at runtime; if missing/blank, raise `ExecutionError("model_not_configured", ...)` — NEVER silently fall back to fake/stub observations!
     - Provide a safe non-secret example config file `configs/model_config.example.json`.
   - `src/o2_audit/model_gateway/client.py`:
     - Provider-neutral `httpx` client posting to `{base_url}/v1/messages` (normalizing trailing slashes cleanly, supporting both `base_url` ending in `/v1` or root `/`).
     - Headers: `content-type: application/json`, `anthropic-version: <version>`, `x-api-key: <token>`, and `authorization: Bearer <token>` (compatible with Anthropic Messages gateways and ZCode). Never include token in error messages, repr, or logs.
     - Request body: `model`, `max_tokens`, `temperature`, `system`, `messages: [{"role": "user", "content": [...text and base64 image blocks...]}]`.
     - Bounded retry on transient 429/5xx/network timeout with exponential backoff; distinguish network/timeout (`gateway_timeout`/`gateway_network_error`), HTTP error (`gateway_http_error`), refusal/truncated/malformed JSON (`gateway_parse_error`/`gateway_incomplete_response`), and track token usage (`input_tokens`, `output_tokens`, `calls`, `latency_ms`) without logging secrets.
   - `src/o2_audit/model_gateway/prompts.py`:
     - Structured observation extraction prompts keyed by `docs/observation_vocabulary.md` (`EntityKind`, predicate vocabulary, `ObservationStatus`).
     - Prompt explicitly instructs the model to output ONLY structured JSON observations (`entities`, `observations`) — **NEVER** final compliance labels (`pass`/`reject`/`ambiguous`) or approval judgments.
     - Material text is explicitly treated as untrusted data, never instructions. Never ask the model to guess physical dimensions from photos. Separate separate labels on the same photo into distinct `entity_id`s and keep `void_marked` per-entity (never global). For matrices/tables, preserve header/row/column correspondence.
     - Also provide a target-directed annotation extraction prompt (`build_annotation_prompt`) for Stage 03b that extracts exact verbatim text (`NFKC + strip` only, preserving internal case/punctuation/spaces), `status` (`value` / `unreadable` / `invalid_value`), and normalized `bbox` `[x1, y1, x2, y2]` for the target only.
   - `src/o2_audit/model_gateway/parser.py`:
     - Strict parser converting model text output (handling optional ```json fenced blocks cleanly) via `strict_loads` into a validated `ObservationBatch`:
       - Reject or drop/flag any forbidden compliance verdict fields if the model emits them? Specifically: NEVER accept a model-emitted `label` as authoritative; validate `entities` and `observations` strictly against `Entity`, `Observation`, `ObservationStatus`, and `ObservationSource`.
       - Enforce source membership: every observation's `source.material_id` and `source.page_index` MUST belong to the materials actually supplied in the current `NeedEvidence` request (if the model omits `material_id` when only one material was sent, bind to that material cleanly; if it cites an unknown material_id or invalid/inverted/out-of-range `bbox`, discard the bad bbox or raise/record cleanly rather than fabricating coordinates!).
       - Preserve `raw_value` verbatim; normalize `normalized_value` only with our shared operators where appropriate (`model_normalize` for models, `annotation_nfkc_strip` for strings, etc.) without mutating `raw_value`.
       - Enforce `confirmed_missing` requires `scope_complete=True`; if model outputs `confirmed_missing` with `scope_complete=False`, downgrade to `unreadable`/`unknown` rather than letting a false absence slip through!
   - `src/o2_audit/model_gateway/gateway.py`:
     - `ZCodeObservationGateway` implementing `ObservationGateway`:
       - Loads requested materials via `materials.loader` (supporting optional crop regions if `NeedEvidence` specifies one, and remapping bboxes back to full source coordinates).
       - Checks optional extraction cache (`ExtractionCache` backed by `TaskStore` or workspace SQLite, NEVER inside frozen artifacts) keyed by `(material_content_sha256s, model, prompt_hash, schema_version, scope, plan_identity_if_rule_dependent)`.
       - **Critical cache rebinding invariant**: on a cache hit, ALWAYS rebind `source.material_id` and `source.page_index` to the CURRENT task's legal `MaterialRef`s (matching by content hash / slot), so cached observations NEVER leak material IDs from another audit unit!
       - Calls `MessagesClient`, parses response via `parser.py`, records usage stats (`usage_summary()`), and returns `ObservationBatch`.
       - Also expose `annotate_target(...)` method for Stage 03b target-directed extraction using the same client/config/usage accounting.

3. **Comprehensive pytest suite (`tests/test_materials_and_gateway.py`)**:
   - Real Pillow PNG/JPEG image loading, invalid/traversal/symlink rejection, decompression/size limit checks.
   - Real `pypdfium2` multi-page PDF creation (generated in `tmp_path` via Pillow or minimal valid PDF bytes) and 1-based `page_index` rendering + out-of-range/bool `page_index` rejection.
   - Crop + inverse coordinate mapping accuracy (`map_crop_bbox_to_source`) and rejection of inverted/out-of-bounds/bool/NaN bboxes.
   - `ModelConfig` missing env var -> `ExecutionError("model_not_configured", ...)`; config JSON serialization never contains API key values.
   - Mocked `httpx.MockTransport` tests for `MessagesClient` and `ZCodeObservationGateway`:
     - Verify exact `/v1/messages` request structure (`model`, `max_tokens`, `system`, base64 image block, headers, no token in error/logs).
     - Verify transient 429/500 retry then success, and persistent timeout/500 -> `ExecutionError`.
     - Verify malformed JSON / truncated response / unknown fields / bad bbox handling (never fabricates coordinates or silently promotes `confirmed_missing` with `scope_complete=False`).
     - Verify separate entities/labels and per-entity `void_marked` preserved.
     - Verify extraction cache hit avoids second HTTP call AND rebinds `material_id`/`page_index` to the second audit unit's legal material IDs (never leaking the first unit's `material_id`!).
     - Verify `AuditController` end-to-end with `ZCodeObservationGateway` backed by `httpx.MockTransport` produces protocol-legal `prediction_semantic_errors == []` rows for both success (`pass`/`reject`/`ambiguous`) and gateway failure (`execution_status="error"`, `label=None`).

## Verification

Run:
- `uv run pytest tests/test_materials_and_gateway.py -v`
- `uv run pytest -q`

Report exact files added/changed, exact commands run, exact pytest counts, and honest remaining limitations. Stop after this stage.
