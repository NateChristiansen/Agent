"""The onboarding fixture repo: same SHA from two builds, and one item in every S3 area."""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

import onboarding_repo

# Changes whenever onboarding_repo.FILES or the commit settings change; every machine must match it.
EXPECTED_SHA = "9048b1c043cfdb3008fc6d416c12814324343c7a"


def _git(repo: Path, *args: str) -> str:
    # the builder's isolated env: an installed git-lfs would otherwise write hooks into the clone
    env = onboarding_repo._env()
    proc = subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def test_two_builds_land_on_the_same_sha(tmp_path):
    first = onboarding_repo.build(tmp_path / "a")
    second = onboarding_repo.build(tmp_path / "b")
    assert first[1:] == second[1:]


def test_sha_is_the_recorded_cross_machine_constant(tmp_path):
    assert onboarding_repo.build(tmp_path / "fx")[2] == EXPECTED_SHA


def test_relative_destination_builds(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    remote, _, sha = onboarding_repo.build(Path("rel/fx"))
    assert remote == (tmp_path / "rel" / "fx" / "remote.git").resolve()
    assert sha == EXPECTED_SHA
    assert (tmp_path / "rel" / "fx" / "product" / "AGENTS.md").exists()


def test_clone_tracks_the_remote_at_that_sha(tmp_path):
    remote, branch, sha = onboarding_repo.build(tmp_path / "fx")
    clone = tmp_path / "fx" / "product"
    assert _git(clone, "rev-parse", "HEAD") == sha
    assert _git(remote, "rev-parse", branch) == sha
    assert _git(clone, "status", "--porcelain") == ""


def test_every_s3_area_has_an_item(tmp_path):
    _, _, sha = onboarding_repo.build(tmp_path / "fx")
    clone = tmp_path / "fx" / "product"
    tracked = set(_git(clone, "ls-files").splitlines())

    agents = (clone / "AGENTS.md").read_text(encoding="utf-8")
    assert "## Ask first" in agents  # instructions
    hooks = json.loads((clone / ".claude/settings.json").read_text(encoding="utf-8"))["hooks"]
    assert "WorktreeCreate" in hooks and "Stop" in hooks  # claude config
    assert {".codex/hooks.json", ".codex/config.toml"} <= tracked
    codex = tomllib.loads((clone / ".codex/config.toml").read_text(encoding="utf-8"))
    assert codex["mcp_servers"]  # codex config
    assert json.loads((clone / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]  # mcp
    assert ".githooks/pre-commit" in tracked  # git hooks
    assert _git(clone, "config", "core.hooksPath") == ".githooks"
    cli = (clone / "bin/prodctl").read_text(encoding="utf-8")
    assert "merge)" in cli and "publish)" in cli  # repo cli
    assert "feat/<id>-<slug>" in agents and "PRD-<id>" in agents  # conventions
    assert "npm test" in (clone / ".github/workflows/ci.yml").read_text(encoding="utf-8")  # checks
    assert _git(clone, "check-ignore", ".env.local") == ".env.local"  # per-machine state
    assert ".env.local" not in tracked and (clone / ".env.local").exists()
    assert "filter=lfs" in (clone / ".gitattributes").read_text(encoding="utf-8")  # storage
    assert "4317" in agents and "app.lock" in agents  # concurrency
    assert _git(clone, "cat-file", "-t", sha) == "commit"


def test_files_are_lf_and_scripts_are_executable(tmp_path):
    onboarding_repo.build(tmp_path / "fx")
    clone = tmp_path / "fx" / "product"
    for rel in onboarding_repo.FILES:
        assert b"\r" not in (clone / rel).read_bytes(), rel
    modes = {line.split("\t")[1]: line.split()[0] for line in _git(clone, "ls-files", "-s").splitlines()}
    assert all(modes[rel] == "100755" for rel in onboarding_repo.EXECUTABLE)
    assert modes["README.md"] == "100644"
