"""Resolve explicit worker MCP sources, without starting servers or granting tool calls.

Launch wiring is separate. `resolve` reads selected definitions once and returns a private
server map plus a safe preview. This validates local configuration structure, not native
permission enforcement. Profile launchers still re-read their profile when executed; this
snapshot is not an atomic grant binding across resolution and process startup.
Selected product files must resolve to regular files and fit MAX_PRODUCT_BYTES; regular-file
symlinks are supported. The existing worker-profile registry reader remains unbounded.
"""

from __future__ import annotations

import copy
import json
import os
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from .. import launcher, worker_mcp
from ..core import AgentError
from ..records.narrow import as_dict_or_none, as_list, as_list_or_none, dig, str_list
from ..repos import Repo

ProductSelection = Literal["default-absent", "default-null", "explicit-empty", "explicit-selected"]
MAX_PRODUCT_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ServerSource:
    """Logical sources only: no commands, URLs, paths to credentials, or environment values."""

    server: str
    selection_source: str
    definition_source: str


@dataclass(frozen=True)
class ResolvedMcp:
    """`servers` is sensitive launch data. Only `preview()` is suitable for diagnostics."""

    servers: dict[str, Any] = field(repr=False)
    sources: tuple[ServerSource, ...]
    product_selection: ProductSelection

    def config(self) -> dict[str, Any]:
        """Return a detached config so an SDK/sidecar consumer cannot mutate the snapshot."""
        return {"mcpServers": copy.deepcopy(self.servers)}

    def preview(self) -> dict[str, Any]:
        return {
            "strict_mcp_config": True,
            "product_selection": self.product_selection,
            "servers": [asdict(source) for source in self.sources],
        }


def _names(value: object, source: str) -> list[str]:
    if value is None:
        return []
    items = as_list_or_none(value)
    names = [name for name in str_list(items) if name.strip() and "\0" not in name]
    if items is None or len(names) != len(items):
        raise AgentError(f"{source} must contain non-empty server/profile names")
    if len(set(names)) != len(names):
        raise AgentError(f"{source} repeats a selection")
    return names


def _label(name: str) -> str:
    """Configuration identifiers are visible; avoid reflecting arbitrary URLs/control text."""
    return name if name.isascii() and all(c.isalnum() or c in "_.-" for c in name) else "[redacted-name]"


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _nonfinite(_value: str) -> Any:
    raise ValueError("non-finite JSON value")


class _SourceInspectionUnavailable(Exception):
    pass


def _windows_product_fd(path: Path) -> int:
    """Open a resolved disk file without following replacement reparse points."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class UnicodeString(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPWSTR),
        ]

    class ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(UnicodeString)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", ctypes.c_void_p),
            ("SecurityQualityOfService", ctypes.c_void_p),
        ]

    class IoStatus(ctypes.Structure):
        _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

    class AttributeTag(ctypes.Structure):
        _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]

    try:
        load_dll = getattr(ctypes, "WinDLL", None)
        to_fd = getattr(msvcrt, "open_osfhandle", None)
        binary = getattr(os, "O_BINARY", None)
        is_reserved = getattr(os.path, "isreserved", None)
        if load_dll is None or to_fd is None or binary is None or is_reserved is None:
            raise _SourceInspectionUnavailable
        kernel = load_dll("kernel32", use_last_error=True)
        native = load_dll("ntdll")
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
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        file_type = kernel.GetFileType
        file_type.argtypes = [wintypes.HANDLE]
        file_type.restype = wintypes.DWORD
        attributes = kernel.GetFileInformationByHandleEx
        attributes.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        attributes.restype = wintypes.BOOL
        nt_create = native.NtCreateFile
        nt_create.argtypes = [
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.DWORD,
            ctypes.POINTER(ObjectAttributes),
            ctypes.POINTER(IoStatus),
            ctypes.c_void_p,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            ctypes.c_void_p,
            wintypes.ULONG,
        ]
        nt_create.restype = wintypes.LONG
    except AttributeError, OSError:
        raise _SourceInspectionUnavailable from None

    def filesystem_path(value: Path) -> None:
        if (
            str(value).startswith(("\\\\?\\", "\\\\.\\"))
            or value.anchor.casefold().rstrip("\\").endswith("\\pipe")
            or any(is_reserved(part) for part in value.parts[1:])
        ):
            raise ValueError("unsupported source path")

    absolute = path.absolute()
    filesystem_path(absolute)
    target = absolute.resolve(strict=True)
    filesystem_path(target)
    before = target.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_PRODUCT_BYTES:
        raise ValueError("source is not a bounded regular file")
    # Anchor the native open in a filesystem root. OBJ_DONT_REPARSE refuses races
    # through any ancestor; FILE_OPEN_REPARSE_POINT exposes a replaced leaf link.
    root = create(target.anchor, 0x80, 7, None, 3, 0x02000000, None)
    if root == ctypes.c_void_p(-1).value:
        raise OSError("source root cannot be opened")
    handle = wintypes.HANDLE()
    try:
        if file_type(root) != 1:
            raise ValueError("source root is not a disk filesystem")
        name = str(target.relative_to(target.anchor))
        buffer = ctypes.create_unicode_buffer(name)
        length = len(name.encode("utf-16-le"))
        if length > 65532:
            raise ValueError("source path is too long")
        unicode_name = UnicodeString(length, length + 2, ctypes.cast(buffer, wintypes.LPWSTR))
        obj = ObjectAttributes(
            ctypes.sizeof(ObjectAttributes), root, ctypes.pointer(unicode_name), 0x1040, None, None
        )
        result = nt_create(
            ctypes.byref(handle),
            0x80100000,
            ctypes.byref(obj),
            ctypes.byref(IoStatus()),
            None,
            0,
            7,
            1,
            0x200060,
            None,
            0,
        )
        if (result & 0xFFFFFFFF) in (0xC0000002, 0xC000000D, 0xC00000BB):
            raise _SourceInspectionUnavailable
        if result < 0:
            raise OSError("source cannot be opened without reparsing")
        tag = AttributeTag()
        if not attributes(handle, 9, ctypes.byref(tag), ctypes.sizeof(tag)):
            raise OSError("source attributes cannot be inspected")
        if file_type(handle) != 1 or tag.FileAttributes & (0x10 | 0x400):
            raise ValueError("source is not a disk regular file")
        assert handle.value is not None
        fd: int = to_fd(handle.value, os.O_RDONLY | binary)
        handle = wintypes.HANDLE()
        try:
            opened = os.fstat(fd)
            if not os.path.samestat(before, opened):
                raise ValueError("source was replaced during inspection")
            return fd
        except BaseException:
            os.close(fd)
            raise
    finally:
        if handle.value:
            close(handle)
        close(root)


def _product_text(path: Path) -> str:
    """Bound bytes on the opened target, including growth; never block opening a FIFO.

    Follow symlinks intentionally, then verify the resulting descriptor is regular before
    reading it. This is a byte/type bound, not a deadline for a stalled filesystem. Platforms
    without either a nonblocking open or the Windows disk-file opener refuse inspection.
    """
    nonblock = getattr(os, "O_NONBLOCK", None)
    if os.name == "nt":
        fd = _windows_product_fd(path)
    elif nonblock is not None:
        fd = os.open(path, os.O_RDONLY | nonblock | getattr(os, "O_NOCTTY", 0))
    else:
        raise _SourceInspectionUnavailable
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PRODUCT_BYTES:
            raise ValueError("source is not a bounded regular file")
        content = bytearray()
        while len(content) <= MAX_PRODUCT_BYTES:
            chunk = os.read(fd, min(65536, MAX_PRODUCT_BYTES + 1 - len(content)))
            if not chunk:
                return content.decode("utf-8")
            content.extend(chunk)
        raise ValueError("source exceeds the byte limit")
    finally:
        os.close(fd)


def _product_definitions(repo: Repo, root: Path, d: dict[str, Any]) -> tuple[dict[str, Any], str]:
    worktree = Path(d["worktree"]) if d.get("worktree") else None
    # A missing future worktree uses the clone for preview. An existing but invalid path
    # must not silently select different definitions from the clone.
    directory, origin = repo.abs_path(root), "clone"
    if worktree is not None:
        try:
            worktree.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            raise AgentError("selected product MCP worktree cannot be inspected") from None
        else:
            directory, origin = worktree, "worktree"
    try:
        data = json.loads(
            _product_text(directory / ".mcp.json"),
            object_pairs_hook=_pairs,
            parse_constant=_nonfinite,
        )
    except _SourceInspectionUnavailable:
        raise AgentError(
            f"selected product MCP source inspection is unsupported on this platform ({origin})"
        ) from None
    except OSError, ValueError, UnicodeError, RecursionError:
        raise AgentError(f"selected product MCP definitions are unreadable or invalid ({origin})") from None
    servers = as_dict_or_none(dig(data, "mcpServers"))
    if servers is None:
        raise AgentError(f"selected product MCP source needs an mcpServers mapping ({origin})")
    return servers, origin


def _string_map(value: object, *, environment: bool = False) -> bool:
    mapping = as_dict_or_none(value)
    return mapping is not None and all(
        isinstance(k, str)  # pyright: ignore[reportUnnecessaryIsInstance] -- YAML can give non-str keys; JSON cannot
        and isinstance(v, str)
        and "\0" not in k
        and "\0" not in v
        and (not environment or (bool(k) and "=" not in k))
        for k, v in mapping.items()
    )


def _definition(value: object) -> bool:
    """Structural subset of installed SDK stdio/http/sse types; no endpoint or auth probe."""
    spec = as_dict_or_none(value)
    if spec is None:
        return False
    kind = spec.get("type", "stdio")
    command, url, args = spec.get("command"), spec.get("url"), as_list_or_none(spec.get("args"))
    if kind == "stdio":
        return (
            isinstance(command, str)
            and bool(command.strip())
            and "\0" not in command
            and (
                "args" not in spec
                or (args is not None and all(isinstance(a, str) and "\0" not in a for a in args))
            )
            and ("env" not in spec or _string_map(spec["env"], environment=True))
            and "url" not in spec
        )
    if kind not in ("http", "sse") or not isinstance(url, str):
        return False
    # Preserve native interpolation strings. Parsing/expanding URLs here would add a
    # second client-specific evaluator (and could read credentials); the SDK type is str.
    return (
        bool(url.strip())
        and "\0" not in url
        and ("headers" not in spec or _string_map(spec["headers"]))
        and not any(key in spec for key in ("command", "args", "env"))
    )


def resolve(root: Path, repo: Repo, d: dict[str, Any]) -> ResolvedMcp:
    """Compose reserved agent, explicitly named profiles, and explicitly selected product MCP.

    Unknown, duplicate, colliding or malformed selected sources raise sanitized AgentError.
    Unselected product configuration is not read. No user config or manager MCP config is read.
    Product omission/null/[] all exclude product servers, with distinct preview provenance.
    Tool permissions remain the responsibility of existing settings/callback callers.
    """
    selected = _names(repo.dispatch.get("repo_mcp"), "dispatch.repo_mcp")
    names = _names(d.get("mcp"), "dispatch.mcp")
    selection: ProductSelection = (
        "default-absent"
        if "repo_mcp" not in repo.dispatch
        else "default-null"
        if repo.dispatch["repo_mcp"] is None
        else "explicit-selected"
        if selected
        else "explicit-empty"
    )
    try:
        profiles = worker_mcp.require(root, names)
    except AgentError, OSError, ValueError, yaml.YAMLError, RecursionError:
        # Existing profile errors can include login paths/hints and raw YAML source.
        raise AgentError(
            "selected worker MCP profiles are unknown, invalid, colliding, or not ready"
        ) from None
    servers: dict[str, Any] = {
        "agent": {
            "command": launcher.command_and_args(root)[0],
            "args": [*launcher.command_and_args(root)[1], "mcp", "--profile", "worker"],
            "env": {
                "AGENT_ROOT": str(root),
                "AGENT_FEATURE": f"{int(d['feature']):03d}",
                "AGENT_DISPATCH_ID": d["id"],
                "AGENT_DISPATCH_TASKS": ",".join(str(t) for t in as_list(d.get("tasks"))),
            },
            "alwaysLoad": True,
        }
    }
    sources = [ServerSource("agent", "harness:worker", "harness:worker")]
    for profile in profiles:
        if "\0" in profile.server or not _definition(
            {"command": profile.command, "args": profile.args, "env": profile.env}
        ):
            raise AgentError("selected worker MCP profile contains invalid process values")
        servers[profile.server] = worker_mcp.server_config(root, d, profile)
        sources.append(
            ServerSource(
                _label(profile.server),
                f"dispatch.mcp:{_label(profile.name)}",
                f"agent.yaml#worker_mcp.{_label(profile.name)}",
            )
        )
    if selected:
        definitions, origin = _product_definitions(repo, root, d)
        for name in selected:
            label = _label(name)
            if name in servers:
                raise AgentError(
                    f"dispatch.repo_mcp selection {label!r} collides with a harness/profile server"
                )
            if name not in definitions:
                raise AgentError(f"dispatch.repo_mcp selection {label!r} is unknown in {origin} definitions")
            if not _definition(definitions[name]):
                raise AgentError(f"dispatch.repo_mcp selection {label!r} has an invalid {origin} definition")
            servers[name] = copy.deepcopy(definitions[name])
            sources.append(
                ServerSource(
                    label,
                    f"agent.yaml#repos.{_label(repo.short)}.dispatch.repo_mcp",
                    f"{origin}:.mcp.json#mcpServers.{label}",
                )
            )
    return ResolvedMcp(servers, tuple(sources), selection)
