"""Install MCP servers from a pinned, locked manifest and probe their binaries before use. npx caches
by package spec rather than Node major, so native modules built under another Node can fail despite
a correct version pin.

Store the manifest beside private config in the notes repo and link .agent/mcp-servers to it for
portable launch paths. With plain private files, use a real local directory. Install once under the
configured Node rather than resolve packages at each server start.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import links
from .core import AgentError, ExternalFailure, agent_path
from .records.narrow import as_dict, as_dict_or_none, dig

LINK = agent_path(Path(), "mcp-servers")
"""Where `.mcp.json` reaches the installed servers from, relative to the vault root."""

MANIFEST_DIR = "mcp"
"""The manifest's directory inside the notes repo."""


def manifest_dir(root: Path) -> Path:
    """Where `package.json` and its lockfile live: the notes repo's `mcp/`, or `.agent/mcp-servers`
    itself when the private set is plain files and there is no separate repo."""
    from . import notes

    notes_root = notes.read_notes_root(root)
    if notes_root is None or notes_root.resolve() == root.resolve():
        return root / LINK
    return notes_root / MANIFEST_DIR


def link_path(root: Path) -> Path:
    return root / LINK


def ensure_link(root: Path) -> Path:
    """Point `.agent/mcp-servers` at the manifest directory. Returns the manifest directory.

    Idempotent, and it repairs a link that points somewhere else — the notes repo can move, and a
    stale link is a server that cannot start."""
    target = manifest_dir(root)
    target.mkdir(parents=True, exist_ok=True)
    link = link_path(root)
    if link == target:
        return target  # plain-files mode: the directory is the manifest directory
    if link.exists() and link.resolve() == target.resolve():
        return target
    if links.is_link(link) or link.exists():
        if links.is_link(link) and link.is_junction():
            link.rmdir()
        elif links.is_link(link) or link.is_file():
            link.unlink()
        elif any(link.iterdir()):
            raise AgentError(f"{link} is a non-empty directory and is in the way; move it aside and re-run")
        else:
            link.rmdir()
    links.make(link, target, is_dir=True)
    return target


def read_manifest(root: Path) -> dict[str, Any]:
    path = manifest_dir(root) / "package.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except json.JSONDecodeError as exc:
        raise AgentError(f"{path}: invalid JSON: {exc}") from exc


def declared(root: Path) -> dict[str, str]:
    """`{package: version spec}` from the manifest's dependencies, or `{}` when there is none."""
    return {str(k): str(v) for k, v in as_dict(read_manifest(root).get("dependencies")).items()}


def binaries(root: Path) -> dict[str, Path]:
    """The executables the DECLARED packages provide, by name.

    Not everything in `node_modules/.bin`: an install drags in transitive binaries
    (`prebuild-install`, `node-which`, `is-docker`) that are not servers, do not answer
    `--help`, and would make the probe below cry wolf on every run. Each declared package's own
    `bin` field is the authority."""
    bindir = link_path(root) / "node_modules" / ".bin"
    if not bindir.is_dir():
        return {}
    wanted: set[str] = set()
    for pkg in declared(root):
        meta = link_path(root) / "node_modules" / Path(pkg) / "package.json"
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            continue
        bins = data.get("bin")
        if isinstance(bins, str):
            wanted.add(Path(pkg).name)
        elif (named := as_dict_or_none(bins)) is not None:
            wanted.update(str(k) for k in named)
    return {p.name: p for p in sorted(bindir.iterdir()) if p.name in wanted}


def node_bin(root: Path) -> str:
    """Read optional agentMcp.nodeBin for the install PATH. Native modules build against that Node
    version, so selecting the build runtime explicitly matters on machines with multiple majors.
    """
    return str(dig(read_manifest(root), "agentMcp", "nodeBin") or "")


def install(root: Path, *, update: bool = False) -> dict[str, Any]:
    """`npm ci` when there is a lockfile, `npm install` when there is not or `update` is set.

    Two modes on purpose. `npm ci` installs exactly the locked tree and refuses when the manifest
    and the lock disagree, which is what a fresh clone must get: the same install everywhere.
    But that refusal also means `ci` can never ADD a server, so the owner who just edited
    `package.json` passes `update`, which runs `npm install` and rewrites the lock. Falling back
    from one to the other silently would throw away the guarantee `ci` exists for.

    Run in the manifest directory so the lockfile lands where it is tracked. npm is required; a
    vault with no manifest is fine and installs nothing."""
    target = ensure_link(root)
    manifest = target / "package.json"
    if not manifest.exists():
        return {"installed": False, "dir": str(target), "packages": {}, "reason": "no manifest"}
    env = dict(os.environ)
    bin_dir = node_bin(root)
    if bin_dir:
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    npm = shutil.which("npm", path=env.get("PATH"))
    if not npm:
        raise ExternalFailure("npm is not installed; it is needed to install the MCP servers")
    lock = target / "package-lock.json"
    verb = "ci" if lock.exists() and not update else "install"
    cmd = [npm, verb, "--prefix", str(target)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=str(target), env=env)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        hint = ""
        if verb == "ci" and "in sync" in detail:
            hint = " — the manifest changed since the lock was written; re-run with --update"
        raise ExternalFailure(f"{' '.join(cmd)}: {detail[:400]}{hint}")
    return {
        "installed": True,
        "verb": verb,
        "dir": str(target),
        "node_bin": bin_dir,
        "packages": declared(root),
        "binaries": sorted(binaries(root)),
    }


def probe(path: Path, timeout: float = 20.0) -> tuple[bool, str]:
    """Probe server startup with --help: native-module import failures occur before MCP
    initialization. Bound the process and give it no stdin so a server ignoring help cannot hang
    while serving.
    """
    from . import procs

    try:
        proc = procs.run_bounded(
            [str(path), "--help"],
            timeout=timeout,
            env={**os.environ, "NO_COLOR": "1"},
        )
    except subprocess.TimeoutExpired:
        return False, f"did not exit within {timeout:g}s"
    except OSError as exc:
        return False, str(exc)
    if proc.returncode == 0:
        return True, "starts"
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    first = next((ln for ln in tail if ln.strip()), "")
    return False, f"exit {proc.returncode}: {first[:200]}"
