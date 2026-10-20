"""Best-effort worker CLI provenance, cached across supervisors for each installation.

Use the launch options rather than resolving PATH again. Codex's command can include a Node
wrapper on Windows; keep that prefix and replace the app-server arguments with --version.
Version diagnostics never decide whether a worker may launch.
"""

from __future__ import annotations

import json
import subprocess
from functools import cache
from pathlib import Path
from typing import Any

from ..core import agent_path, atomic_write
from ..records.narrow import as_dict, str_list


def _installation(command: tuple[str, ...]) -> str | None:
    """Fingerprint the resolved executable and any Node wrapper entry point."""
    try:
        files: list[tuple[str, int, int]] = []
        for part in command:
            path = Path(part).resolve(strict=True)
            info = path.stat()
            files.append((str(path), info.st_mtime_ns, info.st_size))
        return json.dumps(files)
    except OSError, RuntimeError:
        return None


def _read_cache(path: Path) -> dict[str, str]:
    try:
        data = as_dict(json.loads(path.read_text(encoding="utf-8")))
    except OSError, ValueError, UnicodeError:
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str) and v.strip()}


def probe(command: tuple[str, ...], *, root: Path | None = None) -> str | None:
    """Reuse a version until an executable's real path, mtime or size changes.

    Without a harness root, keep only the process cache. Missing file metadata and cache I/O
    failures fall back to probing; transient probe failures stay cached only in this process.
    """
    path = agent_path(root, "dispatches", "cache", "client-versions.json") if root is not None else None
    return _probe(command, _installation(command), path)


@cache
def _probe(command: tuple[str, ...], key: str | None, path: Path | None) -> str | None:
    """The CLI's version line, or unknown on a failed, empty or timed-out probe."""
    if not command:
        return None
    if key is not None and path is not None:
        versions = _read_cache(path)
        if key in versions:
            return versions[key]
    try:
        result = subprocess.run(
            [*command, "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if result.returncode != 0:
        return None
    version = next((line.strip() for line in result.stdout.splitlines() if line.strip()), None)
    if version is not None and key is not None and path is not None:
        try:
            atomic_write(path, json.dumps({**_read_cache(path), key: version}) + "\n")
        except OSError, UnicodeError:
            pass
    return version


def record(provider: str, options: dict[str, Any], *, root: Path | None = None) -> dict[str, Any]:
    """One attempt's client, resolved executable prefix and version; no migration required."""
    if provider == "codex":
        argv = str_list(options.get("command"))
        command = tuple(argv[: argv.index("app-server")]) if "app-server" in argv else ()
    else:
        path = options.get("cli_path")
        command = (path,) if isinstance(path, str) and path else ()
    return {"client": provider, "command": list(command), "version": probe(command, root=root)}
