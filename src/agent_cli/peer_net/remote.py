"""What `pair` runs on the other device, and the Windows file that makes it safe to name.

The bootstrap session runs exactly one command on B, under the owner's own login:

- POSIX B: the repo's `bin/agent peer-net accept`, its path absolute and quoted with
  `shlex.quote`, in sh, bash or zsh.
- Windows B: `"<repo>\\.agent\\peer-net\\accept.cmd"`. B's `init` writes that file with fixed
  content: the real interpreter, `-I`, and the tracked `bin\\peer-net-entry`, which dispatches
  only `peer-net accept`. cmd.exe quoting is never built from A's input.

The repo path is checked against one narrow character set and refused otherwise, never escaped.
`bin\\peer-net-entry` re-derives the file's content at startup and compares it with the file
and the stored hash, so drift (an interpreter upgrade, a moved repo, a half-finished init)
stops it. A file already tampered with has run before any check; anyone who can write the
user-private `.agent\\peer-net` folder already controls the account, the store's own boundary.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import sqlite3
from pathlib import Path, PurePosixPath, PureWindowsPath

from ..core import agent_parts, agent_path
from . import authorized, launcher, mutate, store
from .errors import PeerNetError

ACCEPT_CMD_REL = agent_path(Path(), "peer-net", "accept.cmd")
ENTRY_REL = Path("bin") / "peer-net-entry"
HASH_KEY = "accept_cmd_sha"
WINDOWS_ROOT = re.compile(r"[A-Za-z]:\\")


def check_repo(value: str) -> tuple[str, str]:
    """(`posix` or `windows`, the repo path) for B's absolute harness path, or refused.
    Only `[A-Za-z0-9 _.:\\/()-]`, no `..` component, and an absolute form of one OS."""
    if not launcher.SAFE_PATH_RE.fullmatch(value):
        raise PeerNetError("unsafe-path", "--repo")
    if WINDOWS_ROOT.match(value):
        if "/" in value or ".." in PureWindowsPath(value).parts:
            raise PeerNetError("unsafe-path", "--repo")
        return "windows", value.rstrip("\\") or value
    if value.startswith("/") and "\\" not in value and ":" not in value:
        if ".." in PurePosixPath(value).parts:
            raise PeerNetError("unsafe-path", "--repo")
        return "posix", value.rstrip("/") or "/"
    raise PeerNetError("unsafe-path", "--repo must be absolute")


def command(repo: str) -> str:
    """The remote command string for B's repo."""
    kind, path = check_repo(repo)
    if kind == "posix":
        return shlex.quote(f"{path}/bin/agent") + " peer-net accept"
    return f'"{path}\\{PureWindowsPath(*agent_parts("peer-net", "accept.cmd"))}"'


def accept_cmd_bytes(interpreter: str, root: str) -> bytes:
    """The whole file, derived only from validated inputs. `exit /b` hands accept's status to
    ssh."""
    python = launcher.check_path(interpreter)
    entry = launcher.check_path(f"{root.rstrip(chr(92))}\\{ENTRY_REL.parts[0]}\\{ENTRY_REL.parts[1]}")
    return f'@"{python}" -I "{entry}" peer-net accept\r\n@exit /b %ERRORLEVEL%\r\n'.encode("ascii")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_accept_cmd(root: Path, conn: sqlite3.Connection, interpreter: str) -> bool:
    """B's `init` on Windows: make `accept.cmd` and its stored hash match what the validated
    inputs give. Whatever is on disk is never trusted or read back as content; a file or hash
    that differs is rewritten. True when anything was written."""
    expected = accept_cmd_bytes(interpreter, str(root.resolve()))
    path = root / ACCEPT_CMD_REL
    authorized.refuse_links(path)  # the file and its directory
    store.check_private(root)
    current = path.read_bytes() if path.is_file() else None
    row = conn.execute("SELECT v FROM enrollment WHERE k=?", (HASH_KEY,)).fetchone()
    if current == expected and row is not None and row[0] == sha(expected):
        return False
    if current != expected:
        tmp = path.with_name(f"accept.cmd.tmp-{os.getpid()}")
        if tmp.exists():
            authorized.refuse_links(tmp)
            tmp.unlink()
        mutate.write_private(tmp, expected)
        if tmp.read_bytes() != expected:
            raise PeerNetError("write-mismatch", "accept.cmd")
        os.replace(tmp, path)
    with store.transaction(conn):  # a crash before this is repaired by the next init
        conn.execute("INSERT OR REPLACE INTO enrollment (k, v) VALUES (?, ?)", (HASH_KEY, sha(expected)))
    return True


def check_accept_cmd(root: Path, interpreter: str) -> None:
    """`bin\\peer-net-entry`'s startup check on Windows: the file and the stored hash must both
    be exactly what this interpreter and repo give. Read-only; never creates the store."""
    expected = accept_cmd_bytes(interpreter, str(root.resolve()))
    path = root / ACCEPT_CMD_REL
    stale = PeerNetError("accept-drift", "re-run peer-net init on this machine")
    try:
        authorized.refuse_links(path)
        if not path.is_file() or path.read_bytes() != expected or not store.exists(root):
            raise stale
    except OSError:
        raise stale from None
    conn = store.connect(root)
    try:
        row = conn.execute("SELECT v FROM enrollment WHERE k=?", (HASH_KEY,)).fetchone()
    finally:
        conn.close()
    if row is None or row[0] != sha(expected):
        raise stale
