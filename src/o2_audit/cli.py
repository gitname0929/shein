"""``o2-audit`` CLI entrypoint (Stage 05).

All five official protocol 1.3.1 adapters (exact argument names):

    o2-audit build --rules RULES_JSONL --config CONFIG_JSON --out BUILD_DIR
    o2-audit infer --input INPUT_JSONL --artifacts BUILD_DIR --out PREDICTIONS_JSONL
    o2-audit annotate --input INPUT_JSONL --targets TARGETS_JSONL --artifacts BUILD_DIR --out ANNOTATIONS_JSONL
    o2-audit iterate --base BUILD_DIR --feedback FEEDBACK_JSON --input REPAIR_JSONL --out CANDIDATE_DIR
    o2-audit evaluate --plugin PLUGIN_ID --inputs INPUT_JSONL --outputs PREDICTIONS_JSONL --references REFERENCES_JSONL --out REPORT_JSON

Developer/lifecycle/product subcommands (workspace lives OUTSIDE frozen artifacts):

    o2-audit status
    o2-audit activate --name NAME
    o2-audit rollback --to NAME
    o2-audit compare --a NAME_A --b NAME_B
    o2-audit serve --host 127.0.0.1 --port 8000

``build`` / ``infer`` / ``annotate`` / ``iterate`` never read reference labels;
evaluation runs only inside the isolated ``o2-audit evaluate`` subprocess boundary.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from o2_audit.build.compiler import BuildError, compile_build
from o2_audit.build.lifecycle import (
    activate_version,
    compare_versions,
    rollback_to,
    version_status,
)
from o2_audit.build.loader import ArtifactIntegrityError
from o2_audit.domain.models import ExecutionError
from o2_audit.iterate.engine import IterationError, run_iteration
from o2_audit.protocol.jsonio import ProtocolError, read_json_strict, write_json_atomic
from o2_audit.resources import resources_root
from o2_audit.storage.store import TaskStore


def workspace_dir(explicit: str | None = None) -> Path:
    """Writable workspace (SQLite state) — env var, CLI flag, or repo default.
    NEVER inside a frozen artifact directory."""
    candidate = explicit or os.environ.get("O2_AUDIT_WORKSPACE_DIR")
    path = Path(candidate) if candidate else resources_root().parent / "workspace"
    path.mkdir(parents=True, exist_ok=True)
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 2
    try:
        return args.handler(args)
    except (BuildError, ArtifactIntegrityError, IterationError, ProtocolError,
            ExecutionError, ValueError, KeyError, FileNotFoundError, OSError) as e:
        print(f"o2-audit: error: {e}", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# parsers
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="o2-audit",
        description="O2 product-review ontology-driven audit CLI "
                    "(Protocol 1.3.1: build + infer + annotate + iterate + evaluate + lifecycle + serve)")
    parser.add_argument(
        "--workspace", default=None,
        help="writable workspace dir for SQLite state "
             "(default: ./workspace or $O2_AUDIT_WORKSPACE_DIR; never inside artifacts)")
    sub = parser.add_subparsers(dest="command")

    p_build = sub.add_parser("build", help="compile a frozen artifact build dir")
    p_build.add_argument("--rules", required=True, help="rules JSONL path")
    p_build.add_argument("--config", required=True, help="build config JSON path")
    p_build.add_argument("--out", required=True, help="BUILD_DIR to create (must not exist)")
    p_build.add_argument("--artifact-version", default=None,
                         help="override artifact_version (else config JSON)")
    p_build.add_argument("--version-name", default=None,
                         help="registry name for this version (else artifact_version)")
    p_build.add_argument("--force", action="store_true",
                         help="overwrite an existing non-empty BUILD_DIR")
    p_build.set_defaults(handler=_cmd_build)

    p_status = sub.add_parser("status", help="list artifact versions and active pointer")
    p_status.set_defaults(handler=_cmd_status)

    p_activate = sub.add_parser("activate", help="activate a recorded version (digest-verified)")
    p_activate.add_argument("--name", required=True)
    p_activate.set_defaults(handler=_cmd_activate)

    p_rollback = sub.add_parser("rollback", help="roll back to a recorded version (digest-verified)")
    p_rollback.add_argument("--to", required=True, dest="to_name")
    p_rollback.set_defaults(handler=_cmd_rollback)

    p_compare = sub.add_parser("compare", help="compare two recorded versions")
    p_compare.add_argument("--a", required=True)
    p_compare.add_argument("--b", required=True)
    p_compare.set_defaults(handler=_cmd_compare)

    p_infer = sub.add_parser(
        "infer", help="run inference over INPUT_JSONL with a frozen BUILD_DIR")
    p_infer.add_argument("--input", required=True)
    p_infer.add_argument("--artifacts", required=True)
    p_infer.add_argument("--out", required=True)
    p_infer.add_argument("--data-root", default=None,
                         help="data root for material relative paths "
                              "(default: INPUT_JSONL parent dir)")
    p_infer.add_argument("--model-config", default=None,
                         help="model config JSON (default: built-in env-based config)")
    _add_workspace_arg(p_infer)
    p_infer.add_argument("--run-id", default=None,
                         help="run identity (default: deterministic from artifact+input digest)")
    p_infer.set_defaults(handler=_cmd_infer)

    p_annotate = sub.add_parser(
        "annotate", help="target-directed annotation over INPUT_JSONL + TARGETS_JSONL")
    p_annotate.add_argument("--input", required=True)
    p_annotate.add_argument("--targets", required=True)
    p_annotate.add_argument("--artifacts", required=True)
    p_annotate.add_argument("--out", required=True)
    p_annotate.add_argument("--data-root", default=None,
                            help="data root for material relative paths "
                                 "(default: INPUT_JSONL parent dir)")
    p_annotate.add_argument("--model-config", default=None,
                            help="model config JSON (default: built-in env-based config)")
    _add_workspace_arg(p_annotate)
    p_annotate.add_argument("--run-id", default=None,
                            help="run identity (default: deterministic from artifact+input digest)")
    p_annotate.add_argument("--errors-out", default=None,
                            help="structured error records JSONL "
                                 "(default: sibling <out>.errors.jsonl)")
    p_annotate.set_defaults(handler=_cmd_annotate)
    p_iterate = sub.add_parser(
        "iterate",
        help="safely iterate a frozen BUILD_DIR from validated feedback + unlabeled repair input",
    )
    p_iterate.add_argument("--base", required=True, help="frozen baseline BUILD_DIR (read-only)")
    p_iterate.add_argument("--feedback", required=True, help="feedback packet JSON (schema 1.3.1)")
    p_iterate.add_argument("--input", required=True, help="unlabeled repair tasks JSONL")
    p_iterate.add_argument("--out", required=True, help="new candidate BUILD_DIR to create")
    p_iterate.add_argument("--artifact-version", default=None,
                           help="candidate artifact_version (default: <base>-iter-<feedback_id>)")
    p_iterate.add_argument("--version-name", default=None,
                           help="registry version name (default: candidate artifact_version)")
    p_iterate.add_argument("--force", action="store_true",
                           help="overwrite an existing non-empty CANDIDATE_DIR")
    _add_workspace_arg(p_iterate)
    p_iterate.set_defaults(handler=_cmd_iterate)

    p_evaluate = sub.add_parser(
        "evaluate",
        help="run an isolated evaluation plugin (protocol 1.3.1)",
    )
    p_evaluate.add_argument(
        "--plugin", required=True,
        help="registered plugin ID/alias (conclusion, evidence, version_compare) "
             "or direct module:function entry",
    )
    p_evaluate.add_argument("--inputs", required=True, help="audit inputs JSONL")
    p_evaluate.add_argument("--outputs", required=True, help="predictions JSONL")
    p_evaluate.add_argument(
        "--references", default=None,
        help="optional public/evaluation reference labels JSONL",
    )
    p_evaluate.add_argument("--out", required=True, help="output report JSON path")
    p_evaluate.add_argument(
        "--baseline-outputs", default=None,
        help="optional baseline predictions JSONL (for version_compare)",
    )
    p_evaluate.add_argument(
        "--timeout", type=float, default=None,
        help="per-plugin timeout seconds (overrides config / PLUGIN_TIMEOUT_SECONDS)",
    )
    p_evaluate.add_argument(
        "--purpose", default="local_evaluation",
        help="evaluation context purpose string (default: local_evaluation)",
    )
    p_evaluate.add_argument(
        "--expected-artifact-version", default=None,
        help="optional expected artifact_version to enforce during scoring",
    )
    p_evaluate.add_argument(
        "--plugin-config", default=None,
        help="optional plugin manifest JSON overriding bundled plugins_config.json",
    )
    p_evaluate.set_defaults(handler=_cmd_evaluate)

    p_serve = sub.add_parser(
        "serve",
        help="start the local FastAPI + Jinja operational product server",
    )
    p_serve.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    p_serve.add_argument("--port", type=int, default=8000, help="bind port (default: 8000)")
    _add_workspace_arg(p_serve)
    p_serve.set_defaults(handler=_cmd_serve)
    return parser


def _add_reserved_args(parser: argparse.ArgumentParser, *, single_input: bool = False,
                       artifacts: bool = False, targets: bool = False,
                       base: bool = False, feedback: bool = False,
                       plugin: bool = False, inputs_outputs: bool = False,
                       references: bool = False) -> None:
    if single_input:
        parser.add_argument("--input", default=None)
    if artifacts:
        parser.add_argument("--artifacts", default=None)
    parser.add_argument("--out", default=None)
    if targets:
        parser.add_argument("--targets", default=None)
    if base:
        parser.add_argument("--base", default=None)
    if feedback:
        parser.add_argument("--feedback", default=None)
    if plugin:
        parser.add_argument("--plugin", default=None)
    if inputs_outputs:
        parser.add_argument("--inputs", default=None)
        parser.add_argument("--outputs", default=None)
    if references:
        parser.add_argument("--references", default=None)
    parser.set_defaults(handler=_cmd_not_implemented, command_name=parser.prog)


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------

def _cmd_build(args: argparse.Namespace) -> int:
    config: dict[str, Any] = read_json_strict(args.config)
    artifact_version = args.artifact_version or config.get("artifact_version")
    if not isinstance(artifact_version, str) or not artifact_version.strip():
        raise ValueError(
            "config JSON must provide a non-empty 'artifact_version' "
            "(or pass --artifact-version)")
    store = TaskStore(Path(workspace_dir(args.workspace)) / "state.sqlite3")
    try:
        result = compile_build(
            rules_jsonl=args.rules, config=config, out_dir=args.out,
            artifact_version=artifact_version,
            store=store,
            version_name=args.version_name or config.get("version_name"),
            parent_name=config.get("parent_version"),
            overwrite=args.force,
        )
    finally:
        store.close()
    print(json.dumps(result.summary(), ensure_ascii=False, indent=2))
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    store = TaskStore(Path(workspace_dir(args.workspace)) / "state.sqlite3")
    try:
        status = version_status(store)
        for record in status["versions"]:
            overrides = store.manual_overrides_for(record["name"])
            record["manual_override_count"] = len(overrides)
    finally:
        store.close()
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


def _cmd_activate(args: argparse.Namespace) -> int:
    store = TaskStore(Path(workspace_dir(args.workspace)) / "state.sqlite3")
    try:
        result = activate_version(store, args.name)
    finally:
        store.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _cmd_rollback(args: argparse.Namespace) -> int:
    store = TaskStore(Path(workspace_dir(args.workspace)) / "state.sqlite3")
    try:
        result = rollback_to(store, args.to_name)
    finally:
        store.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    store = TaskStore(Path(workspace_dir(args.workspace)) / "state.sqlite3")
    try:
        result = compare_versions(store, args.a, args.b)
    finally:
        store.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _add_workspace_arg(parser: argparse.ArgumentParser) -> None:
    """Subparser ``--workspace`` with a distinct dest so the top-level
    ``--workspace`` value is preserved when the subcommand omits it."""
    parser.add_argument("--workspace", dest="workspace_sub", default=None,
                        help="writable workspace dir for SQLite state/traces "
                             "(default: ./workspace or $O2_AUDIT_WORKSPACE_DIR; "
                             "never inside artifacts)")


def _effective_workspace(args: argparse.Namespace) -> str | None:
    return args.workspace_sub or args.workspace


def _cmd_infer(args: argparse.Namespace) -> int:
    from o2_audit.runtime.infer import run_inference

    result = run_inference(
        input_jsonl=args.input,
        artifacts_dir=args.artifacts,
        out_path=args.out,
        data_root=args.data_root,
        model_config_path=args.model_config,
        workspace=_effective_workspace(args),
        run_id=args.run_id,
    )
    print(json.dumps(result.summary(), ensure_ascii=False, indent=2))
    return 0


def _cmd_annotate(args: argparse.Namespace) -> int:
    from o2_audit.runtime.annotate import run_annotation

    result = run_annotation(
        input_jsonl=args.input,
        targets_jsonl=args.targets,
        artifacts_dir=args.artifacts,
        out_path=args.out,
        data_root=args.data_root,
        model_config_path=args.model_config,
        workspace=_effective_workspace(args),
        run_id=args.run_id,
        errors_out=args.errors_out,
    )
    print(json.dumps(result.summary(), ensure_ascii=False, indent=2))
    return 0


def _cmd_evaluate(args: argparse.Namespace) -> int:
    # Isolated evaluation imports live strictly inside _cmd_evaluate so build/
    # infer/annotate paths never load reference/evaluation modules.
    from o2_audit.evaluation.manifest import SCHEMA_VERSION, load_plugin_registry
    from o2_audit.evaluation.runner import run_plugin
    from o2_public_contracts import contracts as public_contracts

    registry = load_plugin_registry(args.plugin_config)
    spec = registry.resolve(args.plugin)

    try:
        inputs = public_contracts.read_rows(args.inputs)
        outputs = public_contracts.read_rows(args.outputs)
        references = (
            public_contracts.read_rows(args.references)
            if args.references
            else []
        )
        baseline_outputs = (
            public_contracts.read_rows(args.baseline_outputs)
            if args.baseline_outputs
            else None
        )
    except public_contracts.ContractError as exc:
        raise ProtocolError(str(exc)) from exc

    context: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "purpose": args.purpose,
    }
    if args.expected_artifact_version:
        context["expected_artifact_version"] = args.expected_artifact_version
    if baseline_outputs is not None:
        context["baseline_outputs"] = baseline_outputs

    report = run_plugin(
        spec,
        inputs=inputs,
        outputs=outputs,
        references=references,
        context=context,
        timeout=args.timeout,
    )
    out_path = Path(args.out)
    write_json_atomic(out_path, report)
    print(json.dumps({
        "plugin_id": report.get("plugin_id"),
        "plugin_version": report.get("plugin_version"),
        "status": report.get("status", "error" if report.get("errors") else "ok"),
        "out": str(out_path),
        "metrics": report.get("metrics", {}),
        "errors": report.get("errors", []),
    }, ensure_ascii=False, indent=2))
    return 1 if report.get("errors") else 0


def _cmd_iterate(args: argparse.Namespace) -> int:
    ws = workspace_dir(_effective_workspace(args))
    store = TaskStore(ws / "state.sqlite3")
    try:
        result = run_iteration(
            base_dir=args.base,
            feedback_path=args.feedback,
            input_jsonl=args.input,
            out_dir=args.out,
            artifact_version=args.artifact_version,
            version_name=args.version_name,
            store=store,
            overwrite=args.force,
        )
    finally:
        store.close()
    print(json.dumps(result.summary(), ensure_ascii=False, indent=2))
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn
    from o2_audit.web.app import create_app

    ws = workspace_dir(_effective_workspace(args))
    app = create_app(workspace_dir=ws)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
