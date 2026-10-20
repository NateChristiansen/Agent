"""Wake Codex from sshd's minimal environment using an absolute launch argv recorded by peer-net init.
Node scripts name both runtime and script; never use shell shims.

Revalidate the launch at use and set only the child's PATH to recorded directories plus system
defaults. Missing or changed launch files fail closed and leave messages queued. Init succeeds
without Codex by recording no launch.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from .. import codex_executable
from ..records.narrow import as_dict_or_none, as_list_or_none, str_list

KEY = "wake_codex"
POSIX_DEFAULT_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def _real_file(value: str) -> str:
    """The resolved absolute path of a regular file, or FileNotFoundError."""
    path = Path(os.path.realpath(value))
    if not path.is_absolute() or not path.is_file():
        raise FileNotFoundError(value)
    return str(path)


def _node_script(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            first = handle.readline(256)
    except OSError:
        return False
    return first.startswith(b"#!") and b"node" in first


def resolve() -> dict[str, Any]:
    """The launch config from the owner's current environment, or FileNotFoundError."""
    argv = codex_executable.command()  # native exe, or Windows npm Node + script; never .cmd
    if len(argv) == 1 and os.name != "nt" and _node_script(argv[0]):
        node = shutil.which("node")
        if not node:
            raise FileNotFoundError("Codex is a Node script and node is not on PATH")
        argv = [node, argv[0]]  # pin the runtime: never rely on `env node` under sshd
    launch = [_real_file(part) for part in argv]
    config = {"argv": launch, "path_dirs": sorted({str(Path(part).parent) for part in launch})}
    if not valid(config):
        raise FileNotFoundError("Codex resolved to an unsupported launch form")
    return config


SHIMS = {".cmd", ".bat", ".ps1"}
NODES = {"node", "node.exe"}
CONTROL = {chr(c) for c in range(32)} | {"\x7f"}


def valid(value: object) -> bool:
    """The only shapes the receiver will run: exactly `argv` and `path_dirs`; argv is one native
    executable, or a Node runtime plus its script; every part an absolute, bounded string with
    no control characters, not a shell shim, and a regular file at its own realpath; argv[0]
    executable; path_dirs exactly the parents of argv. Anything else is no launch."""
    launch = as_dict_or_none(value)
    if launch is None or set(launch) != {"argv", "path_dirs"}:
        return False
    raw_argv, raw_dirs = as_list_or_none(launch["argv"]), as_list_or_none(launch["path_dirs"])
    if raw_argv is None or len(raw_argv) not in (1, 2) or raw_dirs is None:
        return False
    argv, dirs = str_list(raw_argv), str_list(raw_dirs)
    if len(argv) != len(raw_argv) or len(dirs) != len(raw_dirs):
        return False
    for part in [*argv, *dirs]:
        if not part or len(part) > 4096 or CONTROL & set(part):
            return False
        if not Path(part).is_absolute():
            return False
    if any(Path(part).suffix.lower() in SHIMS for part in argv):
        return False
    if len(argv) == 2 and (
        Path(argv[0]).name.lower() not in NODES or not (argv[1].endswith(".js") or _node_script(argv[1]))
    ):
        return False
    try:
        if [_real_file(part) for part in argv] != argv:
            return False
    except OSError, ValueError:
        return False
    if os.name != "nt" and not os.access(argv[0], os.X_OK):
        return False
    return dirs == sorted({str(Path(part).parent) for part in argv})


def record(conn: sqlite3.Connection, launch: dict[str, Any] | None) -> None:
    """Store `launch`, or remove any stored one when Codex is absent."""
    from . import store

    with store.transaction(conn):
        if launch is None:
            conn.execute("DELETE FROM enrollment WHERE k=?", (KEY,))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO enrollment (k, v) VALUES (?, ?)",
                (KEY, json.dumps(launch, sort_keys=True)),
            )


def load(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The recorded config, re-validated now by `valid`. None when absent, malformed or
    drifted."""
    row = conn.execute("SELECT v FROM enrollment WHERE k=?", (KEY,)).fetchone()
    if row is None or not isinstance(row[0], str) or len(row[0]) > 16384:
        return None
    try:
        launch = json.loads(row[0])
    except ValueError, RecursionError:
        return None
    return {"argv": launch["argv"], "path_dirs": launch["path_dirs"]} if valid(launch) else None


def child_env(launch: dict[str, Any]) -> dict[str, str]:
    """The wake child's environment: this process's, with PATH replaced for the child only."""
    base = dict(os.environ)
    if os.name == "nt":
        path = os.pathsep.join([*launch["path_dirs"], base.get("PATH", "")])
    else:
        path = os.pathsep.join([*launch["path_dirs"], POSIX_DEFAULT_PATH])
    return codex_executable.child_env({**base, "PATH": path})
