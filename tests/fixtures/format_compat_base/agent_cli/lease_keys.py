# ruff: noqa
from __future__ import annotations
import re
from .core import AgentError
from .records.narrow import as_dict_or_none

SEGMENT = "[A-Za-z0-9][A-Za-z0-9._-]*"
VAULT_SEGMENT = f"\\.?{SEGMENT}"
AREA = re.compile(
    f"(?:vault:{VAULT_SEGMENT}(?:/{VAULT_SEGMENT})*|worktree:{SEGMENT}/{SEGMENT}|repo-slot:{SEGMENT}/(?:0|[1-9][0-9]*)|verify:{SEGMENT}/[1-9][0-9]*|(?!(?:worktree|repo-slot):)[A-Za-z][A-Za-z0-9-]*:{SEGMENT})",
    re.ASCII,
)


def area_key(area: str) -> str:
    if AREA.fullmatch(area) is None:
        raise AgentError(
            "noncanonical lease area: use ASCII namespace:name with one colon; namespace permits letters, digits and hyphens; a segment is letters, digits, dots, underscores and hyphens starting with a letter or digit. `vault:` takes a path of `/`-joined segments, `worktree:` exactly two (`<short>/<name>`), `repo-slot:` `<short>/<k>` with k a nonnegative integer and `verify:` `<short>` or `<short>/<k>` with k from 1; every other namespace, including a repository short, is one segment. Existing ambiguous leases need explicit repair; no lease was migrated or removed."
        )
    return area.lower().replace(":", "_", 1).replace("/", "~")


def malformed(key: str) -> dict[str, object]:
    return {"area": key.replace("_", ":", 1).replace("~", "/"), "session_id": "malformed-lease", "pid": None}


def checked(raw: object, key: str) -> dict[str, object]:
    lease = as_dict_or_none(raw)
    if lease is None:
        return malformed(key)
    area, pid = (lease.get("area"), lease.get("pid"))
    loose = (key.lower(), area.lower().replace(":", "_").replace("/", "~") if isinstance(area, str) else None)
    if loose[0] != loose[1]:
        return malformed(key)
    holder = lease.get("session_id")
    if not isinstance(holder, str) or not holder or "pid" not in lease:
        return malformed(key)
    if pid is not None and (type(pid) is not int or pid <= 0):
        return malformed(key)
    return lease
