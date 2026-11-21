# resources/

Read-only resource copies vendored into this repository so the project works
without the original competition package (`O2_AI商品审核官_资料包`). Nothing in
this directory is written at runtime; frozen inputs stay frozen and running
output / caches / SQLite live in `workspace/`.

## Layout and provenance

| Directory | Content | Copied from (competition package) |
| --- | --- | --- |
| `rules/` | 规则总则.md, 规则口径补充说明.md, 基础规则B001.md, 基础规则B001_3.0.json, 基础规则B001_3.1.json, 基础规则版本.jsonl, 规则目录.json, 进阶规则60条.md/.jsonl | `01_规则包/` |
| `protocol/` | input/output/annotation schemas, 执行与评分协议.md, 元数据字段字典.md, 产物目录哈希规范.md and other protocol docs, 校验工具/ public validator copies, hash_fixture/ + hash_fixture_expected.json | `02_协议与评分/` |
| `public_dev/` | 公开开发集 inputs/assets, 标注样例, metric_reference.py and reference outputs (labels) | `03_公开开发集/` |
| `学生赛题说明.md` | Root student specification | package root |

Copies preserve the original substructure. The vendored public validator code
also exists as the importable package `src/o2_public_contracts/` (byte-identical
copies; see its README for SHA256 provenance).

## Usage rules

- `resources/rules` and `resources/protocol` are reference material for
  humans and for loading rule/protocol definitions; they are never modified.
- `resources/public_dev` contains PUBLIC development data only. Reference
  labels (`audit_gold.jsonl`, `annotation_gold.jsonl`, ...) must NEVER be
  loaded by build/infer/annotate/iterate; evaluation runs separately
  (see AGENTS.md runtime invariants).
- Access paths are exposed by `o2_audit.resources` (`rules_dir()`,
  `protocol_dir()`, `public_dev_dir()`, `student_spec_path()`); they resolve
  relative to this repository, not the parent folder.
- The `protocol/hash_fixture` directory plus `hash_fixture_expected.json` are
  used by tests to verify the vendored artifact-hash implementation against
  the organizer's published fixture.
