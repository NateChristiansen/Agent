"""Seeded fixtures built once per session and copied per test (verify speed, phase 2).

`droot` and the ops-case roots used to run their seeding through the CLI for every test: a
`feature new` (a notes claim of about 13 git calls), task adds, STATUS renders and config writes,
about 6.6 s of setup for a 0.15 s test. The seeded state is the same for every test, so it is built
once per xdist worker in a template laid out exactly like the per-test `tmp_path` (the harness
root, its remotes and the notes repos side by side) and copied over each test's tree.

A copy keeps nothing of the template's location: every text file outside git's object store is
repointed from the template's path to the test's, so registries, git configs, the notes root and
FETCH_HEAD name the test's own remotes. Each test gets its own copies of every repository,
remote included, so nothing one test writes reaches another (`test_seeded_isolation.py`).
"""

from __future__ import annotations

import io
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

import obsidian_guard
from agent_cli import links
from harness_root import (  # pyright: ignore[reportPrivateUsage]
    _REPOINTED,
    FIXED_NOW,
    _pairs,
    copy_notes,
    repoint_files,
    swap,
)

# The caller's Claude and Codex session, cleared for every test (conftest's
# `_no_host_session_identity`, which explains each) and for template seeding.
HOST_IDENTITY = (
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_SESSION_ID",
    "CLAUDE_PID",
    "CODEX_SESSION_ID",
    "CODEX_THREAD_ID",
    "AGENT_RIBBON_LAUNCH",
    # `land`'s verify run carries its own dispatch id; the ask guard reads it as "worker" and
    # goes quiet, so every test that expects a manager block failed under land.
    "AGENT_DISPATCH_ID",
)

_SKIP_DIRS = ("objects",)  # git's object store holds compressed blobs, never a path to repoint
_MAX_REPOINT = 512 * 1024


def repoint(tree: Path, old: Path, new: Path) -> None:
    """Replace `old` with `new` in every text file under `tree`, outside git object stores, in each
    form a path is written in."""
    olds = list(_pairs(old, new))
    for dirpath, dirs, files in os.walk(tree):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            try:
                if path.stat().st_size > _MAX_REPOINT:
                    continue
                data = path.read_bytes()
            except OSError:
                continue
            if not any(o.encode() in data for o in olds):
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            path.write_text(swap(text, old, new), encoding="utf-8")


def writable_retry(func: Callable[..., object], path: str, exc: BaseException) -> None:
    """Git writes its objects read-only, and Windows refuses to delete a read-only file: clear
    the bit and retry once. A link is never chmodded. Anything else still fails the setup.
    Pass it as `shutil.rmtree(..., onexc=writable_retry)`."""
    if os.path.islink(path) or not isinstance(exc, PermissionError):
        raise exc
    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    func(path)


def unlink(path: Path) -> None:
    """`path.unlink()`, clearing a read-only bit Windows would refuse it for."""
    try:
        path.unlink()
    except PermissionError as exc:
        writable_retry(os.unlink, str(path), exc)


def _empty(directory: Path) -> None:
    """Remove everything under `directory` and keep the directory: Windows cannot remove a
    directory that is a process's working directory, and a test's `tmp_root` is one."""
    for child in directory.iterdir():
        if child.is_junction():
            child.rmdir()
        elif child.is_symlink() or not child.is_dir():
            unlink(child)
        else:
            shutil.rmtree(child, onexc=writable_retry)


class _LinkCopy:
    """Copies that keep the links `links.make` falls back to on Windows. `copytree` follows a
    junction and copies a hard link as a separate file, which unlinked the seeded harness from
    its notes repo. A junction into the template is remade into the copy, after every entry is
    copied so its target exists; a file met again by inode is hard linked to its first copy."""

    def __init__(self, template: Path, dest: Path) -> None:
        self.template, self.dest = template, dest
        self.junctions: list[Path] = []
        self.inodes: dict[tuple[int, int], str] = {}

    def skip_junctions(self, directory: str, names: list[str]) -> set[str]:
        skipped = {name for name in names if Path(directory, name).is_junction()}
        self.junctions.extend(Path(directory, name) for name in skipped)
        return skipped

    def copy_file(self, src: str, dst: str) -> object:
        info = os.lstat(src)
        if links.IS_WINDOWS and info.st_nlink > 1:
            first = self.inodes.setdefault((info.st_dev, info.st_ino), dst)
            if first != dst:
                os.link(first, dst)
                return dst
        return shutil.copy2(src, dst, follow_symlinks=False)

    def tree(self, src: Path, dst: Path) -> None:
        if src.is_junction():
            self.junctions.append(src)
            return
        shutil.copytree(
            src,
            dst,
            symlinks=True,
            dirs_exist_ok=True,
            ignore=self.skip_junctions,
            copy_function=self.copy_file,
        )

    def relink(self) -> None:
        for src in self.junctions:
            dst = self.dest / src.relative_to(self.template)
            target = links.link_target(src)
            for root in (self.template, self.template.resolve()):
                if target.is_relative_to(root):
                    target = self.dest / target.relative_to(root)
                    break
            links.make(dst, target, is_dir=True)


def install(template: Path, dest: Path) -> None:
    """Make `dest` hold the template's tree: each top-level entry replaced, then repointed. A
    directory the template also has as a directory is emptied and kept; any other is removed."""
    copy = _LinkCopy(template, dest)
    for entry in template.iterdir():
        target = dest / entry.name
        copies_a_dir = entry.is_dir() and not links.is_link(entry)
        if target.is_junction():
            target.rmdir()
        elif target.is_symlink() or target.is_file():
            unlink(target)
        elif target.exists():
            _empty(target)
            if not copies_a_dir:
                target.rmdir()
        if copies_a_dir or entry.is_junction():
            copy.tree(entry, target)
        else:
            copy.copy_file(str(entry), str(target))
    copy.relink()
    repoint(dest, template, dest)


@contextmanager
def seeding(root: Path) -> Generator[Callable[..., int]]:
    """The environment `tmp_root` gives a test, for building a template outside any test, and a
    quiet in-process CLI call. Session fixtures run before every function-scoped autouse guard,
    so seeding installs the same ones itself : the Obsidian guard, a
    cleared host identity and an empty process table."""
    from agent_cli import codex_mcp, procs
    from agent_cli.cli import main

    mp = pytest.MonkeyPatch()
    stubs = Path(tempfile.mkdtemp(prefix="seed-guard-"))
    guard = obsidian_guard.install(stubs, stubs / "calls", mp)
    try:
        mp.setenv("AGENT_ROOT", str(root))
        mp.setenv("AGENT_NOW", FIXED_NOW)
        mp.setenv("AGENT_NOTIFY", "off")
        for name in HOST_IDENTITY:
            mp.delenv(name, raising=False)

        def no_processes() -> list[object]:
            return []

        mp.setattr(procs, "snapshot", no_processes)

        def probe(_root: object, _entry: object) -> tuple[bool, str]:
            return True, "fixture MCP probe"

        mp.setattr(codex_mcp, "probe", probe)
        mp.chdir(root)

        def call(argv: list[str], stdin: str | None = None) -> int:
            old_stdin = sys.stdin
            if stdin is not None:
                sys.stdin = io.StringIO(stdin)
            try:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    try:
                        code = main(argv)
                    except SystemExit as exc:
                        code = int(exc.code or 0)
            finally:
                sys.stdin = old_stdin
            if code != 0:
                raise AssertionError(f"seeding `agent {' '.join(argv)}` exited {code}")
            return code

        yield call
        assert not guard.blocked, f"seeding tried to start the real Obsidian: {guard.blocked}"
    finally:
        mp.undo()
        shutil.rmtree(stubs, ignore_errors=True)


def base_root(base: Path, root_template: Path, notes_template: Path) -> Path:
    """`tmp_root` plus `notes_remote`, laid out under `base` as a test's `tmp_path` holds them."""
    from agent_cli import notes as notes_mod

    shutil.copytree(root_template, base, symlinks=True, dirs_exist_ok=True)
    repoint_files(base, root_template, _REPOINTED)
    root = base / "harness"
    notes_mod.write_notes_root(root, copy_notes(notes_template, base))
    return root


@pytest.fixture(autouse=True)
def _aa_tmp_path_first(tmp_path: Path) -> Path:
    """Set `tmp_path` up before any other function fixture, so it is torn down last (autouse
    fixtures of one scope are set up in name order, hence the `_aa` prefix). With
    `tmp_path_retention_policy = "failed"` its teardown deletes the tree, and a test's monkeypatch
    of `os.open` or `Path.lstat` must already be undone by then (verify speed, phase 2)."""
    return tmp_path
