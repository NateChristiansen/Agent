"""Runnable verification is a launch prerequisite, distinct from report regexes and grants."""

from __future__ import annotations

import os
import shlex
from pathlib import Path

import pytest
import yaml

from agent_cli import dispatch, leases, repos
from agent_cli.core import AgentError
from agent_cli.dispatch import lifecycle, orders, supervisor
from agent_cli.dispatch.preflight import verification_preflight
from dispatch_helpers import _new, _set_alpha_dispatch, droot, envelope, one, settle  # noqa: F401


def unconfigure(root: Path) -> None:
    path = root / "agent.yaml"
    data = yaml.safe_load(path.read_text())
    config = next(repo for repo in data["repos"] if repo["short"] == "alpha")["dispatch"]
    config.pop("verify", None)
    config.pop("verify_timeout_seconds", None)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else None for path in root.rglob("*")
    }


def forbid_launch_mutations(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("denied launch reached a mutation boundary")

    for module, name in (
        (lifecycle.allocation, "claim"),
        (lifecycle.allocation, "short_guard"),
        (lifecycle.allocation, "alloc_guard"),
        (lifecycle.allocation, "area_guard"),
        (lifecycle.allocation.leases_mod, "take_marked"),
        (lifecycle.allocation.os, "link"),
        (lifecycle.gate, "record_guard"),
        (lifecycle.holdings, "acquire"),
        (lifecycle.holdings, "resume"),
        (lifecycle.holdings.leases_mod, "take"),
        (lifecycle.holdings.leases_mod, "release"),
        (lifecycle, "ensure_worktree"),
        (lifecycle, "reuse_worktree"),
        (lifecycle, "save"),
        (lifecycle, "start"),
        (lifecycle.sessions, "set_record"),
        (lifecycle, "write_worker_files"),
    ):
        monkeypatch.setattr(module, name, forbidden)


@pytest.mark.parametrize(
    "flags",
    [
        {},
        {"allow_unmatched": True},
        {"just_go": True},
        {"watch": False},
        {"allow_unmatched": True, "just_go": True, "watch": False},
        {"kind": "probe"},
    ],
)
def test_unconfigured_new_refuses_before_mutation(droot: Path, monkeypatch, fake_claude, flags):
    unconfigure(droot)
    _set_alpha_dispatch(droot, setup=["touch setup-must-not-run"])
    repo = repos.load_registry(droot).require("alpha")
    before, clone_before, env_before = snapshot(droot), snapshot(repo.abs_path(droot)), dict(os.environ)
    forbid_launch_mutations(monkeypatch)
    with pytest.raises(AgentError, match="runnable verification is not configured") as exc:
        _new(droot, **flags)
    assert "dispatch.verify_timeout_seconds" in str(exc.value)
    assert snapshot(droot) == before and snapshot(repo.abs_path(droot)) == clone_before
    assert dict(os.environ) == env_before and fake_claude.calls() == []


@pytest.mark.parametrize("no_checks", [None, 0, 1, "true", "false", []])
def test_no_checks_requires_exact_boolean(droot: Path, monkeypatch, no_checks):
    forbid_launch_mutations(monkeypatch)
    with pytest.raises(AgentError, match="explicit per-dispatch boolean"):
        _new(droot, no_checks=no_checks)


@pytest.mark.parametrize(
    "configured,no_checks,status,allowed",
    [
        (False, False, "unverified", False),
        (True, False, "unverified", True),
        (False, True, "exempt", True),
        (True, True, "exempt", True),
    ],
)
def test_dry_run_reports_configuration_without_claiming_verified(
    droot: Path, fake_claude, configured, no_checks, status, allowed
):
    if not configured:
        unconfigure(droot)
    before, env_before = snapshot(droot), dict(os.environ)
    preview = _new(droot, dry_run=True, no_checks=no_checks)
    # C2 still permits no allocation; §A3 (plan.md:60-64) adds an independent preview binding.
    order = droot / preview["work_order"]
    manifest = orders._path(droot, preview["preview_id"])
    legacy = orders._path(droot, preview["id"])
    before.update(
        {
            str(order.parent.relative_to(droot)): None,
            str(order.relative_to(droot)): preview["prompt"].encode("utf-8"),
            str(manifest.parent.relative_to(droot)): None,
            str(manifest.relative_to(droot)): manifest.read_bytes(),
            str(legacy.relative_to(droot)): legacy.read_bytes(),
        }
    )
    assessment = preview["verification"]
    assert assessment["status"] == status
    assert assessment["configuration"] == ("configured" if configured else "unconfigured")
    assert assessment["launch_allowed"] is allowed
    assert assessment["required"] is not no_checks
    if configured and not no_checks:
        assert assessment["commands"] == [{"command": "git diff --exit-code HEAD --", "timeout_seconds": 30}]
        assert assessment["timeout_seconds"] == 60
        assert "has not run" in assessment["reason"]
    else:
        assert assessment["commands"] == [] and assessment["timeout_seconds"] is None
    if no_checks:
        assert assessment["exemption"] == "explicit dispatch --no-checks"
        assert "does not make worker permissions read-only" in assessment["reason"]
    assert snapshot(droot) == before and dict(os.environ) == env_before
    assert fake_claude.calls() == []


def test_repo_level_no_checks_does_not_authorize_a_dispatch(tmp_root: Path):
    repo = repos.Repo("alpha", "alpha", "../alpha", "local", dispatch={"no_checks": True})
    assessment = verification_preflight(repo)
    assert assessment["launch_allowed"] is False and assessment["exemption"] is None


def test_explicit_exemption_launches_and_resumes_without_configuration(droot: Path, fake_claude):
    unconfigure(droot)
    fake_claude.scenario([one(envelope()), one(envelope())])
    d = _new(droot, no_checks=True)
    assert d["state"] == "done" and d["verification_result"]["status"] == "exempt"
    settle(droot, d)
    result = dispatch.resume(droot, dispatch_id=d["id"], answer="review once more", watch=True)
    assert result["state"] == "done" and result["verification_result"]["status"] == "exempt"
    assert len(fake_claude.calls()) == 2


def test_configured_launch_executes_real_supervisor_verifier(droot: Path, fake_claude):
    marker = droot / "verifier-ran"
    command = "printf checked > " + shlex.quote(str(marker))
    _set_alpha_dispatch(droot, verify=[{"command": command, "timeout_seconds": 3}], verify_timeout_seconds=5)
    fake_claude.scenario([one(envelope(), commit="add the worker line")])  # a change, so the verifier runs
    d = _new(droot)
    assert marker.read_text() == "checked"
    assert d["state"] == "done" and d["verification_result"]["status"] == "verified"
    assert d["verification"]["binding"]["attempt"] == 1


def test_removed_config_refuses_resume_without_record_or_lease_changes(droot: Path, fake_claude, monkeypatch):
    fake_claude.scenario([one(envelope())])
    d = _new(droot)
    unconfigure(droot)
    before = snapshot(droot)
    forbid_launch_mutations(monkeypatch)
    with pytest.raises(AgentError, match="runnable verification is not configured"):
        dispatch.resume(
            droot,
            dispatch_id=d["id"],
            answer="again",
            allow_unmatched=True,
            again=True,
            budget=2,
            budget_reason="explicit",
            timeout_minutes=90,
        )
    assert snapshot(droot) == before
    assert len(fake_claude.calls()) == 1
    assert leases.get(droot, "repo-slot:alpha/0") is None


def test_missing_config_escalation_is_not_exempt(droot: Path, fake_claude, monkeypatch):
    fake_claude.scenario([one(envelope())])
    old = _new(droot)
    settle(droot, old)
    unconfigure(droot)
    before = snapshot(droot)
    forbid_launch_mutations(monkeypatch)
    with pytest.raises(AgentError, match="runnable verification is not configured"):
        _new(droot, escalate_from=old["id"], allow_unmatched=True, just_go=True)
    assert snapshot(droot) == before and len(fake_claude.calls()) == 1


def queued(root: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    with monkeypatch.context() as patcher:
        patcher.setattr(lifecycle, "start", lambda root, d, watch: d)
        return _new(root, watch=False)


@pytest.mark.parametrize("pending_cleanup", [False, True])
def test_queued_config_removal_blocks_before_setup_and_worker(
    droot: Path, monkeypatch, fake_claude, pending_cleanup
):
    marker = droot / "setup-ran"
    _set_alpha_dispatch(droot, setup=["touch " + shlex.quote(str(marker))], allowed_tools=["Bash(touch:*)"])
    d = queued(droot, monkeypatch)
    if pending_cleanup:
        d["worker_cleanup_pending"] = True
        dispatch.save(droot, d)
    unconfigure(droot)
    monkeypatch.setattr(supervisor, "_launch_attempt", lambda *args: pytest.fail("worker/setup launch"))
    result = supervisor.run(droot, dispatch_id=d["id"])
    assert result["state"] == "blocked" and "runnable verification" in result["error"]
    assert not marker.exists() and fake_claude.calls() == []
    assert bool(leases.get(droot, "repo-slot:alpha/0")) is pending_cleanup
    assert bool(result.get("verification_cleanup_pending")) is pending_cleanup
    assert bool(result.get("terminal_committed")) is not pending_cleanup


def test_automatic_retry_reloads_config_before_second_worker(droot: Path, fake_claude, monkeypatch):
    _set_alpha_dispatch(droot, timeout_minutes=0.005)
    fake_claude.scenario([one(envelope(), sleep=20), one(envelope())])
    queue = supervisor.queue_auto_resume

    def remove_before_retry(*args):
        d = queue(*args)
        unconfigure(droot)
        return d

    monkeypatch.setattr(supervisor, "queue_auto_resume", remove_before_retry)
    result = _new(droot)
    assert result["state"] == "blocked" and result["attempt"] == 2
    assert "runnable verification" in result["error"]
    assert len(fake_claude.calls()) == 1
    assert result["budget_usd"] == 1 and result["max_turns"] == 200
    assert leases.get(droot, "repo-slot:alpha/0") is None


def test_no_checks_changes_no_permissions_or_environment_grants(droot: Path):
    checked = _new(droot, dry_run=True)["options"]
    exempt = _new(droot, dry_run=True, no_checks=True)["options"]
    for field in (
        "permission_mode",
        "settings",
        "add_dirs",
        "allowed_tools",
        "disallowed_tools",
        "env",
        "mcp_servers",
    ):
        assert exempt[field] == checked[field]


def test_resume_rechecks_after_acquiring_existing_guard(droot: Path, fake_claude, monkeypatch):
    from contextlib import contextmanager

    fake_claude.scenario([one(envelope())])
    d = _new(droot)
    record_before = dispatch.file(droot, d["id"], ".json").read_bytes()
    guard = lifecycle.gate.record_guard

    @contextmanager
    def remove_after_outer_check(root, dispatch_id):
        with guard(root, dispatch_id) as current:
            unconfigure(root)
            yield current

    monkeypatch.setattr(lifecycle.gate, "record_guard", remove_after_outer_check)
    monkeypatch.setattr(lifecycle.holdings.leases_mod, "take", lambda *a, **kw: pytest.fail("lease mutation"))
    monkeypatch.setattr(lifecycle, "ensure_worktree", lambda *a, **kw: pytest.fail("worktree mutation"))
    monkeypatch.setattr(lifecycle, "start", lambda *a, **kw: pytest.fail("worker start"))
    with pytest.raises(AgentError, match="runnable verification is not configured"):
        dispatch.resume(droot, dispatch_id=d["id"], answer="again")
    assert dispatch.file(droot, d["id"], ".json").read_bytes() == record_before
    assert len(fake_claude.calls()) == 1
