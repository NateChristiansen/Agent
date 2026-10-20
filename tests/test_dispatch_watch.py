"""Collect (orphan recovery), show/list/pending/inbox blocks, notify, and the hook
bodies, watch, digest and attach commands.


"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_cli import dispatch, launcher
from agent_cli.core import AgentError
from dispatch_helpers import _new, droot, envelope, one  # noqa: F401 -- droot is a pytest fixture

# ------------------------------------------------------------------ collect (orphan), show, list, blocks


def test_collect_recovers_an_orphan(droot: Path, fake_claude, monkeypatch):
    class BeforeTerminalCommit(BaseException):
        """Simulate supervisor death without running its Exception recovery path."""

    def stop_before_commit(root, record, feature, outcome):
        assert outcome["state"] == "done"
        raise BeforeTerminalCommit

    fake_claude.scenario([one(envelope(), commit="T-42 one")])
    # Run the real worker/verifier path, stopping before terminal commit and accounting.
    # A completed record changed back to `running` is not a recoverable orphan.
    with monkeypatch.context() as patch:
        patch.setattr(dispatch.supervisor, "finalize", stop_before_commit)
        with pytest.raises(BeforeTerminalCommit):
            _new(droot)
    d = dispatch.load(droot, "001-mac-01")
    assert d["state"] == "running" and not d.get("terminal_committed")
    assert d["cost_usd"] == 0.0 and not d.get("cost_accounted_attempt")
    assert d["verification"]["mode"] == "run"
    d["supervisor_pid"] = 999_999_999
    dispatch.save(droot, d)
    assert dispatch.collect(droot) and dispatch.collect(droot) == []
    final = dispatch.load(droot, "001-mac-01")
    assert final["state"] == "done" and final["cost_usd"] == 0.42
    assert dispatch.parse_handoff(droot / final["handoff"])[0]["status"] == "done"


def test_show_list_pending_and_inbox_blocks(droot: Path, fake_claude):
    assert dispatch.pending_block(droot) == "" and dispatch.inbox_block(droot) == ""
    q = envelope("question", open_questions=["which port?"], summary="need the port")
    fake_claude.scenario([one(q), one(q)])
    _new(droot)
    block = dispatch.pending_block(droot)
    assert "dispatches waiting on you (1)" in block
    # $0.42, not $0.84: a plain question no longer auto-resumes into a second attempt (1.1)
    assert "001-mac-01 alpha — question ($0.42): need the port" in block
    assert "action resume, with `answer`" in block
    assert "dispatch show 001-mac-01" in block
    assert "bin/agent" not in block
    assert dispatch.inbox_block(droot) == block
    items = dispatch.list_(droot)
    assert (
        [i["id"] for i in items] == ["001-mac-01"]
        and items[0]["chat_pending"] is True
        and items[0]["state"] == "question"
    )
    assert dispatch.list_(droot, feature="1") == items
    shown = dispatch.show(droot, dispatch_id="001-mac-01")
    assert shown["chat_pending"] is False and shown["result"]["open_questions"] == ["which port?"]
    assert dispatch.inbox_block(droot) == ""
    # Seen is not dealt with: the question stays waiting on the owner until acted on.
    assert "001-mac-01 alpha — question" in dispatch.pending_block(droot)
    assert [i["id"] for i in dispatch.list_(droot)] == ["001-mac-01"]
    fake_claude.scenario([one(envelope())])
    _new(droot)
    # a finished `done` waits too, until the owner acks it
    assert [i["id"] for i in dispatch.list_(droot)] == ["001-mac-01", "001-mac-02"]
    dispatch.ack(droot, dispatch_id="001-mac-02", note="pushed")
    assert [i["id"] for i in dispatch.list_(droot)] == ["001-mac-01"]
    assert [i["id"] for i in dispatch.list_(droot, all_=True)] == ["001-mac-01", "001-mac-02"]
    with pytest.raises(AgentError, match="not a dispatch id"):
        dispatch.show(droot, dispatch_id="nope")


def test_handoff_live_fields_follow_ack_and_feature_close(droot: Path, fake_claude, run):
    """`goal`, `ask` and `live` are on the handoff when it is written, and
    `status render` refreshes them after an ack or a feature close touches only the record."""
    from agent_cli import frontmatter, ops

    q = envelope("question", open_questions=["which port?"], summary="need the port")
    fake_claude.scenario([one(q)])
    d = _new(droot)
    path = droot / d["handoff"]
    meta = frontmatter.load(path)[0]
    assert meta["live"] is True and meta["ask"] == "which port?" and meta["goal"]
    dispatch.ack(droot, dispatch_id="001-mac-01")
    meta, body = frontmatter.load(path)
    assert meta["live"] is False
    # a record-only change reaches the handoff on the next render, body untouched
    d = dispatch.load(droot, "001-mac-01")
    d["actioned_at"] = None
    dispatch.save(droot, d)
    run(["status", "render"])
    meta, after = frontmatter.load(path)
    assert meta["live"] is True and after == body
    run(["task", "done", "1", "1"])
    run(["task", "done", "1", "2"])  # a feature with open tasks cannot close
    closed = ops.feature_close(droot, feature="1")
    assert closed.data["dispatches_left_open"] == ["001-mac-01"]
    assert "warning: 1 dispatch(es)" in closed.text
    assert frontmatter.load(path)[0]["live"] is False


def test_pending_block_lists_running(droot: Path, fake_claude, monkeypatch):
    monkeypatch.setattr(
        dispatch.supervisor,
        "popen",
        type("P", (), {"pid": os.getpid(), "__init__": lambda self, *a, **k: None}),
    )
    _new(droot, watch=False)
    block = dispatch.pending_block(droot)
    assert block.startswith("in flight: 001-mac-01 alpha (queued since")
    # a running worker carries its own take-over line, so the owner never has to ask the manager
    # how to look at it
    assert "action watch, dispatch_id 001-mac-01" in block
    assert "claude --resume" in block
    assert dispatch.inbox_block(droot) == ""


# ------------------------------------------------------------------ notify


def test_default_notify_is_silenced_in_tests(droot: Path, fake_claude):
    """AGENT_NOTIFY=off (conftest, session-wide). The default chat+macos channels record
    `skipped`. The patched senders see nothing, and the osascript shim is never reached."""
    fake_claude.scenario([one(envelope())])
    d = _new(droot)
    channels = dispatch.default_channels()
    assert d["notify"] == channels
    assert d["notify_log"][-1]["results"] == {
        c: "pending" if c == "chat" else "skipped: AGENT_NOTIFY=off" for c in channels
    }
    assert fake_claude.notifications == [] and fake_claude.osascript_calls() == []


def test_notify_sends_macos_and_skips_a_retired_channel(droot: Path, fake_claude, monkeypatch):
    monkeypatch.setenv("AGENT_NOTIFY", "on")
    # The default's macos channel. Only notify sees darwin, so git.exe is still found on Windows.
    monkeypatch.setattr(dispatch.notify_mod, "sys", SimpleNamespace(platform="darwin", stderr=sys.stderr))
    sent: list = []
    monkeypatch.setattr(
        dispatch.notify_mod, "_macos_notify", lambda title, text: sent.append((title, text)) or "sent"
    )
    fake_claude.scenario([one(envelope())])
    d = _new(droot)
    assert d["notify"] == ["chat", "macos"]
    assert sent == [("dispatch 001-mac-01 done", "alpha: added the line")]
    assert d["notify_log"][-1]["results"] == {"chat": "pending", "macos": "sent"}
    d["notify"] = ["chat", "macos", "slack"]  # a record from before then
    assert dispatch.notify(droot, d, "done")["slack"] == "skipped: retired channel"
    monkeypatch.setenv("AGENT_NOTIFY", "off")
    assert dispatch.notify(droot, d, "done") == {
        "chat": "pending",
        "macos": "skipped: AGENT_NOTIFY=off",
        "slack": "skipped: AGENT_NOTIFY=off",
    }
    assert len(sent) == 2  # unchanged by the silenced call


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_the_default_drops_macos_off_a_mac(monkeypatch, platform: str):
    monkeypatch.setattr(dispatch.notify_mod.sys, "platform", platform)
    assert dispatch.default_channels() == ["chat"]


def test_macos_notify_escapes_via_argv(monkeypatch):
    seen: dict = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return type("R", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(dispatch.notify_mod.shutil, "which", lambda name: "/usr/bin/osascript")
    monkeypatch.setattr(dispatch.notify_mod.subprocess, "run", fake_run)
    monkeypatch.setenv("AGENT_NOW", "2026-09-03T14:05:00-07:00")
    assert (
        dispatch.notify_mod._macos_notify("t", "x") == "skipped: test clock (AGENT_NOW set)"
    )  # the guard runs first
    assert seen == {}
    monkeypatch.delenv("AGENT_NOW")
    assert dispatch.notify_mod._macos_notify('say "hi"', "it's done; rm -rf /") == "sent"
    assert seen["argv"][:2] == ["osascript", "-e"]
    assert seen["argv"][2] == 'display notification "it\'s done; rm -rf /" with title "say \\"hi\\""'
    # non-ASCII stays literal: AppleScript has no \uXXXX escape
    assert dispatch.notify_mod._macos_notify("t", "PR 2 (2/4) — done") == "sent"
    assert seen["argv"][2] == 'display notification "PR 2 (2/4) — done" with title "t"'


# ------------------------------------------------------------------ hooks bodies, watch, attach


def test_worker_stop_stores_transcript_and_routes_capture(droot: Path, fake_claude, monkeypatch):
    fake_claude.scenario([one(envelope())])
    d = _new(droot)
    payload = {
        "session_id": d["session_id"],
        "hook_event_name": "Stop",
        "transcript_path": "/tmp/t.jsonl",
        "last_assistant_message": "final words",
        "cwd": d["worktree"],
    }
    assert dispatch.worker_stop(droot, payload) == ""
    after = dispatch.load(droot, "001-mac-01")
    assert after["transcript_path"] == "/tmp/t.jsonl"
    raw = (droot / ".agent/raw/001-alpha-work/2026-09-03.jsonl").read_text().splitlines()
    rec = json.loads(raw[-1])
    assert rec["kind"] == "stop" and rec["dispatch_id"] == "001-mac-01" and rec["feature"] == 1
    # env route: no session record, AGENT_DISPATCH_ID set
    monkeypatch.setenv("AGENT_DISPATCH_ID", "001-mac-01")
    dispatch.worker_stop(
        droot, {"session_id": "other", "hook_event_name": "SessionEnd", "transcript_path": "/tmp/u.jsonl"}
    )
    assert dispatch.load(droot, "001-mac-01")["transcript_path"] == "/tmp/u.jsonl"
    rec = json.loads((droot / ".agent/raw/unassigned/2026-09-03.jsonl").read_text().splitlines()[-1])
    assert rec["kind"] == "session-end" and rec["dispatch_id"] == "001-mac-01"


def test_watch_and_attach_commands(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope())])
    d = _new(droot)
    t = droot / "transcript.jsonl"
    t.write_text(
        "\n".join(
            json.dumps(x)
            for x in [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "reading the readme"},
                            {"type": "tool_use", "name": "Edit", "input": {"file_path": "README.md"}},
                        ]
                    },
                },
                {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
                "not json",
            ]
        )
    )
    d["transcript_path"] = str(t)
    dispatch.save(droot, d)
    assert list(dispatch.watch(droot, dispatch_id="001-mac-01")) == [
        "assistant: reading the readme",
        'tool: Edit {"file_path": "README.md"}',
        "result: ok",
    ]
    followed = list(dispatch.watch(droot, dispatch_id="001-mac-01", follow=True))
    assert followed[-1] == "--- dispatch 001-mac-01 done"
    # A finished or blocked worker is exactly the one worth opening. Its transcript is the
    # record of why it stopped, so the take-over hint works in EVERY state. It
    # used to refuse unless queued or running.
    # Claude Code scopes --resume by project directory, so the hint has to carry the worktree.
    # attach_cwd used to be a separate accessor for this. 1.6 folded it into attach_hint.
    for state in ("done", "blocked", "failed", "cancelled", "running"):
        d["state"] = state
        dispatch.save(droot, d)
        hint = dispatch.attach_hint(droot, dispatch_id="001-mac-01")
        assert hint == f"cd {d['worktree']} && claude --resume {d['session_id']}", state
    assert dispatch.watch_command(droot, dispatch_id="001-mac-01") == [
        *launcher.argv_prefix(droot),
        "dispatch",
        "watch",
        "001-mac-01",
        "--follow",
    ]
    assert dispatch.watch_command(droot, dispatch_id="001-mac-01", digest=True)[-1] == "--digest"


def test_dispatch_show_tool_carries_the_take_over_hint(droot: Path, fake_claude):
    """`dispatch_attach_info` is gone (2.2). Its whole job was `attach_hint`, which already
    folded the separate `attach_cwd` accessor in (1.6). `dispatch(action="show")`'s text carries
    the same `cd ... && claude --resume ...` line directly. No second tool call, no `cwd` key
    duplicating what `cd ...` in the hint already says."""
    from agent_cli.mcp import specs_dispatch as tool
    from agent_cli.mcp.tools import Ctx

    fake_claude.scenario([one(envelope())])
    d = _new(droot)
    ctx = Ctx(root=droot, profile="manager")
    result = tool.dispatch(ctx, action="show", dispatch_id="001-mac-01")
    assert result.data["id"] == "001-mac-01"
    assert f"cd {d['worktree']} && claude --resume {d['session_id']}" in result.text


def test_dispatch_tool_validates_required_params_per_action(droot: Path, fake_claude):
    """One tool, eight actions (2.2). Each still needs its own required parameter(s), checked
    in the function body rather than at the schema level, since one signature has to serve all
    eight. `new` and `show` are covered by other tests. This is every other branch."""
    from agent_cli.mcp import specs_dispatch as tool
    from agent_cli.mcp.tools import Ctx

    ctx = Ctx(root=droot, profile="manager")
    cases = [
        ({"action": "new"}, "repo"),
        ({"action": "show"}, "dispatch_id"),
        ({"action": "resume", "dispatch_id": "x"}, "answer"),
        ({"action": "cancel"}, "dispatch_id"),
        ({"action": "say", "dispatch_id": "x"}, "message"),
        ({"action": "watch"}, "dispatch_id"),
    ]
    for kwargs, missing in cases:
        with pytest.raises(AgentError, match=f"needs `{missing}`"):
            # each case's kwargs is a plain dict[str, str] table of action/param values, not a
            # typed call. The missing-param check happens before any of these fields are used
            # as their real (Literal/bool/int) type. The mismatch is noise, not a real bug.
            tool.dispatch(ctx, **kwargs)  # pyright: ignore[reportArgumentType]


def test_dispatch_tool_watch_and_cleanup_actions(droot: Path, fake_claude):
    """`watch` (bounded digest) and `cleanup` (worktree removal) are new on the MCP surface
    (2.2, no CLI equivalent for `watch`). This proves both reach the same `dispatch.py`
    functions the CLI and the other actions already do."""
    from agent_cli.mcp import specs_dispatch as tool
    from agent_cli.mcp.tools import Ctx

    fake_claude.scenario([one(envelope())])
    d = _new(droot)
    ctx = Ctx(root=droot, profile="manager")
    r = tool.dispatch(ctx, action="watch", dispatch_id=d["id"], lines=2)
    assert len(r.data["lines"]) <= 2
    r = tool.dispatch(ctx, action="cleanup", feature="1")
    assert d["worktree"] in r.data["removed"]


def test_cleanup_rejects_ambiguous_scope_before_removing_worktree(droot: Path, fake_claude):
    """Audit 10 plan.md:61 (§A4) adds dispatch scope; combining scopes must not broaden cleanup."""
    from agent_cli.mcp import specs_dispatch as tool
    from agent_cli.mcp.tools import Ctx

    fake_claude.scenario([one(envelope())])
    d = _new(droot)
    worktree = Path(d["worktree"])
    assert worktree.exists()
    ctx = Ctx(root=droot, profile="manager")
    with pytest.raises(AgentError, match="exclusively"):
        tool.dispatch(ctx, action="cleanup", feature="1", dispatch_id=d["id"])
    with pytest.raises(AgentError, match="needs `feature` or `dispatch_id`"):
        tool.dispatch(ctx, action="cleanup")
    assert worktree.exists()


def test_digest_keeps_what_the_worker_did_and_drops_the_rest(droot: Path, fake_claude):
    """A digest line names what the worker touched, not how. Successful results are dropped.
    They are the bulk of a transcript and none of its meaning."""
    fake_claude.scenario([one(envelope())])
    d = _new(droot)
    wt = d["worktree"]
    t = droot / "transcript.jsonl"
    t.write_text(
        "\n".join(
            json.dumps(x)
            for x in [
                {
                    "type": "assistant",
                    "timestamp": "2026-09-08T20:40:00Z",
                    "message": {"content": [{"type": "text", "text": "reading the readme\nthen editing"}]},
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-09-08T20:40:12Z",
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": "Edit", "input": {"file_path": f"{wt}/README.md"}}
                        ]
                    },
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-09-08T20:40:30Z",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Read",
                                "input": {"file_path": f"{droot}/features/001-x/handoffs/01-r.md"},
                            }
                        ]
                    },
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-09-08T20:41:05Z",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "pnpm test", "description": "run tests"},
                            }
                        ]
                    },
                },
                {
                    "type": "user",
                    "timestamp": "2026-09-08T20:41:09Z",
                    "message": {"content": [{"type": "tool_result", "content": "ok"}]},
                },
                {
                    "type": "user",
                    "timestamp": "2026-09-08T20:41:20Z",
                    "message": {"content": [{"type": "tool_result", "is_error": True, "content": "boom"}]},
                },
                {"type": "agent-name", "agentName": "001-mac-01"},
                "not json",
            ]
        )
    )
    d["transcript_path"] = str(t)
    dispatch.save(droot, d)
    lines = list(dispatch.watch(droot, dispatch_id="001-mac-01", digest=True))
    assert lines == [
        "[00:00] say   reading the readme then editing",
        "[00:12] edit  README.md",  # the worktree prefix is stripped so the line fits a pane
        # the vault root is stripped too. It is on the worker's --add-dir path, so a handoff
        # it reads would otherwise eat most of the line.
        "[00:30] read  features/001-x/handoffs/01-r.md",
        "[01:05] bash  pnpm test",  # the clock is relative to the first record, not the wall
        "[01:20] FAIL  boom",  # a successful result is dropped. A failed one never is
    ]
    # And the digest is materially smaller than the plain render of the same transcript.
    plain = list(dispatch.watch(droot, dispatch_id="001-mac-01"))
    assert len("\n".join(lines)) < len("\n".join(plain))


def test_attach_needs_a_session_id(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope())])
    d = _new(droot)
    d["session_id"] = ""
    dispatch.save(droot, d)
    with pytest.raises(AgentError, match="has not started"):
        dispatch.attach_hint(droot, dispatch_id="001-mac-01")
