# ruff: noqa
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

        with area_guard(root, coarse_area(area) or area):
            return fn(root, area, *args, **kwargs)

    return guarded


LEASES_DIR = core.agent_path(Path(), "leases")
IDLE_SECONDS = 30 * 60
WAIT_POLL_SECONDS = 1.0
LEASE_PROTOCOL = 2
HOLDER_STOPPED_KEY = "stopped_at_ms"
LEASE_STATUSES = ("live", "resting", "idle", "dead", "unverified")
BLOCKING_STATUSES = ("live", "unverified")


def _now_ms() -> int:
    return time.time_ns() // 1000000


AREAS: dict[str, tuple[str, ...]] = {
    "vault:src": ("src", "tests", "bin"),
    "vault:harness": ("harness", ".claude", ".agents", "AGENTS.md"),
}
AREA_DEPTH: dict[str, int] = {"src": 3, "tests": 2, "bin": 2, "harness": 2, ".claude": 2, ".agents": 3}


def _parts(area: str) -> list[str] | None:
    namespace, _, name = area.partition(":")
    return name.split("/") if namespace == "vault" and name else None


def coarse_area(area: str) -> str | None:
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
    if area_key(one) == area_key(other):
        return True
    a, b = (_parts(one), _parts(other))
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
    if len(aliases) > 1 or any((not entry.name.isascii() for entry in aliases)):
        raise core.AgentError(
            "existing lease has ambiguous case or Unicode filename aliases; explicit repair is required before mutation; no lease was changed"
        )
    return aliases[0] if aliases else path


def area_for_path(rel: str) -> str | None:
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
            return area
        return "vault:" + "/".join(parts[:depth])
    return None


def _valid_pid(pid: Any) -> bool:
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
        not isinstance((held := value.get("area")), str)
        or area_key(held) != area_key(area)
        or (not isinstance((holder := value.get("session_id")), str))
        or (not holder)
        or ("pid" not in value)
        or (not _valid_pid(value["pid"]))
    ):
        raise core.AgentError("existing lease is malformed; repair it before taking or releasing the area")
    return value


def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def resting(root: Path, lease: dict[str, Any]) -> bool:
    touched = lease.get("touched_at_ms")
    if not touched:
        return False
    try:
        from . import sessions as sessions_mod

        stopped = sessions_mod.get_counter(root, str(lease.get("session_id") or ""), HOLDER_STOPPED_KEY)
    except Exception:
        return False
    return stopped >= int(touched)


def status(lease: dict[str, Any] | None, root: Path | None = None) -> str:
    if lease is None:
        return "dead"
    if root is not None:
        from .dispatch.verification_gate import lease_blocks

        if lease_blocks(root, lease):
            return "live"
    unknown = lease.get("pid") is None
    if not unknown and (not core.is_alive(lease.get("pid"))):
        return "dead"
    touched = lease.get("touched_at")
    if touched:
        try:
            ts = _parse(str(touched))
        except ValueError:
            ts = None
        if ts is not None and core.now() - ts > timedelta(seconds=IDLE_SECONDS):
            if not unknown:
                return "idle"
            if root is not None and resting(root, lease):
                return "resting"
            return "unverified"
    if root is not None and resting(root, lease):
        return "resting"
    return "live"


@lease_guarded
def request(root: Path, area: str, *, session_id: str, pid: int | None, note: str | None = None) -> bool:
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
    try:
        from . import sessions as sessions_mod

        record = sessions_mod.get(root, session_id)
    except Exception:
        record = None
    name = (record or {}).get("name")
    return str(name) if name else session_id


def refusal(root: Path, area: str, lease: dict[str, Any]) -> str:
    holder = holder_label(root, str(lease.get("session_id") or "?"))
    feat_id = as_feature_id(dig(lease, "feature", "id"))
    feat_label = f"feature {feat_id:03d}" if feat_id is not None else "no feature"
    minutes = minutes_ago(lease.get("touched_at"))
    return f"{area} is held by {holder} ({feat_label}, touched {minutes}m ago). Ask for it: the `lease` tool with action `wait`, area `{area}`, and `ask` saying why you need it and which files — this messages {holder} and then waits. Or wait without asking: the same call without `ask`, timeout 120. Either way your request is on the lease and the holder yields it when its turn ends. Ask the owner to arbitrate only after the holder has answered."


def _overlapping(root: Path, area: str, session_id: str) -> list[dict[str, Any]]:
    return [
        lease
        for lease in list_all(root)
        if lease.get("session_id") != session_id
        and str(lease.get("area") or "")
        and conflicts(str(lease["area"]), area)
    ]


def blocker(root: Path, area: str, session_id: str) -> dict[str, Any] | None:
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
    existing = get(root, area)
    same_holder = existing is not None and existing.get("session_id") == session_id
    kept = existing if same_holder and existing is not None else {}
    took_over_from: str | None = None
    if not same_holder:
        stale = _overlapping(root, area, session_id)
        live = next((lease for lease in stale if lease.get("status") in BLOCKING_STATUSES), None)
        if live is not None:
            raise core.AgentError(refusal(root, str(live.get("area") or area), live))
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
        lease["requests"] = kept["requests"]
        lease["notified"] = kept.get("notified") or []
    lease.update(marks or {})
    core.atomic_write(area_file(root, area), json.dumps(lease, indent=2) + "\n")
    shown = {k: v for k, v in lease.items() if k != "touched_at_ms"}
    return {"taken": True, "took_over_from": took_over_from, "lease": shown}


def take_marked(root: Path, area: str, *, worktree_key: str | None = None, **kwargs: Any) -> dict[str, Any]:
    marks = {"lease_protocol": LEASE_PROTOCOL, **({"worktree_key": worktree_key} if worktree_key else {})}
    return take(root, area, marks={**(kwargs.pop("marks", None) or {}), **marks}, **kwargs)


@lease_guarded
def release(root: Path, area: str, session_id: str) -> bool:
    lease = get(root, area)
    if lease is None or lease.get("session_id") != session_id:
        return False
    from .dispatch.verification_gate import lease_blocks

    if lease_blocks(root, lease):
        return False
    area_file(root, area).unlink(missing_ok=True)
    return True


def list_all(root: Path) -> list[dict[str, Any]]:
    base = leases_dir(root)
    if not base.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in core.json_files(base):
        try:
            lease = checked(json.loads(path.read_text(encoding="utf-8")), path.stem)
        except FileNotFoundError:
            continue
        except json.JSONDecodeError:
            lease = malformed(path.stem)
        out.append({**lease, "status": status(lease, root)})
    return out


def release_all_for_session(root: Path, session_id: str) -> list[str]:
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
    deadline = time.monotonic() + float(timeout)
    asked = False
    while True:
        lease = blocker(root, area, session_id)
        if lease is None:
            return take(root, area, session_id=session_id, pid=pid, feature=feature, note=note)
        held = str(lease.get("area") or area)
        if not asked:
            asked = request(root, held, session_id=session_id, pid=pid, note=note)
        if time.monotonic() >= deadline:
            raise core.AgentError(refusal(root, held, lease))
        time.sleep(WAIT_POLL_SECONDS)
