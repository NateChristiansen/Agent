"""Generated-file guards match Windows path spellings.

On a Windows clone a synthetic apply_patch that updated STATUS.md got no deny:
`os.path.normpath` spelled paths with `\\`, and every protected-path pattern matches `/` only.
These run on every OS with backslash spellings; the native tests at the bottom use real
Windows absolute paths on windows-latest.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agent_cli import hooks
from agent_cli.hooks.paths import check_command, relative, slashes

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="real Windows absolute paths")


def _patch(*sections: str) -> str:
    payload = {
        "session_id": "mine",
        "hook_event_name": "PreToolUse",
        "tool_name": "apply_patch",
        "tool_input": {"command": "*** Begin Patch\n" + "\n".join(sections) + "\n*** End Patch"},
    }
    return json.dumps(payload)


def _decision(out: str) -> str:
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else ""


def test_slashes_only_swaps_separators() -> None:
    assert slashes(r"features\030-x\log.md") == "features/030-x/log.md"
    assert slashes("features/030-x/log.md") == "features/030-x/log.md"


@pytest.mark.parametrize(
    "path",
    [
        "STATUS.md",
        r"features\030-example-feature\log.md",
        r"features\030-example-feature\handoffs\h.md",
        r"bases\features.base",
        r"adrs\README.md",
        r".agent\status.json",
        r".agent\sessions\abc.json",
        r".agent\raw\030-x\2026-09-27.jsonl",
    ],
)
def test_backslash_patch_to_a_generated_file_is_denied(tmp_root: Path, path: str) -> None:
    out, code = hooks.run(tmp_root, "protect-generated", _patch(f"*** Update File: {path}\n@@\n-a\n+b"))
    assert code == 0 and _decision(out) == "deny", path


@pytest.fixture
def active_manager(tmp_root: Path, run, monkeypatch: pytest.MonkeyPatch, notes_remote: Path) -> None:
    monkeypatch.delenv("AGENT_DISPATCH_ID", raising=False)
    assert run(["feature", "new", "edit-owner", "--title", "Edit owner"]).code == 0
    assert run(["feature", "use", "1", "--session-id", "mine"]).code == 0


@pytest.mark.parametrize("path", [r".agent\scratch\plan.md", r"features\030-x\plan.md", r"harness\notes.md"])
def test_backslash_patch_to_a_hand_edited_file_is_not_denied(
    tmp_root: Path, path: str, active_manager
) -> None:
    out, _ = hooks.run(tmp_root, "protect-generated", _patch(f"*** Add File: {path}\n+text"))
    assert _decision(out) != "deny", path


def test_relative_spells_forward_slashes(tmp_root: Path) -> None:
    rel = relative(tmp_root, str(tmp_root / "features" / "030-x" / "log.md"))
    assert rel == "features/030-x/log.md"


@pytest.mark.parametrize(
    "cmd",
    [
        r"echo x > .agent\status.json",
        r"cp draft.md features\030-x\log.md",
        r"Set-Content -Path STATUS.md -Value x > STATUS.md",
    ],
)
def test_windows_shell_write_with_backslashes_is_denied(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch, cmd: str
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert _decision(check_command(tmp_root, cmd)) == "deny", cmd


@pytest.mark.parametrize(
    "cmd",
    [
        r"echo x > STATUS\.md",
        r"cp draft.md STATUS\.md",
        r"echo x > features/030-x/log\.md",
        r"cp draft.md \.agent/status.json",
    ],
)
def test_windows_git_bash_escapes_are_still_read_as_escapes(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch, cmd: str
) -> None:
    # Claude's shell on Windows is Git Bash, where `\\.` is an escaped dot, not a separator.
    monkeypatch.setattr(sys, "platform", "win32")
    assert _decision(check_command(tmp_root, cmd)) == "deny", cmd


def test_windows_backslashes_to_a_hand_edited_file_are_allowed(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert check_command(tmp_root, r"echo x > .agent\scratch\plan.md") == ""
    assert check_command(tmp_root, r"echo x > notes\draft\.md") == ""


def test_posix_shell_keeps_backslash_escapes(tmp_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # On POSIX `\ ` escapes a space; only Windows treats a backslash as a separator in commands.
    monkeypatch.setattr(sys, "platform", "linux")
    assert check_command(tmp_root, r"echo a\ b > notes.txt") == ""


@windows_only
def test_native_absolute_windows_patch_path_is_denied(tmp_root: Path) -> None:
    for path in (tmp_root / "STATUS.md", tmp_root / "features" / "030-x" / "log.md"):
        out, _ = hooks.run(tmp_root, "protect-generated", _patch(f"*** Update File: {path}\n@@\n-a\n+b"))
        assert _decision(out) == "deny", str(path)


@windows_only
def test_native_drive_case_does_not_escape_the_root(tmp_root: Path) -> None:
    flipped = str(tmp_root / "STATUS.md")
    flipped = flipped[0].swapcase() + flipped[1:]
    out, _ = hooks.run(tmp_root, "protect-generated", _patch(f"*** Update File: {flipped}\n@@\n-a\n+b"))
    assert _decision(out) == "deny"
