# Architecture: four-layer division (Stage 01)

This is a clean-room challenge implementation. The layering is conceptually
inspired by OntoAgent's four-layer division (pinned references in
`docs/upstream_references.md`); no upstream code is reused or executed, and
the domain semantics below are challenge-specific (protocol 1.3.1, B001, the
规则总则), not code-governance semantics.

```
┌────────────────────────────────────────────────────────────────────┐
│ 4. Execution layer      runtime/controller.py (AuditController)    │
│    ObservationGateway -> ObservationBatch (entities+observations,  │
│    never labels)          TaskStore steps/checkpoints/attempts     │
├────────────────────────────────────────────────────────────────────┤
│ 3. Constraint/control   rules/b001/evaluate.py + controller        │
│    decision order, conflict priority, protocol validation          │
├────────────────────────────────────────────────────────────────────┤
│ 2. Intent/planning      controller._need_evidence                  │
│    applicability short-circuit, NeedEvidence gaps, bounded rounds  │
├────────────────────────────────────────────────────────────────────┤
│ 1. Semantic layer       domain/models.py, runtime/facts.py,        │
│                         rules/b001/plan.py, storage/store.py       │
└────────────────────────────────────────────────────────────────────┘
```

## Layer responsibilities

1. **Semantic layer.** Typed protocol shapes (`AuditInput`, `Entity`,
   `Observation`, `Evidence`), a `FactView` that accumulates immutable
   observations per (entity, predicate) without last-write-wins, versioned
   typed rule plans (`B001Plan` carrying thresholds, the material-number
   pattern and rule-text quotes), and SQLite persistence (transactions,
   steps/checkpoints, attempt history, run-isolated observations, artifact
   pointers).

2. **Intent/planning layer.** Checks applicability from task metadata FIRST;
   a known-false condition or unknown applicability short-circuits the whole
   cycle before any gateway call. When a verdict is blocked by resolvable
   evidence gaps (`NeedEvidence`), the layer emits permitted
   `AllowedAction.INSPECT_MATERIAL` requests with candidate material ids,
   under a strict `max_rounds` bound.

3. **Constraint/control layer.** Runs `evaluate_b001` and enforces the
   protocol decision order: applicability → B001 item-5 conflicts (which
   override ordinary violations → `ambiguous`) → proven violations
   (`reject`) → conclusion-affecting unknowns (`ambiguous`) → satisfied
   (`pass`). Every produced row is validated through the public
   `prediction_semantic_errors` before leaving the controller.

4. **Execution layer.** Invokes the `ObservationGateway` protocol, which
   returns `ObservationBatch(entities, observations)` ONLY — never final
   labels or compliance flags. Observations are recorded immutably in the
   store; completed steps are recovered by digest so a rerun does not
   duplicate gateway calls or final records. `ExecutionError` (tool/network
   failure) becomes a protocol-legal `execution_status="error"` row with
   `label=null` and an `error_code` — never an `ambiguous` verdict.

## Key invariants encoded here

- Raw transcription (`raw_value`) and business normalization
  (`normalized_value`) are separate; readable claims that disagree keep the
  first conflicting observation for conflict reporting.
- Missing (complete clear scope) / unreadable / incomplete inspection /
  identity-unknown / conflict / execution-failure are distinct statuses.
- Rule-specific parameters (thresholds, patterns, quoted rule text) live in
  versioned plans; shared operators (`o2_audit.operators`) are rule-ID
  independent.
- Fake gateways exist for tests only (`StaticObservationGateway`) and are
  documented as such; they are never presented as visual recognition.
- Baseline and candidate runs are isolated by `run_id` at the store level.

## Deliberate differences from upstream inspiration

Unknown observations do not become untriggered-success; rule-specific
conflict precedence is explicit in the rule evaluator rather than a generic
severity fusion; facts carry source material/page/region; runtime bindings
carry exact rule/artifact identity. Database, vector-index, AST and NL2SQL
services of upstream projects are out of scope.
