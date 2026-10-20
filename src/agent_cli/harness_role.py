"""This install's role toward the harness repo: `source` (the default), where harness changes are
made, or `consumer`, a plain clone that runs upstream code and contributes nothing back (see
ADR-0043).

    harness: # agent.local.yaml only: per machine, never synced
      role: consumer

A consumer's agents may not commit, merge, rebase or land in the harness checkout; the one main
move allowed is a fast-forward to its upstream. Every push from the harness checkout is refused,
and a harness-scope lesson is written to the private queue marked as a harness candidate, for
the owner to carry to the source install. Product repos are unaffected. A malformed block reads
as `consumer`: a machine that tried to declare itself a consumer stays one.
"""

from __future__ import annotations

from pathlib import Path

from .records.narrow import as_dict_or_none

SOURCE, CONSUMER = "source", "consumer"
ROLES = (SOURCE, CONSUMER)


def role(root: Path) -> str:
    """The role the main checkout's `agent.local.yaml` declares; linked worktrees share it."""
    from .config_file import machine_section

    raw = machine_section(root, "harness")
    if raw is None:
        return SOURCE
    block = as_dict_or_none(raw)
    if block is None or block.get("role", SOURCE) not in ROLES:
        return CONSUMER
    return str(block.get("role", SOURCE))


def consumer(root: Path) -> bool:
    return role(root) == CONSUMER


NEW_LOCAL = (
    "# This clone runs upstream harness code and makes no local harness code changes (ADR-0043).\n"
    "# A maintainer checkout sets `role: source` here by hand.\n"
    f"harness:\n  role: {CONSUMER}\n"
)


def create_local(root: Path) -> tuple[str, str]:
    """`(state, detail)` for setup: create `agent.local.yaml` declaring `consumer`, only when
    there is none. An existing file is never edited, so an install that predates the role keeps
    reading as `source`. An exclusive create: a file that appears meanwhile is kept."""
    import os

    from . import config_file

    path = config_file.machine_root(root) / config_file.LOCAL
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
    except FileExistsError:
        return "already", f"{config_file.LOCAL} exists, so its role ({role(root)}) is kept"
    except OSError as exc:
        return "failed", f"could not create {path}: {exc}"
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(NEW_LOCAL.encode("utf-8"))
    except OSError as exc:
        path.unlink(missing_ok=True)
        return "failed", f"could not create {path}: {exc}"
    return "done", f"created {path} with `harness: {{role: {CONSUMER}}}`"


def problems(raw: object) -> list[str]:
    """Config errors in a `harness:` block, for doctor."""
    if raw is None:
        return []
    block = as_dict_or_none(raw)
    if block is None or set(block) - {"role"} or block.get("role", SOURCE) not in ROLES:
        return ["agent.local.yaml `harness:` must be `{role: source}` or `{role: consumer}`"]
    return []
