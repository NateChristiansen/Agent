"""One hook process per client event and matcher.

A group runs its member hooks in-process, each exactly as it ran alone, and combines what they
said. These tests pin the combiner's rules, the runner's isolation (order, early stop, failures,
switches, budgets, tool filters) and the wiring in both client configs.
"""

from __future__ import annotations

import importlib
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from agent_cli import entry, sessions, tab_drain
from agent_cli.hooks import groups, toggles
from agent_cli.hooks.groups import GROUPS, Group, Member, combine, matches, wired
from agent_cli.hooks.paths import PROTECTED_NOTE, ask, decision, deny

# `agent_cli.hooks.run` is also the name of the re-exported function; take the module.
run_mod = importlib.import_module("agent_cli.hooks.run")
ROOT = Path(__file__).resolve().parents[1]


def _context(event: str, text: str) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}})


def _block(reason: str) -> str:
    return json.dumps({"decision": "block", "reason": reason})


# --- combine ---------------------------------------------------------------------------------


def test_nothing_said_is_nothing():
    assert combine("PreToolUse", ["", "", ""]) == ""
    assert combine("Stop", []) == ""


def test_a_lone_output_is_returned_byte_for_byte():
    lone = '{"hookSpecificOutput":  {"hookEventName": "SessionStart", "additionalContext": "x"}}'
    assert combine("SessionStart", ["", lone, ""]) == lone


def test_the_first_deny_wins_verbatim_over_every_other_answer():
    first, second = deny("first"), deny("second")
    outputs = [_context("PreToolUse", "note"), decision("allow", "ok"), first, ask("check"), second]
    assert combine("PreToolUse", outputs) == first


def test_an_ask_wins_when_nothing_denies_and_loses_to_a_later_deny():
    asked = ask("owner's ok")
    assert combine("PreToolUse", [_context("PreToolUse", "note"), asked]) == asked
    denied = deny("no")
    assert combine("PreToolUse", [asked, denied]) == denied


def test_an_allow_keeps_its_reason_and_gains_the_context_of_the_others():
    out = json.loads(
        combine("PreToolUse", [_context("PreToolUse", "server is stale"), decision("allow", "plan mode ok")])
    )
    assert out == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": "plan mode ok",
            "additionalContext": "server is stale",
        }
    }


def test_contexts_are_joined_in_member_order():
    out = combine(
        "UserPromptSubmit",
        [_context("UserPromptSubmit", "inbox"), "", _context("UserPromptSubmit", "journal")],
    )
    assert json.loads(out)["hookSpecificOutput"] == {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "inbox\n\njournal",
    }


def test_other_fields_keep_the_first_members_value():
    title = json.dumps(
        {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "sessionTitle": "mgr-045-x"}}
    )
    out = json.loads(combine("UserPromptSubmit", [_context("UserPromptSubmit", "inbox"), title]))
    assert out["hookSpecificOutput"]["sessionTitle"] == "mgr-045-x"
    assert out["hookSpecificOutput"]["additionalContext"] == "inbox"


def test_two_stop_blocks_carry_both_reasons():
    out = json.loads(combine("Stop", [_block("peer message"), "", _block("second reason")]))
    assert out == {"decision": "block", "reason": "peer message\n\nsecond reason"}


def test_a_prompt_block_is_returned_verbatim_and_drops_the_context():
    stale = _block("Peer wake already delivered.")
    assert combine("UserPromptSubmit", [_context("UserPromptSubmit", "journal"), stale]) == stale


def test_plain_text_counts_as_context():
    out = json.loads(combine("SessionStart", ["plain words\n", _context("SessionStart", "json words")]))
    assert out["hookSpecificOutput"]["additionalContext"] == "plain words\n\njson words"


@pytest.mark.parametrize(
    ("matcher", "tool", "hit"),
    [
        (None, "Bash", True),
        ("*", "Read", True),
        (".*", "mcp__agent__peer", True),
        ("Bash", "Bash", True),
        ("Bash", "BashOutput", False),
        ("Edit|Write", "NotebookEdit", False),
        ("Edit|Write", "Write", True),
        ("mcp__agent__.*", "mcp__agent__peer", True),
        ("mcp__agent__(peer|dispatch)", "mcp__agent__dispatch", True),
        ("mcp__agent__(peer|dispatch)", "mcp__agent__feature_new", False),
    ],
)
def test_matchers_read_as_claude_reads_them(matcher, tool, hit):
    assert matches(matcher, tool) is hit


# --- run_group -------------------------------------------------------------------------------


def _group(event: str, *members: Member, matcher: str | None = None) -> Group:
    return Group("test-group", "claude", event, matcher, members)


@pytest.fixture
def calls(monkeypatch) -> list[str]:
    """The names of the handlers `_install` replaced, in the order they ran."""
    seen: list[str] = []
    return seen


def _recorder(seen: list[str], name: str, out: str = ""):
    def handler(_root, payload):
        seen.append(name)
        return out

    return handler


def _install(monkeypatch, seen: list[str], **outs: str) -> None:
    for name, out in outs.items():
        monkeypatch.setitem(
            run_mod.HANDLERS, name.replace("_", "-"), _recorder(seen, name.replace("_", "-"), out)
        )


def test_members_run_in_order_and_a_deny_stops_the_rest(tmp_root, monkeypatch, calls):
    _install(monkeypatch, calls, verify_pipe="", push_guard=deny("unreviewed"), commit_guard="")
    group = _group("PreToolUse", Member("verify-pipe", 5), Member("push-guard", 5), Member("commit-guard", 5))
    out = run_mod.run_group(tmp_root, group, {"hook_event_name": "PreToolUse", "tool_name": "Bash"})
    assert calls == ["verify-pipe", "push-guard"]
    assert json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] == f"unreviewed {PROTECTED_NOTE}"


def test_a_prompt_block_stops_the_rest(tmp_root, monkeypatch, calls):
    _install(
        monkeypatch, calls, capture="", manager_prompt=_block("Peer wake already delivered."), session_name=""
    )
    group = _group(
        "UserPromptSubmit", Member("capture", 5), Member("manager-prompt", 5), Member("session-name", 5)
    )
    out = run_mod.run_group(tmp_root, group, {"hook_event_name": "UserPromptSubmit"})
    assert calls == ["capture", "manager-prompt"]
    assert json.loads(out) == {"decision": "block", "reason": "Peer wake already delivered."}


def test_a_stop_block_lets_the_rest_run(tmp_root, monkeypatch, calls):
    _install(monkeypatch, calls, peer_stop=_block("a peer message"), lease_yield="", activity="")
    group = _group("Stop", Member("peer-stop", 5), Member("lease-yield", 5), Member("activity", 5))
    out = run_mod.run_group(tmp_root, group, {"hook_event_name": "Stop"})
    assert calls == ["peer-stop", "lease-yield", "activity"]
    assert json.loads(out) == {"decision": "block", "reason": "a peer message"}


@pytest.mark.parametrize(("peer_stop", "state"), [(_block("a peer message"), "working"), ("", "idle")])
def test_a_blocked_stop_leaves_the_session_working(tmp_root, monkeypatch, peer_stop, state):
    """A blocked Stop continues the turn, so `activity` must not record idle for it."""
    monkeypatch.setitem(run_mod.HANDLERS, "peer-stop", lambda _root, _payload: peer_stop)
    monkeypatch.setattr(tab_drain, "request", lambda *_a, **_kw: {"started": True})
    sessions.set_record(tmp_root, "s1", pid=4242, name="mgr-009-x")
    sessions.set_field(tmp_root, "s1", "activity", {"state": "working"})
    payload = json.dumps({"session_id": "s1", "hook_event_name": "Stop"})
    out, code = run_mod.run(tmp_root, "stop", payload)
    assert code == 0
    assert (json.loads(out).get("decision") if out else None) == ("block" if peer_stop else None)
    assert (sessions.get(tmp_root, "s1") or {})["activity"]["state"] == state


def test_a_failing_member_fails_open_alone_and_is_logged(tmp_root, monkeypatch, calls):
    logged: list[tuple[str, str]] = []
    monkeypatch.setattr(
        run_mod.decision_log, "error", lambda _r, name, _p, exc, _s: logged.append((name, type(exc).__name__))
    )

    def broken(_root, _payload):
        calls.append("verify-pipe")
        raise RuntimeError("boom")

    monkeypatch.setitem(run_mod.HANDLERS, "verify-pipe", broken)
    _install(monkeypatch, calls, cli_route=deny("use the tool"))
    group = _group("PreToolUse", Member("verify-pipe", 5), Member("cli-route", 5))
    out = run_mod.run_group(tmp_root, group, {"hook_event_name": "PreToolUse", "tool_name": "Bash"})
    assert calls == ["verify-pipe", "cli-route"]
    assert logged == [("verify-pipe", "RuntimeError")]
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_switched_off_member_stays_off_inside_its_group(tmp_root, monkeypatch, calls):
    monkeypatch.setattr(toggles, "settings", lambda _root: {"thrash-guard": False})
    _install(monkeypatch, calls, thrash_guard=_context("PostToolUse", "thrash"), activity="")
    out = run_mod.run_group(
        tmp_root, GROUPS["post-tool"], {"hook_event_name": "PostToolUse", "tool_name": "Bash"}
    )
    assert calls == ["activity"] and out == ""


@pytest.mark.parametrize(
    ("tool", "expected"),
    [
        ("Read", ["activity"]),
        ("Bash", ["thrash-guard", "activity"]),
        ("Edit", ["render-after-plan-edit", "thrash-guard", "activity"]),
        ("NotebookEdit", ["activity"]),
    ],
)
def test_a_member_keeps_its_old_narrower_matcher(tmp_root, monkeypatch, calls, tool, expected):
    _install(monkeypatch, calls, render_after_plan_edit="", thrash_guard="", activity="")
    run_mod.run_group(tmp_root, GROUPS["post-tool"], {"hook_event_name": "PostToolUse", "tool_name": tool})
    assert calls == expected


def test_a_raising_member_fails_open_alone_and_the_others_answer(tmp_root, monkeypatch, calls):
    """Members run in one thread, to completion (Codex review, phase 7: a timed-out thread kept
    mutating shared state). One that raises costs only its own answer."""

    def broken(_root, _payload):
        raise RuntimeError("boom")

    monkeypatch.setitem(run_mod.HANDLERS, "push-guard", broken)
    _install(monkeypatch, calls, cli_route=deny("use the tool"))
    group = _group("PreToolUse", Member("push-guard", 5), Member("cli-route", 5))
    out = run_mod.run_group(tmp_root, group, {"hook_event_name": "PreToolUse", "tool_name": "Bash"})
    assert json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"].startswith("use the tool")
    assert not any(t.name.startswith("hook ") for t in threading.enumerate())


def test_each_member_gets_its_own_payload(tmp_root, monkeypatch, calls):
    def mutating(_root, payload: dict[str, Any]):
        payload["tool_input"]["command"] = "rewritten"
        return ""

    def reader(_root, payload: dict[str, Any]):
        calls.append(payload["tool_input"]["command"])
        return ""

    monkeypatch.setitem(run_mod.HANDLERS, "verify-pipe", mutating)
    monkeypatch.setitem(run_mod.HANDLERS, "cli-route", reader)
    group = _group("PreToolUse", Member("verify-pipe", 5), Member("cli-route", 5))
    run_mod.run_group(
        tmp_root,
        group,
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}},
    )
    assert calls == ["ls"]


@pytest.mark.parametrize("group", ["pre-bash", "codex-pre-bash"])
def test_a_shell_group_answers_what_its_guard_answers_alone(tmp_root, group):
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git status && git log"},
        }
    )
    grouped, code = run_mod.run(tmp_root, group, payload)
    alone, _ = run_mod.run(tmp_root, "no-chaining", payload)
    assert code == 0 and grouped == alone
    assert json.loads(grouped)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("group", ["pre-bash", "codex-pre-bash"])
def test_a_harmless_command_passes_every_shell_guard_silently(tmp_root, group):
    payload = json.dumps(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}}
    )
    assert run_mod.run(tmp_root, group, payload) == ("", 0)


def test_the_fast_path_runs_a_group(tmp_root, monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.stdin",
        _Stdin(
            json.dumps(
                {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "a && b"}}
            )
        ),
    )
    assert entry.main(["hook", "pre-bash"]) == 0
    assert '"deny"' in capsys.readouterr().out


def test_group_names_are_not_hook_names():
    assert not set(groups.NAMES) & set(run_mod.NAMES)
    for group in GROUPS.values():
        assert len(group.members) > 1, f"{group.name}: a lone hook is wired by its own name"
        for member in group.members:
            assert member.name in run_mod.HANDLERS, member.name


# --- wiring ----------------------------------------------------------------------------------

CONFIGS = {"claude": ROOT / ".claude/settings.json", "codex": ROOT / ".codex/hooks.json"}
SHELL_GUARDS = [
    "no-chaining",
    "verify-pipe",
    "push-guard",
    "commit-guard",
    "pattern-kill-guard",
    "sandbox-help-guard",
    "subagent-revert-guard",
    "cli-route",
]
#: What ran on each event and tool before phase 7, one process per name. Order matters only
#: inside a group: the clients start separate commands in parallel.
BEFORE: dict[str, dict[tuple[str, str | None], list[str]]] = {
    "claude": {
        ("SessionStart", None): ["session-start", "capture", "activity"],
        ("UserPromptSubmit", None): [
            "capture",
            "manager-prompt",
            "journal-pending",
            "session-name",
            "notes-sync",
        ],
        ("Stop", None): ["capture", "peer-stop", "notes-sync", "lease-yield", "activity"],
        ("SessionEnd", None): ["capture", "activity", "session-reap"],
        ("SubagentStop", None): ["capture"],
        ("PermissionRequest", "Bash"): ["activity"],
        ("PreToolUse", "Bash"): ["protect-generated", *SHELL_GUARDS],
        ("PreToolUse", "Edit"): ["protect-generated"],
        ("PreToolUse", "Write"): ["protect-generated"],
        ("PreToolUse", "NotebookEdit"): ["protect-generated"],
        ("PreToolUse", "Read"): ["pull-warn"],
        ("PreToolUse", "Grep"): ["pull-warn"],
        ("PreToolUse", "Glob"): ["pull-warn"],
        ("PreToolUse", "ExitPlanMode"): ["plan-gate", "activity"],
        ("PreToolUse", "AskUserQuestion"): ["activity"],
        ("PreToolUse", "SendMessage"): ["peer-route"],
        ("PreToolUse", "mcp__agent__peer"): ["mcp-stale", "plan-mode-allow"],
        ("PreToolUse", "mcp__agent__dispatch"): ["mcp-stale", "plan-mode-allow"],
        ("PreToolUse", "mcp__agent__feature_new"): ["mcp-stale"],
        ("PreToolUse", "TodoWrite"): [],
        ("PostToolUse", "Bash"): ["thrash-guard", "activity"],
        ("PostToolUse", "Edit"): ["render-after-plan-edit", "thrash-guard", "activity"],
        ("PostToolUse", "Write"): ["render-after-plan-edit", "thrash-guard", "activity"],
        ("PostToolUse", "NotebookEdit"): ["activity"],
        ("PostToolUse", "Read"): ["activity"],
        ("PostToolUseFailure", "Bash"): ["thrash-guard"],
        ("PermissionDenied", "Bash"): ["thrash-guard"],
    },
    "codex": {
        ("SessionStart", None): ["session-start", "capture", "codex-activity"],
        ("UserPromptSubmit", None): ["capture", "codex-prompt", "journal-pending", "notes-sync"],
        ("Stop", None): ["capture", "notes-sync", "lease-yield", "codex-activity"],
        ("SessionEnd", None): ["capture", "codex-activity", "session-reap"],
        ("SubagentStop", None): ["capture"],
        ("PermissionRequest", "Bash"): ["codex-activity"],
        ("PreToolUse", "Bash"): [*SHELL_GUARDS, "protect-generated"],
        ("PreToolUse", "apply_patch"): ["protect-generated"],
        ("PreToolUse", "mcp__agent__peer"): ["mcp-stale"],
        ("PreToolUse", "request_user_input"): ["codex-activity"],
        ("PreToolUse", "ExitPlanMode"): ["codex-activity"],
        ("PostToolUse", "Bash"): ["codex-activity"],
        # A Codex plan patch re-renders, as Claude's Edit/Write does
        ("PostToolUse", "apply_patch"): ["codex-activity", "render-after-plan-edit"],
    },
}
#: Members a group gained after phase 7; `test_ask_guard` pins where they run.
ADDED = {"ask-guard"}


def _config(client: str) -> dict[str, Any]:
    return json.loads(CONFIGS[client].read_text(encoding="utf-8"))


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_grouping_keeps_every_hook_on_its_event_and_tool(client: str):
    config = _config(client)
    for (event, tool), before in BEFORE[client].items():
        after = [n for n in wired(config, event, tool) if n not in ADDED]
        assert sorted(after) == sorted(before), (event, tool)
        for group in (g for g in GROUPS.values() if g.client == client and g.event == event):
            inside = [m.name for m in group.members if m.name in after]
            assert inside == [n for n in before if n in inside], (event, tool, group.name)


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_every_wired_command_names_a_hook_or_a_group(client: str):
    for rows in _config(client)["hooks"].values():
        for row in rows:
            for hook in row["hooks"]:
                name = groups.command_name(hook["command"])
                assert name in run_mod.NAMES or name in GROUPS, hook["command"]


@pytest.mark.parametrize("group", sorted(GROUPS))
def test_each_group_is_wired_once_where_it_belongs_with_a_timeout_that_covers_its_members(group: str):
    spec = GROUPS[group]
    hits = [
        (client, event, row, hook)
        for client in CONFIGS
        for event, rows in _config(client)["hooks"].items()
        for row in rows
        for hook in row["hooks"]
        if groups.command_name(hook["command"]) == group
    ]
    assert len(hits) == 1
    client, event, row, hook = hits[0]
    assert (client, event) == (spec.client, spec.event)
    assert row.get("matcher") == spec.matcher
    assert hook["timeout"] == spec.timeout >= sum(m.budget for m in spec.members) + groups.STARTUP_S
    if spec.client == "codex":  # Codex's own default, which each member had with no timeout set
        assert spec.timeout == groups.CODEX_DEFAULT_S


@pytest.mark.parametrize(
    ("client", "tools"),
    [
        (
            "claude",
            [
                "Bash",
                "Edit",
                "Write",
                "Read",
                "ExitPlanMode",
                "AskUserQuestion",
                "mcp__agent__peer",
                "WebFetch",
            ],
        ),
        ("codex", ["Bash", "apply_patch", "mcp__agent__peer", "request_user_input", "WebFetch"]),
    ],
)
def test_one_process_per_tool_call(client: str, tools: list[str]):
    config = _config(client)
    for event in ("PreToolUse", "PostToolUse"):
        for tool in tools:
            commands = [
                hook
                for entry in config["hooks"].get(event, [])
                if matches(entry.get("matcher"), tool)
                for hook in entry["hooks"]
            ]
            assert len(commands) <= 1, (event, tool, commands)


class _Stdin:
    def __init__(self, text: str) -> None:
        self.text = text

    def read(self) -> str:
        return self.text
