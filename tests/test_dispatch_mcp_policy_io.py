"""Bounded product MCP file reads, worktree source selection, and sanitized I/O failures."""

import json
import os
import struct
import subprocess
import sys
import traceback
from pathlib import Path

import pytest

from agent_cli import repos
from agent_cli.core import AgentError
from agent_cli.dispatch import mcp_policy
from agent_cli.dispatch.mcp_policy import MAX_PRODUCT_BYTES, resolve
from test_dispatch_mcp_policy import (  # noqa: F401 -- shared pytest fixtures
    SECRET,
    product,
    record,
    repo,
)


def symlink_or_skip(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        if os.name == "nt" and exc.winerror == 1314:
            pytest.skip("native Windows symlink creation: WinError 1314 (privilege not held)")
        raise


@pytest.mark.parametrize(
    "contents",
    [
        "[" + SECRET,
        "[]",
        "{}",
        '{"mcpServers": []}',
        '{"mcpServers":{"a":{"command":"first"},"a":{"command":"second"}}}',
        '{"mcpServers":{},"mcpServers":{}}',
        '{"mcpServers":{"a":{"command":"x","extra":NaN}}}',
    ],
)
def test_invalid_selected_source_is_sanitized(tmp_path: Path, repo: repos.Repo, record: dict, contents):
    repo.dispatch = {"repo_mcp": ["a"]}
    (Path(repo.path) / ".mcp.json").write_text(contents)
    with pytest.raises(AgentError) as exc:
        resolve(tmp_path, repo, record)
    assert SECRET not in "".join(traceback.format_exception(exc.value))


def test_unknown_and_missing_source_are_errors(tmp_path: Path, repo: repos.Repo, record: dict):
    repo.dispatch = {"repo_mcp": ["missing"]}
    with pytest.raises(AgentError, match="unreadable"):
        resolve(tmp_path, repo, record)
    product(repo, {})
    with pytest.raises(AgentError, match="unknown"):
        resolve(tmp_path, repo, record)


def test_permission_and_encoding_errors_are_sanitized(
    tmp_path: Path,
    repo: repos.Repo,
    record: dict,
    monkeypatch: pytest.MonkeyPatch,
):
    repo.dispatch = {"repo_mcp": ["a"]}
    (Path(repo.path) / ".mcp.json").write_bytes(b"\xff")
    with pytest.raises(AgentError, match="invalid"):
        resolve(tmp_path, repo, record)

    def denied(*args, **kwargs):
        raise PermissionError(SECRET)

    product(repo, {"a": {"command": "fixture"}})
    if os.name == "nt":
        monkeypatch.setattr(mcp_policy, "_windows_product_fd", denied)
    else:
        monkeypatch.setattr(os, "open", denied)
    with pytest.raises(AgentError, match="unreadable") as exc:
        resolve(tmp_path, repo, record)
    assert SECRET not in str(exc.value)


def test_worktree_origin_and_no_existing_worktree_fallback(tmp_path: Path, repo: repos.Repo, record: dict):
    repo.dispatch = {"repo_mcp": ["a"]}
    product(repo, {"a": {"command": "clone"}})
    worktree = tmp_path / "worktree"
    record["worktree"] = str(worktree)
    assert resolve(tmp_path, repo, record).sources[-1].definition_source.startswith("clone:")
    worktree.mkdir()
    for contents in (None, "bad", '{"mcpServers":{}}'):
        if contents is not None:
            (worktree / ".mcp.json").write_text(contents)
        with pytest.raises(AgentError):
            resolve(tmp_path, repo, record)
    (worktree / ".mcp.json").write_text('{"mcpServers":{"a":{"command":"worktree"}}}')
    result = resolve(tmp_path, repo, record)
    assert result.servers["a"]["command"] == "worktree"
    assert result.sources[-1].definition_source.startswith("worktree:")


@pytest.mark.parametrize("kind", ["file", "dangling-link", "permission-error"])
def test_invalid_worktree_never_falls_back(
    tmp_path: Path,
    repo: repos.Repo,
    record: dict,
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
):
    repo.dispatch = {"repo_mcp": ["a"]}
    product(repo, {"a": {"command": "clone"}})
    worktree = tmp_path / "worktree"
    record["worktree"] = str(worktree)
    if kind == "file":
        worktree.write_text("not a directory")
    elif kind == "dangling-link":
        symlink_or_skip(worktree, tmp_path / "missing", directory=True)
    else:
        original = Path.lstat

        def denied(path: Path, *args, **kwargs):
            if path == worktree:
                raise PermissionError(SECRET)
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "lstat", denied)
    with pytest.raises(AgentError) as exc:
        resolve(tmp_path, repo, record)
    assert SECRET not in str(exc.value)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO fixture")
@pytest.mark.parametrize("symlink", [False, True])
def test_selected_fifo_refuses_without_waiting_for_a_writer(
    tmp_path: Path,
    repo: repos.Repo,
    symlink: bool,
):
    source = Path(repo.path) / ".mcp.json"
    fifo = tmp_path / "source-fifo" if symlink else source
    os.mkfifo(fifo)
    if symlink:
        source.symlink_to(fifo)
    # Bound the test itself: the old blocking reader must fail by timeout, not hang pytest.
    script = """
import sys
from pathlib import Path
from agent_cli.core import AgentError
from agent_cli.repos import Repo
from agent_cli.dispatch.mcp_policy import resolve
repo = Repo('fixture', 'fixture', sys.argv[2], 'unused', dispatch={'repo_mcp': ['a']})
try:
    resolve(Path(sys.argv[1]), repo, {'id': '001-01', 'feature': 1})
except AgentError as exc:
    assert 'unreadable or invalid' in str(exc)
    print('refused special source')
else:
    raise AssertionError('FIFO accepted')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), repo.path],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "refused special source"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO fixture")
def test_unselected_fifo_is_never_opened(
    tmp_path: Path,
    repo: repos.Repo,
    record: dict,
    monkeypatch: pytest.MonkeyPatch,
):
    os.mkfifo(Path(repo.path) / ".mcp.json")

    def never_open(*args, **kwargs):
        raise AssertionError("unselected FIFO was opened")

    monkeypatch.setattr(os, "open", never_open)
    assert list(resolve(tmp_path, repo, record).servers) == ["agent"]


def test_regular_file_symlink_is_supported(tmp_path: Path, repo: repos.Repo, record: dict):
    source = tmp_path / "shared-config.json"
    source.write_text('{"mcpServers":{"a":{"command":"fixture"}}}')
    symlink_or_skip(Path(repo.path) / ".mcp.json", source)
    repo.dispatch = {"repo_mcp": ["a"]}
    assert resolve(tmp_path, repo, record).servers["a"]["command"] == "fixture"


@pytest.mark.parametrize("understated_size", [False, True])
def test_oversize_source_refuses_before_json_even_when_stat_understates_size(
    tmp_path: Path,
    repo: repos.Repo,
    record: dict,
    understated_size: bool,
    monkeypatch: pytest.MonkeyPatch,
):
    source = Path(repo.path) / ".mcp.json"
    source.write_bytes(b" " * (MAX_PRODUCT_BYTES + 10))
    repo.dispatch = {"repo_mcp": ["a"]}
    original_stat, original_path_stat, original_read = os.fstat, Path.stat, os.read
    read_bytes = 0

    def small_stat(fd):
        values = list(original_stat(fd))
        values[6] = 0
        return os.stat_result(values)

    def counted_read(fd, size):
        nonlocal read_bytes
        data = original_read(fd, size)
        read_bytes += len(data)
        return data

    def never_parse(*args, **kwargs):
        raise AssertionError("oversize source reached JSON parser")

    if understated_size:
        monkeypatch.setattr(os, "fstat", small_stat)

        def small_path_stat(path, *args, **kwargs):
            values = list(original_path_stat(path, *args, **kwargs))
            values[6] = 0
            return os.stat_result(values)

        monkeypatch.setattr(Path, "stat", small_path_stat)
    monkeypatch.setattr(os, "read", counted_read)
    monkeypatch.setattr(json, "loads", never_parse)
    with pytest.raises(AgentError, match="unreadable or invalid"):
        resolve(tmp_path, repo, record)
    assert read_bytes == (MAX_PRODUCT_BYTES + 1 if understated_size else 0)


def test_exact_byte_limit_accepts_valid_source(tmp_path: Path, repo: repos.Repo, record: dict):
    content = b'{"mcpServers":{"a":{"command":"fixture"}}}'
    (Path(repo.path) / ".mcp.json").write_bytes(content.ljust(MAX_PRODUCT_BYTES, b" "))
    repo.dispatch = {"repo_mcp": ["a"]}
    assert resolve(tmp_path, repo, record).servers["a"]["command"] == "fixture"


def test_unsupported_inspection_has_distinct_sanitized_error(
    tmp_path: Path, repo: repos.Repo, record: dict, monkeypatch: pytest.MonkeyPatch
):
    repo.dispatch = {"repo_mcp": ["a"]}
    product(repo, {"a": {"command": "fixture"}})
    if os.name == "nt":
        import ctypes

        def unavailable(*args, **kwargs):
            raise OSError(SECRET)

        monkeypatch.setattr(ctypes, "WinDLL", unavailable)
    else:
        monkeypatch.delattr(os, "O_NONBLOCK")
    with pytest.raises(AgentError, match="unsupported on this platform") as exc:
        resolve(tmp_path, repo, record)
    assert SECRET not in "".join(traceback.format_exception(exc.value))


def _junction(link: Path, target: Path) -> None:
    import ctypes
    from ctypes import wintypes

    load_dll = getattr(ctypes, "WinDLL", None)
    last_error = getattr(ctypes, "get_last_error", None)
    assert load_dll is not None and last_error is not None
    kernel = load_dll("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    control = kernel.DeviceIoControl
    control.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    control.restype = wintypes.BOOL
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    link.mkdir()
    substitute = ("\\??\\" + str(target)).encode("utf-16-le")
    printable = str(target).encode("utf-16-le")
    names = substitute + b"\0\0" + printable + b"\0\0"
    payload = (
        struct.pack(
            "<IHHHHHH", 0xA0000003, 8 + len(names), 0, 0, len(substitute), len(substitute) + 2, len(printable)
        )
        + names
    )
    handle = create(str(link), 0x40000000, 7, None, 3, 0x02200000, None)
    assert handle != ctypes.c_void_p(-1).value, last_error()
    try:
        returned = wintypes.DWORD()
        assert control(handle, 0x900A4, payload, len(payload), None, 0, ctypes.byref(returned), None), (
            last_error()
        )
    finally:
        close(handle)


def _native_windows_probe(kind: str, root: Path) -> None:
    import ctypes
    from ctypes import wintypes

    source = root / ".mcp.json"
    content = b'{"mcpServers":{"a":{"command":"fixture"}}}'
    source.write_bytes(content)
    if kind == "junction":
        target = root / "target"
        target.mkdir()
        (target / ".mcp.json").write_bytes(content)
        _junction(root / "link", target)
        assert mcp_policy._product_text(root / "link" / ".mcp.json") == content.decode()
        return
    if kind in {"symlink-device", "symlink-directory"}:
        source.unlink()
        target = Path("NUL") if kind == "symlink-device" else root
        symlink_or_skip(source, target, directory=kind == "symlink-directory")
    if kind == "directory":
        source.unlink()
        source.mkdir()
    if kind in {"device", "device-namespace", "extended-path", "stream"}:
        source = {
            "device": root / "NUL",
            "device-namespace": Path(r"\\.\NUL"),
            "extended-path": Path("\\\\?\\" + str(source)),
            "stream": Path(str(source) + ":stream"),
        }[kind]
    if kind in {"pipe", "symlink-pipe"}:
        load_dll = getattr(ctypes, "WinDLL", None)
        last_error = getattr(ctypes, "get_last_error", None)
        assert load_dll is not None and last_error is not None
        kernel = load_dll("kernel32", use_last_error=True)
        create_pipe = kernel.CreateNamedPipeW
        create_pipe.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        create_pipe.restype = wintypes.HANDLE
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        source = Path(r"\\.\pipe\agent-mcp-probe-" + str(os.getpid()))
        handle = create_pipe(str(source), 1, 0, 1, 1024, 1024, 0, None)
        assert handle != ctypes.c_void_p(-1).value, last_error()
        try:
            if kind == "symlink-pipe":
                link = root / "pipe-link"
                symlink_or_skip(link, source)
                source = link
            with pytest.raises((OSError, ValueError)):
                mcp_policy._product_text(source)
        finally:
            close(handle)
        return
    if kind == "growth":
        source.write_bytes(content.ljust(65536, b" "))
        real_read = os.read
        total = 0

        def growing_read(fd, size):
            nonlocal total
            data = real_read(fd, size)
            if total == 0:
                with source.open("ab") as writer:
                    writer.write(b" " * MAX_PRODUCT_BYTES)
            total += len(data)
            return data

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(os, "read", growing_read)
            with pytest.raises(ValueError, match="byte limit"):
                mcp_policy._product_text(source)
        assert total == MAX_PRODUCT_BYTES + 1
        return
    if kind in {"replacement", "directory-race", "ancestor-race", "symlink-race"}:
        real_stat = Path.stat
        swapped = False
        parked = root.with_name(root.name + "-parked")

        def replacing_stat(path, *args, **kwargs):
            nonlocal swapped
            info = real_stat(path, *args, **kwargs)
            if path == source and not swapped:
                swapped = True
                if kind == "ancestor-race":
                    root.rename(parked)
                    _junction(root, parked)
                else:
                    source.rename(root / "old.json")
                    if kind == "replacement":
                        source.write_bytes(content)
                    elif kind == "directory-race":
                        source.mkdir()
                    else:
                        symlink_or_skip(source, root / "old.json")
            return info

        try:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(Path, "stat", replacing_stat)
                with pytest.raises((OSError, ValueError)):
                    mcp_policy._product_text(source)
            assert swapped
        finally:
            if kind == "ancestor-race" and swapped:
                root.rmdir()
                parked.rename(root)
        return
    with pytest.raises((OSError, ValueError)):
        mcp_policy._product_text(source)


@pytest.mark.skipif(os.name != "nt", reason="native Windows file APIs")
@pytest.mark.parametrize(
    "kind",
    [
        "directory",
        "device",
        "device-namespace",
        "extended-path",
        "stream",
        "pipe",
        "symlink-pipe",
        "growth",
        "replacement",
        "directory-race",
        "ancestor-race",
        "junction",
        "symlink-device",
        "symlink-directory",
        "symlink-race",
    ],
)
def test_native_windows_sources_are_bounded(tmp_path: Path, kind: str):
    script = """
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'tests'))
import pytest
from test_dispatch_mcp_policy_io import _native_windows_probe
try:
    _native_windows_probe(sys.argv[1], Path(sys.argv[2]))
except pytest.skip.Exception as exc:
    print('SKIP: ' + str(exc))
else:
    print('PASS: ' + sys.argv[1])
"""
    result = subprocess.run(
        [sys.executable, "-c", script, kind, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    if result.stdout.startswith("SKIP: "):
        pytest.skip(result.stdout.strip())
    assert result.stdout.strip() == "PASS: " + kind
