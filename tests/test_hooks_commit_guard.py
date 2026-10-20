"""A mixed-session stage of append-only records needs every file's lease."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_cli import leases, notes
from agent_cli.hooks import commit_guard as guard_module


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(path: Path) -> None:
    """A repo on a work branch: a commit on the harness's main is main_guard's to refuse."""
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "work")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")


def _decision(result: str) -> str:
    return json.loads(result)["hookSpecificOutput"]["permissionDecision"]


def test_commit_guard_denies_mixed_session_feature_logs(tmp_path: Path, monkeypatch):
    root = tmp_path / "vault"
    notes_root = tmp_path / "notes"
    root.mkdir()
    _repo(notes_root)
    notes.write_notes_root(root, notes_root)
    paths = ["features/001-one/log.md", "features/002-two/log.md"]
    for path in paths:
        file = notes_root / path
        file.parent.mkdir(parents=True)
        file.write_text("first\n", encoding="utf-8")
    _git(notes_root, "add", ".")
    _git(notes_root, "commit", "-qm", "baseline")
    for path in paths:
        with (notes_root / path).open("a", encoding="utf-8") as stream:
            stream.write("next\n")
    _git(notes_root, "add", ".")

    monkeypatch.setattr(guard_module, "other_managers_live", lambda *_: True)
    leases.take(root, f"vault:{paths[0]}", session_id="mine", pid=os.getpid())
    leases.take(root, f"vault:{paths[1]}", session_id="other", pid=os.getpid())
    payload = {
        "tool_name": "Bash",
        "cwd": str(root),
        "session_id": "mine",
        "tool_input": {"command": f"git -C {notes_root.as_posix()} commit -m update"},
    }
    result = guard_module.commit_guard(root, payload)
    assert _decision(result) == "deny"
    assert paths[1] in result
    assert paths[0] not in result

    leases.release(root, f"vault:{paths[1]}", "other")
    leases.take(root, f"vault:{paths[1]}", session_id="mine", pid=os.getpid())
    assert guard_module.commit_guard(root, payload) == ""


@pytest.mark.parametrize(
    ("value", "windows", "posix"),
    [
        ("/c/Users/me/Agent-notes", "C:/Users/me/Agent-notes", "/c/Users/me/Agent-notes"),
        ("/d", "D:/", "/d"),
        ("C:/Users/me/notes", "C:/Users/me/notes", "C:/Users/me/notes"),
        ("../Agent-notes", "../Agent-notes", "../Agent-notes"),
        ("/cdrive/x", "/cdrive/x", "/cdrive/x"),  # not a drive letter
    ],
)
def test_git_bash_drive_paths_become_native_on_windows(monkeypatch, value: str, windows: str, posix: str):
    """`git -C /c/Users/...` from Git Bash made the guard's own git
    call fail, and it denied the owner's notes-repo commit."""
    monkeypatch.setattr(guard_module.sys, "platform", "win32")
    assert guard_module.native_path(value) == windows
    monkeypatch.setattr(guard_module.sys, "platform", "darwin")
    assert guard_module.native_path(value) == posix


@pytest.mark.parametrize(
    ("value", "windows"),
    [
        ("/tmp/x", "C:/Users/me/AppData/Local/Temp/x"),
        ("/tmp", "C:/Users/me/AppData/Local/Temp/"),
        ("/tmpdir/x", "/tmpdir/x"),  # not the /tmp mount
    ],
)
def test_git_bash_tmp_is_the_user_temp_directory_on_windows(monkeypatch, value: str, windows: str):
    """`git -C /tmp/x commit` from Git Bash became `C:\\tmp\\x`, and the
    guard denied it with WinError 267 while another manager was live."""
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: "C:/Users/me/AppData/Local/Temp")
    monkeypatch.setattr(guard_module.sys, "platform", "win32")
    assert guard_module.native_path(value) == windows
    monkeypatch.setattr(guard_module.sys, "platform", "darwin")
    assert guard_module.native_path(value) == value


@pytest.mark.parametrize(
    "command",
    [
        r"git -C C:\Users\me\Agent-notes commit -m update",
        r'git -C "C:\Users\me\Agent notes" commit -m update',
        "git -C /c/Users/me/Agent-notes commit -m update",
    ],
)
def test_windows_notes_paths_resolve_to_the_drive(monkeypatch, command: str):
    monkeypatch.setattr(guard_module.sys, "platform", "win32")
    calls = guard_module.commit_calls(command, Path("C:/vault"))
    # Some reading of the command lands on the notes repo on drive C; the guard checks them all.
    targets = [at.as_posix().lower() for _, at in calls]
    assert any(t.endswith(("users/me/agent-notes", "users/me/agent notes")) for t in targets), targets


@pytest.mark.parametrize(
    ("command", "notes_dir"),
    [
        (r"git -C /c/Users/me/Agent\ notes commit -m update", "users/me/agent notes"),
        (r"git -C /c/Users/me/Agent\.notes commit -m update", "users/me/agent.notes"),
        (r'git -C "/c/Users/me/Agent notes" commit -m "fix \"quoted\" text"', "users/me/agent notes"),
    ],
)
def test_git_bash_escapes_still_reach_the_commit(monkeypatch, command: str, notes_dir: str):
    """Rewriting every backslash turned `Agent\\ notes` into `Agent/` plus a stray `notes`
    subcommand, and the guard never inspected the commit."""
    monkeypatch.setattr(guard_module.sys, "platform", "win32")
    targets = [at.as_posix().lower() for _, at in guard_module.commit_calls(command, Path("C:/vault"))]
    assert any(t.endswith(notes_dir) for t in targets), targets


def test_commit_guard_requires_rubric_lease_only_during_overlap(tmp_path: Path, monkeypatch):
    root = tmp_path / "vault"
    _repo(root)
    file = root / "harness/dispatch-rubric.md"
    file.parent.mkdir()
    file.write_text("baseline\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    file.write_text("baseline\nnew row\n", encoding="utf-8")
    _git(root, "add", ".")
    payload = {
        "tool_name": "Bash",
        "cwd": str(root),
        "session_id": "mine",
        "tool_input": {"command": "git commit -m calibration"},
    }
    monkeypatch.setattr(guard_module, "other_managers_live", lambda *_: False)
    assert guard_module.commit_guard(root, payload) == ""
    monkeypatch.setattr(guard_module, "other_managers_live", lambda *_: True)
    # Uncontended: the lease is taken for this session and the commit goes through (all four
    # commit-guard lease denials in the audit week cleared in seconds this way).
    assert guard_module.commit_guard(root, payload) == ""
    held = leases.get(root, "vault:harness/dispatch-rubric.md")
    assert held is not None and held["session_id"] == "mine"
    # A live holder in another session still refuses, and nothing is taken from it.
    leases.release(root, "vault:harness/dispatch-rubric.md", "mine")
    leases.take(root, "vault:harness/dispatch-rubric.md", session_id="other", pid=os.getpid())
    assert _decision(guard_module.commit_guard(root, payload)) == "deny"
    kept = leases.get(root, "vault:harness/dispatch-rubric.md")
    assert kept is not None and kept["session_id"] == "other"


def test_a_code_commit_needs_no_full_run(tmp_path: Path, monkeypatch):
    """The per-commit full-run gate is gone: landing and pushing require a pass record instead."""
    root = tmp_path / "vault"
    _repo(root)
    (root / "src").mkdir()
    (root / "src" / "x.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    monkeypatch.setattr(guard_module, "other_managers_live", lambda *_: False)
    payload = {
        "tool_name": "Bash",
        "cwd": str(root),
        "session_id": "mine",
        "tool_input": {"command": "git commit -m code"},
    }
    assert guard_module.commit_guard(root, payload) == ""


@pytest.mark.parametrize(
    "command",
    ["git commit --no-verify -m x", "git commit -n -m x", "git -c core.hooksPath=/dev/null commit -m x"],
)
def test_skipping_the_hooks_is_still_refused(tmp_path: Path, command: str):
    root = tmp_path / "vault"
    _repo(root)
    payload = {
        "tool_name": "Bash",
        "cwd": str(root),
        "session_id": "mine",
        "tool_input": {"command": command},
    }
    assert _decision(guard_module.commit_guard(root, payload)) == "deny"
