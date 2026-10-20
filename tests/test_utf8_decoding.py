"""Subprocess output on the dispatch path decodes as UTF-8 whatever the platform default is."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_cli import notes_claims, procs, repos
from agent_cli.core import AgentError
from agent_cli.dispatch import codex_policy

UTF8_D_CARON = b"\xc4\x8f"  # U+010F; byte 0x8f is undefined in cp1252


@pytest.fixture
def cp1252_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make a text-mode pipe with no explicit encoding decode as cp1252, as Windows does."""
    real = subprocess.Popen

    class Windows1252Popen(real):  # type: ignore[type-arg]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            if kwargs.get("text") and not kwargs.get("encoding"):
                kwargs["encoding"] = "cp1252"
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", Windows1252Popen)


def _emit(payload: bytes) -> list[str]:
    code = f"import sys; sys.stdout.buffer.write({payload!r}); sys.stderr.buffer.write({payload!r})"
    return [sys.executable, "-c", code]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows decodes pipes in reader threads, so the error never reaches the caller",
)
def test_shim_reproduces_the_windows_failure(cp1252_default: None) -> None:
    with pytest.raises(UnicodeDecodeError):
        subprocess.run(_emit(UTF8_D_CARON), capture_output=True, text=True, check=False)


def test_run_bounded_decodes_utf8_under_cp1252_default(cp1252_default: None) -> None:
    done = procs.run_bounded(_emit(b"a " + UTF8_D_CARON + " — end".encode()), timeout=30)
    assert done.stdout == "a ď — end"
    assert done.stderr == "a ď — end"


def test_run_bounded_replaces_invalid_utf8() -> None:
    done = procs.run_bounded(_emit(b"ok \xff\xfe end"), timeout=30)
    assert done.stdout == "ok �� end"
    assert done.stderr == "ok �� end"


def test_run_bounded_timeout_output_decodes_utf8_under_cp1252_default(cp1252_default: None) -> None:
    code = "import sys,time; sys.stdout.buffer.write(b'\\xc4\\x8f'); sys.stdout.flush(); time.sleep(60)"
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        procs.run_bounded([sys.executable, "-c", code], timeout=3)
    assert caught.value.output == "ď"


def test_run_git_decodes_utf8_under_cp1252_default(cp1252_default: None, tmp_path: Path) -> None:
    repos.run_git(["init", "-q"], tmp_path)
    repos.run_git(["config", "user.name", "ď"], tmp_path)
    assert repos.run_git(["config", "user.name"], tmp_path) == "ď\n"


def _fake_run(stdout: bytes, seen: list[dict[str, Any]]) -> Any:
    """A `subprocess.run` that decodes `stdout` exactly as the caller's encoding arguments say."""

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout.decode(kwargs["encoding"], kwargs["errors"]), "")

    return run


def test_run_git_keeps_names_differing_in_an_invalid_byte_distinct(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw = b"dir/a\xffb\0dir/a\xfeb\0"
    monkeypatch.setattr(subprocess, "run", _fake_run(raw, []))
    names = {n for n in repos.run_git(["ls-files", "-z"], tmp_path).split("\0") if n}
    assert len(names) == 2
    assert {n.encode("utf-8", "surrogateescape") for n in names} == {b"dir/a\xffb", b"dir/a\xfeb"}


def _git_dir_policy(monkeypatch: pytest.MonkeyPatch, raw: bytes, seen: list[dict[str, Any]]) -> None:
    monkeypatch.setattr(codex_policy.subprocess, "run", _fake_run(raw, seen))
    monkeypatch.setattr(Path, "is_dir", lambda self: True)  # noqa: ARG005 — the raw path cannot exist on this filesystem


def test_codex_writable_root_with_an_invalid_byte_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _git_dir_policy(monkeypatch, b"/repo\xff/.git\n", [])
    with pytest.raises(AgentError, match="not valid UTF-8"):
        codex_policy.writable_roots(tmp_path)


def test_codex_worktree_root_with_an_invalid_byte_refuses(tmp_path: Path) -> None:
    with pytest.raises(AgentError, match="not valid UTF-8"):
        codex_policy.writable_roots(tmp_path / "invalid\udcff")


def test_codex_valid_worktree_root_is_returned_unchanged(tmp_path: Path) -> None:
    assert codex_policy.writable_roots(tmp_path / "café") == [str(tmp_path / "café")]


def test_codex_git_dir_paths_differing_in_an_invalid_byte_stay_distinct(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    refusals: list[str] = []
    for raw in (b"/repo\xff/.git\n", b"/repo\xfe/.git\n"):
        seen: list[dict[str, Any]] = []
        _git_dir_policy(monkeypatch, raw, seen)
        with pytest.raises(AgentError) as caught:
            codex_policy.writable_roots(tmp_path)
        assert seen[0]["errors"] == "surrogateescape"
        refusals.append(str(caught.value))
    assert all("not valid UTF-8" in r for r in refusals)


class _Repo:
    def __init__(self, result: subprocess.CompletedProcess[Any] | None) -> None:
        self.result = result

    def git(self, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[Any] | None:
        return self.result


def test_notes_git_refuses_missing_stdout() -> None:
    repo: Any = _Repo(subprocess.CompletedProcess(["git"], 0, None, ""))
    with pytest.raises(AgentError, match="returned no output"):
        notes_claims._git(repo, "show", "HEAD:x")


def test_notes_git_returns_stdout() -> None:
    repo: Any = _Repo(subprocess.CompletedProcess(["git"], 0, "text\n", ""))
    assert notes_claims._git(repo, "show", "HEAD:x") == "text\n"
