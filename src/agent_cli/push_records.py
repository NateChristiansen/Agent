"""The pre-push record gate (see ADR-0041): every tip a push publishes needs a full pass record.

Git passes each ref update on the hook's stdin, parsed once and strictly (`push_input`). Each
non-deletion update's local tip, an annotated tag peeled to its commit, must have a record that
applies to its tree now; a commit pushed under two names is checked once. Intermediate commits are
not certified, only each tip. An object git cannot read, or one that is not a commit, refuses the
push. Deleting a ref needs no record, except main, which is never deleted from here. The gate
never runs the suite: a refusal names `agent verify record <sha>`.

A person's `git push --no-verify` skips every hook, this one included; that is outside the
guarantee. The trust boundary is the record store's (see ADR-0041). A clone with no agent.yaml, or
whose registry reads and does not list the harness itself (a public clone on the example config),
is not gated; a registry file that cannot be read (malformed, or a link to nothing) refuses the
push.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from .core import AgentError

if TYPE_CHECKING:
    from .push_input import Update

MAIN = "refs/heads/main"


def commit(root: Path, oid: str) -> str | None:
    """The commit `oid` names in `root`, an annotated tag peeled; None when it names none."""
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "--end-of-options", f"{oid}^{{commit}}"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


def problems(root: Path, updates: list[Update]) -> list[str]:
    """Why this push may not go, one line per refused update; empty when every tip is recorded."""
    from . import verify_record
    from .harness_role import consumer

    if consumer(root):
        return ["this is a consumer install (harness.role: consumer); it never pushes the harness"]
    try:
        rp = verify_record.harness_repo(root)
    except verify_record.NotListed:
        return []  # no registry, or one that does not list the harness (a public clone): nothing to gate
    except Exception as exc:  # noqa: BLE001 — an unreadable registry refuses, it never waves a push through
        return [f"the registry could not be read, so no pushed tip can be checked: {exc}"]
    out: list[str] = []
    checked: set[str] = set()
    env_digest = verify_record.environment(rp)["digest"]
    for update in updates:
        name = update.remote_ref or update.local_oid[:12]
        if set(update.local_oid) == {"0"}:
            if update.remote_ref == MAIN:
                out.append("refusing to delete main: a push from here never removes it")
            continue
        sha = commit(root, update.local_oid)
        if sha is None:
            out.append(f"{name}: {update.local_oid[:12]} is not a commit this checkout can read; refusing")
            continue
        if sha in checked:
            continue
        checked.add(sha)
        try:
            found, why = verify_record.lookup(root, sha, env_digest=env_digest)
        except AgentError as exc:
            found, why = None, str(exc)
        if found is None:
            out.append(f"{name}: {why}")
    return out
