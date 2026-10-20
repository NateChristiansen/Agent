"""A passed worker clock reaches `dispatch new`, wins over the repo's, and the record and the
preview say which one applied. Zero, negative and non-finite clocks refuse on
both `new` and resume."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from mcp import Client

from agent_cli import dispatch, ops, repos, repos_validate
from agent_cli.core import AgentError
from agent_cli.dispatch.lifecycle import check_timeout_minutes
from agent_cli.mcp import server as mcp_server
from dispatch_helpers import (  # noqa: F401 -- droot is a pytest fixture, used only as a parameter name
    _new_fast,
    _set_alpha_dispatch,
    droot,
    envelope,
    one,
)

BAD = [0, -1, float("nan"), float("inf"), float("-inf"), True]


@pytest.mark.parametrize("bad", BAD)
def test_the_validator_refuses_zero_negative_non_finite_and_bool(bad):
    with pytest.raises(AgentError, match="timeout_minutes must be"):
        check_timeout_minutes(bad)


def test_the_validator_refuses_an_int_too_large_for_a_float():
    """A2 review: float(10**400) raises OverflowError, which escaped as a crash."""
    with pytest.raises(AgentError, match="timeout_minutes must be"):
        check_timeout_minutes(10**400)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 10**400, 0, -1, True, "45"])
def test_registry_validation_refuses_a_bad_repo_timeout(bad):
    problems = repos_validate.validate_dispatch({"timeout_minutes": bad})
    assert problems == ["dispatch.timeout_minutes must be a positive, finite number"]


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("bad", [float("nan"), float("inf")], ids=["nan", "inf"])
def test_new_refuses_a_non_finite_repo_timeout_at_load(droot: Path, fake_claude, bad, dry_run: bool):
    _set_alpha_dispatch(droot, timeout_minutes=bad)
    with pytest.raises(AgentError, match=r"dispatch\.timeout_minutes must be"):
        _new_fast(droot, dry_run=dry_run)
    assert not fake_claude.calls()
    assert not (droot / ".agent/dispatches").exists() or not any((droot / ".agent/dispatches").iterdir())


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("bad", [float("nan"), float("inf")], ids=["nan", "inf"])
def test_new_checks_the_repo_timeout_even_past_registry_validation(
    droot: Path, fake_claude, monkeypatch: pytest.MonkeyPatch, bad, dry_run: bool
):
    """An agent.local.yaml override is applied after registry validation, so `new` checks the
    applied clock itself and names the repo setting."""
    _set_alpha_dispatch(droot, timeout_minutes=bad)
    monkeypatch.setattr(repos, "validate_registry_data", lambda data, *, unknown_keys=True: [])
    with pytest.raises(AgentError, match=r"alpha: dispatch\.timeout_minutes"):
        _new_fast(droot, dry_run=dry_run)
    assert not fake_claude.calls()
    assert not (droot / ".agent/dispatches").exists() or not any((droot / ".agent/dispatches").iterdir())


def test_a_passed_timeout_still_wins_over_a_bad_repo_value(droot: Path, monkeypatch: pytest.MonkeyPatch):
    _set_alpha_dispatch(droot, timeout_minutes=float("inf"))
    monkeypatch.setattr(repos, "validate_registry_data", lambda data, *, unknown_keys=True: [])
    p = _new_fast(droot, timeout_minutes=120, dry_run=True)
    assert (p["timeout_minutes"], p["timeout_source"]) == (120.0, "passed")


def test_the_validator_returns_a_float():
    assert check_timeout_minutes(120) == 120.0 and isinstance(check_timeout_minutes(120), float)
    assert check_timeout_minutes(0.5) == 0.5


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("bad", BAD)
def test_new_refuses_a_bad_timeout_before_writing_anything(droot: Path, fake_claude, bad, dry_run: bool):
    with pytest.raises(AgentError, match="timeout_minutes must be"):
        _new_fast(droot, timeout_minutes=bad, dry_run=dry_run)
    assert not fake_claude.calls()
    assert not (droot / ".agent/dispatches").exists() or not any((droot / ".agent/dispatches").iterdir())


def test_preview_shows_a_passed_timeout_over_the_repo_value(droot: Path):
    _set_alpha_dispatch(droot, timeout_minutes=45)
    p = _new_fast(droot, timeout_minutes=120, dry_run=True)
    assert p["timeout_minutes"] == 120.0 and p["timeout_source"] == "passed"
    assert "  timeout_minutes: 120.0 (passed)" in ops.dispatch_preview_text(p).splitlines()


def test_preview_shows_the_repo_value_when_none_is_passed(droot: Path):
    _set_alpha_dispatch(droot, timeout_minutes=30)
    p = _new_fast(droot, dry_run=True)
    assert p["timeout_minutes"] == 30.0 and p["timeout_source"] == "repo"
    assert "  timeout_minutes: 30.0 (repo)" in ops.dispatch_preview_text(p).splitlines()


def test_preview_shows_the_default_when_the_repo_sets_nothing(droot: Path):
    p = _new_fast(droot, dry_run=True)
    assert p["timeout_minutes"] == 45.0 and p["timeout_source"] == "default"
    assert "  timeout_minutes: 45.0 (default)" in ops.dispatch_preview_text(p).splitlines()


def test_record_carries_the_passed_timeout_and_its_source(droot: Path, fake_claude):
    _set_alpha_dispatch(droot, timeout_minutes=45)
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot, timeout_minutes=120)
    saved = dispatch.load(droot, d["id"])
    assert saved["timeout_minutes"] == 120.0 and saved["timeout_source"] == "passed"


@pytest.mark.parametrize(
    "repo_value,expected", [(45, (45.0, "repo")), (None, (45.0, "default"))], ids=["repo", "default"]
)
def test_record_carries_the_repo_or_default_timeout(droot: Path, fake_claude, repo_value, expected):
    if repo_value is not None:
        _set_alpha_dispatch(droot, timeout_minutes=repo_value)
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    saved = dispatch.load(droot, d["id"])
    assert (saved["timeout_minutes"], saved["timeout_source"]) == expected


def test_mcp_dispatch_new_forwards_timeout_minutes(droot: Path):
    _set_alpha_dispatch(droot, timeout_minutes=45)
    base = {"action": "new", "repo": "alpha", "feature": "1", "prompt": "add a line", "session_id": "s1"}

    async def call():
        server = mcp_server.build_server(droot, "manager")
        async with Client(server) as client:
            good = await client.call_tool("dispatch", {**base, "dry_run": True, "timeout_minutes": 120})
            bad = await client.call_tool("dispatch", {**base, "dry_run": True, "timeout_minutes": 0})
        return good, bad

    good, bad = asyncio.run(call())
    assert good.is_error is False
    assert good.structured_content["timeout_minutes"] == 120.0
    assert good.structured_content["timeout_source"] == "passed"
    text = "\n".join(getattr(c, "text", "") for c in good.content)
    assert "timeout_minutes: 120.0 (passed)" in text
    assert bad.is_error is True
    assert "positive, finite" in "\n".join(getattr(c, "text", "") for c in bad.content)


def test_cli_dispatch_new_forwards_timeout(droot: Path, run):
    _set_alpha_dispatch(droot, timeout_minutes=45)
    args = ["--root", str(droot), "dispatch", "new", "alpha", "--feature", "1", "--prompt", "add a line"]
    r = run(["--json", *args, "--timeout", "120", "--dry-run"])
    assert r.code == 0, r.err
    assert r.json["timeout_minutes"] == 120.0 and r.json["timeout_source"] == "passed"
    r = run([*args, "--timeout", "120", "--dry-run"])
    assert "timeout_minutes: 120.0 (passed)" in r.out
    r = run([*args, "--timeout", "nan", "--dry-run"])
    assert r.code != 0 and "positive, finite" in r.err
