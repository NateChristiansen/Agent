"""What a rubric row actually sends to a worker.

Rows supply the model and budget. Claude normally omits effort; Codex review/build carry
the owner's medium policy. Explicit choices and inherited effort take precedence. The model
a worker actually ran is recorded, since a vendor can move the alias a row names.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli import dispatch
from agent_cli.core import AgentError
from agent_cli.ops.dispatch import dispatch_preview_text
from dispatch_helpers import (  # noqa: F401 -- droot is a pytest fixture, used only as a parameter name
    _new_fast,
    _options,
    _set_alpha_dispatch,
    droot,
    envelope,
    one,
    says,
)


def _row(root: Path, **kw) -> dict:
    """A row's budget band sits above the fixture repo's $1 default, which still needs a reason."""
    kw.setdefault("budget_reason", "the row's band")
    return _new_fast(root, **kw)


def test_a_row_launch_sends_the_row_model_and_no_effort(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope())])
    d = _row(droot, row="build")
    opts = _options(fake_claude)
    assert opts.model == "opus"
    assert opts.effort is None, "a row must not pin effort; the vendor default applies"
    assert d["row"] == "build" and d["provider"] == "claude" and d["effort"] is None
    assert d["budget_usd"] == 20.0  # the top of the row's band
    assert "rubric row build" in d["choice_reason"]


def test_a_row_never_lowers_a_repo_budget_set_above_its_band(droot: Path, fake_claude):
    """A repo whose runs are a tree of delegate sessions sets a budget far above any row's band.
    A small band must not replace such a budget and stop the worker before the repo's
    own gate ran."""
    _set_alpha_dispatch(droot, max_budget_usd=150)
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot, row="mechanical")
    assert d["budget_usd"] == 150.0 and d["budget_default_usd"] == 150.0
    assert _options(fake_claude).max_budget_usd == 150.0


def _preview(root: Path) -> dict:
    return dispatch.new(
        root,
        repo="alpha",
        feature="1",
        prompt="x",
        row="build",
        budget_reason="band",
        dry_run=True,
        no_checks=True,
    )


def test_the_dry_run_says_what_the_omitted_effort_means(droot: Path, fake_claude):
    p = _preview(droot)
    assert p["effort"] == "vendor default"
    assert p["effort_source"] == "default"
    text = dispatch_preview_text(p)
    assert "row: build" in text and "provider: claude" in text
    assert "effort: vendor default (default)" in text


def test_the_preview_budget_line_names_a_row_raising_the_repo_default(droot: Path):
    _set_alpha_dispatch(droot, max_budget_usd=2)
    p = _new_fast(droot, row="build", dry_run=True, budget_reason="band")
    assert p["budget_source"] == "row" and p["budget_usd"] == 20.0
    lines = [ln for ln in dispatch_preview_text(p).splitlines() if ln.startswith("  budget:")]
    assert lines == ["  budget: $20.00 (row band; repo default $2.00)"]


def test_the_preview_budget_line_names_an_explicit_budget_and_its_reason(droot: Path):
    p = _new_fast(droot, budget=3, budget_reason="two repos", dry_run=True)
    text = dispatch_preview_text(p)
    assert "  budget: $3.00 (explicit budget_usd; repo default $1.00; reason: two repos)" in text


def test_the_preview_budget_line_names_the_repo_default(droot: Path):
    repo = dispatch_preview_text(_new_fast(droot, dry_run=True))
    assert "  budget: $1.00 (repo default, dispatch.max_budget_usd)" in repo


def test_the_preview_budget_line_names_the_harness_default(droot: Path):
    _set_alpha_dispatch(droot, max_budget_usd=None)
    p = _new_fast(droot, dry_run=True)
    assert p["budget_source"] == "default"
    assert "  budget: $2.00 (harness default)" in dispatch_preview_text(p)


def test_only_a_codex_preview_calls_the_budget_a_soft_cap(droot: Path):
    codex = dispatch_preview_text(
        _new_fast(droot, provider="codex", row="build", budget_reason="band", dry_run=True)
    )
    claude = dispatch_preview_text(_new_fast(droot, row="build", budget_reason="band", dry_run=True))
    assert "soft cap" in codex.split("  budget:")[1].splitlines()[0]
    assert "soft cap" not in claude


def test_effort_sources_name_their_origin_in_preview_and_record(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope()) for _ in range(2)])
    passed = _row(droot, row="build", effort="high", choice_reason="explicit selection")
    assert passed["effort_source"] == "passed"
    assert "explicit selection" in passed["choice_reason"]
    _set_alpha_dispatch(droot, effort="low")
    repo = _row(droot, row="build")
    assert repo["effort_source"] == "repo"
    assert "agent.yaml dispatch.effort" in repo["choice_reason"]
    preview = _row(droot, row="build", dry_run=True)
    assert preview["effort_source"] == "repo"
    assert "effort: low (repo)" in dispatch_preview_text(preview)


def test_a_repo_model_from_setting_is_refused_at_launch(droot: Path, fake_claude):
    """An earlier change stopped reading a repo's settings file for its model; a repo still naming one is
    refused rather than silently launched on the row model."""
    _set_alpha_dispatch(droot, model_from=".claude/settings.json")
    with pytest.raises(AgentError, match="dispatch.model_from is no longer read"):
        _row(droot, row="build")


def test_the_owner_only_row_is_refused(droot: Path, fake_claude):
    with pytest.raises(AgentError, match="owner's to choose"):
        _row(droot, row="max")


def test_a_repo_claude_model_never_reaches_a_codex_dispatch(droot: Path, fake_claude):
    """The Codex manager's review: a repo's agent.yaml named an Opus id, and a Codex row for that
    repo would have launched it. Repo model and effort settings are Claude's."""
    from agent_cli import repos as repos_mod
    from agent_cli.dispatch import rubric

    _set_alpha_dispatch(droot, model="claude-opus-4-8", effort="medium")
    rp = repos_mod.load_registry(droot).require("alpha")
    row = rubric.ROWS["build"]
    model, effort, reason, source = rubric.resolve(rp, rubric.CODEX, row, None, None, None)
    assert (model, effort) == ("gpt-6.1-sol", "medium")
    assert source == "row-policy"
    assert reason is not None and "claude-opus-4-8" not in reason
    model, effort, _, source = rubric.resolve(rp, rubric.CLAUDE, row, None, None, None)
    assert (model, effort) == ("claude-opus-4-8", "medium")
    assert source == "repo"


def test_the_resolved_model_is_recorded(droot: Path, fake_claude):
    """A row names an alias; a vendor can move it. The first reply's model id is recorded."""
    reply = says("working on it")
    reply["message"]["model"] = "claude-opus-9"
    fake_claude.scenario([one(reply, envelope())])
    d = _row(droot, row="build")
    record = dispatch.load(droot, d["id"])
    assert record["resolved_model"] == "claude-opus-9"


@pytest.mark.parametrize(
    "effort, parent, expected, source",
    [
        ("low", None, "low", "passed"),
        (None, "parent-id", None, "parent"),
        ("high", "parent-id", "high", "parent"),
    ],
)
def test_codex_row_policy_yields_to_explicit_effort_and_parent(droot: Path, effort, parent, expected, source):
    from agent_cli import repos
    from agent_cli.dispatch import rubric

    rp = repos.load_registry(droot).require("alpha")
    model, chosen, reason, origin = rubric.resolve(
        rp, "codex", rubric.ROWS["review"], None, effort, "selection evidence", parent
    )
    assert model == "gpt-6.1-sol" and chosen == expected and origin == source
    assert "owner policy" not in (reason or "")


def test_codex_effort_policy_belongs_to_row_when_model_changes(droot: Path):
    from dataclasses import replace

    from agent_cli import repos
    from agent_cli.dispatch import rubric

    row = replace(rubric.ROWS["build"], models={"codex": "gpt-6-astra"})
    rp = repos.load_registry(droot).require("alpha")
    model, effort, _, source = rubric.resolve(rp, "codex", row, None, None, None)
    assert (model, effort, source) == ("gpt-6-astra", "medium", "row-policy")


def test_codex_owner_row_policy_survives_an_explicit_model_choice(droot: Path):
    from agent_cli import repos
    from agent_cli.dispatch import rubric

    rp = repos.load_registry(droot).require("alpha")
    model, effort, _, source = rubric.resolve(rp, "codex", rubric.ROWS["review"], "gpt-6-luna", None, None)
    assert (model, effort, source) == ("gpt-6-luna", "medium", "row-policy")
