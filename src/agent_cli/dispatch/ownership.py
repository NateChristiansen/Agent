"""Who may write a handoff file.

Two machines share the notes repo but not `.agent/`. When both allocate the same dispatch id,
both hold a record whose `handoff` names the same synced file, and without this guard the
second machine's writes repaint the first machine's handoff and plan line. This module
CONTAINS that collision: a record writes only a file it can prove it owns. ADR-0036's machine
tag prevents it for distinct labels; two machines sharing one label still collide, and this
guard is what keeps them from overwriting each other.

The rule, per case:

- File absent: the record may write it (a new dispatch, or one whose handoff was deleted).
- File present: the record and the file must both carry a present, nonblank `session_id`, and
  they must be equal. `None == None` and `"" == ""` are not ownership. Where both sides carry
  `id`, `feature` and `repo`, those must match too.
- Missing token (a legacy handoff), malformed or unreadable frontmatter: not adopted. The write
  is skipped and reported, never claimed as success.

The token is the worker's `session_id`, a fresh `uuid4` per dispatch (`lifecycle.new`). An
escalation fork gets its own uuid too; it records the old session only as
`forked_from_session`. Only the library's `worker_session_id` pin (tests) can make two records
share a token, and then the id check still separates records with different seqs. Two records
that share token AND id on one handoff path cannot be told apart; nothing in production makes one.

A skip is reported three ways: `write_handoff` returns None, `upsert_plan_line` returns False,
`sync_live_fields(skipped=...)` collects `{id, handoff, reason}`; and doctor warns on every
local record whose handoff it would not write (`foreign_reason`), so a skip stays visible after
the call that hit it has returned.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import frontmatter
from ..core import AgentError

IDENTITY_KEYS = ("id", "feature", "repo")
MISSING = object()  # tests: a sentinel for "drop this key from the frontmatter"


def _token(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def foreign_reason(path: Path, d: dict[str, Any]) -> str | None:
    """None when record `d` may write `path`; otherwise why not, in one line."""
    if not path.exists():
        return None
    try:
        meta, _ = frontmatter.load(path)
    except (AgentError, OSError, UnicodeDecodeError) as exc:
        detail = exc.message if isinstance(exc, AgentError) else str(exc)
        return f"unreadable frontmatter, not adopted: {detail}"
    ours, theirs = _token(d.get("session_id")), _token(meta.get("session_id"))
    if not theirs:
        return "handoff has no session_id (legacy or hand-edited), not adopted"
    if not ours:
        return "record has no session_id, so it cannot prove ownership"
    if ours != theirs:
        return f"handoff belongs to session_id {theirs}, this record is {ours} (dispatch ids collided)"
    for key in IDENTITY_KEYS:
        mine, file = d.get(key), meta.get(key)
        if mine is not None and file is not None and str(mine) != str(file):
            return f"handoff {key} {file!r} != record {key} {mine!r}"
    return None
