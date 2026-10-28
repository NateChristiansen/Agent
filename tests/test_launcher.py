from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_cli import dispatch, launcher, worker_mcp
from agent_cli import repos as repos_mod
from agent_cli.core import AgentError
from agent_cli.dispatch import driver, mcp_policy, verification_shell
from agent_cli.doctor.checks_mcp import launcher_command_ok
from dispatch_helpers import _new, droot  # noqa: F401 -- droot is a pytest fixture

watch_mod = importlib.import_module("agent_cli.dispatch.watch")


def windows_exe(root: Path) -> str:
    """The venv's interpreter, never the `agent.exe` shim `uv sync` must replace."""
    return str(root / ".venv" / "Scripts" / "python.exe")


MODULE = ["-I", "-m", "agent_cli"]


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher.sys, "platform", "win32")


def test_prefix_per_platform(tmp_path: Path):
    assert launcher.argv_prefix(tmp_path, windows=False) == [str(tmp_path / "bin" / "agent")]
    assert launcher.argv_prefix(tmp_path, windows=True) == [windows_exe(tmp_path), *MODULE]
    assert launcher.command_and_args(tmp_path, windows=True) == (windows_exe(tmp_path), MODULE)
    assert launcher.command_and_args(tmp_path, windows=False) == (str(tmp_path / "bin" / "agent"), [])


def test_prefix_follows_sys_platform(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    assert launcher.argv_prefix(tmp_path) == [windows_exe(tmp_path), *MODULE]
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    assert launcher.argv_prefix(tmp_path) == [str(tmp_path / "bin" / "agent")]


def test_require_names_uv_sync_when_exe_missing(tmp_path: Path):
    with pytest.raises(AgentError, match="uv sync"):
        launcher.require(tmp_path, windows=True)
    launcher.require(tmp_path, windows=False)
    exe = tmp_path / launcher.WINDOWS_ENTRY
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    launcher.require(tmp_path, windows=True)


def test_dispatch_new_refuses_before_allocation(droot: Path, monkeypatch: pytest.MonkeyPatch):
    def allocate(*args, **kwargs):
        raise AssertionError("allocation reached")

    require = launcher.require

    def require_windows(root: Path) -> None:
        with monkeypatch.context() as patch:
            patch.setattr(launcher.sys, "platform", "win32")
            require(root)

    monkeypatch.setattr(launcher, "require", require_windows)
    monkeypatch.setattr(dispatch.lifecycle.allocation, "allocate", allocate)
    (droot / launcher.WINDOWS_ENTRY).unlink(missing_ok=True)
    clone = Path(repos_mod.load_registry(droot).require("alpha").abs_path(droot))

    def snapshot() -> tuple[list[str], str, str]:
        records = sorted(p.name for p in (droot / ".agent").rglob("*") if "dispatches" in p.parts)
        branches = subprocess.run(
            ["git", "branch", "--list"], cwd=clone, capture_output=True, text=True, check=True
        ).stdout
        worktrees = subprocess.run(
            ["git", "worktree", "list"], cwd=clone, capture_output=True, text=True, check=True
        ).stdout
        return records, branches, worktrees

    before = snapshot()
    with pytest.raises(AgentError, match="uv sync"):
        _new(droot, watch=False)
    assert snapshot() == before
    assert not list(droot.parent.glob("**/001-mac-01"))


def test_mcp_policy_agent_server_uses_launcher(tmp_path: Path, windows):
    repo = repos_mod.Repo("Product", "product", str(tmp_path), "unused")
    result = mcp_policy.resolve(tmp_path, repo, {"id": "001-01", "feature": 1, "tasks": []})
    assert result.servers["agent"]["command"] == windows_exe(tmp_path)
    assert result.servers["agent"]["args"] == [*MODULE, "mcp", "--profile", "worker"]


def test_settings_precompact_hook_uses_launcher(tmp_path: Path, windows):
    repo = repos_mod.Repo("Product", "product", str(tmp_path), "unused")
    settings = dispatch.build_settings(tmp_path, repo)
    command = settings["hooks"]["PreCompact"][0]["hooks"][0]["command"]
    assert command == f'"{windows_exe(tmp_path)}" -I -m agent_cli hook precompact'


def test_watch_command_uses_launcher(droot: Path, windows, monkeypatch: pytest.MonkeyPatch):  # noqa: F811
    monkeypatch.setattr(watch_mod, "load", lambda root, dispatch_id: {})
    argv = watch_mod.watch_command(droot, dispatch_id="001-mac-01")
    assert argv[:4] == [windows_exe(droot), *MODULE]


def test_worker_mcp_server_config_uses_launcher(tmp_path: Path, windows):
    prof = worker_mcp.Profile(name="qa", server="browser", command="x")
    config = worker_mcp.server_config(tmp_path, {"id": "001-01"}, prof)
    assert config["command"] == windows_exe(tmp_path)
    assert config["args"][:4] == [*MODULE, "worker-mcp"]


class _Popen:
    pid = 7
    calls: list = []
    refuse_breakaway = False

    def __init__(self, argv, **kw):
        _Popen.calls.append((argv, kw))
        if kw.get("creationflags", 0) & 0x1000000 and _Popen.refuse_breakaway:
            raise PermissionError("no breakaway")


@pytest.mark.parametrize("refuse", [False, True])
def test_windows_detach_hidden_with_breakaway_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, windows, refuse: bool
):
    for name, value in (
        ("CREATE_NO_WINDOW", 0x8000000),
        ("CREATE_NEW_PROCESS_GROUP", 0x200),
        ("CREATE_BREAKAWAY_FROM_JOB", 0x1000000),
    ):
        monkeypatch.setattr(subprocess, name, value, raising=False)
    _Popen.calls = []
    _Popen.refuse_breakaway = refuse
    monkeypatch.setattr(dispatch.supervisor, "popen", _Popen)
    # The Git Bash lookup runs `shutil.which`, which on a patched win32 reaches `_winapi`, absent on POSIX.
    monkeypatch.setattr(verification_shell, "carried", lambda environ=None: {})
    monkeypatch.setattr(driver, "save", lambda root, d: d)
    monkeypatch.setattr(driver, "file", lambda root, id, ext: tmp_path / f"{id}{ext}")
    out = driver.detach(tmp_path, {"id": "001-mac-01"})
    argv, kw = _Popen.calls[0]
    assert argv[:4] == [windows_exe(tmp_path), *MODULE] and "start_new_session" not in kw
    assert out["supervisor_breakaway"] is (not refuse)
    flags = [c[1]["creationflags"] for c in _Popen.calls]
    assert all(f & 0x8000000 and f & 0x200 for f in flags)
    assert not any(f & 0x8 for f in flags)
    assert [bool(f & 0x1000000) for f in flags] == ([True, False] if refuse else [True])


def test_doctor_accepts_only_this_roots_launcher(tmp_path: Path):
    root = tmp_path / "harness"
    other = tmp_path / "other"
    assert launcher_command_ok(root, str(root / "bin" / "agent"))
    assert launcher_command_ok(root, "${CLAUDE_PROJECT_DIR:-.}/bin/agent")
    assert launcher_command_ok(root, str(root / ".venv" / "Scripts" / "agent.exe"))
    assert launcher_command_ok(root, str(root).replace("/", "\\") + "\\.VENV\\Scripts\\agent.exe")
    assert launcher_command_ok(root, ".venv/Scripts/agent.exe")
    assert launcher_command_ok(root, windows_exe(root))
    assert launcher_command_ok(root, str(root).replace("/", "\\") + "\\.venv\\Scripts\\Python.exe")
    assert not launcher_command_ok(root, windows_exe(other))
    assert not launcher_command_ok(root, str(other / "bin" / "agent"))
    assert not launcher_command_ok(root, str(other / ".venv" / "Scripts" / "agent.exe"))
    assert not launcher_command_ok(root, str(root / "other" / ".venv" / "Scripts" / "agent.exe"))
    assert not launcher_command_ok(root, "npx something")


def test_python_dash_m_runs_the_same_cli_as_the_console_script(tmp_path: Path):
    """Option (a) of the Windows venv-lock probe: `python -m agent_cli` must
    work, isolated (`-I`) exactly as the Windows launchers run it. It routes through `entry.main`,
    hooks included."""
    helped = subprocess.run([sys.executable, *MODULE, "--help"], cwd=tmp_path, capture_output=True, text=True)
    assert helped.returncode == 0 and helped.stdout.startswith("usage: agent "), helped.stderr
    hooked = subprocess.run(
        [sys.executable, *MODULE, "hook", "no-chaining"],
        cwd=tmp_path,
        input='{"tool_input": {"command": "ls"}}',
        capture_output=True,
        text=True,
    )
    assert hooked.returncode == 0 and hooked.stdout == "", hooked.stderr


@pytest.mark.skipif(
    sys.platform == "win32", reason="runs bin/agent under a stub uname; Git Bash is not stubbed"
)
@pytest.mark.parametrize(
    ("system", "venv", "no_sync"),
    [("MINGW64_NT-10.0", True, True), ("MINGW64_NT-10.0", False, False), ("Darwin", True, False)],
)
def test_bin_agent_does_not_sync_an_existing_windows_venv(
    tmp_path: Path, system: str, venv: bool, no_sync: bool
):
    """Interim option (b) of the venv-lock probe: on Windows a fallback `uv run`
    over an existing venv uses `--no-sync`, so it cannot fail on a held agent.exe. A first run
    with no venv still syncs, and POSIX keeps its self-healing sync."""
    repo = Path(__file__).resolve().parents[1]
    root, stubs = tmp_path / "harness", tmp_path / "stubs"
    (root / "bin").mkdir(parents=True)
    stubs.mkdir()
    shutil.copy2(repo / "bin" / "agent", root / "bin" / "agent")
    if venv:
        (root / ".venv" / "Scripts").mkdir(parents=True)
    for name, body in (("uname", f"echo {system}"), ("uv", 'echo "$@"')):
        (stubs / name).write_text(f"#!/bin/sh\n{body}\n")
        (stubs / name).chmod(0o755)
    env = {"PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}", "HOME": str(tmp_path)}
    out = subprocess.run(
        [str(root / "bin" / "agent"), "status"], env=env, capture_output=True, text=True, check=True
    ).stdout.split()
    assert out[:2] == ["run", "--no-sync"] if no_sync else out[:2] == ["run", "--quiet"]
    assert out[-2:] == ["agent", "status"]


def test_agent_cmd_does_not_sync_an_existing_venv():
    """The cmd.exe launcher's half of option (b); it keeps the CRLF that cmd.exe needs."""
    raw = (Path(__file__).resolve().parents[1] / "bin" / "agent.cmd").read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n")
    text = raw.decode()
    assert 'else if exist "%AGENT_ROOT%\\.venv\\" (\r\n  uv run --no-sync --quiet' in text
    assert text.index("--no-sync") < text.rindex('uv run --quiet --project "%AGENT_ROOT%"')
    # Option (a): the venv's python as a module; the agent.exe shim is never run.
    assert '"%AGENT_ROOT%\\.venv\\Scripts\\python.exe" -I -m agent_cli %*' in text
    assert "agent.exe" not in text
