"""Measure hook commands through native shells; this does not claim live client delivery.

Use a disposable clone with a synced venv. No session id is supplied, so deny probes do not
update real session state. JSON output checkpoints after each case survive an outer timeout.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import shlex
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .ci_pytest import stop_tree

DENY_PAYLOAD = {
    "hook_event_name": "PreToolUse",
    "tool_name": "Bash",
    "tool_input": {"command": "git status && git log -1"},
}
#: The command each client runs on every shell call: its whole PreToolUse
#: Bash group, so a sample is one tool call's guard cost, not one guard's.
BASH_GROUP = {"claude": "pre-bash", "codex": "codex-pre-bash"}
CLI_CODE = "from agent_cli.entry import main; raise SystemExit(main())"
VERSION_CODE = "import json,sys; print(json.dumps({'executable':sys.executable,'version':sys.version}))"


def quoted(value: str, shell: str) -> str:
    return "'" + value.replace("'", "''") + "'" if shell == "powershell" else shlex.quote(value)


def shell_environment(root: Path) -> dict[str, str]:
    """uv run prepends this venv: remove it from the system-python comparison."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_", "CODEX_", "AGENT_"))}
    env.pop("VIRTUAL_ENV", None)
    venv_paths = {(root / ".venv" / part).resolve() for part in ("bin", "Scripts")}
    env["PATH"] = os.pathsep.join(
        piece
        for piece in env.get("PATH", "").split(os.pathsep)
        if piece and Path(piece).resolve() not in venv_paths
    )
    return env


def shell_call(shell: str, command: list[str] | str, payload: Path | None) -> list[str]:
    executable = shutil.which("powershell.exe" if shell == "powershell" else "bash")
    if executable is None:
        raise FileNotFoundError(f"native {shell} is not available")
    if isinstance(command, list):
        text = " ".join(quoted(arg, shell) for arg in command)
        if shell == "powershell":
            text = "& " + text
    else:
        text = command  # managed command from the clone's trusted client config
    # Windows PowerShell 5.1 adds a BOM when it pipes managed text into a native command.
    # The caller supplies the same UTF-8 text on process stdin for PowerShell instead.
    if payload is not None and shell != "powershell":
        text += " < " + quoted(payload.as_posix(), shell)
    if shell == "powershell":
        # Windows PowerShell can inherit UTF-8 with a BOM from its parent. A BOM in
        # native stdin makes json.loads fail, and ordinary hooks then fail open.
        text = (
            "$OutputEncoding = [Text.UTF8Encoding]::new($false)\n"
            "[Console]::OutputEncoding = $OutputEncoding\n" + text
        )
        return [executable, "-NoProfile", "-NonInteractive", "-Command", text + "\nexit $LASTEXITCODE"]
    return [executable, "-c", text]


def sample(
    call: list[str], cwd: Path, env: dict[str, str], timeout: float, stdin: str | None = None
) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        process = subprocess.Popen(
            call,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if stdin is not None else None,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=os.name != "nt",
        )
        try:
            stdout, stderr = process.communicate(input=stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            stop_tree(process)
            stdout, stderr = process.communicate(timeout=5)
            return {
                "ms": (time.perf_counter() - start) * 1000,
                "exit_code": None,
                "error": "command timeout",
                "stdout": stdout,
                "stderr": stderr,
            }
        return {
            "ms": (time.perf_counter() - start) * 1000,
            "exit_code": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"ms": (time.perf_counter() - start) * 1000, "error": str(error), "exit_code": None}


def statistics_for(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "median_ms": statistics.median(ordered),
        "p95_ms": ordered[math.ceil(len(ordered) * 0.95) - 1],
        "max_ms": max(ordered),
    }


def validates(result: dict[str, Any], stage: str) -> bool:
    if result.get("exit_code") != 0:
        return False
    if stage == "guard":
        try:
            specific = json.loads(result["stdout"])["hookSpecificOutput"]
            return (
                specific.get("hookEventName") == "PreToolUse"
                and specific["permissionDecision"] == "deny"
                and specific["permissionDecisionReason"].startswith("chained commands are not allowed")
            )
        except ValueError, KeyError, TypeError:
            return False
    return True


def measure(
    call: list[str],
    cwd: Path,
    env: dict[str, str],
    count: int,
    timeout: float,
    stage: str,
    deadline: float = float("inf"),
    stdin: str | None = None,
) -> dict[str, Any]:
    if time.monotonic() >= deadline:
        return {"valid": False, "reason": "matrix deadline exceeded", "samples": []}
    cold = sample(call, cwd, env, min(timeout, max(0.1, deadline - time.monotonic())), stdin)
    result: dict[str, Any] = {"command": call, "cold": cold, "samples": [], "warmups": []}
    if not validates(cold, stage):
        return {
            **result,
            "valid": False,
            "reason": "cold probe failed; timings are not successful hook measurements",
        }
    for index in range(2 + count):
        if time.monotonic() >= deadline:
            return {**result, "valid": False, "reason": "matrix deadline exceeded"}
        item = sample(call, cwd, env, min(timeout, max(0.1, deadline - time.monotonic())), stdin)
        result["warmups" if index < 2 else "samples"].append(item)
        if not validates(item, stage):
            return {**result, "valid": False, "reason": "probe failed during repetition"}
    return {**result, "valid": True, **statistics_for([item["ms"] for item in result["samples"]])}


def run(root: Path, output: Path, count: int = 30, timeout: float = 20, budget: float = 2400) -> None:
    output.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    nested = output.resolve() / "cwd with spaces" / "nested"
    nested.mkdir(parents=True, exist_ok=True)
    # Output must be inside the clone: uv and the Node loader discover the project upward.
    if not nested.is_relative_to(root):
        raise ValueError("benchmark output must be inside the disposable clone")
    payload = output.resolve() / "deny payload.json"
    payload.write_text(json.dumps(DENY_PAYLOAD), encoding="utf-8")
    windows = sys.platform == "win32"
    python = root / (".venv/Scripts/python.exe" if windows else ".venv/bin/python")
    agent = root / (".venv/Scripts/agent.exe" if windows else ".venv/bin/agent")
    configs: dict[str, str] = {}
    for client, file in (("claude", ".claude/settings.json"), ("codex", ".codex/hooks.json")):
        data = json.loads((root / file).read_text(encoding="utf-8"))
        configs[client] = next(
            h["command"]
            for group in data["hooks"]["PreToolUse"]
            for h in group["hooks"]
            if h.get("command", "").endswith(f" agent hook {BASH_GROUP[client]}")
        )
    env = shell_environment(root)
    routes = {
        "python3": ["python3", "-c", CLI_CODE],
        "venv-python": [python.as_posix(), "-c", CLI_CODE],
        "agent-executable": [agent.as_posix()],
        "uv-no-sync": ["uv", "run", "--no-sync", "agent"],
    }
    report: dict[str, Any] = {
        "platform": platform.platform(),
        "python": sys.version,
        "revision": os.environ.get("GITHUB_SHA", "local"),
        "samples_per_case": count,
        "scope": "native shell commands; live Codex/Claude events and two-manager load remain unmeasured",
        "paths": {
            name: shutil.which(name, path=env["PATH"])
            for name in ("python3", "uv", "node", "bash", "powershell.exe")
        },
        "cases": {},
    }
    deadline = time.monotonic() + budget

    def save() -> None:
        (output / "launchers.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    save()
    for shell in ("bash", "powershell") if windows else ("bash",):
        for location, cwd in (("root", root), ("nested", nested)):
            for route, base in routes.items():
                interpreter = ["python3"] if route == "python3" else [python.as_posix()]
                stages = {
                    "startup": interpreter + ["-c", "pass"],
                    "import": interpreter + ["-c", "import agent_cli.cli"],
                    "version": interpreter + ["-c", VERSION_CODE],
                    "guard": base + ["hook", BASH_GROUP["claude"]],
                }
                # Interpreter startup/import applies to Python routes, not Node/uv executable startup.
                for stage, command in (
                    stages.items()
                    if route in ("python3", "venv-python")
                    else {"guard": stages["guard"]}.items()
                ):
                    key = f"{shell}/{location}/{route}/{stage}"
                    report["cases"][key] = measure(
                        shell_call(shell, command, payload if stage == "guard" else None),
                        cwd,
                        env,
                        count,
                        timeout,
                        stage,
                        deadline,
                        payload.read_text(encoding="utf-8")
                        if shell == "powershell" and stage == "guard"
                        else None,
                    )
                    save()
            for client, command in configs.items():
                environment = {**env, "CLAUDE_PROJECT_DIR": str(root)} if client == "claude" else env
                key = f"{shell}/{location}/configured-{client}/guard"
                report["cases"][key] = measure(
                    shell_call(shell, command, payload),
                    cwd,
                    environment,
                    count,
                    timeout,
                    "guard",
                    deadline,
                    payload.read_text(encoding="utf-8") if shell == "powershell" else None,
                )
                save()
    report["timed_out"] = time.monotonic() >= deadline
    save()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--budget", type=float, default=2400)
    args = parser.parse_args()
    if args.samples < 30 or args.timeout <= 0 or args.budget <= 0:
        parser.error("at least 30 samples and a positive timeout are required")
    run(args.root, args.output, args.samples, args.timeout, args.budget)


if __name__ == "__main__":
    main()
