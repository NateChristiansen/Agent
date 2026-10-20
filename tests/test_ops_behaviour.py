"""The rest of ops.py's behaviour, off the CASES contract table: CLI text-match checks,
feature-close/worktree-cleanup, dispatch list/show, journal and the dispatch ledger.

Split out of test_ops.py; `test_ops_cases.py` carries the (op name, kwargs,
argv, mutates) contract table and its two parametrized checks, and this file imports its
`externals`/`twins` fixtures for the tests here that also need twin roots."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_cli import dispatch, ops
from agent_cli.core import json_default
from test_dispatch import PROMPT, droot, envelope, one  # noqa: F401 -- reuse the dispatch fixtures
from test_ops_cases import (  # noqa: F401 -- ops_root needs externals and its template
    _ops_template,
    externals,
    ops_root,
)

pytestmark = pytest.mark.usefixtures("notes_remote")


def test_op_text_matches_cli_text(ops_root, run):
    r = run(["--root", str(ops_root), "task", "list", "1"])
    op = ops.task_list(ops_root, feature="1")
    assert r.out == op.text + "\n"
    r = run(["--root", str(ops_root), "feature", "list"])
    assert r.out == ops.feature_list(ops_root).text + "\n"


def test_feature_close_preserves_transcripts(tmp_root: Path, run):
    """`feature_close` (plan 3.5) copies every transcript its raw captures reference, so a
    later /retro has evidence to read even after Claude Code sweeps ~/.claude/projects."""
    from agent_cli import ops

    run(["feature", "new", "x", "--title", "X"])
    run(["feature", "use", "1", "--session-id", "s1"])
    transcript = tmp_root.parent / "t.jsonl"
    transcript.write_text("hi\n", encoding="utf-8")
    run(
        ["capture", "--kind", "stop", "--stdin"],
        stdin=json.dumps({"session_id": "s1", "hook_event_name": "Stop", "transcript_path": str(transcript)}),
    )
    op = ops.feature_close(tmp_root, feature="1", status="done")
    assert op.data["transcripts_preserved"] == 1
    assert "preserved 1 transcript(s)" in op.text
    assert (tmp_root / ".agent/raw/001-x/transcripts/t.jsonl").exists()


def test_close_retires_worktrees_through_ops_not_the_cli_wrapper(tmp_root, monkeypatch):
    """Closing 002 through the MCP tool left its worktrees on disk: the cleanup lived in the
    CLI handler only, so the two paths behaved differently (ADR-0005 says they must not)."""
    from agent_cli import dispatch as dispatch_mod
    from agent_cli import ops

    ops.feature_new(tmp_root, slug="x", title="X", repos=["alpha"], use=False)
    calls: list[tuple[str, str | None]] = []

    def fake_cleanup(root, *, feature, force=False, session_id=None):
        calls.append((feature, session_id))
        return {"removed": ["/wt/alpha/001-x"], "skipped": [], "branches_kept": ["agent/001-x"]}

    monkeypatch.setattr(dispatch_mod, "cleanup", fake_cleanup)
    op = ops.feature_close(tmp_root, feature="1", status="done", session_id="closer")
    assert calls == [("1", "closer")]
    assert op.data["worktrees_removed"] == ["/wt/alpha/001-x"]
    assert "removed 1 worktree(s)" in op.text


def test_dispatch_list_and_show_match_cli(droot: Path, run, fake_claude):
    """The dispatch equivalent of `test_op_data_is_the_cli_json`/`test_op_text_matches_cli_text`
    for the other ops. `ops.dispatch_list`/`ops.dispatch_show` are what cli.py now calls too
    (2.2). This gives one summary format instead of `cli._dispatch_text` and
    `mcp_server._dispatch_line` drifting apart. Uses `droot` (worker contract + an alpha
    feature) and `fake_claude` instead of the generic `twins` fixture, which does not wire a
    dispatch."""
    fake_claude.scenario([one(envelope())])
    d = dispatch.new(droot, repo="alpha", feature="1", prompt=PROMPT, watch=True)

    r = run(["--root", str(droot), "--json", "dispatch", "list"])
    op = ops.dispatch_list(droot)
    assert r.code == 0, r.err
    assert json.dumps(op.data, indent=2, default=json_default) + "\n" == r.out
    r_text = run(["--root", str(droot), "dispatch", "list"])
    assert r_text.out == op.text + "\n"

    r = run(["--root", str(droot), "--json", "dispatch", "show", d["id"]])
    op2 = ops.dispatch_show(droot, dispatch_id=d["id"])
    assert r.code == 0, r.err
    assert json.dumps(op2.data, indent=2, default=json_default) + "\n" == r.out
    r_text2 = run(["--root", str(droot), "dispatch", "show", d["id"]])
    assert r_text2.out == op2.text + "\n"
    assert "take over: cd" in op2.text


# ------------------------------------------------------------------ journal


def test_journal_missing_op(tmp_root: Path, run, monkeypatch):
    run(["feature", "new", "x", "--title", "X"])
    monkeypatch.setenv("AGENT_NOW", "2026-09-05T09:00:00-07:00")
    run(["log", "1", "did a thing"])
    monkeypatch.setenv("AGENT_NOW", "2026-09-06T09:00:00-07:00")
    op = ops.journal_missing(tmp_root)
    assert op.data == {"missing": ["2026-09-03", "2026-09-05"], "stale": []}
    assert op.text == "2026-09-03\n2026-09-05"


# ------------------------------------------------------------------ dispatch ledger


def test_dispatch_ledger_refresh_reports_the_refreshed_count(droot: Path, fake_claude, monkeypatch):
    """`--refresh` calls `gh` for every dispatch record with a branch and says how many it
    touched. Without it (the CASES row above) nothing calls `gh` at all."""
    fake_claude.scenario([one(envelope())])
    dispatch.new(droot, repo="alpha", feature="1", prompt=PROMPT, watch=True)
    # alpha's registry remote is a local bare repo, not github (conftest's REGISTRY). The
    # owner/repo resolution itself is covered directly in test_dispatch.py. Here it is
    # stubbed to isolate what this test is actually about: the refreshed count and that a real
    # `gh` call happens.
    monkeypatch.setattr(dispatch.ledger, "_github_owner_repo", lambda root, rp: ("example", "alpha-api"))
    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(dispatch.ledger.subprocess, "run", fake_run)
    op = ops.dispatch_ledger(droot, refresh=True)
    assert "refreshed 1 record(s)" in op.text
    assert len(calls) == 1
    assert calls[0][:3] == ["gh", "pr", "list"]
    assert op.data == dispatch.ledger_rows(droot, by="pr")


def test_dispatch_ledger_no_refresh_makes_no_gh_call(droot: Path, fake_claude, monkeypatch):
    fake_claude.scenario([one(envelope())])
    dispatch.new(droot, repo="alpha", feature="1", prompt=PROMPT, watch=True)

    def fail_run(*a, **k):
        raise AssertionError("gh should not have been called")

    monkeypatch.setattr(dispatch.ledger.subprocess, "run", fail_run)
    op = ops.dispatch_ledger(droot)
    assert "no refresh" in op.text
    assert len(op.data) == 1
    row = op.data[0]
    assert row["period"] == "(no pr)"
    assert row["dispatches"] == row["attempts"] == 1
    assert row["spend_usd"] == row["recorded_spend_usd"] == 0.42
    assert row["unknown_cost_dispatches"] == row["legacy_cost_dispatches"] == 0
    assert row["ids"] == ["001-mac-01"]
