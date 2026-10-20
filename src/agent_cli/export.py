"""`agent export <dir> -m <message> --remote <url>`: the one route from this checkout to the
public repo (see ADR-0026). `--remote` is the new repository; it must not be this checkout's
origin.

This checkout's history is private: unsquashed commits, messages that name work, the path of
every file that was ever tracked. The public repo gets one squashed commit of `HEAD`'s tree.
The pre-push guard (`gitgate.pre_push`) refuses a direct push, so this is the path that
remains, and it does everything short of the push:

1. Archive `HEAD` into an empty directory and commit it as a fresh single-commit repo on
   `main`, with submodule pointers (gitlinks, which `git archive` drops) restored. The commit
   is authored with `export.identity` from agent.yaml, never this checkout's git config.
2. Confirm no path of the private set (`notes.PRIVATE_SET`) is in the tree.
3. Print the push command and stop. Pushing publishes; the owner runs it after reading.

A private path exits nonzero and prints no push command. The directory is left as built, so
it can be inspected.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from . import config_file, notes
from .core import AgentError
from .lesson_upstream import parse_origin


def _git(cwd: Path, *args: str, stdin: bytes | None = None, env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, input=stdin, capture_output=True, check=False, env=env)
    if proc.returncode:
        raise AgentError(f"git {args[0]} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout.decode()


def _gitlinks(root: Path) -> list[tuple[str, str]]:
    out = _git(root, "ls-tree", "-r", "-z", "HEAD")
    links: list[tuple[str, str]] = []
    for entry in out.split("\0"):
        meta, _, path = entry.partition("\t")
        if meta.startswith("160000 "):
            links.append((meta.split()[2], path))
    return links


EXPORT_MARKER = ".harness-export"
EXPORT_MARKER_TEXT = (
    "This tree was published with `agent export`: a squash of the harness with no private history.\n"
    "Setup reads this file to know the clone is a public export. Do not add it to the\n"
    "source checkout.\n"
)


def _repo_identity(url: str, role: str) -> str:
    try:
        identity = parse_origin(url).lower().rstrip("/")
    except AgentError:
        raise AgentError(f"the {role} {url!r} is not a github.com repository; cannot compare them") from None
    return identity.removesuffix(".git").rstrip("/")


def build(root: Path, dest: Path, *, message: str, remote: str) -> dict[str, Any]:
    dest = dest.expanduser().resolve()
    if dest.exists() and any(dest.iterdir()):
        raise AgentError(f"{dest} is not empty; export needs a fresh directory")
    if dest.is_relative_to(root.resolve()):
        raise AgentError("export into a directory outside this checkout")
    source_origin = _git(root, "remote", "get-url", "origin").strip()
    if _repo_identity(remote, "remote") == _repo_identity(source_origin, "source origin"):
        raise AgentError(
            f"remote {remote!r} is the same repository as this checkout's origin {source_origin!r}; "
            "export into a new repository"
        )
    name, email = config_file.export_identity(root)
    head = _git(root, "rev-parse", "HEAD").strip()

    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "-C", str(root), "archive", "--format=tar", "HEAD"], capture_output=True, check=False
    )
    if archive.returncode:
        raise AgentError(f"git archive failed: {archive.stderr.decode(errors='replace').strip()}")
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, check=True)
    # The public-export marker: tracked only in the export, never in the source tree, so a clone
    # of the public repo can prove what it is (`gitgate.settle_export_only`).
    (dest / EXPORT_MARKER).write_text(EXPORT_MARKER_TEXT, encoding="utf-8")
    _git(dest, "init", "-q", "-b", "main")
    _git(dest, "add", "-A")
    links = _gitlinks(root)
    for sha, path in links:
        _git(dest, "update-index", "--add", "--cacheinfo", f"160000,{sha},{path}")
    # The public identity, never the checkout's `user.email`, which may be a private address. The
    # environment outranks git config, so an inherited GIT_AUTHOR_* cannot leak through either.
    ident = {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }
    _git(dest, "commit", "-q", "-m", message, env={**os.environ, **ident})
    _git(dest, "remote", "add", "origin", remote)

    tracked = set(_git(dest, "ls-files", "-z").split("\0"))
    # The private set, plus the config's old name and this machine's overrides: neither is in
    # PRIVATE_SET, and gitignore alone does not stop a file that was tracked by mistake. The
    # `repos.yaml` reader is gone, but a stray copy is still private config.
    never = [e.path for e in notes.PRIVATE_SET] + ["repos.yaml", config_file.LOCAL]
    private = sorted(p for p in never if any(t == p or t.startswith(p + "/") for t in tracked))
    ok = not private
    return {
        "dest": str(dest),
        "source_head": head,
        "export_head": _git(dest, "rev-parse", "HEAD").strip(),
        "author": _git(dest, "log", "-1", "--format=%an <%ae>").strip(),
        "origin": remote,
        "source_origin": source_origin,
        "gitlinks": [p for _, p in links],
        "files": len(tracked - {""}),
        "private_paths": private,
        "ok": ok,
        "push": f"git -C {dest} push --force origin HEAD:main" if ok else None,
    }


def format_result(data: dict[str, Any]) -> str:
    lines = [
        f"exported {data['source_head'][:12]} to {data['dest']} as {data['export_head'][:12]} "
        f"({data['files']} files, gitlinks: {', '.join(data['gitlinks']) or 'none'})",
    ]
    lines.append(f"source origin {data['source_origin']}; export origin {data['origin']}")
    if data["private_paths"]:
        lines.append("private-set paths in the tree: " + ", ".join(data["private_paths"]))
    if data["ok"]:
        lines += [
            f"clean. The commit is authored as {data['author']}; check that identity is the one "
            "to publish, review the tree, then push:",
            f"  {data['push']}",
        ]
    else:
        lines.append("NOT clean: fix the source and commit, then export again into a fresh directory")
    return "\n".join(lines)
