"""T5's record defects (the steer thread, the repo lease pid, `say` on a dead supervisor,
`cancel` notifying, accumulated denials, `build_settings` built once), the worktree's own
MCP servers (T7 item 3), and the in-process Stop/SessionEnd SDK hooks (T7 item 4).


"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agent_cli import dispatch, repos_validate
from agent_cli import repos as repos_mod
from agent_cli.core import AgentError
from dispatch_helpers import (  # noqa: F401 -- droot is a pytest fixture, used only as a parameter name
    DEAD_PID,
    PROMPT,
    _new_fast,
    _steer_when,
    droot,
    envelope,
    one,
    says,
    settle,
    turn,
)

# ------------------------------------------------------------------ T5: the record defects
#
# An earlier review found: the steer never reached the thread, the repo lease was held by
# a pid that had already exited, `say` accepted a message for a supervisor that was gone,
# `cancel` never notified, the accumulated denials were never printed, and the worker settings
# were rebuilt four times per launch.


def test_a_steer_lands_on_the_record_and_in_the_handoff(droot: Path, fake_claude):
    """T5 item 1. `say` wrote the inbox file and nothing else. The record of a steered dispatch
    was silently incomplete: 002-03's thread has two entries, and its steer survives nowhere but
    the `.err` log. The handoff is the committed record. If a steer changed what the worker did,
    the steer has to be in it."""
    fake_claude.scenario([one(says("on it"), envelope(summary="did both"), await_steer=True)])
    t = _steer_when(droot, "on it", "also do the other thing")
    d = _new_fast(droot)
    t.join(timeout=5)

    assert [e.get("from") for e in d["thread"]] == ["manager", "manager", "worker"]
    steer = d["thread"][1]
    assert steer["text"] == "also do the other thing" and steer["steer"] is True
    assert fake_claude.calls()[0]["steers"] == ["also do the other thing"], "and it reached the worker"

    handoff = (droot / d["handoff"]).read_text(encoding="utf-8")
    assert "→ worker (steer, mid-turn)" in handoff, "a steer is not the work order; say which it is"
    assert "also do the other thing" in handoff


def test_a_resumed_session_is_sent_only_what_is_new():
    """A history-preserving resume already holds the work order, the steer it got mid-turn and
    its own return. The follow-up carries what came after that return, in order, the findings on
    it and the nonce, and none of the earlier turns again."""
    d = {
        "id": "001-mac-01",
        "repo": "alpha-api",
        "attempt": 2,
        "nonce": "001-mac-01-abcd",
        "worktree": "/wt/alpha/001",
        "branch": "agent/001-alpha-work",
        "verification_result": {"status": "failed", "reasons": ["command 3 did not pass within its bound"]},
        "result": {"next_steps": ["comment noise: src/x.py:3 cites a dispatch id"]},
        "thread": [
            {"ts": "t1", "from": "manager", "text": "the work order"},
            {"ts": "t2", "from": "manager", "text": "and also this", "steer": True},
            {"ts": "t3", "from": "worker", "text": "did it", "status": "blocked", "attempt": 1},
            {"ts": "t4", "from": "owner", "text": "now finish"},
            {"ts": "t5", "from": "manager", "text": "and fix the test", "steer": True},
        ],
    }
    text = dispatch.compose_followup(d)
    assert "the work order" not in text and "and also this" not in text and "did it" not in text
    assert text.index("now finish") < text.index("and fix the test")
    assert "### t5 manager → worker (steer, mid-turn)" in text and "001-mac-01-abcd" in text
    assert "verification failed: command 3" in text and "cites a dispatch id" in text
    assert (
        "Weigh review findings: do they reproduce, does real use take the path, and is it worth "
        "building around? Fix the real ones; name each declined finding and its reason in your return. "
        "A failed verification command is not a review finding: fix it."
        "\n\n## Findings on your last return\n"
    ) in text


def test_a_restart_keeps_the_full_work_order():
    from agent_cli.dispatch.prompt import compose_restart

    d = {
        "thread": [
            {"ts": "t1", "from": "manager", "text": "the work order"},
            {"ts": "t2", "from": "owner", "text": "more"},
        ]
    }
    restarted = compose_restart(d, "# the original prompt\n")
    assert "the original prompt" in restarted and "more" in restarted


def test_say_refuses_a_dispatch_whose_supervisor_is_gone(droot: Path, fake_claude, monkeypatch):
    """T5 item 2. `say` checked the state word only. A supervisor that died leaves the record
    saying `running` forever. Nothing else ever rewrites it. The message was written to an
    inbox no process was reading, and reported as queued."""
    monkeypatch.setattr(
        dispatch.supervisor,
        "popen",
        type("P", (), {"pid": os.getpid(), "__init__": lambda self, *a, **k: None}),
    )
    _new_fast(droot, watch=False)
    # a live pid, so this is the live branch
    assert dispatch.say(droot, dispatch_id="001-mac-01", message="mind the config")["queued"]

    rec = dispatch.load(droot, "001-mac-01")
    rec["supervisor_pid"] = DEAD_PID
    dispatch.save(droot, rec)
    with pytest.raises(AgentError, match="action collect, dispatch_id 001-mac-01"):
        dispatch.say(droot, dispatch_id="001-mac-01", message="anyone there?")
    assert "anyone there?" not in dispatch.inbox_path(droot, "001-mac-01").read_text(encoding="utf-8")
    assert len(dispatch.load(droot, "001-mac-01")["thread"]) == 2, "and nothing was recorded either"


def test_the_supervisor_holds_the_repo_lease_with_its_own_pid(droot: Path, fake_claude, monkeypatch):
    """T5 item 3. `new` and `resume` take `repo:<short>` with `os.getpid()` of the manager's
    CLI/MCP process. The supervisor detaches as another one and never re-took it. Seconds later
    `leases.status` read the holder's pid as gone, and called a live dispatch's lease dead.
    That lease is the gate meant to stop two workers in one repo."""
    from agent_cli import leases

    monkeypatch.setattr(
        dispatch.supervisor, "popen", type("P", (), {"pid": 1, "__init__": lambda self, *a, **k: None})
    )
    _new_fast(droot, watch=False)  # queued. The supervisor has not run yet
    # the manager process that took it has exited. That is the normal case for a detached
    # dispatch with the same session_id, so this is that lease with a dead pid, not someone
    # else's.
    leases.take(droot, "repo-slot:alpha/0", session_id="dispatch:001-mac-01", pid=DEAD_PID)
    assert leases.status(leases.get(droot, "repo-slot:alpha/0")) == "dead"

    # read the lease at the moment the supervisor gives it back. That is the last instant it
    # is still held. Everything before that is mid-attempt and racy to sample from here.
    seen: dict = {}
    real_release = leases.release

    def capture(root, area, session_id):
        lease = leases.get(root, area)
        seen.update(lease or {}, status=leases.status(lease))
        return real_release(root, area, session_id)

    monkeypatch.setattr(leases, "release", capture)
    fake_claude.scenario([one(envelope())])
    d = dispatch.run(droot, dispatch_id="001-mac-01")

    assert d["state"] == "done"
    assert seen["status"] == "live", "the worker was running and its lease read dead"
    assert seen["pid"] == os.getpid() and seen["session_id"] == "dispatch:001-mac-01"
    assert leases.get(droot, "repo-slot:alpha/0") is None, "and it is still released when the attempt ends"


def test_cancel_notifies(droot: Path, fake_claude, monkeypatch):
    """T5 item 4. Every other ending notifies. `cancel` never did, so `pending_block`'s
    `cancelled` hint ("worktree kept") was unreachable. A cancelled dispatch never showed up in
    the session-start block at all."""
    monkeypatch.setenv("AGENT_NOTIFY", "on")
    monkeypatch.setattr(dispatch.lifecycle, "default_channels", lambda: ["chat", "macos"])
    sent: list = []
    monkeypatch.setattr(
        dispatch.notify_mod, "_macos_notify", lambda title, text: sent.append((title, text)) or "sent"
    )
    fake_claude.scenario([one(envelope(), sleep=30)])
    result: dict = {}

    def runner():
        try:
            result["d"] = _new_fast(droot)
        except Exception as exc:  # noqa: BLE001
            result["exc"] = exc

    th = threading.Thread(target=runner)
    th.start()
    path = droot / ".agent/dispatches/001-mac-01.json"
    for _ in range(400):
        if (
            path.exists()
            and json.loads(path.read_text()).get("state") == "running"
            and fake_claude.transports
        ):
            break
        time.sleep(0.02)
    else:
        pytest.fail("worker never started")
    c = dispatch.cancel(droot, dispatch_id="001-mac-01")
    th.join(timeout=15)

    # A cancel closes the dispatch, so the chat channel no longer sets a pending
    # return; the owner-facing channel still fires.
    assert not c.get("chat_pending")
    # The runner thread can commit the terminal first; cancel then returns before its notice is logged.
    assert not th.is_alive()
    c = dispatch.load(droot, "001-mac-01")
    assert c["notify_log"][-1]["event"] == "cancelled"
    assert c["notify_log"][-1]["results"] == {"chat": "skipped: closed", "macos": "sent"}
    assert sent == [("dispatch 001-mac-01 cancelled", "alpha: cancelled by the manager")]
    # A cancel is the owner's own act, so nothing waits on it afterwards.
    assert "001-mac-01" not in dispatch.pending_block(droot)


def test_show_and_the_handoff_print_the_denials_from_every_attempt(droot: Path, fake_claude):
    """`permission_denials` accumulates across attempts on the record. Workers no longer report
    commands, so the supervisor's own record is the whole count; old DENIED rows are read in
    test_dispatch_return_storage."""
    from agent_cli import ops

    first = envelope("blocked", summary="pnpm is denied; nothing can be verified")
    first["permission_denials"] = [
        {"tool_name": "Bash", "tool_input": {"command": "pnpm --filter web lint"}},
        {"tool_name": "Bash", "tool_input": {"command": "pnpm --version"}},
    ]
    second = envelope()
    second["permission_denials"] = [{"tool_name": "Bash", "tool_input": {"command": "git push origin HEAD"}}]
    fake_claude.scenario([one(first), one(second)])

    d = _new_fast(droot)
    assert d["state"] == "blocked" and len(d["permission_denials"]) == 2
    settle(droot, d)
    d = dispatch.resume(droot, dispatch_id="001-mac-01", answer="carry on without pnpm", watch=True)
    assert d["state"] == "done" and d["attempt"] == 2
    assert len(d["permission_denials"]) == 3, "accumulated across both attempts, not replaced"

    text = ops.dispatch_show(droot, dispatch_id="001-mac-01").text
    assert "permission denials: 3" in text
    assert "pnpm --filter web lint" in text and "git push origin HEAD" in text

    handoff = (droot / d["handoff"]).read_text(encoding="utf-8")
    assert "permission denials: 3" in handoff
    assert "- `git push origin HEAD`" in handoff


def test_a_clean_dispatch_prints_no_denial_line(droot: Path, fake_claude):
    """The other side of T5 item 7. A dispatch nothing was refused on says nothing, the same
    way the required-checks and files-diff blocks stay silent when there is nothing to report."""
    from agent_cli import ops

    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    assert dispatch.permission_denials(d) == []
    assert "permission denials" not in ops.dispatch_show(droot, dispatch_id="001-mac-01").text
    assert "Permission denials" not in (droot / d["handoff"]).read_text(encoding="utf-8")


def test_a_launch_builds_the_worker_settings_once(droot: Path, fake_claude, monkeypatch):
    """T5 item 10. `build_settings` walks the registry, builds a 75-rule allow list, and lints
    every rule in it. A launch ran the whole thing four times for four identical results
    (preflight, write_worker_files, build_options, and again per attempt in the supervisor).
    `new` builds it once and threads it through."""
    calls: list = []
    real = dispatch.rules.build_settings

    def counting(*args, **kw):
        calls.append(list(kw.get("mcp", ())))
        return real(*args, **kw)

    # both bindings: `rules` owns it, and `lifecycle` imported it by name. The point of the
    # test is the total number of constructions, however they are reached.
    monkeypatch.setattr(dispatch.rules, "build_settings", counting)
    monkeypatch.setattr(dispatch.lifecycle, "build_settings", counting)
    _new_fast(droot, dry_run=True)
    assert calls == [[]], "a preview builds the rules once: preflight and the options share them"

    calls.clear()
    fake_claude.scenario([one(envelope())])
    assert _new_fast(droot)["state"] == "done"
    # one for `new` itself. The supervisor deliberately rebuilds per attempt, so a resume
    # after a harness fix runs with the current rules (write_worker_files + build_options).
    assert len(calls) == 3


# ------------------------------------------------------------------ the worktree's own MCP (T7 item 3)


def test_the_preview_excludes_unselected_product_mcp(droot: Path, fake_claude):
    """Product files supply definitions, never implicit worker grants (ADR-0020)."""
    (droot / "repos/alpha-api" / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"playwright": {"command": "x"}, "notes": {"command": "y"}}}),
        encoding="utf-8",
    )
    from agent_cli import ops

    preview = ops.dispatch_new(droot, repo="alpha", feature="1", prompt=PROMPT, dry_run=True)
    assert preview.data["options"]["repo mcp servers"] == []
    assert preview.data["options"]["strict_mcp_config"] is True
    assert preview.data["options"]["mcp_selection"]["product_selection"] == "default-absent"
    assert preview.data["options"]["mcp_servers"] == ["agent"]
    assert "explicit selection only" in preview.text


def _record(droot: Path) -> dict[str, Any]:
    """The dispatch record fields `build_options` reads, for a worktree that does not exist
    yet (so the repo's `.mcp.json` is read from the clone, as a dry run does)."""
    return {
        "id": "001-mac-01",
        "feature": 1,
        "feature_dir": "features/001-alpha-work",
        "worktree": str(droot / "repos/_worktrees/alpha/none"),
        "session_id": "00000000-0000-0000-0000-000000000000",
        "budget_usd": 1,
    }


def _repo_mcp(droot: Path, names: str) -> None:
    import yaml

    from dispatch_helpers import _set_alpha_dispatch

    _set_alpha_dispatch(droot, repo_mcp=yaml.safe_load(names))
    (droot / "repos/alpha-api" / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"playwright": {"command": "x"}, "notes": {"type": "http", "url": "u"}}}),
        encoding="utf-8",
    )


def test_repo_mcp_keeps_only_the_listed_repo_servers(droot: Path, fake_claude):
    """A repo's `.mcp.json` is written for humans in that repo. `dispatch.repo_mcp` names the
    ones an unattended worker may load. The preview reports that exact selection."""
    _repo_mcp(droot, "[notes]")
    from agent_cli import ops

    preview = ops.dispatch_new(droot, repo="alpha", feature="1", prompt=PROMPT, dry_run=True)
    opts = preview.data["options"]
    assert opts["mcp_servers"] == ["agent", "notes"]
    assert opts["repo mcp servers"] == ["notes"]
    assert opts["mcp_selection"]["product_selection"] == "explicit-selected"
    assert "not inventoried" in opts["repo mcp policy"]

    alpha = repos_mod.load_registry(droot).require("alpha")
    options = dispatch.build_options(droot, _record(droot), alpha, resume=False)
    assert options.strict_mcp_config is True
    assert options.mcp_servers["notes"] == {"type": "http", "url": "u"}  # type: ignore[index]


def test_repo_mcp_absent_excludes_product_servers_and_enables_strict(droot: Path, fake_claude):
    (droot / "repos/alpha-api" / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"playwright": {"command": "x"}}}), encoding="utf-8"
    )
    alpha = repos_mod.load_registry(droot).require("alpha")
    assert alpha.repo_mcp is None
    options = dispatch.build_options(droot, _record(droot), alpha, resume=False)
    assert options.strict_mcp_config is True
    assert isinstance(options.mcp_servers, dict)
    assert list(options.mcp_servers) == ["agent"]
    assert dispatch.options_preview(options, alpha.abs_path(droot))["repo mcp servers"] == []


def test_repo_mcp_refuses_a_name_the_harness_already_passes(droot: Path, fake_claude):
    _repo_mcp(droot, "[agent]")
    from agent_cli import ops

    with pytest.raises(AgentError, match="repo_mcp selection 'agent' collides"):
        ops.dispatch_new(droot, repo="alpha", feature="1", prompt=PROMPT, dry_run=True)


def test_repo_mcp_must_be_a_list_of_names():
    assert repos_validate.validate_dispatch({"repo_mcp": "playwright"}) == [
        "dispatch.repo_mcp must be a list of server names from the repo's own .mcp.json"
    ]
    assert repos_validate.validate_dispatch({"repo_mcp": []}) == []


def test_repo_mcp_servers_is_quiet_about_a_missing_file_and_loud_about_a_broken_one(tmp_path: Path):
    assert dispatch.repo_mcp_servers(None) == []
    assert dispatch.repo_mcp_servers(tmp_path) == [], "no .mcp.json is the normal case"
    (tmp_path / ".mcp.json").write_text("{not json", encoding="utf-8")
    assert dispatch.repo_mcp_servers(tmp_path) == ["(unreadable .mcp.json)"]
    (tmp_path / ".mcp.json").write_text(json.dumps({"other": 1}), encoding="utf-8")
    assert dispatch.repo_mcp_servers(tmp_path) == []


# ------------------------------------------------------------------ in-process SDK hooks (T7 item 4)


def test_stop_and_session_end_are_sdk_callbacks_not_commands(droot: Path, fake_claude):
    """T7 item 4: the supervisor is already a python process holding the client. The Stop and
    SessionEnd bodies run in it instead of re-execing `bin/agent hook worker-stop` (a fresh
    interpreter and the whole CLI import) at the end of every assistant turn."""
    fake_claude.scenario([one(envelope())])
    _new_fast(droot)
    transport = fake_claude.transports[0]
    assert len(transport.hook_callback_ids("Stop")) == 1
    assert len(transport.hook_callback_ids("SessionEnd")) == 1
    # and nothing re-execs the CLI for them any more
    settings = json.loads(dispatch.file(droot, "001-mac-01", ".settings.json").read_text(encoding="utf-8"))
    assert set(settings["hooks"]) == {"PreCompact"}


def test_the_stop_callback_records_what_the_command_hook_used_to(droot: Path, fake_claude):
    """Fired the way the CLI fires it: a `hook_callback` control request carrying the id the
    SDK handed out. The callback writes exactly what `bin/agent hook worker-stop` wrote: the
    transcript path on the dispatch record, and the capture routed into the feature's
    .agent/raw/ with the dispatch id stamped on it."""
    sid = "33333333-3333-4333-8333-333333333333"
    transcript = droot / "sdk-transcript.jsonl"
    fake_claude.scenario(
        [
            {
                "turns": [
                    turn(
                        envelope(),
                        hooks=[
                            {
                                "event": "Stop",
                                "input": {
                                    "hook_event_name": "Stop",
                                    "session_id": sid,
                                    "transcript_path": str(transcript),
                                    "cwd": str(droot),
                                },
                            }
                        ],
                    )
                ]
            }
        ]
    )
    d = _new_fast(droot, worker_session_id=sid)
    assert d["state"] == "done"
    assert dispatch.load(droot, "001-mac-01")["transcript_path"] == str(transcript)
    # the callback runs in the SUPERVISOR's process. That process has no AGENT_DISPATCH_ID in
    # its env (that variable is set on the worker's), so the routing here is the session-record
    # one.
    assert "AGENT_DISPATCH_ID" not in os.environ
    rec = json.loads(
        (droot / ".agent/raw/001-alpha-work/2026-09-03.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    assert rec["kind"] == "stop" and rec["dispatch_id"] == "001-mac-01" and rec["feature"] == 1


def test_a_failing_stop_callback_never_reaches_the_worker(droot: Path, fake_claude, monkeypatch):
    """Same contract as every hook in hooks.py: it fails open. A raise here would end a turn
    that was otherwise fine."""

    def boom(root, payload):
        raise RuntimeError("the record is gone")

    monkeypatch.setattr(dispatch.supervisor, "worker_stop", boom)
    hooks = dispatch.sdk_hooks(droot)
    callback = hooks["Stop"][0].hooks[0]
    payload: Any = {"hook_event_name": "Stop", "session_id": "s", "transcript_path": "t", "cwd": "c"}
    assert asyncio.run(callback(payload, None, {"signal": None})) == {}
