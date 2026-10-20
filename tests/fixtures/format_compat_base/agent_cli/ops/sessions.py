# ruff: noqa
from __future__ import annotations
import os
from pathlib import Path
from typing import Any
from .. import capture as capture_mod
from .. import dispatch as dispatch_mod
from .. import features as features_mod
from .. import leases as leases_mod
from .. import scratch as scratch_mod
from .. import sessions as sessions_mod
from ..core import EXIT_NOT_FOUND, AgentError, NotFound, is_alive, table
from ._shared import Op


def _lease_pid() -> int | None:
    pid, known = sessions_mod.resolve_identity()
    if not known and sessions_mod.resolve_provider() == "codex":
        return None
    return pid or os.getpid()


def session_get(root: Path, *, session_id: str | None = None) -> Op:
    sid = sessions_mod.resolve_session_id(root, session_id)
    record = sessions_mod.get(root, sid)
    if record is None:
        raise AgentError(f"no record for session {sid}", EXIT_NOT_FOUND)
    feat = record.get("feature")
    return Op(
        record, f"session {sid}: " + (f"{feat['id']:03d} {feat['slug']}" if feat else "no feature mapped")
    )


def owner_question(
    root: Path, *, session_id: str | None = None, summary: str | None = None, resolution: str | None = None
) -> Op:
    from ..hooks.activity import refresh_tab
    from ..hooks.attention import clear_question, raise_question

    sid = sessions_mod.resolve_session_id(root, session_id)
    if sessions_mod.get(root, sid) is None:
        raise AgentError(f"no record for session {sid}", EXIT_NOT_FOUND)
    from ..vault import live

    if resolution is not None:
        if resolution not in ("answered", "withdrawn", "replaced"):
            raise AgentError("resolution must be answered, withdrawn or replaced")
        clear_question(root, sid, resolution)
        question_id = None
    else:
        question_id = raise_question(root, sid, "manual", summary)
    try:
        refresh_tab(root, sid)
    finally:
        live.publish_pending(root)
    asked = f": {summary.strip()}" if summary and summary.strip() else ""
    if resolution:
        return Op(
            {"session_id": sid, "owner_question_pending": False, "resolution": resolution},
            f"session {sid}: owner question {resolution}",
        )
    return Op(
        {"session_id": sid, "owner_question_pending": True, "summary": summary, "question_id": question_id},
        f"session {sid} is waiting on the owner{asked}. The tab hand shows the owner nothing of the question: it must already be visible in your reply text.",
    )


def session_set(
    root: Path,
    *,
    session_id: str,
    feature: str | None = None,
    cwd: str | None = None,
    name: str | None = None,
) -> Op:
    if os.environ.get("CLAUDE_AGENT_ID"):
        raise AgentError("a subagent cannot rewrite a manager session with session set")
    mapping = None
    if feature:
        f = features_mod.resolve(root, feature, session_id)
        mapping = {"id": f.id, "slug": f.slug, "dir": f.rel_dir(root)}
    record = sessions_mod.set_record(root, session_id, feature=mapping, cwd=cwd, name=name)
    return Op(record, f"session {session_id} recorded")


def session_prune(root: Path, *, days: int = 14, dead: bool = False) -> Op:

    def keep(record: dict[str, Any]) -> bool:
        unknown = not (record.get("pid") or record.get("dispatch_id"))
        return unknown or _session_live(root, record)

    if dead:
        removed = [
            sid
            for r in sessions_mod.all_records(root)
            if (sid := str(r.get("session_id") or ""))
            and (not keep(r))
            and (sessions_mod.remove(root, sid, keep=keep) is not None)
        ]
    else:
        removed = sessions_mod.prune(root, days, keep=keep)
    return Op({"removed": removed}, f"removed {len(removed)} session record(s)")


def session_export(root: Path, *, dest: str, feature: str | None = None) -> Op:
    from .. import evidence

    feature_dir = None
    if feature:
        f = features_mod.resolve(root, feature, None)
        feature_dir = f"{f.id:03d}-{f.slug}"
    result = evidence.export(root, Path(dest), feature_dir=feature_dir)
    size = result["bytes"] / 1000000
    return Op(result, f"exported {result['files']} file(s), {size:.1f} MB, to {result['dest']}")


def session_rm(root: Path, *, session_id: str, force: bool = False) -> Op:
    if sessions_mod.get(root, session_id) is None:
        raise NotFound(f"no record for session {session_id}")
    cleared = sessions_mod.remove(
        root, session_id, keep=None if force else lambda record: _session_live(root, record)
    )
    if cleared is None:
        raise AgentError(f"session {session_id} is live; close it first, or pass --force")
    return Op(
        {"removed": session_id, "by_pid": cleared},
        f"removed session {session_id}" + (f" and by-pid {', '.join(cleared)}" if cleared else ""),
    )


def _session_live(root: Path, record: dict[str, Any]) -> bool:
    dispatch_id = record.get("dispatch_id")
    if dispatch_id:
        try:
            d = dispatch_mod.load(root, str(dispatch_id))
        except AgentError:
            return is_alive(record.get("pid"))
        return is_alive(d.get("claude_pid")) or is_alive(d.get("supervisor_pid"))
    from ..session_liveness import record_live

    return record_live(record, is_alive)


def session_list(root: Path) -> Op:
    items: list[dict[str, Any]] = []
    rows: list[list[str]] = []
    for record in sessions_mod.all_records(root):
        feature = record.get("feature")
        feature_label = f"{int(feature['id']):03d} {feature['slug']}" if feature else "—"
        pid = record.get("pid")
        live = _session_live(root, record)
        items.append(
            {
                "session_id": record.get("session_id"),
                "name": record.get("name") or "—",
                "feature": feature_label,
                "pid": pid,
                "state": "LIVE" if live else "DEAD",
                "started_at": record.get("started_at"),
            }
        )
        rows.append(
            [
                str(record.get("session_id")),
                str(record.get("name") or "—"),
                feature_label,
                str(pid) if pid is not None else "—",
                "LIVE" if live else "DEAD",
                str(record.get("started_at") or "—"),
            ]
        )
    return Op(items, table(rows, ["session_id", "name", "feature", "pid", "state", "started_at"]))


def _current_feature(root: Path, session_id: str) -> dict[str, Any] | None:
    record = sessions_mod.get(root, session_id)
    feature = (record or {}).get("feature")
    if not feature:
        return None
    return {"id": feature.get("id"), "slug": feature.get("slug")}


def lease_take(root: Path, *, area: str, note: str | None = None, session_id: str | None = None) -> Op:
    sid = sessions_mod.resolve_session_id(root, session_id)
    result = leases_mod.take(
        root, area, session_id=sid, pid=_lease_pid(), feature=_current_feature(root, sid), note=note
    )
    took_over = f" (took over from {result['took_over_from']})" if result.get("took_over_from") else ""
    return Op(result, f"{area}: held by {sid}{took_over}")


def lease_release(root: Path, *, area: str, session_id: str | None = None) -> Op:
    sid = sessions_mod.resolve_session_id(root, session_id)
    released = leases_mod.release(root, area, sid)
    data = {"area": area, "released": released}
    return Op(data, f"{area}: released" if released else f"{area}: not held by {sid}, nothing to release")


def lease_clear(root: Path, *, who: str, confirm: bool = False) -> Op:
    from .. import lease_clear as lease_clear_mod

    data = lease_clear_mod.clear(root, who, confirm=confirm)
    if data["cleared"]:
        from ..vault import live

        live.write_quietly(root)
    return Op(data, lease_clear_mod.format_result(data))


def lease_list(root: Path) -> Op:
    items = leases_mod.list_all(root)
    rows = [
        [
            str(lease.get("area")),
            str(lease.get("session_id") or "—"),
            str(lease.get("status")),
            f"{int(lease['feature']['id']):03d}" if lease.get("feature") else "—",
            str(lease.get("touched_at") or "—"),
        ]
        for lease in items
    ]
    return Op(items, table(rows, ["area", "session_id", "status", "feature", "touched_at"]))


def lease_wait(
    root: Path,
    *,
    area: str,
    note: str | None = None,
    timeout: float = 120,
    session_id: str | None = None,
    ask: str | None = None,
) -> Op:
    sid = sessions_mod.resolve_session_id(root, session_id)
    asked: str | None = None
    if ask:
        asked = _ask_holder(root, area, frm=sid, why=ask)
    result = leases_mod.wait(
        root,
        area,
        session_id=sid,
        pid=_lease_pid(),
        feature=_current_feature(root, sid),
        note=note,
        timeout=timeout,
    )
    text = f"{area}: held by {sid}"
    if asked:
        text += f" (asked {asked})"
    return Op({**result, "asked": asked}, text)


def _ask_holder(root: Path, area: str, *, frm: str, why: str) -> str | None:
    from .. import peers as peers_mod

    try:
        lease = leases_mod.get(root, area) or {}
        holder_id = str(lease.get("session_id") or "")
        if not holder_id or holder_id == frm:
            return None
        record = sessions_mod.get(root, frm) or {}
        holder = sessions_mod.get(root, holder_id) or {}
        peers_mod.send(
            root,
            to=holder_id,
            text=f"Waiting on the `{area}` lease you hold. {why}\n\nRelease it with the `lease` tool (action `release`, area `{area}`) when your turn ends, or say which files you still need and I will work around you.",
            frm=frm,
            frm_name=str(record.get("name") or ""),
        )
        return str(holder.get("name") or holder_id)
    except Exception:
        return None


def scratch_path(root: Path) -> Op:
    path = scratch_mod.ensure(root)
    return Op({"path": str(path)}, str(path))


def capture(root: Path, *, kind: str, payload: str = "{}", session_id: str | None = None) -> Op:
    parsed = capture_mod.parse_payload(payload)
    result = capture_mod.capture(root, kind, parsed, session_id)
    return Op(result, f"{result['kind']} → {result['written']}")
