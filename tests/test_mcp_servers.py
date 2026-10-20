"""MCP servers installed from a pinned manifest, and the check that they actually start.

The failure these exist for, 2026-09-18: pinning a server to an exact version created a fresh
`npx` cache entry whose native module had never been built. Every config-level check passed —
registered, enabled, reachable by a rule, pinned — and the server could not start.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from agent_cli import links, mcp_servers


def _manifest(root: Path, deps: dict[str, str], node_bin: str = "") -> Path:
    target = mcp_servers.manifest_dir(root)
    target.mkdir(parents=True, exist_ok=True)
    body: dict[str, object] = {"name": "t", "private": True, "dependencies": deps}
    if node_bin:
        body["agentMcp"] = {"nodeBin": node_bin}
    path = target / "package.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _fake_bin(root: Path, name: str, *, exit_code: int = 0, message: str = "") -> Path:
    bindir = mcp_servers.link_path(root) / "node_modules" / ".bin"
    bindir.mkdir(parents=True, exist_ok=True)
    path = bindir / name
    body = "#!/bin/sh\n"
    if message:
        body += f"echo '{message}' >&2\n"
    body += f"exit {exit_code}\n"
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _declare_bin(root: Path, pkg: str, bin_name: str) -> None:
    """Write the installed package's own package.json, which is what says it owns a binary."""
    meta = mcp_servers.link_path(root) / "node_modules" / Path(pkg)
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "package.json").write_text(json.dumps({"name": pkg, "bin": {bin_name: "x.js"}}), "utf-8")


# Audit 10 plan.md:116 (K4) retires unused floating-spec reporting.


def test_binaries_are_only_the_declared_packages(tmp_root: Path):
    """An install drags in transitive binaries that are not servers and do not answer --help.
    Probing those would cry wolf on every run."""
    _manifest(tmp_root, {"@example/mcp-server": "1.0.0"})
    _declare_bin(tmp_root, "@example/mcp-server", "the-server")
    _fake_bin(tmp_root, "the-server")
    _fake_bin(tmp_root, "prebuild-install")
    _fake_bin(tmp_root, "node-which")
    assert sorted(mcp_servers.binaries(tmp_root)) == ["the-server"]


@pytest.mark.skipif(
    sys.platform == "win32", reason="the fake server is a #! shell script, which Windows cannot execute"
)
def test_probe_catches_a_binary_that_does_not_start(tmp_root: Path):
    _manifest(tmp_root, {"@example/mcp-server": "1.0.0"})
    _declare_bin(tmp_root, "@example/mcp-server", "the-server")
    good = _fake_bin(tmp_root, "the-server")
    ok, detail = mcp_servers.probe(good)
    assert ok and detail == "starts"

    broken = _fake_bin(tmp_root, "the-server", exit_code=1, message="Cannot find module keytar.node")
    ok, detail = mcp_servers.probe(broken)
    assert not ok
    assert "exit 1" in detail and "keytar" in detail


def test_probe_reports_a_missing_binary_rather_than_raising(tmp_root: Path):
    ok, detail = mcp_servers.probe(tmp_root / "nope")
    assert not ok and detail


def test_ensure_link_points_at_the_manifest_dir_and_repairs_itself(tmp_root: Path, tmp_path: Path):
    notes = tmp_path / "notes-repo"
    notes.mkdir()
    (tmp_root / ".agent").mkdir(parents=True, exist_ok=True)
    (tmp_root / ".agent" / "notes-root").write_text(f"{notes}\n", encoding="utf-8")
    target = mcp_servers.ensure_link(tmp_root)
    link = mcp_servers.link_path(tmp_root)
    assert target == notes / mcp_servers.MANIFEST_DIR
    assert links.is_link(link) and link.resolve() == target.resolve()

    # a stale link, from a notes repo that moved, is repaired rather than left broken
    if link.is_junction():
        link.rmdir()
    else:
        link.unlink()
    if sys.platform == "win32":  # a junction needs an existing target
        (tmp_path / "somewhere-else").mkdir()
    links.make(link, tmp_path / "somewhere-else", is_dir=True)
    assert mcp_servers.ensure_link(tmp_root).resolve() == target.resolve()
    assert link.resolve() == target.resolve()


def test_plain_files_mode_uses_a_real_directory(tmp_root: Path):
    """With no notes repo (ADR-0012) there is nothing to link to, so the directory is its own
    manifest directory and `ensure_link` must not try to link it to itself."""
    assert mcp_servers.manifest_dir(tmp_root) == mcp_servers.link_path(tmp_root)
    target = mcp_servers.ensure_link(tmp_root)
    assert target.is_dir() and not links.is_link(target)


def test_install_without_a_manifest_does_nothing(tmp_root: Path):
    result = mcp_servers.install(tmp_root)
    assert result["installed"] is False
    assert result["packages"] == {}


def test_ci_by_default_install_when_updating(tmp_root: Path, monkeypatch):
    """`npm ci` gives a fresh clone exactly the locked tree, and refuses when the manifest and the
    lock disagree — which also means it can never ADD a server. So adding one is an explicit
    `--update`, and there is no silent fallback that would throw the guarantee away."""
    target = mcp_servers.manifest_dir(tmp_root)
    _manifest(tmp_root, {"x": "1.0.0"})
    (target / "package-lock.json").write_text("{}", encoding="utf-8")
    verbs: list[str] = []

    def fake_run(cmd, **kwargs):
        verbs.append(cmd[1])

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    monkeypatch.setattr(mcp_servers.shutil, "which", lambda *a, **k: "/usr/bin/npm")
    monkeypatch.setattr(mcp_servers.subprocess, "run", fake_run)
    mcp_servers.install(tmp_root)
    mcp_servers.install(tmp_root, update=True)
    assert verbs == ["ci", "install"]


def test_an_out_of_sync_lock_names_the_fix(tmp_root: Path, monkeypatch):
    import pytest

    from agent_cli.core import ExternalFailure

    target = mcp_servers.manifest_dir(tmp_root)
    _manifest(tmp_root, {"x": "1.0.0"})
    (target / "package-lock.json").write_text("{}", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        class R:
            returncode = 1
            stdout = ""
            stderr = (
                "npm error `npm ci` can only install packages when your package.json and lock are in sync"
            )

        return R()

    monkeypatch.setattr(mcp_servers.shutil, "which", lambda *a, **k: "/usr/bin/npm")
    monkeypatch.setattr(mcp_servers.subprocess, "run", fake_run)
    with pytest.raises(ExternalFailure) as exc:
        mcp_servers.install(tmp_root)
    assert "--update" in str(exc.value)


def test_node_bin_goes_first_on_path_for_the_install(tmp_root: Path, tmp_path: Path, monkeypatch):
    """A native module is built against the node that installs it, so which node runs npm is the
    whole difference between a server that starts and one that does not."""
    fake_node_bin = tmp_path / "node22" / "bin"
    fake_node_bin.mkdir(parents=True)
    _manifest(tmp_root, {"x": "1.0.0"}, node_bin=str(fake_node_bin))
    assert mcp_servers.node_bin(tmp_root) == str(fake_node_bin)

    seen: dict[str, str] = {}

    def fake_run(cmd, **kwargs):
        seen["path"] = (kwargs.get("env") or {}).get("PATH", "")

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    monkeypatch.setattr(mcp_servers.shutil, "which", lambda *a, **k: "/usr/bin/npm")
    monkeypatch.setattr(mcp_servers.subprocess, "run", fake_run)
    mcp_servers.install(tmp_root)
    assert seen["path"].split(os.pathsep)[0] == str(fake_node_bin)
