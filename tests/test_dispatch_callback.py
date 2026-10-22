"""The `can_use_tool` callback (T8): `decide`'s pure answer for a tool call that fell through
the rule strings, and the callback's own recording and end-to-end behaviour.

`_record_and_repo` is local: nothing
outside this file needs it.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

from claude_agent_sdk import CanUseToolShadowedWarning

from agent_cli import dispatch, ops
from agent_cli import features as features_mod
from agent_cli import repos as repos_mod
from agent_cli.core import AgentError
from dispatch_helpers import (  # noqa: F401 -- droot is a fixture
    _new_fast,
    _options,
    droot,
    envelope,
    one,
    turn,
)

# ------------------------------------------------------------------ the can_use_tool callback (T8)


def _record_and_repo(droot: Path, fake_claude) -> tuple[dict, Any]:
    """A real dispatch record (it has a worktree) and its repo, for the pure-function tests."""
    fake_claude.scenario([one(envelope())])
    _new_fast(droot)
    return dispatch.load(droot, "001-mac-01"), repos_mod.load_registry(droot).require("alpha")


def test_decide_denies_a_command_no_rule_covers_and_says_to_ask(droot: Path, fake_claude):
    """The default answer, and the whole reason the callback exists. Before it, a fall-through
    was refused by the CLI with no reason at all. The worker had nothing to put in a question,
    and spent turns guessing spellings."""
    d, repo = _record_and_repo(droot, fake_claude)
    behavior, reason = dispatch.decide("Bash", {"command": "sleep 30"}, d, repo, droot)
    assert behavior == "deny"
    assert reason == dispatch.NO_RULE_REASON
    assert "`question`" in reason


def test_decide_names_the_deny_rule_that_refused_the_command(droot: Path, fake_claude):
    """`<rule> is never a worker's`: the family, not a bare no. A worker that reads 'git push
    is never yours' stops. One that reads nothing tries `git push --set-upstream` next."""
    d, repo = _record_and_repo(droot, fake_claude)
    behavior, reason = dispatch.decide("Bash", {"command": "git push origin HEAD"}, d, repo, droot)
    assert behavior == "deny"
    assert "Bash(git push:*)" in reason and "is never a worker's" in reason


def test_decide_denies_a_chained_command_with_the_managers_own_respelling_hint(droot: Path, fake_claude):
    """The no-chaining rule was hook-enforced for the manager and only advisory for workers
    (a chained command was allowed). Same detector, same hint, now on the worker side too."""
    from agent_cli import hooks

    d, repo = _record_and_repo(droot, fake_claude)
    behavior, reason = dispatch.decide("Bash", {"command": "git status ; git log"}, d, repo, droot)
    assert behavior == "deny"
    assert reason.startswith(hooks.NO_CHAIN_REASON)
    assert "Run `git status` first" in reason


def test_decide_allows_a_read_only_git_verb_against_a_registered_clone_or_the_worktree(
    droot: Path, fake_claude
):
    """The one case rule strings could not reach without generating one per clone per verb:
    a read-only git verb against a sibling clone."""
    d, repo = _record_and_repo(droot, fake_claude)
    # Slashed, as Git Bash reads it: there a backslash is an escape, not a separator.
    clone = (droot / "repos" / "alpha-api").as_posix()
    behavior, reason = dispatch.decide(
        "Bash", {"command": f"git -C {clone} show HEAD:README.md"}, d, repo, droot
    )
    assert behavior == "allow" and "`alpha`" in reason and "`show`" in reason

    behavior, reason = dispatch.decide(
        "Bash", {"command": f"git -C {Path(d['worktree']).as_posix()} log -1"}, d, repo, droot
    )
    assert behavior == "allow" and "own worktree" in reason

    # a write verb against the same clone is not a read: a worker never commits into a repo it
    # was not dispatched to
    assert dispatch.decide("Bash", {"command": f"git -C {clone} commit -m x"}, d, repo, droot)[0] == "deny"
    # nor is a path that is not a registered repo
    assert dispatch.decide("Bash", {"command": "git -C /tmp/elsewhere log"}, d, repo, droot)[0] == "deny"


def test_decide_never_allows_what_a_deny_rule_would_deny(droot: Path, fake_claude):
    """The ordering invariant. The deny scan runs first, and scores a compound command part by
    part. A read verb cannot be used as a carrier for something denied."""
    d, repo = _record_and_repo(droot, fake_claude)
    clone = droot / "repos" / "alpha-api"
    behavior, reason = dispatch.decide(
        "Bash", {"command": f"git -C {clone} log | rm -rf /tmp/x"}, d, repo, droot
    )
    assert behavior == "deny" and "rm -r*" in reason
    assert (
        dispatch.decide("Bash", {"command": f"git -C {clone} push origin main"}, d, repo, droot)[0] == "deny"
    )


def test_decide_denies_a_tool_that_is_not_bash(droot: Path, fake_claude):
    """Deny-only. A non-Bash tool that reached the callback matched no rule either way. 'ask'
    is a later feature. The input head is what the manager reads to write the rule."""
    d, repo = _record_and_repo(droot, fake_claude)
    assert dispatch.decide("NotebookEdit", {"notebook_path": "x.ipynb"}, d, repo, droot) == (
        "deny",
        dispatch.NO_RULE_REASON,
    )


def _settings_edit(droot: Path, fake_claude) -> tuple[dict, Any, Path, dict]:
    """A dispatch that declares `.claude/settings.json`, a repo granting exactly that file, the
    regular file itself, and the Edit payload the worker sent. The worktree sits beside the harness
    root, as live worktrees do: one under it is the main vault's generated deny."""
    d, repo = _record_and_repo(droot, fake_claude)
    worktree = droot.parent / "wt-outside"
    target = worktree / ".claude" / "settings.json"
    target.parent.mkdir(parents=True)
    target.write_text("{}\n", encoding="utf-8")
    d = {**d, "worktree": str(worktree), "writes": ["src/x.py", ".claude/settings.json"]}
    grant = f"Edit(//{target.as_posix().lstrip('/')})"
    repo = replace(repo, dispatch={**repo.dispatch, "allowed_tools": [grant]})
    payload = {"file_path": str(target), "old_string": "{}", "new_string": '{"a": 1}'}
    return d, repo, target, payload


def _ask(droot: Path, d: dict, repo: Any, tool: str, payload: Any) -> Any:
    from claude_agent_sdk import ToolPermissionContext

    callback = dispatch.supervisor.permission_callback(droot, d, repo)
    return asyncio.run(callback(tool, payload, ToolPermissionContext(tool_use_id="toolu_edit")))


def test_the_callback_allows_the_declared_settings_edit_only_with_both_consents(droot: Path, fake_claude):
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    d, repo, _target, payload = _settings_edit(droot, fake_claude)
    assert isinstance(_ask(droot, d, repo, "Edit", payload), PermissionResultAllow)
    assert dispatch.permission_events(dispatch.load(droot, "001-mac-01"))[-1]["decision"] == "allow"

    bare = replace(repo, dispatch={**repo.dispatch, "allowed_tools": []})
    for writes in (None, [], ["src/x.py"], [".claude"], [".claude/settings.json", "../x"]):
        answer = _ask(droot, {**d, "writes": writes}, repo, "Edit", payload)
        assert isinstance(answer, PermissionResultDeny), writes
    assert isinstance(_ask(droot, d, bare, "Edit", payload), PermissionResultDeny)
    assert isinstance(_ask(droot, {**d, "writes": None}, bare, "Edit", payload), PermissionResultDeny)


def test_decide_refuses_a_settings_edit_the_repo_grants_only_broadly(droot: Path, fake_claude):
    d, repo, target, payload = _settings_edit(droot, fake_claude)
    wt = Path(d["worktree"]).as_posix().lstrip("/")
    for grants in (
        ["Edit"],
        [f"Edit(//{wt}/**)"],
        [f"Edit(//{wt}/.claude/*)"],
        [f"Edit(/{target.as_posix().lstrip('/')})"],
        [f"Edit({target.as_posix()})"],
        ["Edit(.claude/settings.json)"],
        [f"Write(//{target.as_posix().lstrip('/')})"],
    ):
        granted = replace(repo, dispatch={**repo.dispatch, "allowed_tools": grants})
        assert dispatch.decide("Edit", payload, d, granted, droot)[0] == "deny", grants


def test_decide_refuses_other_files_worktrees_tools_and_malformed_payloads(droot: Path, fake_claude):
    d, repo, target, payload = _settings_edit(droot, fake_claude)
    wt = Path(d["worktree"])
    other = droot.parent / "other-worker"
    (other / ".claude").mkdir(parents=True)
    (other / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
    (wt / ".claude" / "settings.local.json").write_text("{}\n", encoding="utf-8")
    for path in (
        str(wt / ".claude" / "settings.local.json"),
        str(other / ".claude" / "settings.json"),
        f"{wt}/src/../.claude/settings.json",
        f"{wt}/.claude/./settings.json",
        f"{wt}/.claude/../.claude/settings.json",
        f"{other}/../{wt.name}/.claude/settings.json",
        ".claude/settings.json",
        "",
    ):
        assert dispatch.decide("Edit", {**payload, "file_path": path}, d, repo, droot)[0] == "deny", path
    for tool in ("Write", "MultiEdit", "NotebookEdit", "edit"):
        assert dispatch.decide(tool, payload, d, repo, droot)[0] == "deny", tool
    bad: Any
    for bad in (
        None,
        {},
        [payload],
        {**payload, "file_path": None},
        {**payload, "file_path": [str(target)]},
        {**payload, "file_path": str(target) + "\0"},
        {**payload, "old_string": None},
        {k: v for k, v in payload.items() if k != "new_string"},
        {**payload, "replace_all": "yes"},
    ):
        assert dispatch.decide("Edit", bad, d, repo, droot)[0] == "deny", bad
    assert dispatch.decide("Edit", {**payload, "replace_all": True}, d, repo, droot)[0] == "allow"
    for worktree in (None, "", "relative/wt", str(other), f"{other}/../{wt.name}", f"{wt}/."):
        assert dispatch.decide("Edit", payload, {**d, "worktree": worktree}, repo, droot)[0] == "deny"


def test_decide_refuses_a_settings_file_reached_through_a_symlink(droot: Path, fake_claude, tmp_path: Path):
    import pytest

    d, repo, target, payload = _settings_edit(droot, fake_claude)
    real = tmp_path / "real-settings.json"
    real.write_text("{}\n", encoding="utf-8")
    target.unlink()
    try:
        target.symlink_to(real)
    except OSError:
        pytest.skip("this host cannot create symlinks")
    assert dispatch.decide("Edit", payload, d, repo, droot)[0] == "deny"

    target.unlink()
    assert dispatch.decide("Edit", payload, d, repo, droot)[0] == "deny", "a missing file is not consent"
    target.mkdir()
    assert dispatch.decide("Edit", payload, d, repo, droot)[0] == "deny", "nor is a directory"
    target.rmdir()

    # a symlinked parent: the worktree reached through a link to itself
    link = tmp_path / "wt-link"
    link.symlink_to(Path(d["worktree"]), target_is_directory=True)
    target.write_text("{}\n", encoding="utf-8")
    linked = link / ".claude" / "settings.json"
    grant = f"Edit(//{linked.as_posix().lstrip('/')})"
    via = replace(repo, dispatch={**repo.dispatch, "allowed_tools": [grant]})
    linked_payload = {**payload, "file_path": str(linked)}
    assert dispatch.decide("Edit", linked_payload, {**d, "worktree": str(link)}, via, droot)[0] == "deny"


def test_decide_lets_an_applicable_edit_deny_win_over_the_settings_consent(
    droot: Path, fake_claude, monkeypatch
):
    from agent_cli.dispatch import rules

    d, repo, target, payload = _settings_edit(droot, fake_claude)
    wt = Path(d["worktree"])
    real = rules.dispatch_rules

    def with_deny(extra: list[str]):
        def patched(root, repo, d):
            found, hide = real(root, repo, d)
            return {**found, "deny": [*found["deny"], *extra]}, hide

        return patched

    for deny in (
        "Edit",
        "Edit*",
        "Ed*",
        f"Edit(//{target.as_posix().lstrip('/')})",
        f"Edit(//{wt.as_posix().lstrip('/')}/**)",
        f"Edit(//{wt.parent.as_posix().lstrip('/')}/**)",
        f"Edit(///{wt.as_posix().lstrip('/')}/**)",
        f"Edit({wt}/**)",
        "Edit(/.claude/settings.json)",
        "Edit(.claude/**)",
        "Edit(//**/settings.json)",
    ):
        monkeypatch.setattr(rules, "dispatch_rules", with_deny([deny]))
        behavior, reason = dispatch.decide("Edit", payload, d, repo, droot)
        assert behavior == "deny", deny
    for unrelated in (
        "Write",
        "MultiEdit(//x/**)",
        "Bash(rm:*)",
        f"Edit(//{wt.as_posix().lstrip('/')}/src/**)",
    ):
        monkeypatch.setattr(rules, "dispatch_rules", with_deny([unrelated]))
        assert dispatch.decide("Edit", payload, d, repo, droot)[0] == "allow", unrelated

    def broken(root, repo, d):
        raise AgentError("selected worker MCP profiles are unknown")

    monkeypatch.setattr(rules, "dispatch_rules", broken)
    assert dispatch.decide("Edit", payload, d, repo, droot)[0] == "deny"


def test_decide_keeps_the_generated_main_vault_deny_on_the_settings_edit(droot: Path, fake_claude):
    """The generated `Edit(//<root>/**)` deny covers a worktree under the harness root."""
    d, repo, _target, payload = _settings_edit(droot, fake_claude)
    inside = droot / "wt-inside"
    (inside / ".claude").mkdir(parents=True)
    target = inside / ".claude" / "settings.json"
    target.write_text("{}\n", encoding="utf-8")
    assert dispatch.decide("Edit", payload, d, repo, droot)[0] == "allow"
    grant = f"Edit(//{target.as_posix().lstrip('/')})"
    repo = replace(repo, dispatch={**repo.dispatch, "allowed_tools": [grant]})
    behavior, reason = dispatch.decide(
        "Edit", {**payload, "file_path": str(target)}, {**d, "worktree": str(inside)}, repo, droot
    )
    assert behavior == "deny" and "is never a worker's" in reason


def test_the_settings_grant_spelling_is_the_absolute_double_slash_form():
    from pathlib import PurePosixPath, PureWindowsPath

    from agent_cli.dispatch import rules

    assert (
        rules._settings_grant(PurePosixPath("/Users/me/wt/.claude/settings.json"))  # pyright: ignore[reportPrivateUsage]
        == "Edit(//Users/me/wt/.claude/settings.json)"
    )
    assert (
        rules._settings_grant(PureWindowsPath(r"C:\Users\me\wt\.claude\settings.json"))  # pyright: ignore[reportPrivateUsage]
        == "Edit(//C:/Users/me/wt/.claude/settings.json)"
    )


def test_the_callback_records_the_event_before_it_answers(droot: Path, fake_claude, monkeypatch):
    """Order is the point. The worker is blocked inside the tool call, so the record has to be
    written while it is still there. An after-the-fact list is exactly what `permission_denials`
    already is. A denial inside a subagent never reaches the top-level transcript at all."""
    from claude_agent_sdk import PermissionResultDeny, ToolPermissionContext

    d, repo = _record_and_repo(droot, fake_claude)
    order: list[str] = []
    real = dispatch.supervisor.record_permission_event

    def spy(root, rec, event):
        order.append("recorded")
        real(root, rec, event)

    monkeypatch.setattr(dispatch.supervisor, "record_permission_event", spy)
    callback = dispatch.supervisor.permission_callback(droot, d, repo)
    context = ToolPermissionContext(tool_use_id="toolu_9", agent_id="reviewer")
    answer = asyncio.run(callback("Bash", {"command": "sleep 30"}, context))
    order.append("answered")

    assert order == ["recorded", "answered"]
    assert isinstance(answer, PermissionResultDeny) and answer.message == dispatch.NO_RULE_REASON
    event = dispatch.permission_events(dispatch.load(droot, "001-mac-01"))[-1]
    assert event["tool"] == "Bash" and event["input"] == "[tool arguments omitted]"
    assert event["input_omitted"] is True
    assert event["agent_id"] == "reviewer" and event["tool_use_id"] == "toolu_9"
    assert event["decision"] == "deny"
    assert (
        event["reason"] == "Fall-through callback denied this call; detailed diagnostic returned to worker."
    )


def test_a_failed_record_write_still_answers_the_worker(droot: Path, fake_claude, monkeypatch, capsys):
    """The record is the point, but the worker is sitting inside the tool call waiting for this.
    Same contract as the hooks: never wedge the thing you are observing."""
    from claude_agent_sdk import PermissionResultDeny, ToolPermissionContext

    d, repo = _record_and_repo(droot, fake_claude)

    def boom(root, rec, event):
        raise RuntimeError("the record is gone")

    monkeypatch.setattr(dispatch.supervisor, "record_permission_event", boom)
    callback = dispatch.supervisor.permission_callback(droot, d, repo)
    answer = asyncio.run(callback("Bash", {"command": "sleep 30"}, ToolPermissionContext(tool_use_id="t")))
    assert isinstance(answer, PermissionResultDeny)
    assert "could not record a permission event for 001-mac-01" in capsys.readouterr().err


def test_the_input_head_omits_bash_and_arbitrary_mcp_arguments(droot: Path, fake_claude):
    from claude_agent_sdk import PermissionResultDeny, ToolPermissionContext

    d, repo = _record_and_repo(droot, fake_claude)
    callback = dispatch.supervisor.permission_callback(droot, d, repo)
    bash_secret = "g12d2b-bash-secret"
    mcp_secret = "g12d2b-mcp-secret"
    answer = asyncio.run(
        callback("Bash", {"command": f"echo {bash_secret}"}, ToolPermissionContext(tool_use_id="bash"))
    )
    assert isinstance(answer, PermissionResultDeny) and answer.message == dispatch.NO_RULE_REASON
    answer = asyncio.run(
        callback("mcp__example__write", {"token": mcp_secret}, ToolPermissionContext(tool_use_id="mcp"))
    )
    assert isinstance(answer, PermissionResultDeny) and mcp_secret not in answer.message

    events = dispatch.permission_events(dispatch.load(droot, "001-mac-01"))
    assert all(event["input"] == "[tool arguments omitted]" for event in events)
    assert all(event["input_omitted"] is True for event in events)
    assert all(bash_secret not in repr(event) and mcp_secret not in repr(event) for event in events)
    dispatch.write_handoff(droot, dispatch.load(droot, "001-mac-01"), features_mod.resolve(droot, "1"))
    show = ops.dispatch_show(droot, dispatch_id="001-mac-01").text
    handoff = (droot / dispatch.load(droot, "001-mac-01")["handoff"]).read_text(encoding="utf-8")
    assert bash_secret not in show and mcp_secret not in show
    assert bash_secret not in handoff and mcp_secret not in handoff


def test_a_live_fall_through_is_denied_with_a_reason_and_lands_on_the_record(droot: Path, fake_claude):
    """End to end over the control protocol, the way the CLI asks. The worker blocks inside the
    call. The supervisor answers `deny` with the reason. The event is on the record."""
    fake_claude.scenario(
        [
            {
                "turns": [
                    turn(
                        envelope(),
                        permission=[
                            {
                                "tool": "Bash",
                                "input": {"command": "sleep 30"},
                                "tool_use_id": "toolu_live",
                                "agent_id": "reviewer",
                            }
                        ],
                    )
                ]
            }
        ]
    )
    d = _new_fast(droot)
    assert d["state"] == "done"
    asked = fake_claude.transports[0].permissions
    assert len(asked) == 1
    assert asked[0]["response"]["subtype"] == "success"
    assert asked[0]["response"]["response"] == {"behavior": "deny", "message": dispatch.NO_RULE_REASON}
    events = dispatch.permission_events(d)
    assert len(events) == 1
    assert events[0]["input"] == "[tool arguments omitted]" and events[0]["input_omitted"] is True
    assert events[0]["tool_use_id"] == "toolu_live"
    assert events[0]["agent_id"] == "reviewer", "a denial inside a subagent is invisible otherwise"
    # the two lists are a cross-check, never merged: claude's envelope said nothing here
    assert dispatch.permission_denials(d) == []


def test_show_and_the_handoff_print_the_permission_events(droot: Path, fake_claude):
    """Under the denials block, with a line per denied event and its reason. The allowed event
    is counted but not listed: a `git -C` read the generated rules could not reach is not news."""
    from agent_cli import ops

    clone = (droot / "repos" / "alpha-api").as_posix()
    fake_claude.scenario(
        [
            {
                "turns": [
                    turn(
                        envelope(),
                        permission=[
                            {"input": {"command": "sleep 30"}},
                            {"input": {"command": f"git -C {clone} log -1"}},
                        ],
                    )
                ]
            }
        ]
    )
    d = _new_fast(droot)
    events = dispatch.permission_events(d)
    assert [e["decision"] for e in events] == ["deny", "allow"]
    assert events[1]["reason"] == "Fall-through callback allowed this call."

    text = ops.dispatch_show(droot, dispatch_id="001-mac-01").text
    assert "permission events: 2 (denied 1)" in text
    assert "[tool arguments omitted]: Fall-through callback denied this call" in text
    assert "git -C" not in text, "the allowed event is counted, not listed"

    handoff = (droot / d["handoff"]).read_text(encoding="utf-8")
    assert "## Permission denials" in handoff
    assert "permission events: 2 (denied 1)" in handoff
    assert "- `[tool arguments omitted]` — Fall-through callback denied this call" in handoff


def test_chain_diagnostic_stays_with_worker_but_not_event_show_or_handoff(droot: Path, fake_claude):
    from agent_cli import hooks, ops

    secret = "g12d2b-chain-secret"
    command = f"echo {secret} ; git log"
    fake_claude.scenario([{"turns": [turn(envelope(), permission=[{"input": {"command": command}}])]}])
    d = _new_fast(droot)
    worker_reply = fake_claude.transports[0].permissions[0]["response"]["response"]
    assert worker_reply["message"].startswith(hooks.NO_CHAIN_REASON)
    assert f"Run `echo {secret}` first" in worker_reply["message"]

    event = dispatch.permission_events(d)[0]
    assert event["input"] == "[tool arguments omitted]"
    assert (
        event["reason"] == "Fall-through callback denied this call; detailed diagnostic returned to worker."
    )
    show = ops.dispatch_show(droot, dispatch_id="001-mac-01").text
    handoff = (droot / d["handoff"]).read_text(encoding="utf-8")
    assert secret not in repr(event)
    assert secret not in show
    assert secret not in handoff


def test_a_dispatch_with_no_fall_through_prints_no_event_line(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    from agent_cli import ops

    assert dispatch.permission_events(d) == []
    assert "permission events" not in ops.dispatch_show(droot, dispatch_id="001-mac-01").text
    assert "Permission denials" not in (droot / d["handoff"]).read_text(encoding="utf-8")


def test_the_launch_carries_the_callback_and_swallows_the_shadow_warning(droot: Path, fake_claude):
    """Whole-tool allow entries (`Edit`, `WebFetch`, `mcp__agent__log`) auto-approve before the
    callback, and the SDK warns about it at connect. That is the design. The rule strings stay
    the primary mechanism, so the supervisor filters the warning instead. As an error it would
    take the connection down inside `_drive_worker`'s own except."""
    import warnings as warnings_mod

    fake_claude.scenario([one(envelope())])
    with warnings_mod.catch_warnings(record=True) as caught:
        warnings_mod.simplefilter("error", CanUseToolShadowedWarning)
        assert _new_fast(droot)["state"] == "done"
    assert caught == []
    assert _options(fake_claude).can_use_tool is not None
