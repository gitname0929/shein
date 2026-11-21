# Upstream architecture references

Resolved with `git ls-remote` on 2026-11-21. These identify architecture references, not dependencies or copied implementations. No upstream application has been installed or executed as part of this design investigation.

| Project | Ref | Observed commit | Use |
| --- | --- | --- | --- |
| yql210/ontology-driven-agent | refs/heads/master | a3151de94ae9407fcd6243c3b42c2f3f62c8555f | Four-layer separation, constraints before actions, capability dispatch and trace concepts |
| NVIDIA/auto-ontology | refs/heads/main | f3c310c7a279fcdfadf559d3fd7cd34e976d73e0 | Offline semantic compilation and bounded tool-loop concepts |

## Pinned paths

- [OntoAgent README](https://github.com/yql210/ontology-driven-agent/blob/a3151de94ae9407fcd6243c3b42c2f3f62c8555f/README.md)
- [OntoAgent domain model](https://github.com/yql210/ontology-driven-agent/blob/a3151de94ae9407fcd6243c3b42c2f3f62c8555f/src/ontoagent/domain/schema.py)
- [OntoAgent shape evaluator](https://github.com/yql210/ontology-driven-agent/blob/a3151de94ae9407fcd6243c3b42c2f3f62c8555f/src/ontoagent/execution/shape_evaluator.py)
- [OntoAgent decision fuser](https://github.com/yql210/ontology-driven-agent/blob/a3151de94ae9407fcd6243c3b42c2f3f62c8555f/src/ontoagent/execution/decision_fuser.py)
- [Auto Ontology semantic compilation](https://github.com/NVIDIA/auto-ontology/blob/f3c310c7a279fcdfadf559d3fd7cd34e976d73e0/auto_ontology/semantic/compile.py)
- [Auto Ontology SQL tool loop](https://github.com/NVIDIA/auto-ontology/blob/f3c310c7a279fcdfadf559d3fd7cd34e976d73e0/auto_ontology/retrieval/text_to_sql/react/loop.py)

## Deliberate differences

Our domain is independently scoped product evidence, not code modification or SQL question answering. Unknown observations do not become untriggered-success. Rule-specific conflict precedence does not use generic action severity fusion. Facts carry source material/page/region; runtime bindings carry exact rule/artifact identity. Database, vector-index, AST and NL2SQL services are not needed for this architecture.

OntoAgent declares MPL-2.0; Auto Ontology declares Apache-2.0. This project references architectural ideas. Any future source copying requires its own provenance and applicable notices; no license grant for the supplied challenge materials is invented here.
