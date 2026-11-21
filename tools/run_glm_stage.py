"""Run a bounded GLM coding stage using the user's existing ZCode gateway.

This is a development helper, not part of the competition inference runtime.
Credentials are inherited only through environment variables and never written.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt", type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    prompt = args.prompt.resolve().read_text(encoding="utf-8")
    executable = Path(os.environ.get("APPDATA", "")) / "npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
    if not executable.is_file():
        raise SystemExit("Claude executable not found; set up Claude Code before running this helper")
    model = "zcode/GLM-5.3-Flash"
    tools = ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "PowerShell"]
    agent = {
        "o2-worker": {
            "description": "Bounded O2 implementation worker supervised and reviewed by the parent assistant",
            "model": model,
            "prompt": "Read AGENTS.md. Implement only the assigned stage. Do not launch subagents, commit, push, expose credentials or claim unrun tests. Read-only source materials are in the parent folder. Use the repository as your working directory. Report files, checks actually run, results and limitations.",
            "tools": tools,
        }
    }
    command = [str(executable), "--print", "--setting-sources", "", "--disable-slash-commands", "--strict-mcp-config", "--no-session-persistence", "--model", model, "--agent", "o2-worker", "--agents", json.dumps(agent), "--tools", ",".join(tools), "--allowedTools", ",".join(tools), "--permission-mode", "dontAsk", "--output-format", "stream-json", "--verbose"]
    env = os.environ.copy()
    for key in ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION"):
        env.pop(key, None)
    output = root / "workspace/supervision" / args.name
    output.mkdir(parents=True, exist_ok=False)
    start = time.monotonic()
    with (output / "events.jsonl").open("w", encoding="utf-8") as stdout, (output / "stderr.log").open("w", encoding="utf-8") as stderr:
        process = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, text=True, encoding="utf-8")
        try:
            process.communicate(prompt, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            print(json.dumps({"stage": args.name, "status": "timeout", "log": str(output)}, ensure_ascii=True))
            return 124
    result = None
    for line in (output / "events.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("type") == "result":
            result = item
    summary = {"stage": args.name, "exit_code": process.returncode, "duration_s": round(time.monotonic() - start, 2), "log": str(output), "result": result}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=True, indent=2))
    return process.returncode or (1 if result is None or result.get("is_error") else 0)


if __name__ == "__main__":
    raise SystemExit(main())
