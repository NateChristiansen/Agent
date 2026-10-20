"""First claim of a Codex manager's Obsidian terminal tab."""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from agent_cli import obsidian_tabs, sessions
from agent_cli.hooks.activity import activity, codex_activity

# `agent_cli.hooks` re-exports the `activity` function under the module's own name.
activity_mod = importlib.import_module("agent_cli.hooks.activity")


def test_codex_first_claim_targets_only_the_active_unnamed_agent_terminal():
    code = obsidian_tabs.eval_code("mgr-030-x-codex-123456", "⚙️ mgr-030-x-codex-123456", claim_active=True)
    assert "leaf === app.workspace.activeLeaf" in code
    assert "!s.userTitle" in code
    assert "includes('| Agent')" in code
    assert "String(s.userTitle || '').endsWith(' ' + name)" in code
    assert "includes('-codex-')" not in code
    assert "includes('| Agent') || false" in code
    start_code = obsidian_tabs.eval_code(
        "mgr-030-x-codex-123456",
        "⚙️ mgr-030-x-codex-123456",
        claim_active=True,
        claim_untitled=True,
    )
    assert "includes('| Agent') || true" in start_code
    assert "leaf === app.workspace.activeLeaf" in start_code
    assert "!s.userTitle" in start_code
    resume_code = obsidian_tabs.eval_code(
        "mgr-030-x-codex-123456",
        "⚙️ mgr-030-x-codex-123456",
        claim_active=True,
        claim_untitled=True,
        resume_display="001 example · cx 0002",
    )
    assert f"String(s.userTitle || '').endsWith({json.dumps('001 example · cx 0002')})" in resume_code
    assert "true || (!s.userTitle" not in resume_code


@pytest.mark.skipif(shutil.which("node") is None, reason="Node executes the tab claim")
def test_ribbon_claim_matches_its_launch_marker_even_when_another_tab_is_active():
    for client, title in (
        ("codex", obsidian_tabs.CODEX_STARTING_TITLE),
        ("claude", obsidian_tabs.CLAUDE_STARTING_TITLE),
    ):
        marker = f"{client}:launch-123"
        label = f"⚙️ mgr-{client}"
        code = obsidian_tabs.eval_code(
            f"mgr-{client}",
            label,
            claim_active=True,
            provisional_title=title,
            launch_marker=marker,
        )
        # Claims still find the launch after the 30 s provisional-title cap clears it.
        for provisional in (title, title.replace("✅", "⏳"), ""):
            states = [
                {"userTitle": provisional, "profile": {"args": [marker]}},
                {"userTitle": title, "profile": {"args": [f"{client}:other-launch"]}},
                {"userTitle": "Owner title", "profile": {"args": [marker]}},
            ]
            script = (
                "const leaves = "
                + json.dumps(states)
                + ".map(state => ({view: {state}, getDisplayText: () => 'Terminal | Agent'}));"
                "const app = {workspace: {activeLeaf: leaves[1], getLeavesOfType: () => leaves}};"
                "const result = "
                + code
                + "; console.log(JSON.stringify({result, titles: leaves.map(l => l.view.state.userTitle)}));"
            )
            result = subprocess.run(["node", "-e", script], capture_output=True, encoding="utf-8", check=True)
            outcome = json.loads(result.stdout)
            assert "relabeled 1 hits=1" in outcome["result"]
            assert outcome["titles"] == [label, title, "Owner title"]


def test_codex_claim_does_not_accept_a_multi_tab_relabel(tmp_root: Path, monkeypatch):
    monkeypatch.delenv("AGENT_NOW", raising=False)
    monkeypatch.delenv("AGENT_NO_TABS", raising=False)
    monkeypatch.setenv("AGENT_ROOT", str(tmp_root))
    monkeypatch.setattr("agent_cli.dispatch.opener.obsidian_cli", lambda: "/usr/local/bin/obsidian")
    monkeypatch.setattr(obsidian_tabs.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kw: CompletedProcess(argv, 0, stdout="=> relabeled 10", stderr=""),
    )
    assert not obsidian_tabs.claim_active("mgr-codex-x", "⚙️ mgr-codex-x")


def test_codex_session_start_claim_is_single_attempt_and_records_the_early_tab(tmp_root: Path, monkeypatch):
    monkeypatch.delenv("AGENT_NOW", raising=False)
    monkeypatch.delenv("AGENT_NO_TABS", raising=False)
    monkeypatch.setenv("AGENT_ROOT", str(tmp_root))
    monkeypatch.setattr("agent_cli.dispatch.opener.obsidian_cli", lambda: "/usr/local/bin/obsidian")
    calls: list[str] = []

    def run(argv, **kw):
        calls.append(argv[-1])
        return CompletedProcess(
            argv,
            0,
            stdout="=> relabeled 1 active_terminal=true agent_title=false active_display=Terminal%20A",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", run)
    assert obsidian_tabs.claim_active("mgr-codex-x", "⚙️ mgr-codex-x", allow_untitled=True)
    assert len(calls) == 1 and "includes('| Agent') || true" in calls[0]
    claim = json.loads((tmp_root / ".agent/tabs/claims.jsonl").read_text().splitlines()[-1])
    assert claim["active_display"] == "Terminal A"


def test_codex_only_claims_an_untitled_active_tab_on_session_start(tmp_root: Path, monkeypatch):
    sid = "12345678-codex"
    sessions.set_record(tmp_root, sid)
    attempts: list[bool] = []

    def claim(name: str, label: str, **kwargs) -> bool:
        allow_untitled = kwargs.get("allow_untitled", False)
        attempts.append(allow_untitled)
        return False

    monkeypatch.setattr(obsidian_tabs, "claim_active", claim)
    # the ordinary claim needs a positive marker since #67
    monkeypatch.setattr(
        "agent_cli.codex_tab_link.marker_for", lambda s: "codex:" + "3" * 8 + "-3333-4333-8333-" + "3" * 12
    )
    codex_activity(tmp_root, {"session_id": sid, "hook_event_name": "SessionStart"})
    codex_activity(tmp_root, {"session_id": sid, "hook_event_name": "UserPromptSubmit"})
    assert attempts == [True, False]


def test_failed_codex_claim_backs_off_instead_of_retrying_every_event(tmp_root: Path, monkeypatch):
    """About 1,000 failed Codex claims in two days on the owner's vault, each one three
    `obsidian eval` calls inside a hook. A failure now waits."""
    sid = "12345678-codex"
    sessions.set_record(tmp_root, sid, name="mgr-030-x-codex-12345678")
    sessions.set_field(tmp_root, sid, "provider", "codex")
    clock = [1000.0]
    monkeypatch.setattr(activity_mod.time, "time", lambda: clock[0])
    results = [False, False, True]
    tries: list[str] = []

    def claim(name: str, label: str, **kwargs) -> bool:
        tries.append(name)
        return results.pop(0)

    monkeypatch.setattr(obsidian_tabs, "claim_active", claim)
    monkeypatch.setattr(
        "agent_cli.codex_tab_link.marker_for", lambda s: "codex:" + "3" * 8 + "-3333-4333-8333-" + "3" * 12
    )
    prompt = {"session_id": sid, "hook_event_name": "UserPromptSubmit"}
    codex_activity(tmp_root, prompt)  # fails: waits 10 s
    codex_activity(tmp_root, prompt)  # too soon: not tried
    assert len(tries) == 1
    state = (sessions.get(tmp_root, sid) or {})[activity_mod.CLAIM_FIELD]
    assert state["attempts"] == 1 and state["next_at"] == 1010.0
    clock[0] = 1011.0
    codex_activity(tmp_root, prompt)  # fails again: waits 20 s
    assert (sessions.get(tmp_root, sid) or {})[activity_mod.CLAIM_FIELD]["next_at"] == 1031.0
    clock[0] = 1020.0
    codex_activity(tmp_root, prompt)
    assert len(tries) == 2
    clock[0] = 1032.0
    codex_activity(tmp_root, prompt)  # succeeds: the backoff state is cleared
    record = sessions.get(tmp_root, sid) or {}
    assert len(tries) == 3 and not record.get(activity_mod.CLAIM_FIELD) and record["tab_display_name"]


def test_a_failed_codex_claim_still_relabels_a_tab_by_its_session_suffix(tmp_root: Path, monkeypatch):
    """#59: the launch marker left a title we had renamed, so every claim failed and the tab
    kept ✋ from 23:25 through the session going idle and working. The stable suffix is still
    there, so the drainer is asked to relabel by it: by the suffix alone, with no name that could
    match another tab, and on every event, not only when the claim's backoff lets it retry."""
    from agent_cli import tab_drain

    sid = "019b76da-a800-7000-8000-000000000001"
    sessions.set_record(tmp_root, sid, name="mgr-001-example-codex-00000001")
    sessions.set_field(tmp_root, sid, "provider", "codex")
    tries: list[str] = []
    monkeypatch.setattr(obsidian_tabs, "claim_active", lambda name, label, **kw: tries.append(name) and False)
    # Stub the marker lookup so the fixture does not depend on machine-local Codex data.
    monkeypatch.setattr(
        "agent_cli.codex_tab_link.marker_for", lambda s: "codex:" + "5" * 8 + "-5555-4555-8555-" + "5" * 12
    )
    asked: list[dict] = []
    monkeypatch.setattr(
        tab_drain,
        "request",
        lambda root, kind, s, match, label, **kw: asked.append({"match": match, "label": label, **kw}),
    )
    prompt = {"session_id": sid, "hook_event_name": "UserPromptSubmit"}
    codex_activity(tmp_root, prompt)
    codex_activity(tmp_root, {**prompt, "hook_event_name": "Stop"})  # the claim is backing off
    assert len(tries) == 1 and len(asked) == 2
    for ask in asked:
        assert ask["match"] == "" and not ask.get("generated_name") and ask["generated"] is False
        assert ask["token"] == obsidian_tabs.manager_identity(sid, "codex")
    assert asked[0]["label"].startswith("⚙️") and asked[1]["label"].startswith("✅")


def test_claim_backoff_caps_and_a_new_ribbon_marker_retries_at_once(tmp_root: Path, monkeypatch):
    monkeypatch.setattr(activity_mod.time, "time", lambda: 5000.0)
    state = {"attempts": 12, "next_at": 9999.0, "marker": "codex:old"}
    assert activity_mod.claim_failed(state, "")["next_at"] == 5000.0 + activity_mod.CLAIM_BACKOFF_MAX_S
    assert not activity_mod.claim_due(state, "codex:old")
    assert activity_mod.claim_due(state, "codex:new-launch")
    # A failure under the new marker starts over at 10 s instead of the old 600 s.
    fresh = activity_mod.claim_failed(state, "codex:new-launch")
    assert fresh["attempts"] == 1 and fresh["next_at"] == 5000.0 + activity_mod.CLAIM_BACKOFF_S
    # A state that has failed for days still computes a finite, capped wait.
    ancient = activity_mod.claim_failed({"attempts": 10_000, "marker": ""}, "")
    assert ancient["next_at"] == 5000.0 + activity_mod.CLAIM_BACKOFF_MAX_S


def test_codex_resume_claims_the_new_tab_even_when_the_old_tab_had_a_title(tmp_root: Path, monkeypatch):
    sid = "019b76da-a800-7000-8000-000000000002"
    sessions.set_record(
        tmp_root,
        sid,
        feature={"id": 1, "slug": "example"},
        pid=11111,
        name="mgr-001-example-codex-00000002",
    )
    sessions.set_field(tmp_root, sid, "provider", "codex")
    sessions.set_field(tmp_root, sid, "tab_display_name", "001 example · cx 0002")
    sessions.set_field(tmp_root, sid, "tab_label", "✅ 001 example · cx 0002")
    monkeypatch.setattr(sessions, "resolve_pid", lambda: 22222)
    claims: list[dict] = []

    def claim(name: str, label: str, **kwargs) -> bool:
        claims.append(kwargs)
        return True

    monkeypatch.setattr(obsidian_tabs, "claim_active", claim)
    monkeypatch.setattr(
        "agent_cli.codex_tab_link.marker_for", lambda s: "codex:" + "4" * 8 + "-4444-4444-8444-" + "4" * 12
    )
    codex_activity(tmp_root, {"session_id": sid, "hook_event_name": "SessionStart", "source": "resume"})
    assert len(claims) == 1
    assert claims[0]["allow_untitled"] is True
    assert claims[0]["resume_display"] == "001 example · cx 0002"
    record = sessions.get(tmp_root, sid) or {}
    assert record["tab_label"].startswith("✅ 001 example")


def test_claude_ribbon_claims_its_provisional_tab(tmp_root: Path, monkeypatch):
    sid = "claude-session"
    sessions.set_record(tmp_root, sid, name="mgr-034-harness-cleanup")
    monkeypatch.setenv("AGENT_RIBBON_LAUNCH", "claude:launch-123")
    claimed: list[dict] = []

    def claim(name: str, label: str, **kwargs) -> bool:
        claimed.append({"name": name, "label": label, **kwargs})
        return True

    monkeypatch.setattr(obsidian_tabs, "claim_active", claim)
    activity(tmp_root, {"session_id": sid, "hook_event_name": "SessionStart"})
    assert len(claimed) == 1
    assert claimed[0]["provisional_title"] == obsidian_tabs.CLAUDE_STARTING_TITLE
    assert claimed[0]["launch_marker"] == "claude:launch-123"
    assert not claimed[0]["allow_untitled"]
    record = sessions.get(tmp_root, sid)
    assert record and record["tab_display_name"]


def test_codex_ignores_the_daemons_inherited_ribbon_marker(tmp_root: Path, monkeypatch):
    """#67: Codex hooks run in a shared app-server daemon that kept the first ribbon tab's
    AGENT_RIBBON_LAUNCH, so every later Codex session claimed with that marker and found no tab
    (fresh Windows clone, 2026-09-30). The marker comes from the session's own TUI."""
    from agent_cli import codex_tab_link

    sid = "01a0f288-0000-7000-8000-000000000001"
    sessions.set_record(tmp_root, sid, name="mgr-030-x-codex-00000001")
    sessions.set_field(tmp_root, sid, "provider", "codex")
    monkeypatch.setenv("AGENT_RIBBON_LAUNCH", "codex:11111111-1111-4111-8111-111111111111")
    monkeypatch.setattr(codex_tab_link, "marker_for", lambda s: "codex:22222222-2222-4222-8222-222222222222")
    claimed: list[dict] = []
    monkeypatch.setattr(obsidian_tabs, "claim_active", lambda name, label, **kw: claimed.append(kw) or True)
    codex_activity(tmp_root, {"session_id": sid, "hook_event_name": "UserPromptSubmit"})
    assert claimed and claimed[0]["launch_marker"] == "codex:22222222-2222-4222-8222-222222222222"
