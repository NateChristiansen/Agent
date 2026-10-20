"""Which processes hold a harness checkout's venv on Windows: read-only.

`uv sync`, and a `uv run` that decides to sync, fail with os error 32 when it cannot replace
`.venv\\Scripts\\agent.exe` because another process has it open. Before
choosing a fix, the plan wants the holders counted during a normal session: the manager's MCP
server, a worker's MCP server, the dispatch supervisor, `watch --follow`, hook calls. A compiled
extension (`.pyd`) can be locked the same way, so mapped files under
the venv are listed too.

For every process it can read, this prints one JSON row per held venv file:
- `how`: `exe` (the process image is that file), `open` (an open handle, psutil `open_files`) or
  `mapped` (a mapped image such as a `.pyd` or `.dll`, psutil `memory_maps`);
- the process's pid, parent pid and parent name, name, exe and command line;
- `role`: a guess from the command line (`mcp-manager`, `mcp-worker`, `supervisor`, `watch`,
  `hook`, `other`). The command line is printed so the guess can be checked.

It writes nothing and stops nothing. Processes it may not read are counted, not guessed.

Run it with a Python outside the checkout's venv, or that interpreter is a holder itself:

    uv run --no-project --with psutil python scripts\\win_venv_holders.py [<checkout>]

`<checkout>` defaults to the current directory. Exit 0 always; the JSON is the result.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import psutil

# A named tuple of classes, not `except A, B:`: the script may run on a Python older than 3.14.
_UNREADABLE = (psutil.AccessDenied, psutil.ZombieProcess)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def role(cmdline: list[str]) -> str:
    """A guess at what this harness process is, from its arguments (`launcher.argv_prefix`)."""
    words = [w.lower() for w in cmdline[1:]]
    if "mcp" in words:
        return "mcp-worker" if "worker" in words else "mcp-manager"
    if words[:2] == ["dispatch", "run"] or ("dispatch" in words and "run" in words):
        return "supervisor"
    if "watch" in words and "--follow" in words:
        return "watch"
    if "hook" in words:
        return "hook"
    return "other"


def holders(venv: Path) -> dict[str, Any]:
    prefix = _norm(str(venv)) + os.sep
    rows: list[dict[str, Any]] = []
    unreadable = 0
    for proc in psutil.process_iter(["pid", "ppid", "name"]):
        if proc.pid == os.getpid():
            continue
        try:
            exe = proc.exe() or ""
            held: list[tuple[str, str]] = []
            if exe and _norm(exe).startswith(prefix):
                held.append(("exe", exe))
            held += [("open", f.path) for f in proc.open_files() if _norm(f.path).startswith(prefix)]
            if hasattr(proc, "memory_maps"):  # not on macOS
                held += [
                    ("mapped", m.path)
                    for m in proc.memory_maps(grouped=True)
                    if _norm(m.path).startswith(prefix) and _norm(m.path) != _norm(exe)
                ]
            if not held:
                continue
            cmdline = proc.cmdline()
            try:
                parent = proc.parent()
                parent_name = parent.name() if parent else ""
            except psutil.Error:
                parent_name = ""
        except _UNREADABLE:
            unreadable += 1
            continue
        except psutil.NoSuchProcess:
            continue
        for how, path in held:
            rows.append(
                {
                    "how": how,
                    "path": path,
                    "pid": proc.pid,
                    "ppid": proc.info["ppid"],
                    "parent": parent_name,
                    "name": proc.info["name"],
                    "exe": exe,
                    "cmdline": cmdline,
                    "role": role(cmdline),
                }
            )
    entry = venv / "Scripts" / "agent.exe"
    return {
        "venv": str(venv),
        "entry": str(entry),
        "entry_holders": sorted({r["pid"] for r in rows if _norm(r["path"]) == _norm(str(entry))}),
        "by_role": {
            name: sorted({r["pid"] for r in rows if r["role"] == name})
            for name in sorted({r["role"] for r in rows})
        },
        "unreadable_processes": unreadable,
        "rows": rows,
    }


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    print(json.dumps(holders(root / ".venv"), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
