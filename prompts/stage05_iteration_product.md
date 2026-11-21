# Stage 05: safe iteration, persistent product and integration

Read AGENTS/TODO and existing code/docs. Complete the existing architecture; preserve and reuse build/infer/annotate/evaluation services. Scope: iteration/version services, jobs and FastAPI/Jinja product, CLI integration, focused tests and docs. Do not rewrite working rule semantics. No commits/push/nested agents; do not mark TODO or supervisor status done.

## Iteration

Implement exact iterate --base --feedback --input --out. Validate feedback schema and bound whole-baseline hash BEFORE writing output. baseline/input read-only; candidate new directory, parent identity, full decisions/report. Every packet gets accept/reject/defer with original rule citations, teacher source, reason and actual diff. Teacher text is a candidate, not authority. Reject incompatible thresholds, wrong conflict priority, ungrounded rule additions. Correct already-implemented advice may produce evidence-based no_change; do not implement only blanket defer.

Support a real bounded changed path: source-consistent extraction clarification or already-supported policy/config correction can create a rule-scoped frozen configuration/plan change with new artifact identity, dependencies and explainable diff. Do not execute teacher-generated code or change published rule meaning. Any accepted prompt/config change must actually be loaded by subsequent inference. If model judgment is used to parse proposals, validate it against allowed mutations and source evidence. Never read hidden protection/final labels; local repair inputs are unlabeled. Official promotion is organizer-owned; local UI gate reports must clearly be development reports.

Use existing evaluation gate/comparison only on explicit evaluation inputs. Keep baseline/candidate outputs separately. Active pointer update atomic; rollback restores actual artifact hashes/dependencies. Running jobs remain bound to initial selected artifact.

## Persistent tasks and web

Single FastAPI app + Jinja templates and minimal JS. No npm frontend framework. SQLite persistent jobs, worker queue/resume, idempotency keys and attempt history. Long calls run in background job worker, not request handler. Use a real job runner with states and handler registry, stop/restart semantics; do not silently rerun successful work. UI and CLI call the same functions. Safe material display only from registered run input roots/current evidence, not arbitrary filesystem paths.

Implement actual operational UI for P-TASK-01..16:
- import all60/deduplicate/line errors preserving valid records;
- single real build and resumable bulk builds, genuine counts/failed filters;
- run audit/annotate jobs with frozen artifact, evidence source page/crop and trace/rule quote;
- result comparison and JSONL/report export matching stored results;
- retry with one effective new attempt, process interruption recovery;
- candidate creation/activate/rollback actual pointer bindings;
- plugin enable/disable affects real invocation;
- restart preserves rules/jobs/results/artifacts/versions/plugin configs IDs.

UX: show five required entrypoints and five explanation fields. Bad rule/build/plugin failure each has object,cause,next action. Include rule management/build status/evidence/version comparison/plugin config views (can share page). Avoid fabricated placeholders or success counters. No references passed to inference. With no gold, call comparison prediction differences, not repairs/errors. With development refs, label as public evaluation only.

## Tests and documentation

Test API and persistent services via TestClient, real handlers/injected bounded gateways: imports/badrows/unique60, single+bulk build, taskretry/restart, evidence root traversal, plugin disabling, exports, candidate feedback wronghash/rejected incorrect advice/realchanged supported path/no_change, activation/rollback hash. Invoke actual local app for smoke if possible; report exact checks. Add README five CLI commands, configuration, web startup, queue processing/recovery, limitations and third-party sources. Clean checkout must work without original parent folder.

Stop after implementation and tests. Report actual outcomes and remaining gaps honestly; supervisor will perform live-app and full-system review.
