"""``o2-audit`` CLI entrypoint (Stage 02b).

Public adapter (protocol 1.3.1 signature):

    o2-audit build --rules RULES_JSONL --config CONFIG_JSON --out BUILD_DIR

Developer/lifecycle subcommands (workspace lives OUTSIDE frozen artifacts):

    o2-audit status
    o2-audit activate --name NAME
    o2-audit rollback --to NAME

``infer`` / ``annotate`` / ``iterate`` / ``evaluate`` are RESERVED exact CLI
names: they parse the protocol arguments but exit NON-ZERO with an explicit
"not yet implemented in Stage 02b" message — they never fake exit 0.

``build`` never reads reference labels: it consumes only the rules JSONL and
a build config JSON (evaluation runs separately).
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
from o2_audit.protocol.jsonio import ProtocolError, read_json_strict
from o2_audit.resources import resources_root
from o2_audit.storage.store import TaskStore

_NOT_IMPLEMENTED = "not yet implemented in Stage 02b"


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
    except (BuildError, ArtifactIntegrityError, ProtocolError, ValueError,
            KeyError, FileNotFoundError, OSError) as e:
        print(f"o2-audit: error: {e}", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# parsers
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="o2-audit",
        description="O2 product-review audit core (Stage 02b: build + lifecycle)")
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

    p_infer = sub.add_parser("infer", help="[reserved] infer --input --artifacts --out")
    _add_reserved_args(p_infer, single_input=True, artifacts=True)
    p_annotate = sub.add_parser("annotate", help="[reserved] annotate --input --targets --artifacts --out")
    _add_reserved_args(p_annotate, single_input=True, targets=True, artifacts=True)
    p_iterate = sub.add_parser("iterate", help="[reserved] iterate --base --feedback --input --out")
    _add_reserved_args(p_iterate, single_input=True, base=True, feedback=True)
    p_evaluate = sub.add_parser("evaluate", help="[reserved] evaluate --plugin --inputs --outputs --references --out")
    _add_reserved_args(p_evaluate, plugin=True, inputs_outputs=True, references=True)
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


def _cmd_not_implemented(args: argparse.Namespace) -> int:
    """Reserved protocol commands NEVER fake exit 0."""
    print(
        f"o2-audit {_NOT_IMPLEMENTED}: command {args.command_name!r} is "
        "reserved for later stages (materials/inference: Stage 03, "
        "evaluation: Stage 04). Exiting non-zero; no output files written.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
