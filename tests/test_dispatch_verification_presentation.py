"""CLI/MCP/handoff verification wording never promotes plans or worker reports to evidence."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import pytest
import yaml
from mcp import Client

from agent_cli import dispatch, features, ops, repos
from agent_cli.dispatch import lifecycle
from agent_cli.dispatch.handoffs import render_handoff
from agent_cli.dispatch.state import verification_summary
from agent_cli.mcp import server as mcp_server
from dispatch_helpers import _new, _set_alpha_dispatch, droot, envelope, one  # noqa: F401


def remove_verifiers(root: Path) -> None:
    path = root / "agent.yaml"
    data = yaml.safe_load(path.read_text())
    config = next(repo for repo in data["repos"] if repo["short"] == "alpha")["dispatch"]
    config.pop("verify", None)
    config.pop("verify_timeout_seconds", None)
    path.write_text(yaml.safe_dump(data))


def mcp_text(root: Path, **kwargs) -> str:
    async def call():
        server = mcp_server.build_server(root, "manager")
        async with Client(server) as client:
            result = await client.call_tool("dispatch", kwargs)
        assert not result.is_error
        return "\n".join(getattr(content, "text", "") for content in result.content)

    return asyncio.run(call())


@pytest.mark.parametrize("configured,no_checks", [(True, False), (False, False), (False, True), (True, True)])
def test_cli_and_mcp_preview_distinguish_plan_configuration_and_exemption(
    droot: Path, run, configured, no_checks
):
    if not configured:
        remove_verifiers(droot)
    args = ["dispatch", "new", "alpha", "--feature", "1", "--prompt", "review README.md", "--dry-run"]
    if no_checks:
        args.append("--no-checks")
    before = (droot / "agent.yaml").read_bytes()
    cli = run(["--root", str(droot), *args])
    assert cli.code == 0, cli.err
    tool = mcp_text(
        droot,
        action="new",
        repo="alpha",
        feature="1",
        prompt="review README.md",
        dry_run=True,
        no_checks=no_checks,
        session_id="s1",
    )
    for text in (cli.out, tool):
        assert "supervisor verification: verified" not in text
        assert "configuration: " + ("configured" if configured else "unconfigured") in text
        if no_checks:
            assert "supervisor verification: exempt (explicit request; not verified)" in text
            assert "does not make worker permissions read-only" in text
            assert "planned supervisor checks (" not in text
        elif configured:
            assert "supervisor verification: unverified (planned checks have not run)" in text
            assert "git diff --exit-code HEAD --" in text
            assert "1. timeout 30s:" in text and "total verification timeout: 60s" in text
        else:
            assert "verification prerequisite: BLOCKED" in text
            assert "dispatch.verify_timeout_seconds" in text
    assert (droot / "agent.yaml").read_bytes() == before
    assert not (droot / ".agent/dispatches").exists()


def test_dry_run_preview_shows_verifier_env_names_never_values(droot: Path, run, monkeypatch):
    """A missing toolchain grant (`uv`, `npm`, ...) has to be visible in
    `--dry-run` before launch, not only after a blocked supervisor verification log. The
    verifier's `env` line sits next to the `supervisor verification` block, names only, same
    contract as the worker's own `options.env` line above it."""
    monkeypatch.setenv("SUPERVISOR_ONLY_SENTINEL", "must-not-appear")
    monkeypatch.setenv("PIP_CACHE_DIR", "/opt/cache/pip")
    _set_alpha_dispatch(droot, env_allow=["PIP_CACHE_DIR"])
    cli = run(
        [
            "--root",
            str(droot),
            "dispatch",
            "new",
            "alpha",
            "--feature",
            "1",
            "--prompt",
            "review README.md",
            "--dry-run",
        ]
    )
    assert cli.code == 0, cli.err
    assert "verifier env (" in cli.out
    assert "PATH" in cli.out
    assert "PIP_CACHE_DIR" in cli.out
    assert "SUPERVISOR_ONLY_SENTINEL" not in cli.out
    assert "must-not-appear" not in cli.out
    assert "/opt/cache/pip" not in cli.out


@pytest.mark.parametrize("status", ["unverified", "incomplete", "failed", "verified", "exempt"])
def test_record_assessment_is_consistent_across_cli_mcp_and_handoff(droot: Path, run, monkeypatch, status):
    monkeypatch.setattr(lifecycle, "start", lambda root, d, watch: d)
    d = _new(droot)
    d.update(
        state="blocked" if status in {"failed", "incomplete"} else "done",
        chat_pending=False,
        verification_result={"status": status, "reasons": ["synthetic recorded reason"]},
    )
    if status == "incomplete":
        d["verification_cleanup_pending"] = True
    dispatch.save(droot, d)
    record = dispatch.file(droot, d["id"], ".json")
    before = record.read_bytes()
    cli = run(["--root", str(droot), "dispatch", "show", d["id"]])
    assert cli.code == 0, cli.err
    tool = mcp_text(droot, action="show", dispatch_id=d["id"])
    for text in (cli.out, tool, ops.dispatch_list(droot, include_all=True).text):
        assert f"recorded supervisor verification: {status}" in text
        assert "synthetic recorded reason" in text
        if status == "incomplete":
            assert "repo lease retained" in text
    rendered = render_handoff(droot, d, features.resolve(droot, "1"))
    assert f"Recorded status: **{status}**" in rendered
    assert "does not rerun checks" in rendered
    if status == "exempt":
        assert "Exemption does not make worker permissions read-only" in rendered
    if status == "incomplete":
        assert "> [!warning] Cleanup incomplete" in rendered
    assert record.read_bytes() == before


def test_legacy_done_and_requested_no_checks_never_mint_assessment(droot: Path, run, monkeypatch):
    monkeypatch.setattr(lifecycle, "start", lambda root, d, watch: d)
    d = _new(droot)
    d.update(
        state="done",
        no_checks=True,
        chat_pending=False,
        result={
            "status": "done",
            "summary": "worker says verified",
            "verification_result": {"status": "verified", "reasons": []},
        },
    )
    d.pop("verification_result", None)
    d.pop("verification", None)
    dispatch.save(droot, d)
    handoff = droot / d["handoff"]
    handoff.write_text("# Historical handoff\n\nWorker said done.\n")
    record = dispatch.file(droot, d["id"], ".json")
    before, old_handoff = record.read_bytes(), handoff.read_bytes()
    shown = run(["--root", str(droot), "--json", "dispatch", "show", d["id"]])
    assert shown.code == 0 and shown.json["state"] == "done"
    assert shown.json["verification_result"]["status"] == "unverified"
    assert "recorded supervisor verification: unverified" in mcp_text(
        droot, action="show", dispatch_id=d["id"]
    )
    text = run(["--root", str(droot), "dispatch", "list", "--all"]).out
    assert "[done]" in text and "recorded supervisor verification: unverified" in text
    rendered = render_handoff(droot, d, features.resolve(droot, "1"))
    assert "Recorded status: **unverified**" in rendered
    assert record.read_bytes() == before and handoff.read_bytes() == old_handoff


@pytest.mark.parametrize(
    "raw",
    [None, "verified", {}, {"status": ["verified"], "reasons": []}, {"status": "verified", "reasons": "bad"}],
)
def test_invalid_recorded_assessment_is_unverified_without_mutation(raw):
    record = {"state": "done", "no_checks": True, "verification_result": raw}
    before = copy.deepcopy(record)
    assert verification_summary(record)["status"] == "unverified"
    assert record == before


@pytest.mark.parametrize("mode", ["verified", "failed", "exempt"])
def test_actual_supervisor_result_reaches_cli_and_generated_handoff(droot: Path, run, fake_claude, mode):
    if mode == "failed":
        _set_alpha_dispatch(
            droot, verify=[{"command": "exit 1", "timeout_seconds": 3}], verify_timeout_seconds=5
        )
    # A change, so the verifier runs (an unchanged dispatch takes the no-diff exemption).
    fake_claude.scenario([one(envelope(), commit="add the worker line")])
    d = _new(droot, no_checks=mode == "exempt")
    # A failing command is reported as failed, naming the command and its exit.
    expected = mode
    assert d["verification_result"]["status"] == expected
    cli = run(["--root", str(droot), "dispatch", "show", d["id"]])
    assert cli.code == 0 and f"recorded supervisor verification: {expected}" in cli.out
    assert "worker-reported done: added the line" in cli.out
    if mode == "failed":
        assert "[blocked]" in cli.out
    handoff = (droot / d["handoff"]).read_text()
    assert f"Recorded status: **{expected}**" in handoff
    assert "Required checks" not in handoff


def test_cli_and_mcp_help_explain_exemption_and_required_runnable_config(droot: Path, run):
    from agent_cli.mcp.specs_dispatch import DISPATCH_TOOLS

    new = run(["--root", str(droot), "dispatch", "new", "--help"])
    resume = run(["--root", str(droot), "dispatch", "resume", "--help"])
    assert new.code == resume.code == 0
    text = " ".join(new.out.split())
    assert "dispatch.verify" in text and "does not make worker permissions read-only" in text
    assert "current runnable verification configuration" in " ".join(resume.out.split())
    description = DISPATCH_TOOLS["dispatch"].description
    assert "never verified results" in description
    assert "legacy done records" in description


def test_tracked_example_verification_schema_is_valid_and_explicit():
    example = Path(__file__).resolve().parents[1] / "agent.example.yaml"
    data = yaml.safe_load(example.read_text())
    assert repos.validate_registry_data(data) == []
    config = repos.verification_config(data["repos"][0]["dispatch"])
    assert config is not None
    assert [(c.command, c.timeout_seconds) for c in config.commands] == [("npm test", 120)]
    assert config.timeout_seconds == 180
