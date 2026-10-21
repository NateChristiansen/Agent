"""Shared fixtures and scenario-building helpers for the `test_dispatch_*` files
task 8: `test_dispatch.py` split by concern, no file over 500 lines).

Every split file does `from dispatch_helpers import (...)` for the names it needs. A helper
used by only one file stays local to that file instead (`_pr`/`_record` in
`test_dispatch_ledger.py`, `_precedents_d` in `test_dispatch_preflight.py`,
`_budget_envelope` in `test_dispatch_resume.py`, `_record_and_repo` in
`test_dispatch_callback.py`).
"""

from __future__ import annotations

import functools
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

import seeded
from agent_cli import dispatch, frontmatter, tidy
from agent_cli.core import AgentError
from agent_cli.dispatch import machine, verification_process

CONTRACT = Path(__file__).resolve().parent.parent / "harness" / "worker-contract.md"
PROMPT = "add a readme line\n\nsay what alpha is for, one line, then commit it"
DEAD_PID = 2**22  # far past any real pid. os.kill on it always raises ProcessLookupError
# The bare environment a child Python running dispatch code gets. Windows Python needs
# SYSTEMROOT to start, and the verifier finds Git Bash through git on PATH.
CHILD_ENV = (
    {"PATH": os.environ["PATH"], "SYSTEMROOT": os.environ["SYSTEMROOT"]}
    if sys.platform == "win32"
    else {"PATH": os.defpath}
)


def _seed_droot(root: Path, call: Any) -> None:
    """The worker contract and one open feature (alpha, T-42, two tasks), as `droot` holds."""
    (root / "harness").mkdir(exist_ok=True)
    (root / "harness" / "worker-contract.md").write_text(
        CONTRACT.read_text(encoding="utf-8"), encoding="utf-8"
    )
    call(["feature", "new", "alpha-work", "--title", "Alpha work", "--ticket", "T-42", "--repo", "alpha"])
    call(["task", "add", "1", "add the readme line"])
    call(["task", "add", "1", "run the tests"])
    plan = root / "features/001-alpha-work/plan.md"
    meta, body = frontmatter.load(plan)
    frontmatter.dump(
        plan, meta, body.replace("## Context\n", "## Context\n\nthe alpha service needs a readme line.\n")
    )
    _set_alpha_dispatch(
        root,
        # The command takes milliseconds; the budgets only bound a stalled worker under heavy
        # parallel load (verify speed, phase 4). Hang tests set their own tight bounds.
        verify=[{"command": "git diff --exit-code HEAD --", "timeout_seconds": 30}],
        verify_timeout_seconds=60,
    )
    # The first dispatch of every test claimed `mac` on the notes remote (about a second of git
    # fetch and push). The template claims it once; the claim's own tests build fresh roots.
    machine.require_claim(root, None)


def direct_capture(
    argv: list[str], cwd: Path, env: dict[str, str], timeout: float, limit: int = 8 * 1024 * 1024, **_: Any
) -> tuple[int, bytes]:
    """`verification_process.capture` without its `python -I` trampoline, which arms a timer that
    outlives a SIGKILLed parent. Every in-process git query of a finalize paid one interpreter
    start for it (about 18 per dispatch). Same bounds and errors; guardians still use the real one."""
    if timeout <= 0:
        raise AgentError("verification observation deadline expired")
    try:
        done = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise AgentError("verification observation timed out") from None
    if len(done.stdout) > limit:
        raise AgentError("verification observation exceeded its output bound")
    return done.returncode, done.stdout


@functools.cache
def direct_git() -> str | None:
    """The git binary behind macOS's `/usr/bin/git`, an xcrun shim that triples the cost of every
    call (about 22 ms against 7 ms). None elsewhere, or when git cannot say where it lives."""
    if sys.platform != "darwin":
        return None
    try:
        found = subprocess.run(["git", "--exec-path"], capture_output=True, text=True, check=True, timeout=30)
    except OSError, subprocess.SubprocessError:
        return None
    real = Path(found.stdout.strip()) / "git"
    return str(real) if os.access(real, os.X_OK) else None


@pytest.fixture(scope="session")
def _droot_template(
    tmp_path_factory: pytest.TempPathFactory, _root_template: Path, _notes_template: Path
) -> Path:
    """`droot`'s seeded tree, built once per worker (verify speed, phase 2): the seeding cost a
    feature claim on the notes remote, task adds and STATUS renders for every test."""
    base = tmp_path_factory.mktemp("droot-template")
    root = seeded.base_root(base, _root_template, _notes_template)
    with seeded.seeding(root) as call:
        _seed_droot(root, call)
    return base


@pytest.fixture
def droot(
    tmp_root: Path,
    notes_remote: Path,
    run,
    fake_claude,
    _droot_template: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """tmp_root plus the worker contract and one open feature (alpha, T-42, two tasks). The
    notes remote is there because `feature new` claimed its id on it; the seeded tree is a copy
    of a session template (`seeded.py`), with its own remotes."""
    if sys.platform != "win32":
        monkeypatch.setattr(verification_process, "capture", direct_capture)
    if git := direct_git():
        monkeypatch.setattr(verification_process, "system_git", lambda: git)
    # Every cleanup hold check ran `lsof` over all of the owner's processes (0.2 s idle, seconds
    # under load), and no dispatch test starts a process inside a tree it cleans. The scan
    # itself is `test_tidy.py`'s subject.
    monkeypatch.setattr(tidy, "process_cwds", lambda: [])
    # A test that clears AGENT_NOW (notice_helpers' `wired`) turns tabs on, and its finalize then
    # starts the detached tab drainer and snapshot watchdog, which outlive the test. pytest reuses
    # a passed test's tmp_path name, so they wrote `.agent/tabs/eval.lock` into the next test's
    # tree while `install` emptied it.
    monkeypatch.setenv("AGENT_NO_TABS", "1")
    seeded.install(_droot_template, tmp_root.parent)
    os.chdir(tmp_root)  # the install replaced the directory tmp_root chdir'd into
    return tmp_root


def envelope(
    status: str = "done", *, nonce: str = "{{NONCE}}", cost: float = 0.42, turns: int = 5, **over: Any
) -> dict[str, Any]:
    """One CLI-shaped `result` record, the wire form the fake transport emits and the SDK
    parses into a ResultMessage. `duration_ms`/`duration_api_ms` are required by the SDK's
    parser even though nothing here reads them."""
    ret = {
        "nonce": nonce,
        "status": status,
        "summary": "added the line",
        "open_questions": [],
        "next_steps": ["push and open the PR"],
        "notes": "",
    }
    ret.update(over)
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 12,
        "duration_api_ms": 9,
        "session_id": "{{SESSION}}",
        "total_cost_usd": cost,
        "num_turns": turns,
        "result": "ok",
        "terminal_reason": "completed",
        "structured_output": ret,
    }


def worker_return(d: dict[str, Any]) -> dict[str, Any]:
    """The worker's latest return as `evaluate` reads it, wherever the record keeps it."""
    ret = dispatch.state.envelope_return(d, d["last_result"])
    assert ret is not None
    return ret


def says(text: str, *tools: str) -> dict[str, Any]:
    """An assistant message, for the turns that need something before the result."""
    blocks: list[dict] = [{"type": "text", "text": text}]
    blocks += [{"type": "tool_use", "id": f"t{i}", "name": name, "input": {}} for i, name in enumerate(tools)]
    return {"type": "assistant", "message": {"role": "assistant", "model": "sonnet", "content": blocks}}


def turn(*messages: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return {"messages": list(messages), **kw}


def one(*messages: dict[str, Any], **kw: Any) -> dict[str, Any]:
    """One attempt, one turn: a single result and nothing else, which covers most scenarios."""
    return {"turns": [turn(*messages, **kw)]}


def _new(root: Path, **kw: Any) -> dict[str, Any]:
    kw.setdefault("repo", "alpha")
    kw.setdefault("feature", "1")
    kw.setdefault("prompt", PROMPT)
    kw.setdefault("watch", True)
    return dispatch.new(root, **kw)


def _new_fast(root: Path, **kw: Any) -> dict[str, Any]:
    """`_new` with the supervisor's verification gate exempted.

    `droot` gives every dispatch a `verify=` config, so a `done` outcome runs `gate.execute`,
    which spawns a guardian, a command process and a watchdog: three cold `python -I`
    interpreters importing `agent_cli` at ~0.59s each, on every test that finishes a dispatch.
    Most of those tests are about worktree reuse, prompt composition, the env allowlist or branch
    naming, and the verifier is incidental to all of them (2026-09-24, measured).

    Use this ONLY where verification is not the subject. `_new` stays production-faithful on
    purpose: `test_dispatch_records.py` and `test_dispatch_ledger.py` have required-check
    behaviour as the condition under test, so switching their default would quietly change what
    they assert rather than speed them up. Same trap at any call site that reads verification
    status or exemption off the record — if the test would still pass with its assertion deleted,
    it has been broken, not sped up.
    """
    kw.setdefault("no_checks", True)
    return _new(root, **kw)


def _set_alpha_dispatch(root: Path, **fields: Any) -> None:
    reg = root / "agent.yaml"
    data = yaml.safe_load(reg.read_text(encoding="utf-8"))
    alpha = next(r for r in data["repos"] if r["short"] == "alpha")
    alpha.setdefault("dispatch", {}).update({k: v for k, v in fields.items() if k != "worktrees_dir"})
    if "worktrees_dir" in fields:
        alpha["worktrees_dir"] = fields["worktrees_dir"]
    reg.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _options(fake, n: int = 0):
    """The ClaudeAgentOptions the supervisor built for attempt n: the successor to the argv
    every one of these tests used to read."""
    return fake.calls()[n]["options"]


def _out(root: Path, dispatch_id: str = "001-mac-01") -> str:
    """The supervisor's own record of the connection, one JSON line per SDK message. Tests
    watch it to know the worker has got somewhere before steering it."""
    path = dispatch.file(root, dispatch_id, ".out")
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def _steer_when(root: Path, marker: str, message: str) -> threading.Thread:
    """Send `message` once `marker` shows up in the worker's output. Steering before the
    worker has said anything would test the inbox, not the steer."""

    def run() -> None:
        for _ in range(500):
            if marker in _out(root) and dispatch.load(root, "001-mac-01")["state"] == "running":
                dispatch.say(root, dispatch_id="001-mac-01", message=message)
                return
            time.sleep(0.02)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _log(root: Path) -> str:
    return (root / "features/001-alpha-work/log.md").read_text(encoding="utf-8")


def _plan(root: Path) -> str:
    return (root / "features/001-alpha-work/plan.md").read_text(encoding="utf-8")


def malformed_record(**extra: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": "999-01",
        "feature": 999,
        "repo": "web-app",
        "short": "web",
        "state": "done",
        "attempt": 1,
        "nonce": "999-01-abcd",
        "branch": "agent/999-x",
        "base": "origin/main",
        "worktree": "/nonexistent/wt",
        "handoff": "features/999-x/handoffs/01-web.md",
        "budget_usd": 5.0,
        "cost_usd": 1.0,
    }
    d.update(extra)
    return d


def options_record(root: Path) -> dict[str, Any]:
    return {
        "id": "001-01",
        "feature": 1,
        "feature_dir": "features/001-alpha-work",
        "worktree": str(root / "future-worktree"),
        "budget_usd": 1,
        "session_id": "00000000-0000-0000-0000-000000000000",
    }
