"""Advisory, cooperative area locks over `.agent/leases/<area>.json`.

One machine, atomic writes and bounded reentrant POSIX area locks serialize cooperating
takers. Dispatch verifier activity and unconfirmed cleanup prevent dead-holder takeover. A lease says
"this session is working in this area" so a second live session touching an overlapping area
waits instead of racing it (harness/sessions.md).

An area is either a `vault:` path — `vault:src` and `vault:harness` for a whole subtree, or a
narrower path like `vault:src/agent_cli/dispatch` that `area_for_path` infers from a file —
or `repo:<short>`, which dispatch names explicitly and nothing infers from a path. Two areas
collide when their paths overlap (`conflicts`), so sessions in sibling packages do not queue
for each other.

`core.is_alive`/`core.now` are read through the `core` module, so a test that monkeypatches
them (a fake dead or live pid, or fake idle time via `AGENT_NOW`) reaches every call site here.
"""

from __future__ import annotations

import json
import time
import unicodedata
from collections.abc import Callable
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, Concatenate

from . import core
from .lease_keys import area_key, checked, malformed
from .records.narrow import as_dict, as_feature_id, dict_list, dig


def lease_guarded[**P, R](fn: Callable[Concatenate[Path, str, P], R]):
    @wraps(fn)
    def guarded(root: Path, area: str, *args: P.args, **kwargs: P.kwargs) -> R:
        from .dispatch.guards import area_guard

        # Lock the whole-subtree root, not the narrow area. Every take under `vault:src` serializes on one
        # lock, so the conflict scan cannot race a sibling take, and there is no second lock to order against
        # — which is how the narrowing avoids inventing a deadlock the coarse scheme got for free.
        with area_guard(root, coarse_area(area) or area):
            return fn(root, area, *args, **kwargs)

    return guarded


LEASES_DIR = core.agent_path(Path(), "leases")
IDLE_SECONDS = 30 * 60
WAIT_POLL_SECONDS = 1.0
LEASE_PROTOCOL = 2  # take_marked stamps the protocol on every dispatch lease
# The session counter the `lease-yield` Stop hook writes at the end of every turn.
# A holder whose turn ended after its last touch is at rest, not mid-edit.
HOLDER_STOPPED_KEY = "stopped_at_ms"


# Every value `status` returns. A test that reads a real vault's leases imports this, so a new
# status cannot pass it by luck.
LEASE_STATUSES = ("live", "resting", "idle", "dead", "unverified")
# Statuses that refuse another session. `unverified` is an unknown-pid holder quiet past
# IDLE_SECONDS: it may still be working, so only its release or `lease clear` frees it.
BLOCKING_STATUSES = ("live", "unverified")


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


# vault:src covers the harness's own code and tests; vault:harness covers the harness
# configuration and docs a hook or a worker contract change touches. `repo:<short>` is never
# inferred from a path — dispatch names it explicitly.
AREAS: dict[str, tuple[str, ...]] = {
    "vault:src": ("src", "tests", "bin"),
    "vault:harness": ("harness", ".claude", ".agents", "AGENTS.md"),
}

# How much of a path names the area it falls in. Whole-subtree areas made two managers queue
# for each other over files neither wanted: one held vault:harness to rewrite six docs while
# the other only needed to add a row to a seventh. The depth per top-level is set by how that
# directory is actually shaped — `src/` is packages, `tests/` and `harness/` are mostly flat —
# so the area lands on the unit people really contend for.
AREA_DEPTH: dict[str, int] = {
    "src": 3,  # src/agent_cli/dispatch
    "tests": 2,  # tests/test_leases.py
    "bin": 2,  # bin/agent
    "harness": 2,  # harness/safety.md, harness/obsidian
    ".claude": 2,  # .claude/settings.json, .claude/rules
    ".agents": 3,  # .agents/skills/dispatch
}


def _parts(area: str) -> list[str] | None:
    """The path segments a `vault:` area names, or None for an area that is not a path
    (`repo:<short>`, or anything outside the `vault:` namespace)."""
    namespace, _, name = area.partition(":")
    return name.split("/") if namespace == "vault" and name else None


def coarse_area(area: str) -> str | None:
    """The whole-subtree area a path area falls under: `vault:src/agent_cli/dispatch` is part
    of `vault:src`. A coarse area is its own root. None for a non-path area."""
    parts = _parts(area)
    if parts is None:
        return None
    if area in AREAS:
        return area
    for coarse, tops in AREAS.items():
        if parts[0] in tops:
            return coarse
    return None


def conflicts(one: str, other: str) -> bool:
    """Whether two areas overlap, so a live lease on one refuses the other.

    Same area, always. A coarse area against anything inside it, both ways. Two path areas
    when one is a prefix of the other — `vault:src/agent_cli/dispatch` and
    `vault:src/agent_cli/dispatch/verification.py` are the same work; `.../dispatch` and
    `.../cli` are not. Everything else (`repo:<short>`) conflicts only with itself.
    """
    if area_key(one) == area_key(other):
        return True
    a, b = _parts(one), _parts(other)
    if a is None or b is None:
        return False
    if one in AREAS or other in AREAS:
        return coarse_area(one) == coarse_area(other)
    if coarse_area(one) != coarse_area(other):
        return False
    shared = min(len(a), len(b))
    return a[:shared] == b[:shared]


def leases_dir(root: Path) -> Path:
    return root / LEASES_DIR


def area_file(root: Path, area: str) -> Path:
    path = leases_dir(root) / f"{area_key(area)}.json"
    if not path.parent.exists():
        return path
    wanted = path.name
    aliases = [
        entry
        for entry in path.parent.iterdir()
        if unicodedata.normalize("NFKC", entry.name).casefold() == wanted
    ]
    if len(aliases) > 1 or any(not entry.name.isascii() for entry in aliases):
        raise core.AgentError(
            "existing lease has ambiguous case or Unicode filename aliases; "
            "explicit repair is required before mutation; no lease was changed"
        )
    # A single case-only legacy filename stays authoritative. No rename/migration occurs.
    return aliases[0] if aliases else path


def area_for_path(rel: str) -> str | None:
    """The narrowest area a vault-relative path (forward-slash, no leading `./`) falls in, or
    `None` for a path outside every known area.

    `src/agent_cli/dispatch/verification.py` is `vault:src/agent_cli/dispatch`, so a session
    working in `src/agent_cli/cli` does not have to wait for it. A top-level file that is its
    own area, like `AGENTS.md`, keeps the coarse name it always had.
    """
    if not rel:
        return None
    parts = [p for p in rel.replace("\\", "/").split("/") if p and p != "."]
    if not parts:
        return None
    top = parts[0]
    for area, tops in AREAS.items():
        if top not in tops:
            continue
        depth = AREA_DEPTH.get(top)
        if depth is None:
            # a top-level file listed as its own member, such as AGENTS.md
            return area
        return "vault:" + "/".join(parts[:depth])
    return None


def _valid_pid(pid: Any) -> bool:
    """A positive int, or None: a holder whose process the harness could not prove (a Codex session with no positive tab association)."""
    return pid is None or (type(pid) is int and pid > 0)


def get(root: Path, area: str) -> dict[str, Any] | None:
    path = area_file(root, area)
    if not path.exists():
        return None
    try:
        value = as_dict(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        raise core.AgentError(
            "existing lease is malformed; repair it before taking or releasing the area"
        ) from None
    if (
        not isinstance(held := value.get("area"), str)
        or area_key(held) != area_key(area)
        or not isinstance(holder := value.get("session_id"), str)
        or not holder
        or "pid" not in value  # a missing key is malformed; only an explicit null means unknown
        or not _valid_pid(value["pid"])
    ):
        raise core.AgentError("existing lease is malformed; repair it before taking or releasing the area")
    return value


def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def resting(root: Path, lease: dict[str, Any]) -> bool:
    """A holder is resting when its turn ends after its latest lease touch. It may yield without
    interrupting an edit. Legacy records without touched_at_ms cannot prove rest.
    """
    touched = lease.get("touched_at_ms")
    if not touched:
        return False
    try:
        from . import sessions as sessions_mod

        stopped = sessions_mod.get_counter(root, str(lease.get("session_id") or ""), HOLDER_STOPPED_KEY)
    except Exception:  # noqa: BLE001 — an unreadable holder record is not evidence of rest
        return False
    # `>=`: a Stop in the same millisecond as the last touch still ended the turn after it; a
    # new turn's edit can never land in the millisecond its previous turn stopped.
    return stopped >= int(touched)


def status(lease: dict[str, Any] | None, root: Path | None = None) -> str:
    """`"live"`, `"resting"`, `"idle"`, `"dead"` or `"unverified"` for a lease dict (as returned
    by `get`/`list_all`). Dead when the holder's pid is gone. Idle when it has not been touched
    in `IDLE_SECONDS`; unverified instead when the holder's pid is unknown (`None`), which
    still refuses others (`BLOCKING_STATUSES`). Resting (only when `root` is given, so the holder's session record can be
    read) when the holder's turn ended after its last touch. Live otherwise. Only a live lease
    refuses another session. A lease that does not exist has no status. Callers check for
    `None` first (`get` returning `None` IS "free", which is not the same as "dead": nothing to
    take over, so `take` on a `None` lease never logs a `took_over_from`)."""
    if lease is None:
        return "dead"
    if root is not None:
        from .dispatch.verification_gate import lease_blocks

        if lease_blocks(root, lease):
            return "live"
    unknown = lease.get("pid") is None
    if not unknown and not core.is_alive(lease.get("pid")):
        return "dead"
    touched = lease.get("touched_at")
    if touched:
        try:
            ts = _parse(str(touched))
        except ValueError:
            ts = None
        if ts is not None and (core.now() - ts) > timedelta(seconds=IDLE_SECONDS):
            if not unknown:
                return "idle"
            # An unknown holder may still be editing, so its lease never expires. Only positive evidence frees
            # it: its own Stop hook
            # (resting), its release, or the owner's `lease clear`.
            if root is not None and resting(root, lease):
                return "resting"
            return "unverified"
    if root is not None and resting(root, lease):
        return "resting"
    return "live"


@lease_guarded
def request(root: Path, area: str, *, session_id: str, pid: int | None, note: str | None = None) -> bool:
    """Record on the lease that `session_id` needs `area`. The lease yields when
    the holder's turn ends (`resting`). A repeat request refreshes the
    timestamp. Returns False when there is nothing to ask: no lease, or this session already
    holds it."""
    lease = get(root, area)
    if lease is None or lease.get("session_id") == session_id:
        return False
    requests = [r for r in dict_list(lease.get("requests")) if r.get("session_id") != session_id]
    requests.append({"session_id": session_id, "pid": pid, "at": core.iso(core.now()), "note": note})
    lease["requests"] = requests
    core.atomic_write(area_file(root, area), json.dumps(lease, indent=2) + "\n")
    return True


def minutes_ago(value: str | None) -> int:
    if not value:
        return 0
    try:
        ts = _parse(str(value))
    except ValueError:
        return 0
    return max(0, int((core.now() - ts).total_seconds() // 60))


def holder_label(root: Path, session_id: str) -> str:
    """The holder's session `name` when one is on record, else the bare session id."""
    try:
        from . import sessions as sessions_mod

        record = sessions_mod.get(root, session_id)
    except Exception:  # noqa: BLE001 — a name lookup failure falls back to the id
        record = None
    name = (record or {}).get("name")
    return str(name) if name else session_id


def refusal(root: Path, area: str, lease: dict[str, Any]) -> str:
    """Give the same actionable refusal as take and wait. Name both waiting and asking the holder by
    its peer address, so a timeout does not leave owner mediation as the only visible next step.
    """
    holder = holder_label(root, str(lease.get("session_id") or "?"))
    feat_id = as_feature_id(dig(lease, "feature", "id"))
    feat_label = f"feature {feat_id:03d}" if feat_id is not None else "no feature"
    minutes = minutes_ago(lease.get("touched_at"))
    return (
        f"{area} is held by {holder} ({feat_label}, touched {minutes}m ago). "
        f"Ask for it: the `lease` tool with action `wait`, area `{area}`, and `ask` saying why you "
        f"need it and which files — this messages {holder} and then waits. "
        f"Or wait without asking: the same call without `ask`, timeout 120. "
        f"Either way your request is on the lease and the holder yields it when its turn ends. "
        f"Ask the owner to arbitrate only after the holder has answered."
    )


def _overlapping(root: Path, area: str, session_id: str) -> list[dict[str, Any]]:
    """Every other session's lease whose area overlaps `area`, live or not.

    It is not enough to read this area's own file any more: since an area can be a path
    prefix, the thing in the way may be a lease on a parent or a child. `vault:src` held by
    someone else covers `vault:src/agent_cli/cli`, and vice versa.
    """
    return [
        lease
        for lease in list_all(root)
        if lease.get("session_id") != session_id
        and str(lease.get("area") or "")
        and conflicts(str(lease["area"]), area)
    ]


def blocker(root: Path, area: str, session_id: str) -> dict[str, Any] | None:
    """The live lease held by another session that overlaps `area`, or None. Only a live lease
    refuses; a resting, idle or dead one is cleared by `take`."""
    for lease in _overlapping(root, area, session_id):
        if lease.get("status") in BLOCKING_STATUSES:
            return lease
    return None


@lease_guarded
def take(
    root: Path,
    area: str,
    *,
    session_id: str,
    pid: int | None,
    feature: dict[str, Any] | None = None,
    note: str | None = None,
    marks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Take (or, for the same holder, touch) the lease when nothing live overlaps it, else raise
    `AgentError` naming the holder (`refusal`). `took_over_from` names a DIFFERENT, idle or dead
    holder this call replaced. `marks` are extra lease-file fields (`take_marked`)."""
    existing = get(root, area)
    same_holder = existing is not None and existing.get("session_id") == session_id
    kept = existing if same_holder and existing is not None else {}
    took_over_from: str | None = None
    if not same_holder:
        stale = _overlapping(root, area, session_id)
        live = next((lease for lease in stale if lease.get("status") in BLOCKING_STATUSES), None)
        if live is not None:
            raise core.AgentError(refusal(root, str(live.get("area") or area), live))
        # Clear what this take supersedes rather than layering a narrow lease under a resting
        # parent: an overlapping lease nobody releases blocks the next taker for no reason.
        for lease in stale:
            took_over_from = took_over_from or lease.get("session_id")
            area_file(root, str(lease["area"])).unlink(missing_ok=True)
    ts = core.iso(core.now())
    lease = {
        "area": existing["area"] if existing is not None else area,
        "session_id": session_id,
        "pid": pid,
        "feature": feature,
        "taken_at": kept.get("taken_at") if kept else ts,
        "touched_at": ts,
        "touched_at_ms": _now_ms(),
        "note": note if note is not None else kept.get("note"),
    }
    if kept.get("requests"):
        # the holder touching its own lease keeps who is waiting on it
        lease["requests"] = kept["requests"]
        lease["notified"] = kept.get("notified") or []
    lease.update(marks or {})
    core.atomic_write(area_file(root, area), json.dumps(lease, indent=2) + "\n")
    # `touched_at_ms` (the resting check's wall clock) stays on disk and out of the returned
    # record, which the CLI and MCP tool print and which must not differ between two runs.
    shown = {k: v for k, v in lease.items() if k != "touched_at_ms"}
    return {"taken": True, "took_over_from": took_over_from, "lease": shown}


def take_marked(root: Path, area: str, *, worktree_key: str | None = None, **kwargs: Any) -> dict[str, Any]:
    """Take or touch a dispatch lease with `lease_protocol: 2`, its `worktree_key` when present, and
    any caller marks so reconciliation can verify ownership.
    """
    marks = {"lease_protocol": LEASE_PROTOCOL, **({"worktree_key": worktree_key} if worktree_key else {})}
    return take(root, area, marks={**(kwargs.pop("marks", None) or {}), **marks}, **kwargs)


@lease_guarded
def release(root: Path, area: str, session_id: str) -> bool:
    """Remove the lease file if this session holds it. Idempotent: releasing an already-free
    lease, or one held by someone else, is a no-op (`False`), not an error."""
    lease = get(root, area)
    if lease is None or lease.get("session_id") != session_id:
        return False
    from .dispatch.verification_gate import lease_blocks

    if lease_blocks(root, lease):
        return False
    area_file(root, area).unlink(missing_ok=True)
    return True


def list_all(root: Path) -> list[dict[str, Any]]:
    """Every lease on disk with its `status`; one released mid-scan is skipped, a malformed one holds."""
    base = leases_dir(root)
    if not base.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in core.json_files(base):  # raises under core.strict_reads when it cannot list
        try:
            lease = checked(json.loads(path.read_text(encoding="utf-8")), path.stem)
        except FileNotFoundError:
            continue
        except json.JSONDecodeError:
            lease = malformed(path.stem)
        out.append({**lease, "status": status(lease, root)})
    return out


def release_all_for_session(root: Path, session_id: str) -> list[str]:
    """Release every lease this session holds (session-end). Returns the areas released."""
    released: list[str] = []
    for lease in list_all(root):
        area = str(lease.get("area") or "")
        if lease.get("session_id") == session_id and area and release(root, area, session_id):
            released.append(area)
    return released


def wait(
    root: Path,
    area: str,
    *,
    session_id: str,
    pid: int | None,
    feature: dict[str, Any] | None = None,
    note: str | None = None,
    timeout: float = 120,
) -> dict[str, Any]:
    """Poll once a second until `area` is free (no lease, idle, dead, or already held by this
    session), then `take` it and return. `AgentError` naming the holder (`refusal`) on
    timeout. The same message `take` would have raised on the first try."""
    deadline = time.monotonic() + float(timeout)
    asked = False
    while True:
        lease = blocker(root, area, session_id)
        if lease is None:
            return take(root, area, session_id=session_id, pid=pid, feature=feature, note=note)
        held = str(lease.get("area") or area)
        if not asked:
            # the request goes on the lease that is actually in the way, which may be a parent
            # or a child of the area being waited for
            asked = request(root, held, session_id=session_id, pid=pid, note=note)
        if time.monotonic() >= deadline:
            raise core.AgentError(refusal(root, held, lease))
        time.sleep(WAIT_POLL_SECONDS)
