"""Let the owner clear a stuck session's leases through CLI, MCP or Dashboard. Unknown-pid holdings do
not expire automatically. Ask if a lease was touched within leases.IDLE_SECONDS, because editing
may still be active. Never clear a verification-protected lease.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import core, leases
from .records.narrow import as_dict, as_feature_id


def resolve_session(root: Path, who: str) -> str:
    """A session id from an id or a session name (the peer address)."""
    from . import sessions

    who = who.strip()
    if not who:
        raise core.AgentError("name the session whose leases to clear")
    records = [r for r in sessions.all_records(root) if r.get("session_id")]
    for record in records:
        if record["session_id"] == who or record.get("name") == who:
            return str(record["session_id"])
    if any(lease.get("session_id") == who for lease in leases.list_all(root)):
        return who
    raise core.AgentError(
        f"no session or lease holder named {who!r}; the `lease` tool's `list` action names them"
    )


@leases.lease_guarded
def _remove(root: Path, area: str, session_id: str, confirm: bool) -> bool:
    """Remove one lease under its lock. Recency is checked again here: the holder may have
    touched the lease since `clear` looked, and then it needs the owner's confirm again."""
    lease = leases.get(root, area)
    if lease is None or lease.get("session_id") != session_id:
        return False
    if not confirm and _recent(lease):
        return False
    from .dispatch.verification_gate import lease_blocks

    if lease_blocks(root, lease):
        return False
    leases.area_file(root, area).unlink(missing_ok=True)
    return True


def _recent(lease: dict[str, Any]) -> bool:
    """Touched within IDLE_SECONDS by a holder that is alive or unknown: it may be mid-edit."""
    pid = lease.get("pid")
    if pid is not None and not core.is_alive(pid):
        return False
    return leases.minutes_ago(lease.get("touched_at")) * 60 < leases.IDLE_SECONDS


def clear(root: Path, who: str, *, confirm: bool = False) -> dict[str, Any]:
    """Remove every lease `who` holds. Without `confirm`, a recently active holder gets
    `needs_confirm` and nothing is removed."""
    session_id = resolve_session(root, who)
    held = [lease for lease in leases.list_all(root) if lease.get("session_id") == session_id]
    recent = [lease for lease in held if _recent(lease)]
    result: dict[str, Any] = {
        "session_id": session_id,
        "areas": sorted(str(lease.get("area")) for lease in held),
        "cleared": [],
        "kept": [],
        "needs_confirm": False,
    }
    if recent and not confirm:
        result["needs_confirm"] = True
        result["recent_minutes"] = min(leases.minutes_ago(lease.get("touched_at")) for lease in recent)
        return result
    for lease in held:
        area = str(lease.get("area") or "")
        (result["cleared"] if area and _remove(root, area, session_id, confirm) else result["kept"]).append(
            area
        )
    if result["cleared"]:
        _log(root, held, session_id, result["cleared"])
    return result


def _log(root: Path, held: list[dict[str, Any]], session_id: str, cleared: list[str]) -> None:
    """One note per feature the cleared leases named, so the owner's action is on record."""
    from . import features, log

    by_feature: dict[int, list[str]] = {}
    for lease in held:
        feature_id = as_feature_id(as_dict(lease.get("feature")).get("id"))
        if lease.get("area") in cleared and feature_id is not None:
            by_feature.setdefault(feature_id, []).append(str(lease["area"]))
    for feature_id, areas in by_feature.items():
        try:
            feat = features.resolve(root, str(feature_id))
            log.append(feat, "note", f"The owner cleared {session_id}'s leases: {', '.join(areas)}.")
        except core.AgentError:
            continue


def format_result(data: dict[str, Any]) -> str:
    if data["needs_confirm"]:
        return (
            f"{data['session_id']} touched a lease {data['recent_minutes']}m ago and may still be "
            f"editing: {', '.join(data['areas'])}. Call again with confirm=True (`--yes` on the CLI) "
            "to clear anyway."
        )
    if not data["areas"]:
        return f"{data['session_id']} holds no leases"
    text = f"cleared {', '.join(data['cleared'])}" if data["cleared"] else "cleared nothing"
    if data["kept"]:
        text += f"; kept {', '.join(data['kept'])} (a running verifier holds it, or it was just touched)"
    return text
