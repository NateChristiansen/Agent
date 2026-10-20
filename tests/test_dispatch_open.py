"""`dispatch open` picks watch or resume by state and opens it in an
Obsidian terminal through `obsidian eval`, or prints the command when it cannot."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_cli import dispatch, obsidian_tabs, ops
from agent_cli.core import AgentError


def _record(root: Path, **fields) -> dict:
    d = {
        "id": "001-01",
        "feature": 1,
        "short": "alpha",
        "state": "done",
        "session_id": "worker-sid",
        "worktree": str(root),
    }
    d.update(fields)
    base = root / ".agent" / "dispatches"
    base.mkdir(parents=True, exist_ok=True)
    (base / "001-01.json").write_text(json.dumps(d), encoding="utf-8")
    return d


def test_a_running_worker_opens_a_watch_and_a_stopped_one_a_resume(tmp_root: Path, monkeypatch):
    monkeypatch.setattr(dispatch.opener.sys, "platform", "linux")
    cwd, cmd = dispatch.open_command(tmp_root, _record(tmp_root, state="running"))
    assert cwd == str(tmp_root) and cmd == "bin/agent dispatch watch 001-01 --follow"
    cwd, cmd = dispatch.open_command(tmp_root, _record(tmp_root, state="question", worktree=str(tmp_root)))
    assert cwd == str(tmp_root) and cmd == "claude --resume worker-sid"
    # a removed worktree falls back to the vault root; --resume finds the session from anywhere
    _, cmd = dispatch.open_command(tmp_root, _record(tmp_root, worktree=str(tmp_root / "gone")))
    assert cmd == "claude --resume worker-sid"


def test_open_runs_obsidian_eval_with_a_terminal_view_state(tmp_root: Path, monkeypatch):
    _record(tmp_root, state="blocked")
    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="=> opened", stderr="")

    monkeypatch.setattr(dispatch.opener, "obsidian_cli", lambda: "/usr/local/bin/obsidian")
    monkeypatch.setattr(dispatch.opener, "user_shell", lambda: "/opt/fish/bin/fish")
    monkeypatch.setattr(dispatch.opener.sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "run", fake_run)
    op = ops.dispatch_open(tmp_root, dispatch_id="001-01")
    assert op.data["opened"] is True and op.data["mode"] == "resume"
    assert op.text == "opened 001-01 (resume) in an Obsidian terminal"
    argv = calls[0]
    assert argv[:2] == ["/usr/local/bin/obsidian", "eval"] and argv[2].startswith("code=")
    assert (
        "terminal:terminal" in argv[2] and "claude --resume worker-sid; exec /opt/fish/bin/fish -l" in argv[2]
    )
    assert '"executable": "/opt/fish/bin/fish"' in argv[2]  # the owner's shell, not /bin/zsh
    # a resume tab carries no label, so Claude's own title and live glyph show through
    assert '"profileSourceId": null' in argv[2] and '"userTitle": ""' in argv[2]
    # interactive as well as login, so ~/.zshrc puts claude on PATH
    assert '"args": ["-i", "-l", "-c"' in argv[2]


def test_the_tab_shell_is_the_users_own_else_the_platform_default(tmp_path: Path, monkeypatch):
    fish = tmp_path / "fish"
    fish.write_text("", encoding="utf-8")
    monkeypatch.setenv("SHELL", str(fish))
    assert dispatch.opener.user_shell() == str(fish)
    for unusable in ("", "fish", str(tmp_path / "missing")):  # unset, not absolute, not a file
        monkeypatch.setenv("SHELL", unusable)
        monkeypatch.setattr(dispatch.opener.sys, "platform", "darwin")
        assert dispatch.opener.user_shell() == "/bin/zsh"
        monkeypatch.setattr(dispatch.opener.sys, "platform", "linux")
        assert dispatch.opener.user_shell() in ("/bin/bash", "/bin/sh")


def test_open_prints_the_command_without_the_cli(tmp_root: Path, monkeypatch):
    _record(tmp_root, state="running")
    monkeypatch.setattr(dispatch.opener, "obsidian_cli", lambda: None)
    monkeypatch.setattr(dispatch.opener.sys, "platform", "linux")
    op = ops.dispatch_open(tmp_root, dispatch_id="001-01")
    assert op.data["opened"] is False
    assert op.text == f"cd {tmp_root} && bin/agent dispatch watch 001-01 --follow  (obsidian CLI not on PATH)"
    assert ops.dispatch_open(tmp_root, dispatch_id="001-01", print_only=True).data["reason"] == "print only"


def test_open_prints_a_powershell_pasteable_line_on_windows(tmp_root: Path, monkeypatch):
    _record(tmp_root, state="running")
    monkeypatch.setattr(dispatch.opener, "obsidian_cli", lambda: None)
    monkeypatch.setattr(dispatch.opener.sys, "platform", "win32")
    monkeypatch.setattr(
        dispatch,
        "open_",
        lambda *_a, **_kw: {
            "opened": False,
            "cwd": "C:\\Users\\o'brien\\wt",
            "command": "bin/agent dispatch watch 001-01 --follow",
            "reason": "print only",
        },
    )
    op = ops.dispatch_open(tmp_root, dispatch_id="001-01")
    assert op.text == (
        "Set-Location -LiteralPath 'C:\\Users\\o''brien\\wt'; bin/agent dispatch watch 001-01 --follow  # print only"
    )


@pytest.mark.skipif(shutil.which("powershell.exe") is None, reason="needs powershell.exe")
def test_windows_text_runs_only_the_command_in_powershell(tmp_root: Path, tmp_path: Path, monkeypatch):
    cwd = tmp_path / "o'brien"
    cwd.mkdir()
    monkeypatch.setattr(dispatch.opener.sys, "platform", "win32")
    monkeypatch.setattr(
        dispatch,
        "open_",
        lambda *_a, **_kw: {
            "opened": False,
            "cwd": str(cwd),
            "command": "Write-Output ran; (Get-Location).Path",
            "reason": "(Write-Output nested)",
        },
    )
    text = ops.dispatch_open(tmp_root, dispatch_id="001-01").text
    done = subprocess.run(["powershell.exe", "-NoProfile", "-Command", text], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["ran", str(cwd)]


MARKER_RE = r"\$env:AGENT_WORKER_LAUNCH='worker:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}'; "


def _open_on_windows(root: Path, monkeypatch) -> tuple[dict, dict]:
    """The result and the Terminal view state `dispatch open` sends on win32."""
    from agent_cli import obsidian_run, tab_drain

    codes: list[str] = []

    def fake_eval(_root, code, *_a, **_kw):
        codes.append(code)
        return SimpleNamespace(ended="exited", stdout="=> opened", stderr="", returncode=0)

    monkeypatch.setattr(dispatch.opener.sys, "platform", "win32")
    monkeypatch.setattr(dispatch.opener, "obsidian_cli", lambda: "C:\\obsidian.com")
    monkeypatch.setattr(obsidian_run, "launch", lambda: True)
    monkeypatch.setattr(tab_drain, "run_eval", fake_eval)
    out = dispatch.open_(root, dispatch_id="001-01")
    view = json.loads(codes[0].split("setViewState(", 1)[1].split("); return", 1)[0])
    return out, view["state"]["terminal:terminal"]


def test_windows_commands_are_powershell(tmp_root: Path, monkeypatch):
    monkeypatch.setattr(dispatch.opener.sys, "platform", "win32")
    cwd, cmd = dispatch.open_command(tmp_root, _record(tmp_root, state="running"))
    assert cwd == str(tmp_root) and cmd == "./bin/agent.cmd dispatch watch '001-01' --follow"
    _, cmd = dispatch.open_command(tmp_root, _record(tmp_root, state="question"))
    assert cmd == "claude --resume 'worker-sid'"


@pytest.mark.parametrize("sid", ["x'; calc", 'a"b', "50%path%", "a b", "x\n"])
def test_windows_refuses_an_id_powershell_or_the_bat_would_parse(tmp_root: Path, monkeypatch, sid: str):
    monkeypatch.setattr(dispatch.opener.sys, "platform", "win32")
    with pytest.raises(AgentError, match="Windows terminal tab"):
        dispatch.open_command(tmp_root, _record(tmp_root, state="question", session_id=sid))


def test_windows_watch_tab_is_a_powershell_profile_with_a_worker_marker(tmp_root: Path, monkeypatch):
    _record(tmp_root, state="running")
    out, state = _open_on_windows(tmp_root, monkeypatch)
    assert out["opened"] is True and out["mode"] == "watch"
    profile = state["profile"]
    assert profile["executable"] == "powershell.exe" and profile["platforms"] == {"win32": True}
    assert profile["args"][:3] == ["-NoLogo", "-NoExit", "-Command"]  # -NoExit keeps the shell open
    assert re.fullmatch(MARKER_RE + r"\./bin/agent\.cmd dispatch watch '001-01' --follow", profile["args"][3])
    assert profile["pythonExecutable"].endswith("\\.agent\\terminal-python\\Scripts\\python.exe")
    assert "/" not in profile["pythonExecutable"] and profile["useWin32Conhost"] is True
    # the label the state relabel finds the tab by
    assert state["userTitle"] == obsidian_tabs.worker_label("001-01", "running")
    assert state["cwd"] == str(tmp_root)


def test_windows_resume_tab_runs_claude_in_the_worktree_and_each_launch_has_its_own_marker(
    tmp_root: Path, monkeypatch
):
    _record(tmp_root, state="blocked")
    out, state = _open_on_windows(tmp_root, monkeypatch)
    _, again = _open_on_windows(tmp_root, monkeypatch)
    assert out["mode"] == "resume" and state["userTitle"] == ""
    command = state["profile"]["args"][3]
    assert re.fullmatch(MARKER_RE + "claude --resume 'worker-sid'", command)
    assert command != again["profile"]["args"][3]


def test_the_workers_row_link_and_the_shell_command_agree():
    root = Path(__file__).resolve().parents[1]
    uri = dispatch.open_uri(root, "002-22")
    vault = dispatch.opener.vault_name(root)
    assert uri == f"obsidian://shell-commands/?vault={vault}&execute=agent-worker-open&_dispatch=002-22"
    # The tracked seed, which `obsidian setup` copies into .obsidian/ (untracked since then).
    seed = root / "harness/obsidian/config/plugins/obsidian-shellcommands/data.json"
    data = json.loads(seed.read_text(encoding="utf-8"))
    entry = next(c for c in data["shell_commands"] if c["id"] == "agent-worker-open")
    assert entry["platform_specific_commands"]["default"] == "bin/agent dispatch open {{_dispatch}}"
    assert any(v["name"] == "dispatch" for v in data["custom_variables"])
