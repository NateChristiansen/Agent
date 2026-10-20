"""One validated identity for lease storage and cooperating mutation locks.

Canonical v1 paths are preserved. Ambiguous legacy names require explicit repair; there is
no automatic filename migration or permission-policy change here. ASCII case aliases share one
identity; caller spelling can remain in the lease data.
"""

from __future__ import annotations

import re

from .core import AgentError
from .records.narrow import as_dict_or_none

SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]*"
# A vault segment may also start with one dot, because `.claude` and `.agents` are real
# directories. The alphanumeric that must follow it still rejects `.` and `..`.
VAULT_SEGMENT = rf"\.?{SEGMENT}"
# A `vault:` name may be several `/`-joined segments, so an area can be a path prefix and two
# sessions working in different subtrees do not collide (`leases.conflicts`). A `worktree:`
# name is exactly `<short>/<name>`, two segments with no prefix semantics: it
# conflicts only with itself. A `repo-slot:` name is `<short>/<k>` with k a nonnegative integer
# and no leading zero, so each slot has one spelling. A verification lane is `verify:<short>`,
# and a lane past the first `verify:<short>/<k>` with k from 1. No leading, trailing or doubled
# slash. Every other namespace stays one segment with no leading dot: a repository short is a name.
AREA = re.compile(
    rf"(?:vault:{VAULT_SEGMENT}(?:/{VAULT_SEGMENT})*|worktree:{SEGMENT}/{SEGMENT}"
    rf"|repo-slot:{SEGMENT}/(?:0|[1-9][0-9]*)|verify:{SEGMENT}/[1-9][0-9]*"
    rf"|(?!(?:worktree|repo-slot):)[A-Za-z][A-Za-z0-9-]*:{SEGMENT})",
    re.ASCII,
)


def area_key(area: str) -> str:
    """Injective within canonical inputs: namespace has no underscore, one colon only, and `~`
    is outside the segment alphabet, so neither substitution can forge another area's key."""
    if AREA.fullmatch(area) is None:
        raise AgentError(
            "noncanonical lease area: use ASCII namespace:name with one colon; "
            "namespace permits letters, digits and hyphens; a segment is letters, digits, "
            "dots, underscores and hyphens starting with a letter or digit. `vault:` "
            "takes a path of `/`-joined segments, `worktree:` exactly two "
            "(`<short>/<name>`), `repo-slot:` `<short>/<k>` with k a nonnegative integer and "
            "`verify:` `<short>` or `<short>/<k>` with k from 1; "
            "every other namespace, including a repository short, is "
            "one segment. Existing ambiguous leases need explicit "
            "repair; no lease was migrated or removed."
        )
    return area.lower().replace(":", "_", 1).replace("/", "~")


def malformed(key: str) -> dict[str, object]:
    """A lease record for a file that exists but does not parse: it holds the area its file name
    encodes, with no holder or pid, so its status is `unverified` and it refuses others. Skipping
    it let another session take an overlapping subtree."""
    return {"area": key.replace("_", ":", 1).replace("~", "/"), "session_id": "malformed-lease", "pid": None}


def checked(raw: object, key: str) -> dict[str, object]:
    """`raw` as a lease when it is one for the area its file name encodes: an object whose `area`
    maps back to `key` (legacy spellings included), whose `session_id` is text and whose `pid` is an integer or null. Anything
    else is `malformed(key)`, which holds the area: `{}`, a mismatched area or a string pid let an
    overlapping take through (found in review)."""
    lease = as_dict_or_none(raw)
    if lease is None:
        return malformed(key)
    area, pid = lease.get("area"), lease.get("pid")
    # Compared loosely, as legacy aliases were written (any case, every colon as `_`), so an older
    # holder stays visible; only an area that maps to another file is rejected.
    loose = (key.lower(), area.lower().replace(":", "_").replace("/", "~") if isinstance(area, str) else None)
    if loose[0] != loose[1]:
        return malformed(key)
    # The exact-lookup rules (`leases.get`): a non-empty holder, an explicit `pid` key, and a pid
    # that is a positive int or null. `pid: 0` read as a dead holder and lost its area.
    holder = lease.get("session_id")
    if not isinstance(holder, str) or not holder or "pid" not in lease:
        return malformed(key)
    if pid is not None and (type(pid) is not int or pid <= 0):
        return malformed(key)
    return lease
