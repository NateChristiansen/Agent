"""Detect cached MCP schema changes. Fresh operation processes run current source on every call; only
a changed tool list or parameters require reconnect. Compare the connected server's schema with
current definitions.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import time
from pathlib import Path

from ..core import agent_path, is_alive, iso, now
from ..records.narrow import as_dict_or_none, as_float, as_int

STAMP_DIR = agent_path(Path(), "mcp")


def _stamp_path(root: Path, pid: int) -> Path:
    return root / STAMP_DIR / f"server-{pid}.json"


def write_stamp(root: Path, profile: str = "manager", fresh: bool = False) -> Path | None:
    """Record that a server started now, under this pid. Never raises: a server that cannot
    write its stamp must still serve. `fresh` marks a server that runs every call in a new
    process, which is stale only when its tool schemas changed."""
    try:
        path = _stamp_path(root, os.getpid())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "ppid": os.getppid(),
                    "profile": profile,
                    "started_at": iso(now()),
                    "code_mtime": newest_code_mtime(root),
                    "fresh": fresh,
                    "schema_hash": schema_hash(root),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return path
    except OSError:
        return None


def newest_code_mtime(root: Path) -> float:
    """The newest mtime of any `.py` under `src/agent_cli/`. 0.0 when the tree is absent (a
    packaged install, a clone without sources)."""
    src = root / "src" / "agent_cli"
    if not src.is_dir():
        return 0.0
    newest = 0.0
    for path in src.rglob("*.py"):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


# The files that define what the client sees at connect time: tool names, parameters and
# descriptions. A fresh server runs current code for everything else.
def schema_files(src: Path) -> list[Path]:
    """The tool-definition sources: `mcp/tools.py` and every `mcp/specs_*.py`."""
    return [src / "mcp" / "tools.py", *sorted((src / "mcp").glob("specs_*.py"))]


def schema_hash(root: Path) -> str:
    """Hash the client-visible tool contract: syntax with function bodies reduced to docstrings.
    Include signatures, annotations, decorators and ToolSpec descriptions; exclude comments,
    formatting and implementation-only changes so those do not require reconnect. Return empty when
    the source tree is absent.
    """
    src = root / "src" / "agent_cli"
    digest = hashlib.sha256()
    found = False
    for path in schema_files(src):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except OSError, SyntaxError, ValueError:
            continue
        found = True
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = node.body[0] if node.body and ast.get_docstring(node) is not None else None
                node.body = [doc] if doc is not None else [ast.Pass()]
        digest.update(path.name.encode() + b"\0" + ast.dump(tree).encode())
    return digest.hexdigest()[:16] if found else ""


def stale_servers(root: Path) -> list[dict[str, object]]:
    """Live servers that would answer wrongly. An in-process server is stale when its imported code predates the newest source
    file. A fresh server runs current code on every call, so it is stale only when the tool
    schemas changed since it started.

    A stamp whose pid is gone is swept: the server it described is not answering anything.
    """
    base = root / STAMP_DIR
    if not base.is_dir():
        return []
    newest = newest_code_mtime(root)
    current_schema = schema_hash(root)
    stale: list[dict[str, object]] = []
    for path in sorted(base.glob("server-*.json")):
        try:
            record = as_dict_or_none(json.loads(path.read_text(encoding="utf-8")))
        except OSError, json.JSONDecodeError:
            continue
        if record is None:
            continue
        pid = as_int(record.get("pid"))
        if not is_alive(pid):
            path.unlink(missing_ok=True)
            continue
        if record.get("fresh"):
            if str(record.get("schema_hash") or "") != current_schema:
                stale.append(record)
            continue
        imported = as_float(record.get("code_mtime"))
        if newest > imported:
            record["newest_code_mtime"] = newest
            stale.append(record)
    return stale


def stale_line(root: Path) -> str:
    """One line for a hook or `doctor`, empty when every live server is current."""
    stale = stale_servers(root)
    if not stale:
        return ""
    pids = ", ".join(str(s.get("pid")) for s in stale)
    if all(s.get("fresh") for s in stale):
        return (
            f"the MCP server (pid {pids}) runs current code on every call, but a tool's name, "
            "parameters or description changed under src/agent_cli/mcp/ since it started, so "
            "the tool list you see is out of date. Reconnect (/mcp in Claude Code, a "
            "restart in Codex) before using a new or changed parameter."
        )
    return (
        f"the MCP server (pid {pids}) imported agent_cli before the newest edit under "
        "src/agent_cli/, so its tools run the OLD code. Ask the owner to reconnect it "
        "(/mcp in Claude Code, a restart in Codex) before relying on the changed behavior."
    )


#: Warning markers older than this are swept when a new one is written.
WARNED_RETENTION_S = 3 * 86400


def warned_before(root: Path, session: str, line: str) -> bool:
    """Record the schema warning already shown to this session so repeated hooks do not repeat
    unchanged restart guidance.
    """
    if not session:
        return False
    folder = root / STAMP_DIR
    mark = folder / f"warned-{hashlib.sha256(session.encode()).hexdigest()[:32]}.txt"
    try:
        identity = f"{line}\n{newest_code_mtime(root)}"
        if mark.exists() and mark.read_text(encoding="utf-8") == identity:
            return True
        folder.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - WARNED_RETENTION_S
        for old in folder.glob("warned-*.txt"):
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
        mark.write_text(identity, encoding="utf-8")
    except OSError:
        return False
    return False
