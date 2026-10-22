"""Session/capture stamping, the done path's options/prompt/settings/handoff/log record, the
return checks (item 1), and the repo lease around one attempt.


"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agent_cli import capture as capture_mod
from agent_cli import dispatch, frontmatter, launcher, sessions, templates
from agent_cli import features as features_mod
from agent_cli import plan_review_ledger as ledger
from agent_cli import repos as repos_mod
from agent_cli.core import AgentError
from agent_cli.ops import code_review as ops_code_review
from agent_cli.ops import plan_review as ops_plan_review
from dispatch_helpers import (  # noqa: F401 -- droot is a pytest fixture, used only as a parameter name
    PROMPT,
    _log,
    _new,
    _new_fast,
    _options,
    _plan,
    _set_alpha_dispatch,
    droot,
    envelope,
    one,
)

# ------------------------------------------------------------------ session / capture stamping


def test_set_record_and_capture_stamp_dispatch_id(droot: Path, monkeypatch):
    rec = sessions.set_record(
        droot,
        "w1",
        feature={"id": 1, "slug": "alpha-work", "dir": "features/001-alpha-work"},
        dispatch_id="001-mac-01",
    )
    assert rec["dispatch_id"] == "001-mac-01"
    assert sessions.set_record(droot, "w1", cwd="/x")["dispatch_id"] == "001-mac-01"  # preserved
    capture_mod.capture(
        droot, "stop", {"session_id": "w1", "hook_event_name": "Stop", "dispatch_id": "payload-ignored"}
    )
    line = json.loads((droot / ".agent/raw/001-alpha-work/2026-09-03.jsonl").read_text().splitlines()[-1])
    assert line["dispatch_id"] == "001-mac-01"
    assert line["extra"] == {"dispatch_id": "payload-ignored"}
    monkeypatch.setenv("AGENT_DISPATCH_ID", "001-09")
    capture_mod.capture(droot, "stop", {"session_id": "nobody", "hook_event_name": "Stop"})
    line = json.loads((droot / ".agent/raw/unassigned/2026-09-03.jsonl").read_text().splitlines()[-1])
    assert line["dispatch_id"] == "001-09"


# ------------------------------------------------------------------ the done path


def test_done_path_options_env_prompt_files_and_records(droot: Path, fake_claude):
    _set_alpha_dispatch(droot, allowed_tools=["Bash(uv run pytest:*)"])
    plan_before = _plan(droot)
    fake_claude.scenario([one(envelope(), commit="T-42 add readme line")])
    d = _new(droot, tasks=["1"], worker_session_id="11111111-1111-4111-8111-111111111111")
    assert d["state"] == "done"
    assert d["attempt"] == 1 and d["exit_code"] == 0 and d["stop_reason"] == "result"
    assert d["cost_usd"] == 0.42 and d["num_turns"] == 5
    # Commits and files come from git since the base; the worker's return does not carry them.
    assert [c["subject"] for c in d["git"]["commits"]] == ["T-42 add readme line"]
    assert "commits" not in d["result"] and "branch" not in d["result"]
    assert d["branch"] == "feature/alpha-work-mac-01"
    # The return is stored once: the envelope keeps its metadata and says where the return went.
    assert d["last_result"]["return_in"] == "result"
    assert "structured_output" not in d["last_result"] and "result" not in d["last_result"]
    assert d["last_result"]["session_id"] == d["session_id"] and d["last_result"]["total_cost_usd"] == 0.42

    call = fake_claude.calls()[0]
    opts = call["options"]
    dd = droot / ".agent/dispatches"
    assert opts.session_id == "11111111-1111-4111-8111-111111111111" and opts.resume is None
    # the address the owner sends to: `@001-mac-01` from their own terminal reaches this worker
    assert opts.extra_args["name"] == "001-mac-01"
    assert opts.output_format["schema"] == dispatch.RETURN_SCHEMA
    assert opts.permission_mode == "acceptEdits"
    assert opts.settings == str(dd / "001-mac-01.settings.json")
    # the SDK takes the servers inline, so the .mcp.json on disk is now a record for a human
    # resuming the session by hand rather than something the worker is pointed at
    assert opts.mcp_servers == json.loads((dd / "001-mac-01.mcp.json").read_text())["mcpServers"]
    assert opts.add_dirs == [str(droot / "features/001-alpha-work")]
    assert opts.system_prompt["preset"] == "claude_code"
    assert opts.system_prompt["append"] == (droot / "harness/worker-contract.md").read_text(encoding="utf-8")
    assert opts.max_budget_usd == 1.0
    assert opts.model is None and opts.effort is None
    # the permission rules go to the SDK as the same strings build_settings writes
    assert "Bash(uv run pytest:*)" in opts.allowed_tools and "Bash(git push:*)" in opts.disallowed_tools
    assert call["cwd"] == d["worktree"]
    env = call["env"]
    assert not any(k.startswith("CLAUDE") for k in env)
    assert env["AGENT_ROOT"] == str(droot)
    assert env["AGENT_DISPATCH_ID"] == "001-mac-01"
    assert env["AGENT_FEATURE"] == "001"

    # prompt composition (A3)
    text = call["prompt"]
    assert text == (dd / "001-mac-01.prompt.md").read_text(encoding="utf-8")
    assert text.startswith("# Work order 001-mac-01 — alpha-api\n\nnonce: " + d["nonce"] + "\n")
    assert "## Task\n\n" + PROMPT in text
    assert "## Feature context" in text and "the alpha service needs a readme line." in text
    assert (
        "## Tasks assigned to you" in text
        and "1. (#1) add the readme line" in text
        and "run the tests" not in text
    )
    assert "## Repo notes" in text
    assert "## Workspace" in text and "- base: origin/main" in text
    assert "- branch: feature/alpha-work-mac-01" in text
    assert "## Thread" not in text

    # settings / mcp / schema files (A3)
    settings = json.loads((dd / "001-mac-01.settings.json").read_text())
    allow, deny = settings["permissions"]["allow"], settings["permissions"]["deny"]
    assert (
        "Bash(uv run pytest:*)" in allow
        and "Edit" in allow
        and "mcp__agent__log" in allow
        and "Bash(git commit:*)" in allow
    )
    assert (
        "Bash(git push:*)" in deny
        and "Bash(git worktree:*)" in deny
        and f"Edit(//{droot}/**)" in deny
        and "Bash(rm -r*)" in deny
    )
    # Stop and SessionEnd are SDK hook callbacks now, not command hooks (T7 item 4). PreCompact
    # is the one still wired here. See test_stop_and_session_end_are_sdk_callbacks_not_commands.
    assert set(settings["hooks"]) == {"PreCompact"}
    # without this the worker binds its inbox socket but drops what arrives, so `@001-mac-01`
    # would silently do nothing
    assert settings["crossSessionInbound"] == "accept"
    # nothing durable in ~/.claude holds for workers too: a worker was writing auto-memory
    # notes under ~/.claude/projects/<worktree slug>/ until this was set
    assert settings["autoMemoryEnabled"] is False
    assert settings["attribution"] == {"commit": "", "pr": "", "sessionUrl": False}
    mcp = json.loads((dd / "001-mac-01.mcp.json").read_text())["mcpServers"]["agent"]
    assert (
        mcp["command"] == launcher.command_and_args(droot)[0]
        and mcp["args"] == [*launcher.command_and_args(droot)[1], "mcp", "--profile", "worker"]
        and mcp["alwaysLoad"] is True
    )
    assert mcp["env"] == {
        "AGENT_ROOT": str(droot),
        "AGENT_FEATURE": "001",
        "AGENT_DISPATCH_ID": "001-mac-01",
        "AGENT_DISPATCH_TASKS": "1",
    }
    assert json.loads((dd / "001-mac-01.schema.json").read_text()) == dispatch.RETURN_SCHEMA

    # session pre-mapped with dispatch_id (A1)
    rec = sessions.get(droot, d["session_id"])
    assert rec is not None
    assert rec["feature"]["id"] == 1 and rec["dispatch_id"] == "001-mac-01" and rec["cwd"] == d["worktree"]

    # handoff (A5)
    meta, sections = dispatch.parse_handoff(droot / d["handoff"])
    assert meta["id"] == "001-mac-01" and meta["status"] == "done" and meta["tags"] == ["handoff"]
    assert meta["links"] == ["[[features/001-alpha-work/plan|001 alpha-work]]"]
    assert (
        meta["cost_usd"] == 0.42
        and meta["budget_usd"] == 1.0
        and meta["tasks"] == [1]
        and meta["attempt"] == 1
    )
    assert "# Work order 001-mac-01" in sections["Prompt"]
    assert '"status": "done"' in sections["Return"] and "T-42 add readme line" in sections["Return"]
    assert (
        "manager → worker" in sections["Thread"]
        and "worker → manager (done, attempt 1, $0.42)" in sections["Thread"]
    )
    # the plan is untouched (its Handoffs view reads the handoff above), log callouts, chat notify
    assert templates.HANDOFFS_VIEW in _plan(droot) and _plan(droot) == plan_before
    log = _log(droot)
    assert "> [!dispatch] 2026-09-03 14:05\n> dispatch 001-mac-01 → alpha: add a readme line\n" in log
    assert (
        "budget $1.00 (repo default); model default; effort default; tasks #1; "
        f"notify {'+'.join(dispatch.default_channels())}; approval: yes" in log
    )
    assert (
        "> [!handoff] 2026-09-03 14:05\n> handoff 001-mac-01 ← alpha: done (attempt 1, 5 turns, $0.42)\n> added the line\n"
        in log
    )
    assert "> commits: " in log and "> next: push and open the PR" in log
    assert d["chat_pending"] is True
    assert (droot / "bases/handoffs.base").exists()


def test_model_and_raised_budget_and_just_go(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope())])
    with pytest.raises(AgentError, match="--reason"):
        _new_fast(droot, budget=5)
    d = _new_fast(droot, budget=5, budget_reason="big refactor", model="claude-opus-4-8", just_go=True)
    opts = _options(fake_claude)
    assert opts.max_budget_usd == 5.0
    assert opts.model == "claude-opus-4-8"
    assert d["budget_usd"] == 5.0 and d["budget_default_usd"] == 1.0
    assert "budget $5.00 (raised from $1.00: big refactor); model claude-opus-4-8; effort default" in _log(
        droot
    )
    assert "approval: just-go" in _log(droot)


def test_effort_and_choice_reason_in_options_and_handoff(droot: Path, fake_claude):
    """Neither model nor effort was ever chosen on any of the first nine dispatches (feature
    009 audit). Effort and the reason for it now travel with the dispatch the same way the
    model does."""
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot, effort="high", choice_reason="cross-cutting change, unfamiliar repo")
    assert _options(fake_claude).effort == "high"
    assert d["effort"] == "high" and d["effort_source"] == "passed"
    assert d["choice_reason"] == "cross-cutting change, unfamiliar repo"
    meta, _ = dispatch.parse_handoff(droot / d["handoff"])
    assert meta["effort"] == "high" and meta["choice_reason"] == "cross-cutting change, unfamiliar repo"
    assert "effort high; " in _log(droot)
    assert "why: cross-cutting change, unfamiliar repo" in _log(droot)


def test_effort_omitted_leaves_the_model_default(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    assert d["effort"] is None
    assert d["effort_source"] == "default"
    assert _options(fake_claude).effort is None


def test_effort_rejects_an_unknown_level(droot: Path, fake_claude):
    with pytest.raises(AgentError, match="--effort"):
        _new(droot, effort="extreme")


def test_effort_repo_default_applies_when_flag_absent(droot: Path, fake_claude):
    _set_alpha_dispatch(droot, effort="low")
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    assert d["effort"] == "low"
    assert d["effort_source"] == "repo"
    assert _options(fake_claude).effort == "low"


def test_a_resumed_dispatch_still_carries_effort(droot: Path, fake_claude):
    """`build_options` reads `effort` off the same record a resume reloads from disk, so the
    choice made on attempt 1 must still be on the options for attempt 2."""
    q = envelope("question", open_questions=["which port?"])
    fake_claude.scenario([one(q), one(envelope(cost=0.1))])
    d = _new(droot, effort="high")
    assert d["state"] == "question"
    r = dispatch.resume(droot, dispatch_id="001-mac-01", answer="use port 8080", watch=True)
    assert r["state"] == "done"
    assert _options(fake_claude, 1).resume == d["session_id"]
    assert _options(fake_claude, 1).effort == "high"


# ------------------------------------------------------------------ the retired report checks


def test_the_registry_refuses_retired_required_checks():
    base = {
        "version": 1,
        "repos": [{"name": "x", "path": "../x", "remote": "r", "dispatch": {"required_checks": []}}],
    }
    assert any("required_checks is retired" in p for p in repos_mod.validate_registry_data(base))
    base["repos"][0]["dispatch"] = {}
    assert repos_mod.validate_registry_data(base) == []


@pytest.mark.parametrize("status", ["done", "question", "blocked"])
def test_a_return_without_commands_is_never_demoted_and_git_supplies_the_files(
    droot: Path, fake_claude, status: str
):
    questions = ["which port?"] if status == "question" else []
    fake_claude.scenario([one(envelope(status, open_questions=questions), dirty=True)])
    d = _new(droot)
    assert d["state"] == status
    assert "checks" not in d and "files_diff" not in d
    assert "dirty.txt" in d["git"]["files"]
    _, sections = dispatch.parse_handoff(droot / d["handoff"])
    assert "- `dirty.txt`" in sections["Return"]
    assert "Required checks" not in sections and "Files diff (report only)" not in sections


def test_the_prompt_and_dry_run_carry_no_report_checks(droot: Path, fake_claude):
    p = _new(droot, dry_run=True)
    assert "required_checks" not in p
    fake_claude.scenario([one(envelope())])
    _new(droot)
    assert "## Required checks" not in fake_claude.calls()[0]["prompt"]


def test_handoff_keeps_git_facts_after_the_worktree_is_removed(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope(), commit="T-42 add readme line")])
    d = _new(droot)
    shutil.rmtree(d["worktree"])
    text = dispatch.render_handoff(droot, dispatch.load(droot, d["id"]), features_mod.resolve(droot, "1"))
    assert d["git"]["files"]
    assert "T-42 add readme line" in text
    assert all(f"- `{f}`" in text for f in d["git"]["files"])


def test_new_validations(droot: Path, fake_claude, run):
    with pytest.raises(AgentError, match="not in feature 001's repos"):
        _new(droot, repo="beta")
    with pytest.raises(AgentError, match="no task #9"):
        _new(droot, tasks=["9"])
    run(["task", "done", "1", "2"])
    with pytest.raises(AgentError, match="already done"):
        _new(droot, tasks=["2"])
    run(["task", "supersede", "1", "1", "--reason", "test", "--link", "none"])
    with pytest.raises(AgentError, match="already superseded"):
        _new(droot, tasks=["1"])
    assert run(["feature", "close", "1"]).code == 0  # no open task left
    with pytest.raises(AgentError, match="is done"):
        _new(droot)


def test_dry_run_writes_only_the_order_and_preview_binding(droot: Path, fake_claude):
    """Plan C2 retires zero writes: the order and its nonce binding precede approval."""
    before = _log(droot)
    p = _new(droot, dry_run=True, tasks=["1"])
    assert p["dry_run"] is True and p["id"] == "001-mac-01"
    # Plan §A3 (plan.md:60-64) replaces the unallocated dispatch id with a preview identity.
    suffix = p["preview_id"].removeprefix("preview-")[:12]
    assert p["goal"] == "add a readme line" and p["branch"] == f"feature/alpha-work-mac-01-{suffix}"
    assert (
        p["tasks"] == ["#1 add the readme line"]
        and p["budget_usd"] == 1.0
        and p["notify"] == dispatch.default_channels()
    )
    # the WHOLE work order, not a twelve-line head (T5 item 5): first line to last
    assert p["prompt_bytes"] > 100 and p["prompt_lines"] == len(p["prompt"].splitlines())
    assert p["preview_id"] != p["id"]
    assert p["prompt"].splitlines()[0] == f"# Work order {p['preview_id']} — alpha-api"
    assert p["prompt"].rstrip().endswith("(read-only)"), "the workspace block is the tail of the order"
    assert "the alpha service needs a readme line." in p["prompt"], "and the feature context is in it"
    assert not (droot / ".agent/dispatches").exists()
    assert (droot / p["work_order"]).read_bytes() == p["prompt"].encode("utf-8")
    assert not (droot / "features/001-alpha-work/handoffs").exists()
    assert not (droot / "repos/_worktrees").exists()
    assert _log(droot) == before and fake_claude.calls() == []


# ------------------------------------------------------------------ repo lease


def test_new_takes_the_repo_lease_and_releases_it_after_finalize(droot: Path, fake_claude):
    from agent_cli import leases

    fake_claude.scenario([one(envelope())])
    d = _new(droot)
    assert d["state"] == "done"
    assert leases.get(droot, "repo-slot:alpha/0") is None


def test_new_releases_the_repo_lease_after_cancel(droot: Path, fake_claude):
    from agent_cli import leases

    fake_claude.scenario([one(envelope(), sleep=30)])
    result: dict = {}

    def runner() -> None:
        try:
            result["d"] = _new(droot)
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
    assert leases.get(droot, "repo-slot:alpha/0") is not None  # held while the worker runs
    dispatch.cancel(droot, dispatch_id="001-mac-01")
    th.join(timeout=15)
    assert not th.is_alive()
    assert leases.get(droot, "repo-slot:alpha/0") is None


def test_resume_takes_the_repo_lease_again_and_a_held_repo_refuses_it(droot: Path, fake_claude):
    """`finalize` released the lease when the first attempt went terminal. A resume holds
    the repo again for its own attempt. It is refused the same way `new` is."""
    from agent_cli import leases

    q = envelope("question", open_questions=["which port?"])
    fake_claude.scenario([one(q), one(envelope(cost=0.1))])
    d = _new(droot)
    assert d["state"] == "question"
    assert leases.get(droot, "repo-slot:alpha/0") is None
    leases.take(droot, "repo:alpha", session_id="someone-else", pid=os.getpid())
    with pytest.raises(AgentError, match=r"repo:alpha is held by someone-else"):
        dispatch.resume(droot, dispatch_id="001-mac-01", answer="use port 8080", watch=True)
    assert dispatch.load(droot, "001-mac-01")["attempt"] == 1
    assert leases.get(droot, "repo-slot:alpha/0") is None
    leases.release(droot, "repo:alpha", "someone-else")
    r = dispatch.resume(droot, dispatch_id="001-mac-01", answer="use port 8080", watch=True)
    assert r["state"] == "done" and r["attempt"] == 2
    assert leases.get(droot, "repo-slot:alpha/0") is None


def test_a_held_repo_refuses_new_with_the_wait_line(droot: Path, fake_claude):
    from agent_cli import leases

    leases.take(droot, "repo:alpha", session_id="someone-else", pid=os.getpid())
    with pytest.raises(
        AgentError,
        match=r"held by someone-else .*action `wait`, area `repo:alpha`",
    ):
        _new(droot)
    assert not (droot / ".agent/dispatches/001-mac-01.json").exists()
    assert fake_claude.calls() == []


def test_dry_run_does_not_take_the_repo_lease(droot: Path, fake_claude):
    from agent_cli import leases

    p = _new(droot, dry_run=True)
    assert p["repo_lease"] is None
    assert leases.get(droot, "repo:alpha") is None
    leases.take(droot, "repo:alpha", session_id="someone-else", pid=os.getpid())
    p2 = _new(droot, dry_run=True)
    assert p2["repo_lease"]["status"] == "live" and p2["repo_lease"]["session_id"] == "someone-else"
    # still not taken by the preview itself
    lease = leases.get(droot, "repo:alpha")
    assert lease is not None
    assert lease["session_id"] == "someone-else"


def test_detach_spawns_supervisor_in_new_session(droot: Path, fake_claude, monkeypatch):
    spawned: dict = {}
    plan_before = _plan(droot)

    class FakePopen:
        pid = 4242

        def __init__(self, argv, **kw):
            spawned["argv"] = argv
            spawned.update(kw)

    monkeypatch.setattr(dispatch.supervisor, "popen", FakePopen)
    d = _new(droot, watch=False)
    assert d["state"] == "queued" and d["supervisor_pid"] == 4242
    assert spawned["argv"] == [*launcher.argv_prefix(droot), "dispatch", "run", "001-mac-01"]
    if launcher.is_windows():
        assert spawned["creationflags"] & 0x200  # CREATE_NEW_PROCESS_GROUP
    else:
        assert spawned["start_new_session"] is True
    assert not any(k.startswith("CLAUDE") for k in spawned["env"])
    # the queued handoff exists before the supervisor runs; the plan is untouched
    meta, sections = dispatch.parse_handoff(droot / d["handoff"])
    assert meta["status"] == "queued" and sections["Return"] == "_no return yet_"
    assert templates.HANDOFFS_VIEW in _plan(droot) and _plan(droot) == plan_before
    assert fake_claude.calls() == []
    with pytest.raises(AgentError, match="is queued"):
        dispatch.resume(droot, dispatch_id="001-mac-01", answer="x")


# ------------------------------------------------------------------ blind review dispatches

EARLIER_VERDICT = "The previous review returned VERDICT: revise because widget.py:1 was wrong."


def _context_names_a_verdict(root: Path) -> None:
    plan = root / "features/001-alpha-work/plan.md"
    meta, body = frontmatter.load(plan)
    frontmatter.dump(plan, meta, body.replace("## Context\n", f"## Context\n\n{EARLIER_VERDICT}\n"))


def _readonly(root: Path, monkeypatch: pytest.MonkeyPatch, order: str, provider: str, **over: Any) -> dict:
    """One `launch_readonly` launch through the real `dispatch.new`, placed in the alpha feature."""
    real = dispatch.new
    monkeypatch.setattr(
        dispatch,
        "new",
        lambda r, **kw: real(
            r, **{**kw, "feature": "1", "tasks": ["1"], "budget_reason": "review row", **over}
        ),
    )
    model, effort = ("gpt-6.1-sol", "high") if provider == "codex" else ("sonnet", "high")
    return ledger.launch_readonly(
        root,
        prompt=order,
        provider=provider,
        model=model,
        effort=effort,
        row="review",
        choice_reason="blind review",
        session_id=None,
        repo="alpha",
        target="main",
    )


def _orders() -> dict[str, tuple[str, str]]:
    entries = [{"id": "F1", "text": "widget.py:1 returns 2", "round": 1, "client": "codex"}]
    return {
        "plan reviewer": ("claude", ops_plan_review.work_order("ab" * 32, "# Plan\n\nBuild the widget.\n")),
        "code reviewer": (
            "codex",
            ops_code_review.work_order("aaa111", "bbb222", "1. `widget()` returns 3."),
        ),
        "verifier": ("codex", ledger.verifier_work_order("ab" * 32, "# Plan\n", entries)),
    }


@pytest.mark.parametrize("who", ["plan reviewer", "code reviewer", "verifier"])
def test_a_blind_review_prompt_carries_no_feature_context(droot: Path, monkeypatch, who: str):
    """A feature's Context records earlier verdicts; a reviewer that read them was not blind."""
    _context_names_a_verdict(droot)
    provider, order = _orders()[who]
    p = _readonly(droot, monkeypatch, order, provider, dry_run=True)
    text = p["prompt"]
    assert EARLIER_VERDICT not in text
    assert p["blind"] is True
    assert "Alpha work" not in text and "add the readme line" not in text
    for section in ("## Feature context", "## Tasks assigned to you", "## Repo notes", "feature folder"):
        assert section not in text
    assert "\nnonce: " in text and order.strip("\n") in text
    assert "## Workspace" in text and f"- branch: {p['branch']}" in text and "- review target: main" in text


def test_a_blind_dispatch_stays_blind_on_resume_and_escalation(droot: Path, monkeypatch, fake_claude):
    _context_names_a_verdict(droot)
    q = envelope("question", open_questions=["which file?"])
    fake_claude.scenario([one(q), one(envelope(cost=0.1))])
    _, order = _orders()["plan reviewer"]
    real = dispatch.new
    d = _readonly(droot, monkeypatch, order, "claude", watch=True)
    assert d["state"] == "question" and d["blind"] is True
    assert dispatch.load(droot, d["id"])["blind"] is True
    assert EARLIER_VERDICT not in dispatch.file(droot, d["id"], ".prompt.md").read_text(encoding="utf-8")
    dispatch.resume(droot, dispatch_id=d["id"], answer="read the plan file", watch=True)
    assert all(EARLIER_VERDICT not in c["prompt"] for c in fake_claude.calls())
    monkeypatch.setattr(dispatch, "new", real)
    up = dispatch.new(
        droot,
        repo=None,
        feature="1",
        prompt="Review again at high effort.",
        escalate_from=d["id"],
        budget_reason="review row",
        dry_run=True,
    )
    assert up["blind"] is True and EARLIER_VERDICT not in up["prompt"]
    assert "## Feature context" not in up["prompt"]


def test_an_ordinary_dispatch_keeps_its_feature_context(droot: Path):
    _context_names_a_verdict(droot)
    p = _new(droot, tasks=["1"], dry_run=True)
    assert "blind" not in p
    assert "## Feature context" in p["prompt"] and EARLIER_VERDICT in p["prompt"]
    assert "1. (#1) add the readme line" in p["prompt"] and "## Repo notes" in p["prompt"]
