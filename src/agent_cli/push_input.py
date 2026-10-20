"""One strict reading of the pre-push hook's stdin: git's four fields per ref update, or explicit
local/remote OID pairs. Never a ref lookup."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .core import AgentError

MAX_INPUT_CHARS = 1024 * 1024


@dataclass(frozen=True)
class Update:
    local_oid: str
    remote_oid: str
    local_ref: str = ""
    remote_ref: str = ""


def _oid(value: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value):
        raise AgentError("pre-push requires full hexadecimal object IDs (40 or 64 characters)")
    return value.lower()


def parse_updates(text: str | None = None, *, pairs: list[list[str]] | None = None) -> list[Update]:
    """Accept Git's four-field stdin or explicit local/remote OID pairs, never ref lookups."""
    if text is not None and pairs is not None:
        raise AgentError("choose pre-push input or explicit --update pairs, not both")
    if text is not None and len(text) > MAX_INPUT_CHARS:
        raise AgentError("pre-push input exceeds the 1 Mi-character bound")
    raw: list[tuple[str, str, str, str]] = []
    seen_refs: set[str] = set()
    if pairs is not None:
        for pair in pairs:
            if len(pair) != 2:
                raise AgentError("--update requires LOCAL_OID REMOTE_OID")
            raw.append((pair[0], pair[1], "", ""))
    else:
        for line in (text or "").splitlines():
            fields = line.split()
            if len(fields) != 4 or "\0" in line:
                raise AgentError("malformed pre-push input: expected four fields per line")
            local_ref, local_oid, remote_ref, remote_oid = fields
            if not remote_ref.startswith("refs/") or remote_ref in seen_refs:
                raise AgentError("malformed pre-push input: invalid or duplicate remote ref")
            seen_refs.add(remote_ref)
            if (local_ref == "(delete)") != (set(local_oid) == {"0"}):
                raise AgentError("malformed pre-push deletion: (delete) requires a zero local OID")
            raw.append((local_oid, remote_oid, local_ref, remote_ref))
    updates: list[Update] = []
    for local, remote, local_ref, remote_ref in raw:
        local, remote = _oid(local), _oid(remote)
        if len(local) != len(remote) or set(local + remote) == {"0"}:
            raise AgentError("pre-push OIDs must use one hash format and cannot both be zero")
        updates.append(Update(local, remote, local_ref, remote_ref))
    return updates
