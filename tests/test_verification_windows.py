"""Supervisor verification on Windows: the derived verifier env, the Git Bash lookup, the
supervisor env filter, and, natively, the kill-on-close job runner."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_cli import dispatch
from agent_cli.core import AgentError
from agent_cli.dispatch import driver, options, verification_shell
from agent_cli.dispatch import verification as runner
from agent_cli.dispatch import verification_process as procs
from agent_cli.dispatch.preflight import verification_preflight
from agent_cli.repos import VerificationCommand, VerificationConfig
from verification_fixtures import assessment, config, fixture, git, start  # noqa: F401 — fixture

native = pytest.mark.skipif(sys.platform != "win32", reason="native Windows job objects")

WINDOWS_ENV = {
    "PATH": r"C:\Windows\System32",
    "SYSTEMROOT": r"C:\WINDOWS",
    "TEMP": r"C:\Users\w\AppData\Local\Temp",
    "USERPROFILE": r"C:\Users\w",
    "USERNAME": "w",
    "COMSPEC": r"C:\WINDOWS\system32\cmd.exe",
    "HOME": "/home/w",
    "LANG": "en_US.UTF-8",
    "LC_CTYPE": "en_US.UTF-8",
    "TERM": "xterm",
    "SSH_AUTH_SOCK": "/tmp/agent.sock",
    "Claude_Session": "x",
    "SECRET_TOKEN": "s",
}


def _verifier(monkeypatch: pytest.MonkeyPatch, platform: str, env: dict[str, str]) -> dict[str, str]:
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(os, "environ", env)
    return options.verifier_env(SimpleNamespace(env_allow=[], dispatch={}))  # type: ignore[arg-type]


def test_windows_verifier_env_carries_the_windows_names(monkeypatch: pytest.MonkeyPatch):
    env = _verifier(monkeypatch, "win32", WINDOWS_ENV)
    assert {"SYSTEMROOT", "TEMP", "USERPROFILE", "USERNAME", "COMSPEC", "PATH", "HOME", "TERM"} <= set(env)
    assert not {"SSH_AUTH_SOCK", "Claude_Session", "SECRET_TOKEN", "LC_CTYPE"} & set(env)
    assert env["LANG"] == env["LC_ALL"] == options.VERIFIER_LOCALE


def test_windows_native_shell_home_comes_from_userprofile(monkeypatch: pytest.MonkeyPatch):
    env = _verifier(monkeypatch, "win32", {k: v for k, v in WINDOWS_ENV.items() if k != "HOME"})
    assert env["HOME"] == env["USERPROFILE"] == WINDOWS_ENV["USERPROFILE"]
    assert env["USERNAME"] == "w"


def test_linux_verifier_env_is_unchanged(monkeypatch: pytest.MonkeyPatch):
    assert _verifier(monkeypatch, "linux", WINDOWS_ENV) == {
        "PATH": r"C:\Windows\System32",
        "HOME": "/home/w",
        "TERM": "xterm",
        "LANG": options.VERIFIER_LOCALE,
        "LC_ALL": options.VERIFIER_LOCALE,
    }


def _bash(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name / "bash.exe"
    path.parent.mkdir(parents=True)
    path.write_text("")
    return path


def _git_exec_path(monkeypatch: pytest.MonkeyPatch, exec_path: str | None) -> None:
    monkeypatch.setattr(
        verification_shell.shutil,
        "which",
        lambda name, path=None: {"git": "git.exe" if exec_path else None, "bash": PATH_BASH.get("bash")}[
            name
        ],
    )

    def run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[1:] == ["--version"]:
            version = BASH_VERSION.get(argv[0], "GNU bash, version 5.2.37(1)-release (x86_64-pc-msys)")
            return subprocess.CompletedProcess(argv, 0, stdout=f"{version}\n")
        assert argv[1:] == ["--exec-path"]
        return subprocess.CompletedProcess(argv, 0, stdout=f"{exec_path}\n")

    monkeypatch.setattr(verification_shell.subprocess, "run", run)


PATH_BASH: dict[str, str | None] = {}
#: `--version` output by executable path, for the ones that must not pass as Git Bash.
BASH_VERSION: dict[str, str] = {}


@pytest.fixture
def git_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bash = _bash(tmp_path, "Git/bin")
    (tmp_path / "Git/mingw64/libexec/git-core").mkdir(parents=True)
    PATH_BASH["bash"] = str(_bash(tmp_path, "path"))
    _git_exec_path(monkeypatch, str(tmp_path / "Git/mingw64/libexec/git-core"))
    return bash


def test_bash_lookup_prefers_the_claude_variable_then_the_carried_one(tmp_path: Path, git_install: Path):
    claude, carried = _bash(tmp_path, "claude"), _bash(tmp_path, "carried")
    both = {"CLAUDE_CODE_GIT_BASH_PATH": str(claude), "AGENT_VERIFIER_BASH": str(carried)}
    assert verification_shell.find_bash(both) == claude
    assert verification_shell.find_bash({"AGENT_VERIFIER_BASH": str(carried)}) == carried


def test_bash_lookup_falls_back_to_the_git_install_then_path(
    tmp_path: Path, git_install: Path, monkeypatch: pytest.MonkeyPatch
):
    assert verification_shell.find_bash({"PATH": "p"}) == git_install
    _git_exec_path(monkeypatch, None)
    assert verification_shell.find_bash({"PATH": "p"}) == Path(str(PATH_BASH["bash"]))


def test_bash_lookup_refuses_wsl_and_names_every_cause(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    PATH_BASH["bash"] = r"C:\WINDOWS\System32\bash.exe"
    _git_exec_path(monkeypatch, None)
    with pytest.raises(AgentError) as caught:
        verification_shell.find_bash({"PATH": "p", "SYSTEMROOT": r"C:\WINDOWS"})
    message = str(caught.value)
    assert "CLAUDE_CODE_GIT_BASH_PATH is unset" in message
    assert "no git on PATH" in message
    assert "starts WSL" in message


def test_a_claude_variable_naming_no_file_refuses(tmp_path: Path, git_install: Path):
    with pytest.raises(AgentError, match="CLAUDE_CODE_GIT_BASH_PATH names .*missing.*is not a file"):
        verification_shell.find_bash({"CLAUDE_CODE_GIT_BASH_PATH": str(tmp_path / "missing" / "bash.exe")})


@pytest.mark.parametrize("name", ["CLAUDE_CODE_GIT_BASH_PATH", "AGENT_VERIFIER_BASH"])
def test_a_variable_naming_a_file_that_is_not_bash_refuses(tmp_path: Path, git_install: Path, name: str):
    """An ordinary file passed preflight in review: every verify command would have run through it."""
    script = tmp_path / "tool.py"
    script.write_text("print('hi')\n")
    with pytest.raises(AgentError, match=f"{name} names .*is not a bash.exe"):
        verification_shell.find_bash({name: str(script)})


def test_a_bash_exe_that_is_not_gnu_bash_refuses(
    tmp_path: Path, git_install: Path, monkeypatch: pytest.MonkeyPatch
):
    impostor = _bash(tmp_path, "impostor")
    monkeypatch.setitem(BASH_VERSION, str(impostor), "something else 1.0")
    with pytest.raises(AgentError, match="does not report GNU bash"):
        verification_shell.find_bash({"CLAUDE_CODE_GIT_BASH_PATH": str(impostor)})


def test_a_bash_exe_naming_gnu_bash_only_on_a_later_line_refuses(
    tmp_path: Path, git_install: Path, monkeypatch: pytest.MonkeyPatch
):
    impostor = _bash(tmp_path, "impostor")
    monkeypatch.setitem(BASH_VERSION, str(impostor), "wrapper 1.0\nGNU bash, version 5.2")
    with pytest.raises(AgentError, match="does not report GNU bash"):
        verification_shell.find_bash({"CLAUDE_CODE_GIT_BASH_PATH": str(impostor)})


@pytest.mark.parametrize("version", ["\nGNU bash, version 5.2", "  GNU bash, version 5.2"])
def test_a_bash_exe_whose_first_line_is_not_gnu_bash_refuses(
    tmp_path: Path, git_install: Path, monkeypatch: pytest.MonkeyPatch, version: str
):
    impostor = _bash(tmp_path, "impostor")
    monkeypatch.setitem(BASH_VERSION, str(impostor), version)
    with pytest.raises(AgentError, match="does not report GNU bash"):
        verification_shell.find_bash({"CLAUDE_CODE_GIT_BASH_PATH": str(impostor)})


def test_a_crlf_version_line_passes(tmp_path: Path, git_install: Path, monkeypatch: pytest.MonkeyPatch):
    real = _bash(tmp_path, "crlf")
    monkeypatch.setitem(BASH_VERSION, str(real), "GNU bash, version 5.2.37(1)-release\r\nCopyright")
    assert verification_shell.find_bash({"CLAUDE_CODE_GIT_BASH_PATH": str(real)}) == real


def test_a_bash_is_version_checked_once_per_request_only(
    tmp_path: Path, git_install: Path, monkeypatch: pytest.MonkeyPatch
):
    real = _bash(tmp_path, "counted")
    probed: list[str] = []
    fake_run = verification_shell.subprocess.run

    def counted(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        probed.append(argv[0])
        return fake_run(argv, **kwargs)

    monkeypatch.setattr(verification_shell.subprocess, "run", counted)
    environ = {"CLAUDE_CODE_GIT_BASH_PATH": str(real)}
    with verification_shell.bash_scope():
        for _ in range(3):
            assert verification_shell.find_bash(environ) == real
        assert probed == [str(real)]
        verification_shell.find_bash({**environ, "TZ": "UTC"})
        assert probed == [str(real)] * 2
    verification_shell.find_bash(environ)
    verification_shell.find_bash(environ)
    assert probed == [str(real)] * 4


def test_an_extended_length_system32_path_still_starts_wsl(git_install: Path):
    launcher = r"\\?\C:\Windows\System32\bash.exe"
    with pytest.raises(AgentError, match="starts WSL"):
        verification_shell.find_bash({"CLAUDE_CODE_GIT_BASH_PATH": launcher, "SYSTEMROOT": r"C:\Windows"})


def test_a_link_named_bash_exe_that_leads_to_the_launcher_refuses(tmp_path: Path, git_install: Path):
    launcher = _bash(tmp_path, "Windows/System32")
    link = tmp_path / "alias" / "bash.exe"
    link.parent.mkdir()
    try:
        link.symlink_to(launcher)
    except OSError:
        pytest.skip("this account cannot create symlinks")
    environ = {"CLAUDE_CODE_GIT_BASH_PATH": str(link), "SYSTEMROOT": str(tmp_path.resolve() / "Windows")}
    with pytest.raises(AgentError, match="which starts WSL"):
        verification_shell.find_bash(environ)


def test_carried_is_empty_off_windows(monkeypatch: pytest.MonkeyPatch, git_install: Path):
    monkeypatch.setattr(sys, "platform", "linux")
    assert verification_shell.carried({}) == {}
    monkeypatch.setattr(sys, "platform", "win32")
    assert verification_shell.carried({"PATH": "p"}) == {"AGENT_VERIFIER_BASH": str(git_install)}


def _repo(commands: bool) -> Any:
    verify = VerificationConfig((VerificationCommand("true", 5),), 10) if commands else None
    return SimpleNamespace(verification=verify, short="x")


def test_checked_windows_launch_refuses_without_git_bash(monkeypatch: pytest.MonkeyPatch):
    def missing(_environ: object = None) -> Path:
        raise AgentError("no Git Bash here")

    monkeypatch.setattr(verification_shell, "find_bash", missing)
    monkeypatch.setattr(sys, "platform", "win32")
    refused = verification_preflight(_repo(True))
    assert (refused["launch_allowed"], refused["reason"], refused["shell"]) == (
        False,
        "no Git Bash here",
        None,
    )
    assert verification_preflight(_repo(True), no_checks=True)["launch_allowed"] is True
    monkeypatch.setattr(sys, "platform", "linux")
    posix = verification_preflight(_repo(True))
    assert posix["launch_allowed"] is True and "shell" not in posix


class _Popen:
    pid = 7
    env: dict[str, str] = {}

    def __init__(self, _argv: list[str], **kw: Any):
        _Popen.env = kw["env"]


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_supervisor_env_drops_claude_names_and_carries_bash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str
):
    bash = _bash(tmp_path, "Git/bin")
    environ = {
        "CLAUDE_CODE_GIT_BASH_PATH": str(bash),
        "claude_session_id": "a",
        "Claude_X": "b",
        "AGENT_VERIFIER_BASH": "stale",
        "PATH": "p",
    }
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(os, "environ", environ)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x8000000, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x1000000, raising=False)
    monkeypatch.setattr(dispatch.supervisor, "popen", _Popen)
    monkeypatch.setattr(verification_shell, "_not_git_bash", lambda path, environ: "")
    monkeypatch.setattr(driver, "save", lambda root, d: d)
    monkeypatch.setattr(driver, "file", lambda root, id, ext: tmp_path / f"{id}{ext}")
    driver.detach(tmp_path, {"id": "001-01"})
    if platform == "win32":
        assert not any(k.upper().startswith("CLAUDE") for k in _Popen.env)
        assert _Popen.env["AGENT_VERIFIER_BASH"] == str(bash)
    else:
        assert {"claude_session_id", "Claude_X"} <= set(_Popen.env)
        assert "CLAUDE_CODE_GIT_BASH_PATH" not in _Popen.env
        assert "AGENT_VERIFIER_BASH" not in _Popen.env


@native
def test_native_verifier_pass_and_failing_command(fixture):
    settings = config("printf first", "echo from-bash-$BASH_VERSION")
    result = runner.wait(start(fixture, settings))
    assert result.cleanup_confirmed and assessment(fixture, result, settings).verified
    failing = config("printf before-failure; exit 7")
    result = runner.wait(start(fixture, failing))
    assert result.evidence is not None and result.cleanup_confirmed
    assert [(c.outcome, c.exit_code) for c in result.evidence.commands] == [("exited", 7)]
    assert not assessment(fixture, result, failing).verified


@native
def test_native_refused_job_assignment_runs_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from agent_cli.dispatch import verification_win as win

    def refuse(_name: object) -> object:
        raise OSError("assignment refused")

    monkeypatch.setattr(win, "create", refuse)
    marker = tmp_path / "marker"
    argv = [sys.executable, "-c", f"open(r'{marker}', 'w')"]
    with pytest.raises(AgentError, match="refusing to run it"):
        procs.capture(argv, tmp_path, procs.BASE_ENV, 5)
    assert not marker.exists()


@native
def test_native_kill_group_ends_a_worker_outside_any_verifier_job():
    """A real worker is the SDK's plain process, with no job named for its pid; a cancel's
    reap through `settle` under a live supervisor must still end it."""
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert procs.kill_worker(worker.pid)
        assert worker.wait(timeout=5) is not None
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait()


@native
def test_native_gitlink_through_a_junction_is_refused(tmp_path: Path):
    import _winapi

    worktree, target = tmp_path / "worktree", tmp_path / "elsewhere"
    worktree.mkdir()
    target.mkdir()
    _winapi.CreateJunction(str(target), str(worktree / "nested"))  # type: ignore[attr-defined]
    with pytest.raises(procs.UnsupportedTree, match="not an unpopulated directory"):
        procs._unpopulated_gitlinks(worktree, {b"nested"})
    (worktree / "empty").mkdir()
    procs._unpopulated_gitlinks(worktree, {b"empty", b"absent"})
