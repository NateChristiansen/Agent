"""`agent hook <name>` starts without the Claude SDK or the dispatcher. Each hook is a fresh interpreter, and on Windows the full CLI import cost about
0.93 s per hook before the hook read its stdin."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_cli import entry
from agent_cli.hooks import NAMES

# Records where the first heavy module came from, so a failure names the import to make lazy.
PROBE = """
import builtins, io, json, sys, traceback
sys.stdin = io.StringIO(sys.argv[2])
HEAVY = ("claude_agent_sdk", "mcp", "agent_cli.cli", "agent_cli.dispatch.supervisor",
         "agent_cli.dispatch.rules", "agent_cli.dispatch.options", "agent_cli.dispatch.lifecycle")
real, first = builtins.__import__, []
def tracing(name, *args, **kwargs):
    loaded = real(name, *args, **kwargs)
    if not first and any(m == h or m.startswith(h + ".") for m in sys.modules for h in HEAVY):
        first.append("".join(traceback.format_stack(limit=12)[:-1]))
    return loaded
builtins.__import__ = tracing
from agent_cli.entry import main
code = main(["hook", sys.argv[1]])
heavy = sorted(m for m in sys.modules if any(m == h or m.startswith(h + ".") for h in HEAVY))
print(json.dumps({"code": code, "heavy": heavy[:5], "first": first[0] if first else None}), file=sys.stderr)
"""

LIGHT_HOOKS = ["no-chaining", "activity", "manager-prompt", "capture", "commit-guard", "peer-stop"]


@pytest.mark.parametrize("name", LIGHT_HOOKS)
def test_an_ordinary_hook_loads_no_sdk_or_dispatcher(tmp_root: Path, name: str):
    payload = json.dumps({"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "ls"}})
    proc = subprocess.run(
        [sys.executable, "-c", PROBE, name, payload],
        cwd=tmp_root,
        env={**_env(), "AGENT_ROOT": str(tmp_root)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    report = json.loads(proc.stderr.strip().splitlines()[-1])
    assert report["code"] == 0, proc.stderr
    assert report["heavy"] == [], f"{report['heavy']} first imported from:\n{report['first']}"


def test_the_record_modules_hooks_read_load_no_sdk():
    """What a hook reaches when a dispatch or lease exists: `state`, `guards`, `opener` and
    the verification gate. Importing any of them used to load the whole package, and with it
    `supervisor` and the SDK. The package now loads those on first use."""
    code = (
        "import sys, agent_cli.dispatch.state, agent_cli.dispatch.guards, agent_cli.dispatch.opener, "
        "agent_cli.dispatch.verification_gate, agent_cli.dispatch.handoffs, agent_cli.dispatch.prompt, "
        "agent_cli.hooks.run, agent_cli.leases, agent_cli.peers\n"
        "print(sorted(m for m in sys.modules if m.startswith(('claude_agent_sdk', 'mcp'))))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], env=_env(), capture_output=True, text=True, timeout=60, check=True
    )
    assert proc.stdout.strip() == "[]"


def test_the_lazy_dispatch_names_still_resolve():
    from agent_cli import dispatch
    from agent_cli.dispatch import lifecycle, rules, supervisor

    assert dispatch.run is supervisor.run and dispatch.new is lifecycle.new
    assert dispatch.BASE_ALLOW is rules.BASE_ALLOW and dispatch.supervisor is supervisor
    # Both a submodule and a function; loading `supervisor` must not leave the module here.
    for name in ("preflight", "evaluate", "notify", "watch"):
        assert callable(getattr(dispatch, name)), name
    with pytest.raises(AttributeError):
        _ = dispatch.no_such_name


def _env() -> dict[str, str]:
    import os

    return {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}


def test_the_fast_path_prints_what_the_hook_says(tmp_root: Path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_ROOT", str(tmp_root))
    monkeypatch.setattr(
        "sys.stdin", _Stdin(json.dumps({"tool_name": "Bash", "tool_input": {"command": "a && b"}}))
    )
    assert entry.main(["hook", "no-chaining"]) == 0
    assert "deny" in capsys.readouterr().out


def test_a_byte_order_mark_on_stdin_does_not_empty_the_payload(tmp_root: Path):
    from agent_cli.hooks.run import run

    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "a && b"}})
    text, code = run(tmp_root, "no-chaining", chr(0xFEFF) + payload)
    assert code == 0 and "deny" in text


def test_a_removable_hooks_denial_names_its_switch(tmp_root: Path, monkeypatch):
    from agent_cli.hooks.run import HANDLERS, run

    block = json.dumps({"decision": "block", "reason": "rewrite it"})
    monkeypatch.setitem(HANDLERS, "thrash-guard", lambda _root, _payload: block)
    text, code = run(tmp_root, "thrash-guard", json.dumps({"hook_event_name": "Stop"}))
    reason = json.loads(text)["reason"]
    assert code == 0
    assert "hooks: {thrash-guard: false}" in reason
    assert "agent.local.yaml" in reason


def test_a_core_hooks_denial_says_it_is_protected(tmp_root: Path):
    from agent_cli.hooks.paths import PROTECTED_NOTE
    from agent_cli.hooks.run import run

    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "a && b"}})
    text, _ = run(tmp_root, "no-chaining", payload)
    reason = json.loads(text)["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason.endswith(PROTECTED_NOTE)
    assert "hooks:" not in reason.replace(PROTECTED_NOTE, "")


@pytest.mark.parametrize("name", ["thrash-guard", "no-chaining"])
def test_an_ask_gets_no_suffix(tmp_root: Path, monkeypatch, name: str):
    from agent_cli.hooks.paths import ask
    from agent_cli.hooks.run import HANDLERS, run

    monkeypatch.setitem(HANDLERS, name, lambda _root, _payload: ask("check first"))
    text, _ = run(tmp_root, name, "{}")
    assert json.loads(text)["hookSpecificOutput"]["permissionDecisionReason"] == "check first"


def test_a_user_prompt_submit_block_gets_no_note(tmp_root: Path, monkeypatch):
    from agent_cli.hooks.run import HANDLERS, run

    block = json.dumps({"decision": "block", "reason": "Peer wake already delivered."})
    monkeypatch.setitem(HANDLERS, "manager-prompt", lambda _root, _payload: block)
    text, _ = run(tmp_root, "manager-prompt", json.dumps({"hook_event_name": "UserPromptSubmit"}))
    assert json.loads(text) == {"decision": "block", "reason": "Peer wake already delivered."}


def test_a_peer_stop_block_gets_no_note(tmp_root: Path, monkeypatch):
    from agent_cli.hooks.run import HANDLERS, run

    block = json.dumps({"decision": "block", "reason": "A message from peer cd22."})
    monkeypatch.setitem(HANDLERS, "peer-stop", lambda _root, _payload: block)
    text, _ = run(tmp_root, "peer-stop", json.dumps({"hook_event_name": "Stop"}))
    assert json.loads(text) == {"decision": "block", "reason": "A message from peer cd22."}


def test_other_arguments_take_the_full_cli(monkeypatch):
    calls = []
    monkeypatch.setattr("agent_cli.cli.main", lambda argv=None: calls.append(argv) or 7)
    assert entry.main(["hook", "no-chaining", "--probe"]) == 7
    assert entry.main(["hook", "not-a-hook"]) == 7
    assert entry.main(["status", "check"]) == 7
    assert len(calls) == 3


def test_every_hook_name_is_known_to_the_fast_path():
    from agent_cli.hooks.run import HANDLERS

    assert set(NAMES) == set(HANDLERS)


class _Stdin:
    def __init__(self, text: str) -> None:
        self.text = text

    def read(self) -> str:
        return self.text
