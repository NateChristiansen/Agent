"""One explicit MCP composition per attempt, serialized identically for SDK and sidecar.

Fake worker transport tests prove launch configuration, not native MCP enforcement.
"""

import json
import os
from pathlib import Path

import pytest
import yaml
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from agent_cli import dispatch, repos
from agent_cli.core import AgentError
from agent_cli.dispatch import mcp_policy
from agent_cli.dispatch import options as options_mod
from conftest import git
from dispatch_helpers import (  # noqa: F401 — pytest fixture registration
    _new_fast,
    _set_alpha_dispatch,
    droot,
    envelope,
    one,
)
from dispatch_helpers import options_record as record

SECRET = "synthetic-mcp-secret"


def configure(root: Path, *, commit: bool = False) -> None:
    _set_alpha_dispatch(root, repo_mcp=["api"])
    reg = root / "agent.yaml"
    data = yaml.safe_load(reg.read_text())
    data["worker_mcp"] = {"qa": {"server": "browser", "command": "fixture-browser"}}
    reg.write_text(yaml.safe_dump(data))
    clone = root / "repos/alpha-api"
    (clone / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "api": {"type": "http", "url": "https://fixture.invalid/" + SECRET},
                    "ignored": {"command": "never-start"},
                }
            }
        )
    )
    if commit:
        git(["add", ".mcp.json"], clone)
        git(["commit", "-m", "fixture MCP definitions"], clone)


@pytest.mark.parametrize("selection", ["absent", None, []])
@pytest.mark.parametrize("resume", [False, True])
def test_strict_default_for_initial_and_resumed_options(droot: Path, selection, resume: bool):
    repo = repos.load_registry(droot).require("alpha")
    if selection != "absent":
        repo.dispatch["repo_mcp"] = selection
    (repo.abs_path(droot) / ".mcp.json").write_text("unselected invalid file")
    options = dispatch.build_options(droot, record(droot), repo, resume=resume)
    assert options.strict_mcp_config is True
    assert isinstance(options.mcp_servers, dict)
    assert list(options.mcp_servers) == ["agent"]
    assert options.setting_sources is None
    assert callable(options.can_use_tool)
    assert set(options.hooks or {}) == {"PreToolUse", "Stop", "SessionEnd"}
    assert options.env["AGENT_FEATURE"] == "001"
    assert options.resume == (record(droot)["session_id"] if resume else None)


def test_sidecar_sdk_and_preview_share_one_composition(droot: Path, monkeypatch: pytest.MonkeyPatch):
    configure(droot)
    repo = repos.load_registry(droot).require("alpha")
    d = {**record(droot), "mcp": ["qa"]}
    original, calls = mcp_policy.resolve, []

    def count(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(mcp_policy, "resolve", count)
    mcp = dispatch.write_worker_files(droot, d, repo)
    (repo.abs_path(droot) / ".mcp.json").write_text("changed after composition")
    options = dispatch.build_options(droot, d, repo, resume=False, mcp=mcp)
    preview = dispatch.options_preview(options, repo.abs_path(droot))
    sidecar = json.loads(dispatch.file(droot, d["id"], ".mcp.json").read_text())
    assert calls == [1]
    assert sidecar["mcpServers"] == options.mcp_servers == mcp.config()["mcpServers"]
    assert preview["mcp_servers"] == ["agent", "api", "browser"]
    assert preview["repo mcp servers"] == ["api"]
    assert preview["mcp_selection"] == mcp.preview()
    assert SECRET not in json.dumps(preview)
    assert options.setting_sources is None
    assert not any(tool.startswith("mcp__api") for tool in options.allowed_tools)
    argv = SubprocessCLITransport(prompt="fixture", options=options)._build_command()  # noqa: SLF001
    assert "--strict-mcp-config" in argv
    assert json.loads(argv[argv.index("--mcp-config") + 1]) == sidecar
    assert not any(arg.startswith("--setting-sources") for arg in argv)


def test_actual_attempt_resolves_once_and_resume_gets_new_worktree_definitions(
    droot: Path,
    fake_claude,
    monkeypatch: pytest.MonkeyPatch,
):
    configure(droot, commit=True)
    original, attempts = mcp_policy.resolve, []

    def count(root, repo, d):
        attempts.append((d["state"], d["attempt"]))
        return original(root, repo, d)

    monkeypatch.setattr(mcp_policy, "resolve", count)
    fake_claude.scenario([one(envelope(status="question")), one(envelope())])
    base = git(["rev-parse", "HEAD"], droot / "repos/alpha-api").strip()
    d = _new_fast(droot, mcp=["qa"], base=base)
    assert d["state"] == "question"
    assert attempts == [("queued", 1), ("running", 1)]
    first = fake_claude.calls()[0]["options"]
    assert (
        first.mcp_servers == json.loads(dispatch.file(droot, d["id"], ".mcp.json").read_text())["mcpServers"]
    )
    source = Path(d["worktree"]) / ".mcp.json"
    source.write_text(json.dumps({"mcpServers": {"api": {"command": "changed-for-resume"}}}))
    resumed = dispatch.resume(droot, dispatch_id=d["id"], answer="continue", watch=True)
    second = fake_claude.calls()[1]["options"]
    assert attempts == [("queued", 1), ("running", 1), ("running", 2)]
    assert second.strict_mcp_config is True
    assert second.mcp_servers["api"] == {"command": "changed-for-resume"}
    assert (
        second.mcp_servers == json.loads(dispatch.file(droot, d["id"], ".mcp.json").read_text())["mcpServers"]
    )
    assert resumed["options"]["mcp_servers"] == sorted(second.mcp_servers)
    assert resumed["options"]["mcp_selection"]["servers"][-1]["definition_source"].startswith("worktree:")
    assert first.mcp_servers["api"] != second.mcp_servers["api"]


@pytest.mark.parametrize(
    "selection,content",
    [
        (["missing"], '{"mcpServers":{}}'),
        (["api"], "broken " + SECRET),
        (["agent"], '{"mcpServers":{}}'),
        (["api"], '{"mcpServers":{"api":null}}'),
    ],
)
def test_invalid_composition_refuses_before_worker_or_partial_sidecars(
    droot: Path,
    fake_claude,
    selection,
    content: str,
):
    repo = repos.load_registry(droot).require("alpha")
    repo.dispatch["repo_mcp"] = selection
    (repo.abs_path(droot) / ".mcp.json").write_text(content)
    d = record(droot)
    with pytest.raises(AgentError) as exc:
        dispatch.write_worker_files(droot, d, repo)
    assert SECRET not in str(exc.value)
    assert not dispatch.file(droot, d["id"], ".settings.json").exists()
    with pytest.raises(AgentError):
        dispatch.build_options(droot, d, repo, resume=True)
    assert not fake_claude.calls()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO fixture")
def test_preview_never_inventories_unselected_fifo(droot: Path, monkeypatch: pytest.MonkeyPatch):
    repo = repos.load_registry(droot).require("alpha")
    os.mkfifo(repo.abs_path(droot) / ".mcp.json")

    def forbidden(*args, **kwargs):
        raise AssertionError("legacy inventory called during launch/preview")

    monkeypatch.setattr(options_mod, "repo_mcp_servers", forbidden)
    options = dispatch.build_options(droot, record(droot), repo, resume=False)
    assert dispatch.options_preview(options)["mcp_servers"] == ["agent"]


def test_preview_does_not_write_worker_sidecars(droot: Path):
    configure(droot)
    before = set((droot / ".agent").rglob("*"))
    preview = _new_fast(droot, dry_run=True)
    assert preview["options"]["repo mcp servers"] == ["api"]
    added = set((droot / ".agent").rglob("*")) - before
    # C2 permits only the order and its binding; no dispatch state or lease is allocated.
    assert all(path.relative_to(droot / ".agent").parts[0] == "work-order-previews" for path in added)
    assert not (droot / ".agent/dispatches").exists()
    assert list((droot / ".agent/work-order-previews").glob("*.json"))
    assert not Path(preview["worktree"]).exists()
    assert (droot / preview["work_order"]).read_text(encoding="utf-8") == preview["prompt"]
