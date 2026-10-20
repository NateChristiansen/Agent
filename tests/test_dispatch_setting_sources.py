"""A dispatch into this harness loads only the user setting source.

A dispatch ran in a worktree of the harness with every setting source on, so the
manager's own project hooks ran in the worker: the session-start capture and the session-end
status render rewrote `adrs/README.md`, and the caveman SessionStart hook failed. Product repos
keep the CLI default.
"""

import subprocess
from pathlib import Path

import pytest
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

from agent_cli import dispatch, repos
from agent_cli.dispatch import options as options_mod
from dispatch_helpers import droot  # noqa: F401 -- shared pytest fixture
from dispatch_helpers import options_record as _record


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def harness(tmp_path: Path) -> Path:
    root = tmp_path / "Agent"
    root.mkdir()
    _git(root, "init", "-q", "--initial-branch=main")
    _git(
        root,
        "-c",
        "user.email=f@example.test",
        "-c",
        "user.name=F",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "base",
    )
    return root


def _repo(path: str) -> repos.Repo:
    return repos.Repo(name="Agent", short="agent", path=path, remote="git@example.test:o/Agent.git")


def test_the_harness_checkout_and_its_worktrees_load_only_the_user_source(harness: Path):
    worktree = harness.parent / "Agent-worktrees" / "022-integration"
    _git(harness, "worktree", "add", "-q", str(worktree))
    assert options_mod.setting_sources_for(harness, _repo(".")) == ["user"]
    assert options_mod.setting_sources_for(harness, _repo("../Agent-worktrees/022-integration")) == ["user"]
    # the same answer when the root itself is a worktree of the harness
    assert options_mod.setting_sources_for(worktree, _repo("../../Agent")) == ["user"]


def test_a_product_repo_or_an_unreadable_path_keeps_the_default(harness: Path):
    product = harness.parent / "repos" / "web"
    product.mkdir(parents=True)
    _git(product, "init", "-q")
    assert options_mod.setting_sources_for(harness, _repo("../repos/web")) is None
    assert options_mod.setting_sources_for(harness, _repo("../repos/missing")) is None
    plain = harness.parent / "plain"
    plain.mkdir()
    assert options_mod.setting_sources_for(harness, _repo("../plain")) is None


@pytest.mark.parametrize("resume", [False, True])
def test_self_dispatch_options_keep_the_worker_policy_on_launch_and_resume(
    droot: Path, monkeypatch: pytest.MonkeyPatch, resume: bool
):
    monkeypatch.setattr(options_mod, "_harness_self", lambda root, repo: True)
    repo = repos.load_registry(droot).require("alpha")
    d = _record(droot)
    settings = dispatch.build_settings(droot, repo)
    options = dispatch.build_options(droot, d, repo, resume=resume, settings=settings)
    assert options.setting_sources == ["user"]
    # the worker policy is not a setting source: the --settings file and the SDK hooks stay
    assert options.allowed_tools == settings["permissions"]["allow"]
    assert options.disallowed_tools == settings["permissions"]["deny"]
    assert options.hooks and callable(options.can_use_tool)
    argv = SubprocessCLITransport(prompt="fixture", options=options)._build_command()  # noqa: SLF001
    assert "--setting-sources=user" in argv
    assert argv[argv.index("--settings") + 1] == str(dispatch.file(droot, d["id"], ".settings.json"))
    assert dispatch.options_preview(options)["setting_sources"] == ["user"]


def test_product_dispatch_options_and_preview_keep_the_default_sources(droot: Path):
    repo = repos.load_registry(droot).require("alpha")
    options = dispatch.build_options(droot, _record(droot), repo, resume=False)
    assert options.setting_sources is None
    argv = SubprocessCLITransport(prompt="fixture", options=options)._build_command()  # noqa: SLF001
    assert not any(arg.startswith("--setting-sources") for arg in argv)
    assert dispatch.options_preview(options)["setting_sources"] == "default"
