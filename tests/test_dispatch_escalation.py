"""Escalation attempt limits, rubric bumps, session forks, turn caps, and worktree cleanup.

Shared fixtures and scenario-building helpers are in `dispatch_helpers.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli import dispatch, tidy
from agent_cli import repos as repos_mod
from agent_cli.core import AgentError
from agent_cli.dispatch import options as options_mod
from agent_cli.dispatch import rubric
from agent_cli.mcp import specs_dispatch
from agent_cli.mcp.tools import Ctx
from conftest import git
from dispatch_helpers import (  # noqa: F401 -- droot is a pytest fixture, used only as a parameter name
    PROMPT,
    _new_fast,
    _options,
    _set_alpha_dispatch,
    droot,
    envelope,
    one,
)

EFFORT_CASES = [
    ("claude", "sonnet", "opus"),
    ("claude", "opus", "opus"),
    ("codex", "gpt-6-sol", "gpt-6-astra"),
    ("codex", "gpt-6.1-sol", "gpt-6-astra"),
    ("codex", "gpt-6-luna", "gpt-6.1-sol"),
    ("codex", "gpt-6-astra", "gpt-6-astra"),
]

# ------------------------------------------------------------------ escalation guard (item 2)


def test_candidate_source_is_imported():
    import agent_cli

    source = Path(agent_cli.__file__).resolve()
    assert source.is_relative_to(Path(__file__).resolve().parents[1] / "src")


def test_next_rubric_tier_steps_the_model_and_keeps_effort_at_the_top():
    assert dispatch.next_rubric_tier("haiku", "low") == ("sonnet", "low")
    assert dispatch.next_rubric_tier("sonnet", None) == ("opus", None)
    assert dispatch.next_rubric_tier("opus", None) == ("opus", None)
    assert dispatch.next_rubric_tier("opus", "high") == ("opus", "high")
    assert dispatch.next_rubric_tier("opus", "xhigh") == ("opus", "xhigh")
    assert dispatch.next_rubric_tier("fable", "max") == ("fable", "max")
    assert dispatch.next_rubric_tier("claude-opus-4-8", None) == ("claude-opus-4-8", None)
    assert dispatch.next_rubric_tier(None, None) == (None, None)


def test_codex_top_rung_escalation_keeps_omission():
    rp = repos_mod.Repo("test", "test", ".", "unused")
    old = {"provider": "codex", "model": "gpt-6-astra", "effort": None}
    assert rubric.escalate(old, rp, None, None) == ("gpt-6-astra", None)


@pytest.mark.parametrize("provider,model,next_model", EFFORT_CASES)
def test_escalation_keeps_parent_omission_despite_new_repo_effort(provider: str, model: str, next_model: str):
    rp = repos_mod.Repo("test", "test", ".", "unused", dispatch={"effort": "high"})
    old = {"provider": provider, "model": model, "effort": None}
    assert rubric.escalate(old, rp, None, None) == (next_model, None)


@pytest.mark.parametrize("provider,model,next_model", EFFORT_CASES)
def test_escalation_keeps_explicit_parent_effort(provider: str, model: str, next_model: str):
    rp = repos_mod.Repo("test", "test", ".", "unused", dispatch={"effort": "low"})
    old = {"provider": provider, "model": model, "effort": "high"}
    assert rubric.escalate(old, rp, None, None) == (next_model, "high")


def test_public_preview_and_claude_launch_keep_omitted_parent_effort(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope("blocked")), one(envelope())])
    parent = _new_fast(droot, model="sonnet")
    _set_alpha_dispatch(droot, effort="high")
    op = specs_dispatch.dispatch(
        Ctx(root=droot, profile="manager"),
        action="new",
        repo="alpha",
        feature="1",
        prompt="finish",
        escalate_from=parent["id"],
        dry_run=True,
        no_checks=True,
        session_id="manager-test",
    )
    assert op.data["model"] == "opus" and op.data["effort_source"] == "parent"
    assert op.data["options"]["effort"] is None
    assert f"parent dispatch {parent['id']} omitted effort" in op.text
    # C2 binds the approved prompt: launch the exact public preview above.
    child = _new_fast(droot, prompt="finish", escalate_from=parent["id"])
    assert child["effort"] is None and child["effort_source"] == "parent"
    assert _options(fake_claude, 1).effort is None


def test_escalation_explicit_effort_wins_and_a_cross_provider_one_keeps_the_parents(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope("blocked")), one(envelope())])
    parent = _new_fast(droot, model="sonnet", effort="low")
    preview = _new_fast(
        droot, escalate_from=parent["id"], provider="codex", model="gpt-6-astra", dry_run=True
    )
    assert preview["model"] == "gpt-6-astra" and preview["effort"] == "low"
    assert preview["effort_source"] == "parent"
    preview = _new_fast(droot, escalate_from=parent["id"], effort="high", dry_run=True)
    assert preview["effort"] == "high" and preview["effort_source"] == "passed"
    child = _new_fast(droot, escalate_from=parent["id"], effort="high")
    assert child["effort"] == "high" and child["effort_source"] == "passed"
    assert _options(fake_claude, 1).effort == "high"


def test_codex_fake_launch_keeps_parent_omission(droot: Path, fake_claude, monkeypatch: pytest.MonkeyPatch):
    from agent_cli.dispatch import codex_transport

    fake_claude.scenario([one(envelope("blocked"))])
    parent = _new_fast(droot)
    old = dispatch.load(droot, parent["id"])
    old.update(provider="codex", model="gpt-6-sol", effort=None)
    dispatch.save(droot, old)
    _set_alpha_dispatch(droot, effort="high")
    seen = []

    def fake_run(root: Path, d: dict, repo: object, **kwargs: object) -> dict:
        seen.append(d.copy())
        final = envelope(nonce=d["nonce"])
        final["session_id"] = d["session_id"]
        return {
            "last_result": final,
            "session_established": True,
            "exit_code": 0,
            "timed_out": False,
            "stop_reason": "result",
            "resolved_model": "gpt-6-astra",
        }

    monkeypatch.setattr(codex_transport, "run_codex", fake_run)
    preview = _new_fast(droot, provider="codex", escalate_from=parent["id"], dry_run=True)
    assert preview["effort_source"] == "parent" and preview["options"]["effort"] is None
    assert preview["effort"] == "vendor default"
    child = _new_fast(droot, provider="codex", escalate_from=parent["id"])
    assert child["model"] == "gpt-6-astra" and child["effort"] is None
    assert child["effort_source"] == "parent" and seen[0]["effort"] is None


def test_resume_raises_at_the_3_to_4_attempt_boundary_unless_again(droot: Path, fake_claude):
    q = envelope("question", open_questions=["which port?"])
    fake_claude.scenario([one(q), one(q), one(q), one(envelope(cost=0.1))])
    d = _new_fast(droot)
    assert d["state"] == "question" and d["attempt"] == 1
    d = dispatch.resume(droot, dispatch_id="001-mac-01", answer="a", watch=True)
    assert d["state"] == "question" and d["attempt"] == 2
    # a third attempt (this resume) does not raise
    d = dispatch.resume(droot, dispatch_id="001-mac-01", answer="b", watch=True)
    assert d["state"] == "question" and d["attempt"] == 3
    escalate_cmd = "the dispatch tool, action new, repo alpha, escalate_from 001-mac-01, and a prompt"
    with pytest.raises(AgentError, match=escalate_cmd) as exc:
        dispatch.resume(droot, dispatch_id="001-mac-01", answer="c")
    assert "attempt 4" in str(exc.value) and "thrash" in str(exc.value)
    assert "playbook" not in str(exc.value), "a playbook id a reader cannot resolve"
    assert dispatch.load(droot, "001-mac-01")["attempt"] == 3, "the refused resume must not have run"
    d = dispatch.resume(droot, dispatch_id="001-mac-01", answer="c", watch=True, again=True)
    assert d["state"] == "done" and d["attempt"] == 4
    # A 1.2 MB tool result killed a worker under the SDK's 1 MB default reader buffer.
    for n in (0, 1):
        size = _options(fake_claude, n).max_buffer_size
        assert size == options_mod.WORKER_MAX_BUFFER_BYTES
        assert size > 1024 * 1024


def test_escalate_from_reuses_the_worktree_bumps_the_tier_and_forks_the_old_session(droot: Path, fake_claude):
    """T7 item 2: the escalated worker resumes the OLD session with `fork_session=True`. It
    carries the previous attempt's real context (what it read, ran and was refused) instead of
    the one manager message the paste could reach. `session_id` is pinned alongside `resume`,
    which the CLI allows precisely when `--fork-session` is set. Without it the fork would come
    back under an id `evaluate`'s session cross-check does not recognise."""
    from agent_cli import ops

    fake_claude.scenario([one(envelope("blocked", summary="stuck"), commit="T-42 partial"), one(envelope())])
    d1 = _new_fast(droot, model="haiku", effort="low", choice_reason="probe")
    assert d1["state"] == "blocked"
    d2 = _new_fast(
        droot,
        prompt="finish the readme line",
        escalate_from="001-mac-01",
        worker_session_id="22222222-2222-4222-8222-222222222222",
    )
    assert d2["state"] == "done"
    assert d2["escalated_from"] == "001-mac-01"
    assert d2["worktree"] == d1["worktree"] and d2["reused"] is True
    assert d2["branch"] == d1["branch"] and d2["base"] == d1["base"]
    assert d2["model"] == "sonnet" and d2["effort"] == "low"
    assert d2["effort_source"] == "parent" and _options(fake_claude, 1).effort == "low"
    text = fake_claude.calls()[1]["prompt"]
    assert "finish the readme line" in text
    # the prior commit is still there. The branch was checked out as-is, not reset.
    assert (
        git(["log", "--format=%s", d1["branch"]], droot / "repos/alpha-api").strip().splitlines()[0]
        == "T-42 partial"
    )
    opts = _options(fake_claude, 1)
    assert opts.resume == d1["session_id"] and opts.fork_session is True
    assert opts.session_id == d2["session_id"] != d1["session_id"]
    assert d2["forked_from_session"] == d1["session_id"]
    # the whole old conversation is in context, so nothing is pasted. The old work order and
    # its nonce are in there too. A worker that echoed the wrong one is a terminal fail.
    assert "## Escalated from" not in text and PROMPT not in text
    assert "## Forked from 001-mac-01" in text and f"The nonce for THIS dispatch is {d2['nonce']}" in text
    assert dispatch.options_preview(opts)["fork_session"] is True
    shown = ops.dispatch_show(droot, dispatch_id=d2["id"]).text
    assert f"forked from: 001-mac-01 (session {d1['session_id']})" in shown


def test_escalate_from_without_a_session_to_fork_still_pastes_the_last_message(droot: Path, fake_claude):
    """The fallback. An old record with no session id (one that never reached a launch) has
    nothing to fork. The escalation keeps the pasted prefix it always had. An escalation to
    another repo is refused."""
    fake_claude.scenario([one(envelope("blocked", summary="stuck")), one(envelope())])
    _new_fast(droot)
    with pytest.raises(AgentError, match="was dispatched to"):
        _new_fast(droot, repo="beta", escalate_from="001-mac-01")
    stale = dispatch.load(droot, "001-mac-01")
    stale["session_id"] = None
    dispatch.save(droot, stale)
    d2 = _new_fast(droot, prompt="finish the readme line", escalate_from="001-mac-01")
    assert d2["forked_from_session"] is None
    opts = _options(fake_claude, 1)
    assert opts.resume is None and opts.fork_session is False
    text = fake_claude.calls()[1]["prompt"]
    assert "## Escalated from 001-mac-01: the last instruction to the previous worker" in text
    assert PROMPT in text and "## Forked from" not in text


# ------------------------------------------------------------------ max_turns (T7 item 1)


def test_max_turns_reaches_the_worker_from_the_repo_and_shows_in_the_preview(droot: Path, fake_claude):
    """A worker that never reaches a return used to be bounded only by its budget and the
    clock, both of which are spent in full before they stop anything (audit section E)."""
    _set_alpha_dispatch(droot, max_turns=17)
    fake_claude.scenario([one(envelope())])
    preview = _new_fast(droot, dry_run=True)
    assert preview["options"]["max_turns"] == 17
    d = _new_fast(droot)
    assert d["max_turns"] == 17
    assert _options(fake_claude).max_turns == 17


def test_new_max_turns_overrides_the_repo_and_refuses_a_non_positive_cap(droot: Path, fake_claude):
    """The per-dispatch override the `max_turns_ends_blocked` eval probe needs. With neither
    set, the cap is the default; the preview carries what the launch passes (test above)."""
    _set_alpha_dispatch(droot, max_turns=None)
    assert _new_fast(droot, dry_run=True)["options"]["max_turns"] == repos_mod.DEFAULT_MAX_TURNS
    _new_fast(droot, max_turns=2, dry_run=True)
    fake_claude.scenario([one(envelope())])
    _new_fast(droot, max_turns=2)
    assert _options(fake_claude).max_turns == 2
    with pytest.raises(AgentError, match="--max-turns must be"):
        _new_fast(droot, max_turns=0)


def test_registry_validates_max_turns(droot: Path):
    for bad in (0, -1, 1.5, "many", True):
        assert repos_mod.validate_registry_data(
            {"repos": [{"name": "a", "path": "p", "remote": "r", "dispatch": {"max_turns": bad}}]}
        ) == ["repos[0] dispatch.max_turns must be a positive integer"]
    assert (
        repos_mod.validate_registry_data(
            {"repos": [{"name": "a", "path": "p", "remote": "r", "dispatch": {"max_turns": 200}}]}
        )
        == []
    )


def test_a_turn_cap_hit_is_blocked_naming_the_cap_and_never_auto_resumed(droot: Path, fake_claude):
    """Same shape as the budget hit, for the same reason. A cap the harness set is not a
    question the harness gets to answer. The CLI reports it as the subtype `error_max_turns`,
    read off the envelope rather than the exit code."""
    hit = {
        "type": "result",
        "subtype": "error_max_turns",
        "is_error": True,
        "duration_ms": 12,
        "duration_api_ms": 9,
        "session_id": "{{SESSION}}",
        "total_cost_usd": 0.31,
        "num_turns": 2,
        "result": "max turns reached",
        "structured_output": None,
    }
    fake_claude.scenario([one(hit)])
    d = _new_fast(droot, max_turns=2)
    assert d["state"] == "blocked"
    assert "turn cap 2" in d["error"] and "dispatch.max_turns" in d["error"]
    assert d["auto_resumes"] == [], "a turn cap never raises itself, any more than a budget does"
    assert d["result"]["status"] == "blocked" and "turn cap" in d["result"]["notes"]


def test_a_valid_done_that_coincides_with_the_turn_cap_stays_done(droot: Path, fake_claude):
    """The worker got there on its last allowed turn. Same carve-out the budget branch has."""
    done_at_the_cap = {**envelope(), "subtype": "error_max_turns", "terminal_reason": "max_turns"}
    fake_claude.scenario([one(done_at_the_cap)])
    d = _new_fast(droot, max_turns=2)
    assert d["state"] == "done" and d["result"]["summary"] == "added the line"


# ------------------------------------------------------------------ cleanup


@pytest.mark.parametrize("status", ["question", "blocked"])
@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("scope", ["feature", "dispatch_id"])
def test_cleanup_owner_wait_respects_force(droot: Path, fake_claude, status: str, force: bool, scope: str):
    """Audit 10 integration alignment overrides A4 R3's owner hold only with explicit force."""
    fake_claude.scenario([one(envelope(status))])
    waiting = _new_fast(droot)
    assert waiting["state"] == status
    selected = {scope: "1" if scope == "feature" else waiting["id"]}
    wt = Path(waiting["worktree"])
    for dry_run in (True, False):
        result = dispatch.cleanup(droot, **selected, force=force, dry_run=dry_run)
        if force:
            assert result["would_remove" if dry_run else "removed"] == [str(wt)]
            assert result["skipped"] == []
            assert result["branches_kept"] == [waiting["branch"]]
            assert wt.exists() is dry_run
        else:
            assert result["would_remove" if dry_run else "removed"] == []
            assert result["skipped"][0]["reason"] == "waiting on the owner"
            assert wt.exists()


def test_cleanup_ignores_private_links_and_preserves_targets(droot: Path, fake_claude):
    from agent_cli import links

    fake_claude.scenario([one(envelope())])
    done = _new_fast(droot)
    wt = Path(done["worktree"])
    main = droot / "repos/alpha-api"
    (main / ".git/info").mkdir(exist_ok=True)
    with (main / ".git/info/exclude").open("a") as stream:
        stream.write("\nprivate\nprivate.md\n")
    target = droot / "private-target"
    target.mkdir()
    draft = target / "draft.md"
    draft.write_text("private work\n")
    links.make(wt / "private", target, is_dir=True)
    links.make(wt / "private.md", draft, is_dir=False)
    if not links.is_link(wt / "private.md"):
        pytest.skip("file symlinks unavailable")
    for dry_run in (True, False):
        result = dispatch.cleanup(droot, dispatch_id=done["id"], dry_run=dry_run)
        assert result["would_remove" if dry_run else "removed"] == [str(wt)]
        assert draft.read_text() == "private work\n"
    assert not wt.exists()


@pytest.mark.parametrize("surface", ["cli", "mcp"])
def test_single_dispatch_cleanup_allows_a_running_sibling(droot: Path, fake_claude, surface: str, capsys):
    """Audit 10 plan §A4 permits cleanup while another tree of this open feature runs."""
    import json
    import os

    from agent_cli import cli, leases
    from agent_cli.core import Output

    fake_claude.scenario([one(envelope(), commit="T-42 landed"), one(envelope())])
    done = _new_fast(droot)
    sibling = _new_fast(droot)
    assert done["worktree"] != sibling["worktree"]
    git(["merge", "--ff-only", done["branch"]], droot / "repos/alpha-api")
    sibling["state"] = "running"
    dispatch.save(droot, sibling)
    sibling_tree = Path(sibling["worktree"])
    for area in (f"worktree:alpha/{sibling_tree.name}", "repo-slot:alpha/0"):
        leases.take_marked(
            droot, area, session_id="sibling-holder", pid=os.getpid(), worktree_key=str(sibling_tree)
        )
    sibling_before = dispatch.load(droot, sibling["id"])
    for dry_run in (True, False):
        if surface == "mcp":
            result = specs_dispatch.dispatch(
                Ctx(root=droot, profile="manager"), action="cleanup", dispatch_id=done["id"], dry_run=dry_run
            ).data
        else:
            args = cli.parse(
                [
                    "--json",
                    "dispatch",
                    "cleanup",
                    "--dispatch-id",
                    done["id"],
                    *(["--dry-run"] if dry_run else []),
                ]
            )
            assert cli.dispatch_cleanup(droot, args, Output(as_json=True)) == 0
            result = json.loads(capsys.readouterr().out)
        assert result["would_remove" if dry_run else "removed"] == [done["worktree"]]
        assert result["skipped"] == []
        assert Path(done["worktree"]).exists() is dry_run
        assert Path(sibling["worktree"]).exists()
        assert dispatch.load(droot, sibling["id"]) == sibling_before
    assert done["branch"] in git(["branch", "--list"], droot / "repos/alpha-api")


def test_single_dispatch_cleanup_cannot_remove_a_shared_running_tree(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope()), one(envelope())])
    done = _new_fast(droot)
    sibling = _new_fast(droot)
    assert done["worktree"] == sibling["worktree"]
    sibling["state"] = "running"
    dispatch.save(droot, sibling)
    with pytest.raises(AgentError, match="still running"):
        dispatch.cleanup(droot, dispatch_id=done["id"], force=True)
    assert Path(done["worktree"]).exists()


@pytest.mark.parametrize("force", [False, True])
def test_dispatch_cleanup_preserves_an_ignored_product_draft(droot: Path, fake_claude, force: bool):
    fake_claude.scenario([one(envelope())])
    done = _new_fast(droot)
    wt = Path(done["worktree"])
    main = droot / "repos/alpha-api"
    (main / ".git/info").mkdir(exist_ok=True)
    with (main / ".git/info/exclude").open("a") as stream:
        stream.write("\n.agent/\n.venv/\nnode_modules/\n")
    for name in (".agent/scratch/draft.md", ".venv/lib/cache.py", "node_modules/pkg/index.js"):
        path = wt / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("work\n")
    assert git(["status", "--porcelain"], wt) == ""
    for dry_run in (True, False):
        result = dispatch.cleanup(droot, dispatch_id=done["id"], force=force, dry_run=dry_run)
        assert result["would_remove" if dry_run else "removed"] == []
        assert ".agent/scratch/draft.md" in result["skipped"][0]["reason"]
        assert ".venv" not in result["skipped"][0]["reason"]
        assert "node_modules" not in result["skipped"][0]["reason"]
        assert (wt / ".agent/scratch/draft.md").read_text() == "work\n"


def test_cleanup_dry_run_then_skips_dirty_removes_clean_keeps_branch_refuses_running(
    droot: Path, fake_claude
):
    fake_claude.scenario([one(envelope(), commit="T-42 one", dirty=True), one(envelope())])
    d1 = _new_fast(droot)
    d2 = _new_fast(droot)
    main = droot / "repos/alpha-api"
    before = {i: dispatch.load(droot, i) for i in ("001-mac-01", "001-mac-02")}
    ctx = Ctx(root=droot, profile="manager")
    op = specs_dispatch.dispatch(ctx, action="cleanup", feature="1", dry_run=True)
    assert op.data["dry_run"] is True and "removed" not in op.data
    assert op.data["would_remove"] == [d2["worktree"]]
    assert op.data["skipped"][0]["id"] == "001-mac-01" and "dirty" in op.data["skipped"][0]["reason"]
    assert op.text.startswith("dry run:")
    assert Path(d1["worktree"]).is_dir() and Path(d2["worktree"]).is_dir()
    assert {i: dispatch.load(droot, i) for i in before} == before
    real = specs_dispatch.dispatch(ctx, action="cleanup", feature="1")
    assert real.data["removed"] == op.data["would_remove"] and "dry_run" not in real.data
    assert real.data["skipped"][0]["id"] == "001-mac-01" and "dirty" in real.data["skipped"][0]["reason"]
    assert Path(d1["worktree"]).is_dir() and not Path(d2["worktree"]).exists()
    assert dispatch.load(droot, "001-mac-02")["worktree_removed"] is True
    assert "uncollected files" in dispatch.cleanup(droot, feature="1", force=True)["skipped"][0]["reason"]
    dispatch.collect_files(droot, dispatch_id="001-mac-01", paths=["dirty.txt"])
    r = dispatch.cleanup(droot, feature="1", force=True)
    assert r["removed"] == [d1["worktree"]] and not Path(d1["worktree"]).exists()
    assert "feature/alpha-work-mac-01" in git(["branch", "--list"], main)
    assert "feature/alpha-work-mac-02" in git(["branch", "--list"], main)
    assert git(["worktree", "list"], main).count("\n") == 1
    d = dispatch.load(droot, "001-mac-01")
    d["state"] = "running"
    dispatch.save(droot, d)
    with pytest.raises(AgentError, match="still running"):
        dispatch.cleanup(droot, feature="1")


def test_cleanup_dedupes_a_shared_worktree_and_resume_recreates_it(droot: Path, fake_claude):
    """d2 reuses d1's clean, done worktree (1.5), so both records point at one physical path.
    Cleanup must remove it once, not twice. It must not raise on the second record."""
    fake_claude.scenario([one(envelope())])
    d1 = _new_fast(droot)
    d2 = _new_fast(droot)
    assert d2["worktree"] == d1["worktree"]
    r = dispatch.cleanup(droot, feature="1")
    assert r["removed"] == [d1["worktree"]]
    assert r["skipped"] == []
    assert not Path(d1["worktree"]).exists()
    assert dispatch.load(droot, "001-mac-01")["worktree_removed"] is True
    assert dispatch.load(droot, "001-mac-02")["worktree_removed"] is True
    r = dispatch.resume(droot, dispatch_id="001-mac-02", answer="one more thing", watch=True)
    assert Path(r["worktree"]).is_dir() and r["worktree_removed"] is False
    head = git(["rev-parse", "--abbrev-ref", "HEAD"], Path(r["worktree"])).strip()
    assert head == "feature/alpha-work-mac-02"


def test_generated_output_does_not_hold_a_resolved_dispatch_tree(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope(), commit="T-42 one")])
    d = _new_fast(droot)
    wt = Path(d["worktree"])
    assert dispatch.cleanup(droot, feature="1")["skipped"][0]["reason"] == tidy.UNRESOLVED_BRANCH
    assert tidy.UNRESOLVED_BRANCH.startswith("unresolved branch")
    tip = git(["rev-parse", "HEAD"], wt).strip()
    current = dispatch.load(droot, d["id"])
    current["integration"] = {"state": "landed", "tip": tip}
    dispatch.save(droot, current)
    for directory in (".bt1", ".pytest-tmp", "pytest-temproot"):
        (wt / directory).mkdir()
        (wt / directory / "out.txt").write_text("generated\n")
    result = dispatch.cleanup(droot, feature="1")
    assert result["removed"] == [str(wt)] and not wt.exists()
    assert current["branch"] in git(["branch", "--list"], droot / "repos/alpha-api")


def test_collect_files_preserves_output_and_refuses_changed_or_unselected_files(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    wt = Path(d["worktree"])
    (wt / "output.txt").write_text("first\n", encoding="utf-8")
    (wt / "other.txt").write_text("keep\n", encoding="utf-8")

    assert "uncollected files" in dispatch.cleanup(droot, feature="1", force=True)["skipped"][0]["reason"]
    with pytest.raises(AgentError, match="unignored regular file"):
        dispatch.collect_files(droot, dispatch_id=d["id"], paths=["../outside.txt"])

    result = dispatch.collect_files(droot, dispatch_id=d["id"], paths=["output.txt"])
    artifact = droot / result["artifacts"][0]
    assert artifact.read_text(encoding="utf-8") == "first\n"
    (wt / "output.txt").write_text("changed\n", encoding="utf-8")
    assert "uncollected files" in dispatch.cleanup(droot, feature="1", force=True)["skipped"][0]["reason"]

    dispatch.collect_files(droot, dispatch_id=d["id"], paths=["other.txt"])
    newer = dispatch.collect_files(droot, dispatch_id=d["id"], paths=["output.txt"])
    assert dispatch.cleanup(droot, feature="1", force=True)["removed"] == [str(wt)]
    assert artifact.read_text(encoding="utf-8") == "first\n"
    assert (droot / newer["artifacts"][0]).read_text(encoding="utf-8") == "changed\n"
