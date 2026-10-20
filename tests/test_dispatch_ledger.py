"""Pure helpers (schema checking, display trimming, plan/handoff line upsert), `doctor`'s
handoff warnings, and the PR ledger: `refresh_pr`, PR inheritance across a
stack, and `ledger_rows`/`render_ledger`.

`_pr`/`_record` are local: nothing
outside the PR ledger section needs them.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_cli import dispatch
from agent_cli import repos as repos_mod
from agent_cli.core import AgentError, iso, now
from dispatch_helpers import _log, _new, _plan, droot, envelope, one  # noqa: F401 -- droot is a fixture

# ------------------------------------------------------------------ pure helpers


def test_schema_checker():
    """`parse_envelope`, `stream_results`, `_json_object` and `extract_json_object` used to be
    checked here too. All four are gone with the homegrown transport (ADR-0009). The SDK hands
    over a typed result, so there is no text to parse and nothing to recover from."""
    good = envelope()["structured_output"]
    assert dispatch.check_schema(good, dispatch.RETURN_SCHEMA) == []
    bad = dict(good, status="maybe", extra=1)
    bad.pop("notes")
    problems = dispatch.check_schema(bad, dispatch.RETURN_SCHEMA)
    assert any("notes: missing" in p for p in problems)
    assert any("not in" in p for p in problems)
    assert any("extra: unexpected" in p for p in problems)
    assert dispatch.check_schema(
        dict(good, comments=[{"path": "a.py", "line": "1", "reason": "r"}]), dispatch.RETURN_SCHEMA
    ) == ["$.comments[0].line: expected integer"]
    # no maxLength any more (1.3): a long summary is never rejected, only trimmed for display
    assert dispatch.check_schema(dict(good, summary="x" * 1500), dispatch.RETURN_SCHEMA) == []


def test_display_summary_trims_for_display_only():
    """1.3: nothing about a worker's return is rejected for length any more. Only what a
    display prints gets shortened. The full text is never lost."""
    long = "x" * 3000
    short = "short and sweet"
    trimmed = dispatch.display_summary(long)
    assert len(trimmed) == dispatch.DISPLAY_SUMMARY_CHARS and trimmed.endswith("…")
    assert dispatch.display_summary(short) == short
    assert dispatch.display_summary(long, 120)[:119] == long[:119]


def test_a_1500_char_summary_reaches_done(droot: Path, fake_claude):
    """Regression: `RETURN_SCHEMA`'s old `maxLength: 1200` rejected this return outright.
    It cost three turns at max context on a dispatch that was already done. The schema no
    longer bounds summary length at all (1.3). Only the log line is display-capped."""
    long_summary = "detailed work " * 120  # > 1500 chars
    assert len(long_summary) > 1500
    fake_claude.scenario([one(envelope(summary=long_summary))])
    d = _new(droot)
    assert d["state"] == "done"
    assert d["result"]["summary"] == long_summary  # kept in full on the record
    log_text = _log(droot)
    assert long_summary not in log_text  # the log line is display-capped, not the full text
    assert long_summary[: dispatch.DISPLAY_SUMMARY_CHARS - 1] in log_text


def test_plan_sections_are_read_only_dispatch_views(droot: Path):
    from agent_cli import features as features_mod

    f = features_mod.resolve(droot, "1")
    assert dispatch.plan_section(f.body, "Context") == "the alpha service needs a readme line."
    assert dispatch.plan_section(f.body, "Nope") == ""
    assert 'await dv.view("harness/obsidian/views/handoffs")' in _plan(droot)
    assert not hasattr(dispatch, "upsert_plan_line")


# ------------------------------------------------------------------ doctor


def test_budget_hit_is_read_from_the_envelope_not_the_exit_code(tmp_root: Path):
    """A budget hit exits 1, not 2. Keying on the exit code meant a real budget
    hit fell through to `failed`, with no auto-resume and no synthesized return. The exit code
    was never a usable signal, fallback or otherwise. `_hit_budget` does not take one (1.6)."""
    from agent_cli import dispatch as dispatch_mod

    assert dispatch_mod.evaluate_mod._hit_budget({"terminal_reason": "budget_exhausted"}) is True
    assert dispatch_mod.evaluate_mod._hit_budget({"terminal_reason": "error_max_budget_usd"}) is True
    # an ordinary error is not a budget hit
    assert dispatch_mod.evaluate_mod._hit_budget({"terminal_reason": "error_during_execution"}) is False
    assert dispatch_mod.evaluate_mod._hit_budget({}) is False


# ------------------------------------------------------------------ PR ledger

GH_REPO = repos_mod.Repo(
    name="web-app",
    short="web",
    path="repos/web-app",
    remote="git@github.com:example-org/web-app.git",
)


def _pr(
    number: int = 42,
    state: str = "OPEN",
    merged_at=None,
    checked_at="2026-09-01T00:00:00-07:00",
    inherited_from=None,
) -> dict:
    return {
        "number": number,
        "url": f"https://github.com/example-org/web-app/pull/{number}",
        "state": state,
        "merged_at": merged_at,
        "checked_at": checked_at,
        "inherited_from": inherited_from,
    }


def _record(
    id_: str,
    *,
    branch: str | None,
    base: str = "origin/main",
    feature: int = 1,
    repo: str = "web-app",
    short: str = "web",
    pr=None,
    finished: str | None = None,
    attempt: int = 1,
    cost: float = 1.0,
) -> dict:
    return {
        "id": id_,
        "feature": feature,
        "repo": repo,
        "short": short,
        "branch": branch,
        "base": base,
        "attempt": attempt,
        "cost_usd": cost,
        "pr": pr,
        "finished": finished,
        "state": "done",
    }


def test_github_owner_repo_parses_ssh_and_https_remotes(tmp_root: Path):
    ssh = repos_mod.Repo(name="x", short="x", path="repos/x", remote="git@github.com:example-org/web-app.git")
    assert dispatch.ledger._github_owner_repo(tmp_root, ssh) == ("example-org", "web-app")
    https = repos_mod.Repo(
        name="x", short="x", path="repos/x", remote="https://github.com/example-org/web-app"
    )
    assert dispatch.ledger._github_owner_repo(tmp_root, https) == ("example-org", "web-app")


def test_github_owner_repo_falls_back_to_clone_origin_then_gives_up(tmp_root: Path):
    # alpha (tmp_root fixture) is a real clone whose origin is a plain bare local path, not
    # github. The fallback resolves it, finds it is not github either, and gives up with None.
    not_github = repos_mod.Repo(name="alpha-api", short="alpha", path="repos/alpha-api", remote="not-a-url")
    assert dispatch.ledger._github_owner_repo(tmp_root, not_github) is None
    # no clone at all: nothing to fall back to
    missing = repos_mod.Repo(name="Ghost", short="ghost", path="repos/Ghost", remote="not-a-url")
    assert dispatch.ledger._github_owner_repo(tmp_root, missing) is None


def test_refresh_pr_records_the_first_match_and_the_exact_gh_argv(tmp_root: Path, monkeypatch):
    d = _record("001-01", branch="agent/001-alpha-work-01")
    dispatch.save(tmp_root, d)
    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(argv)
        items = [
            {
                "number": 42,
                "url": "https://github.com/example-org/web-app/pull/42",
                "state": "OPEN",
                "mergedAt": None,
            }
        ]
        return SimpleNamespace(returncode=0, stdout=json.dumps(items), stderr="")

    monkeypatch.setattr(dispatch.ledger.subprocess, "run", fake_run)
    pr = dispatch.refresh_pr(tmp_root, d, GH_REPO, force=True)
    assert pr == {
        "number": 42,
        "url": "https://github.com/example-org/web-app/pull/42",
        "state": "OPEN",
        "merged_at": None,
        "checked_at": iso(now()),
        "inherited_from": None,
    }
    assert calls == [
        [
            "gh",
            "pr",
            "list",
            "--repo",
            "example-org/web-app",
            "--head",
            "agent/001-alpha-work-01",
            "--state",
            "all",
            "--json",
            "number,url,state,mergedAt",
        ]
    ]
    assert dispatch.load(tmp_root, "001-01")["pr"] == pr


def test_refresh_pr_empty_result_clears_the_field(tmp_root: Path, monkeypatch):
    d = _record("001-01", branch="agent/001-alpha-work-01", pr=_pr())
    dispatch.save(tmp_root, d)
    monkeypatch.setattr(
        dispatch.ledger.subprocess,
        "run",
        lambda argv, **kw: SimpleNamespace(returncode=0, stdout="[]", stderr=""),
    )
    pr = dispatch.refresh_pr(tmp_root, d, GH_REPO, force=True)
    assert pr is None
    assert dispatch.load(tmp_root, "001-01")["pr"] is None


def test_refresh_pr_skips_a_cached_hit_inside_six_hours(tmp_root: Path, monkeypatch):
    fresh = _pr(checked_at=iso(now()))
    d = _record("001-01", branch="b", pr=fresh)
    dispatch.save(tmp_root, d)

    def fail_run(*a, **k):
        raise AssertionError("gh should not have been called")

    monkeypatch.setattr(dispatch.ledger.subprocess, "run", fail_run)
    assert dispatch.refresh_pr(tmp_root, d, GH_REPO) == fresh


def test_refresh_pr_force_calls_even_when_fresh(tmp_root: Path, monkeypatch):
    fresh = _pr(checked_at=iso(now()))
    d = _record("001-01", branch="b", pr=fresh)
    dispatch.save(tmp_root, d)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        dispatch.ledger.subprocess,
        "run",
        lambda argv, **kw: calls.append(argv) or SimpleNamespace(returncode=0, stdout="[]", stderr=""),
    )
    dispatch.refresh_pr(tmp_root, d, GH_REPO, force=True)
    assert calls


def test_refresh_pr_nonzero_exit_leaves_the_field_unchanged(tmp_root: Path, monkeypatch):
    existing = _pr(checked_at="2020-01-01T00:00:00-07:00")
    d = _record("001-01", branch="b", pr=existing)
    dispatch.save(tmp_root, d)
    monkeypatch.setattr(
        dispatch.ledger.subprocess,
        "run",
        lambda argv, **kw: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    pr = dispatch.refresh_pr(tmp_root, d, GH_REPO, force=True)
    assert pr == existing
    assert dispatch.load(tmp_root, "001-01")["pr"] == existing


def test_refresh_pr_skips_without_a_branch_or_a_resolvable_remote(tmp_root: Path, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(
        dispatch.ledger.subprocess,
        "run",
        lambda argv, **kw: calls.append(argv) or SimpleNamespace(returncode=0, stdout="[]", stderr=""),
    )
    no_branch = _record("001-01", branch=None)
    dispatch.save(tmp_root, no_branch)
    assert dispatch.refresh_pr(tmp_root, no_branch, GH_REPO, force=True) is None
    not_github = repos_mod.Repo(name="Ghost", short="ghost", path="repos/Ghost", remote="not-a-url")
    has_branch = _record("001-02", branch="b", repo="Ghost", short="ghost")
    dispatch.save(tmp_root, has_branch)
    assert dispatch.refresh_pr(tmp_root, has_branch, not_github, force=True) is None
    assert calls == []


def test_apply_pr_inheritance_copies_the_nearest_descendants_pr(tmp_root: Path):
    parent = _record("001-01", branch="agent/001-a-01", base="origin/main", pr=None)
    child_pr = _pr(number=42, checked_at="2020-01-01T00:00:00-07:00")
    child = _record("001-02", branch="agent/001-a-02", base="agent/001-a-01", pr=child_pr)
    dispatch.save(tmp_root, parent)
    dispatch.save(tmp_root, child)
    changed = dispatch.apply_pr_inheritance(tmp_root, dispatch.load_all(tmp_root))
    assert [r["id"] for r in changed] == ["001-01"]
    reloaded = dispatch.load(tmp_root, "001-01")
    assert reloaded["pr"]["number"] == 42
    assert reloaded["pr"]["inherited_from"] == "001-02"
    # the donor itself is never written to
    donor = dispatch.load(tmp_root, "001-02")
    assert donor["pr"]["inherited_from"] is None
    assert donor["pr"] == child_pr


def test_apply_pr_inheritance_strips_a_leading_origin_prefix_from_base(tmp_root: Path):
    """A dispatch's `base` is `origin/agent/001-...-02` while its parent's `branch` has no
    `origin/` prefix. Without stripping it, the two never match and nothing inherits."""
    parent = _record("001-01", branch="agent/001-a-01", base="origin/main", pr=_pr(number=1))
    child = _record("001-02", branch="agent/001-a-02", base="origin/agent/001-a-01", pr=None)
    dispatch.save(tmp_root, parent)
    dispatch.save(tmp_root, child)
    changed = dispatch.apply_pr_inheritance(tmp_root, dispatch.load_all(tmp_root))
    assert [r["id"] for r in changed] == ["001-02"]
    assert dispatch.load(tmp_root, "001-02")["pr"]["inherited_from"] == "001-01"


def test_apply_pr_inheritance_runs_both_directions_ancestor_wins_own_pr_kept(tmp_root: Path):
    """A stacked shape: 02 is the PR head pushed to GitHub. 03, 06
    and 07 are cut from it and never get a PR of their own, because the manager pushes their
    commits onto 02's branch instead. 08 is cut from 07, two hops from 02. A second, separate
    stack (09 -> 11) shows a record with its own real PR keeping it, even though its base is a
    relative that also carries a (different) real PR."""
    r02 = _record(
        "003-02", branch="agent/003-st-02", base="origin/main", feature=5, pr=_pr(number=42, state="MERGED")
    )
    r03 = _record("003-03", branch="agent/003-st-03", base="origin/agent/003-st-02", feature=5, pr=None)
    r06 = _record("003-06", branch="agent/003-st-06", base="origin/agent/003-st-02", feature=5, pr=None)
    r07 = _record("003-07", branch="agent/003-st-07", base="origin/agent/003-st-02", feature=5, pr=None)
    r08 = _record("003-08", branch="agent/003-st-08", base="agent/003-st-07", feature=5, pr=None)
    r09 = _record(
        "003-09", branch="agent/003-st-09", base="origin/main", feature=5, pr=_pr(number=43, state="MERGED")
    )
    r11 = _record(
        "003-11",
        branch="agent/003-st-11",
        base="agent/003-st-09",
        feature=5,
        pr=_pr(number=44, state="MERGED"),
    )
    for r in (r02, r03, r06, r07, r08, r09, r11):
        dispatch.save(tmp_root, r)

    changed = dispatch.apply_pr_inheritance(tmp_root, dispatch.load_all(tmp_root))
    by_id = {r["id"]: r for r in changed}
    assert set(by_id) == {"003-03", "003-06", "003-07", "003-08"}
    for rid in ("003-03", "003-06", "003-07"):
        assert by_id[rid]["pr"]["number"] == 42
        assert by_id[rid]["pr"]["inherited_from"] == "003-02"
    # nearest ancestor: 08 is one hop from 07 (already resolved to 42) and two from 02
    assert by_id["003-08"]["pr"]["number"] == 42
    assert by_id["003-08"]["pr"]["inherited_from"] == "003-07"
    # a record with its own real PR is a source, never a target, even though its base is
    # another real-PR record's branch
    kept = dispatch.load(tmp_root, "003-11")
    assert kept["pr"]["number"] == 44
    assert kept["pr"]["inherited_from"] is None
    untouched_09 = dispatch.load(tmp_root, "003-09")
    assert untouched_09["pr"]["number"] == 43 and untouched_09["pr"]["inherited_from"] is None


def test_ledger_rows_by_pr_feature_and_week_with_totals_and_no_pr_group(tmp_root: Path):
    merged = _pr(number=42, state="MERGED", merged_at="2026-09-09T00:00:00-07:00")
    d1 = _record(
        "001-01", branch="b1", feature=1, pr=merged, finished="2026-09-01T10:00:00-07:00", attempt=2, cost=1.5
    )
    d2 = _record(
        "001-02", branch="b2", feature=1, pr=None, finished="2026-09-08T10:00:00-07:00", attempt=1, cost=2.0
    )
    d3 = _record(
        "002-01",
        branch="b3",
        feature=2,
        repo="alpha-api",
        short="alpha",
        pr=None,
        finished=None,
        attempt=1,
        cost=99.0,
    )
    for d in (d1, d2, d3):
        dispatch.save(tmp_root, d)

    by_pr = dispatch.ledger_rows(tmp_root, by="pr")
    assert [r["period"] for r in by_pr] == ["#42 (MERGED, web)", "(no pr)"]
    assert [r["dispatches"] for r in by_pr] == [1, 1]
    assert [r["attempts"] for r in by_pr] == [2, 1]
    assert [r["spend_usd"] for r in by_pr] == [None, None]
    assert [r["recorded_spend_usd"] for r in by_pr] == [1.5, 2.0]
    assert [r["legacy_cost_dispatches"] for r in by_pr] == [1, 1]
    assert "**total**" in dispatch.render_ledger(by_pr, "pr")
    assert dispatch.render_ledger([], "pr") == "_none_"

    by_feature = dispatch.ledger_rows(tmp_root, by="feature")
    assert len(by_feature) == 1
    assert by_feature[0]["period"] == "001"
    assert by_feature[0]["spend_usd"] is None
    assert by_feature[0]["recorded_spend_usd"] == 3.5
    assert by_feature[0]["ids"] == ["001-01", "001-02"]

    # an eval probe's spend stays out of dispatches and spend_usd and shows apart
    probe = _record(
        "001-03", branch="b4", feature=1, pr=None, finished="2026-09-08T11:00:00-07:00", attempt=1, cost=0.25
    )
    dispatch.save(
        tmp_root, {**probe, "kind": "probe", "last_result": {"type": "result", "total_cost_usd": 0.25}}
    )
    no_pr = next(r for r in dispatch.ledger_rows(tmp_root, by="pr") if r["period"] == "(no pr)")
    assert no_pr["dispatches"] == 1 and no_pr["spend_usd"] is None and no_pr["probe_spend_usd"] == 0.25
    assert no_pr["ids"] == ["001-02"]
    assert "eval probes, not counted above: $0.25" in dispatch.render_ledger(
        dispatch.ledger_rows(tmp_root, by="pr"), "pr"
    )

    by_week = dispatch.ledger_rows(tmp_root, by="week")
    assert sum(r["dispatches"] for r in by_week) == 2  # d3 is excluded: not finished
    assert [r["period"] for r in by_week] == sorted(r["period"] for r in by_week)
    with pytest.raises(AgentError):
        dispatch.ledger_rows(tmp_root, by="month")
