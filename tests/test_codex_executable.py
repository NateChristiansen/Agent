"""One Codex resolver protects all four launch sites from Windows .cmd argument reparsing."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_cli import codex_executable
from agent_cli.dispatch.codex_rpc import CodexAppServer


def _windows(monkeypatch: pytest.MonkeyPatch, found: dict[str, str | None]) -> None:
    monkeypatch.setattr(codex_executable, "os", SimpleNamespace(name="nt", environ={}))
    monkeypatch.setattr(codex_executable, "shutil", SimpleNamespace(which=lambda name: found.get(name)))


def test_windows_codex_child_gets_forward_slash_home_without_parent_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _windows(monkeypatch, {})
    monkeypatch.setattr(codex_executable.Path, "is_dir", lambda _self: True)
    base = {"USERPROFILE": "C:\\Users\\example", "OTHER": "kept"}
    child = codex_executable.child_env(base)
    assert child == {
        "USERPROFILE": "C:\\Users\\example",
        "OTHER": "kept",
        "CODEX_HOME": "C:/Users/example/.codex",
    }
    assert "CODEX_HOME" not in base
    assert codex_executable.child_env({"CODEX_HOME": "D:\\state\\codex"})["CODEX_HOME"] == "D:/state/codex"


def test_windows_codex_child_keeps_default_when_profile_has_no_state_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _windows(monkeypatch, {})
    monkeypatch.setattr(codex_executable.Path, "is_dir", lambda _self: False)
    base = {"USERPROFILE": "C:\\Users\\runneradmin"}
    assert codex_executable.child_env(base) == base


def test_windows_codex_child_requires_a_resolved_home(monkeypatch: pytest.MonkeyPatch) -> None:
    _windows(monkeypatch, {})
    with pytest.raises(FileNotFoundError, match="Codex home is unresolved"):
        codex_executable.child_env({})


def test_native_windows_executable_wins_over_a_batch_shim(monkeypatch: pytest.MonkeyPatch) -> None:
    _windows(monkeypatch, {"codex.exe": "C:/bin/codex.exe", "codex": "C:/bin/codex.cmd"})
    assert codex_executable.command("app-server", "--stdio") == [
        "C:/bin/codex.exe",
        "app-server",
        "--stdio",
    ]


def test_npm_batch_shim_uses_node_and_preserves_json_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to exercise the npm launcher")
    shim = tmp_path / "npm with spaces" / "codex.cmd"
    shim.parent.mkdir()
    shim.write_text("@echo off\n", encoding="utf-8")
    script = codex_executable._npm_entry(shim)
    script.parent.mkdir(parents=True)
    script.write_text("console.log(JSON.stringify(process.argv.slice(2)))\n", encoding="utf-8")
    _windows(monkeypatch, {"codex": str(shim), "node": node})
    message = '{"thread":"a b","text":"caret ^ & percent %"}'
    argv = codex_executable.command("queue", "--message", message)
    assert argv[:2] == [node, str(script)]
    result = subprocess.run(argv, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == ["queue", "--message", message]


def test_broken_batch_shim_refuses_instead_of_reparsing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _windows(monkeypatch, {"codex": str(tmp_path / "codex.cmd"), "node": "C:/node.exe"})
    with pytest.raises(FileNotFoundError, match="no adjacent npm script"):
        codex_executable.command("queue")


@pytest.mark.skipif(os.name != "nt", reason="native Windows Codex npm or executable launch")
def test_real_windows_codex_version_catalog_and_stdio_handshake():
    """No paid turn: prove the selected executable starts and its app-server speaks JSON-RPC."""
    assert shutil.which("codex"), "Windows CI must install Codex before this gate"
    version = subprocess.run(
        codex_executable.command("--version"),
        env=codex_executable.child_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=True,
    )
    assert "codex" in version.stdout.lower()
    catalog = subprocess.run(
        codex_executable.command("debug", "models", "--bundled"),
        env=codex_executable.child_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=True,
    )
    assert isinstance(json.loads(catalog.stdout).get("models"), list)

    async def handshake(cwd: Path) -> None:
        async with CodexAppServer(cwd) as rpc:
            assert rpc.process is not None and rpc.process.returncode is None

    # A fresh directory has no untrusted project hooks or config to muddy the stdio probe.
    with tempfile.TemporaryDirectory() as directory:
        asyncio.run(handshake(Path(directory)))
