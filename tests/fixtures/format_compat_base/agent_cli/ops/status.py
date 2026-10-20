# ruff: noqa
from __future__ import annotations
import contextlib
from pathlib import Path
from typing import Any
from .. import core, sessions
from .. import dispatch as dispatch_mod
from .. import leases as leases_mod
from .. import obsidian as obsidian_mod
from .. import status as status_mod
from ..core import EXIT_NOT_FOUND, EXIT_OK
from ..journal import tokens as tokens_mod
from ..records.narrow import as_dict, feature_number, str_list
from ..session_liveness import record_live
from ._shared import Op


def status_render(root: Path) -> Op:
    payload = status_mod.render(root)
    return Op(
        payload,
        f"rendered STATUS.md, adrs/README.md, {core.agent_display('status.json')} (hash {payload['source_hash']})",
    )


def status_check(root: Path) -> Op:
    result = status_mod.check(root)
    stale = [
        f"{name} (expected {row['expected']}, found {row['found']})"
        for name, row in result["views"].items()
        if not row["fresh"]
    ]
    text = "fresh" if result["fresh"] else f"stale: {'; '.join(stale) or result['reason']}"
    return Op(result, text, code=EXIT_OK if result["fresh"] else EXIT_NOT_FOUND)


def status_show(root: Path) -> Op:
    text = status_mod.show(root)
    return Op({"status_md": text}, text)


def obsidian_setup(root: Path) -> Op:
    result = obsidian_mod.setup(root)
    lines = ["wrote:"] + [f"  {w}" for w in result["written"]]
    if result["missing_plugins"]:
        lines.append("install these in Settings → Community plugins, then restart Obsidian:")
        lines += [f"  {m}" for m in result["missing_plugins"]]
    lines.append("optional:")
    lines += [f"  {m}" for m in result["optional_plugins"]]
    lines.append(
        "obsidian CLI: "
        + (
            "available"
            if result["cli_available"]
            else "not on PATH (Settings → General → Command line interface)"
        )
    )
    return Op(result, "\n".join(lines))


def obsidian_open(root: Path, *, path: str) -> Op:
    result = obsidian_mod.open_note(root, path)
    return Op(result, result.get("skipped") or f"opened {path}")


def obsidian_daily(root: Path) -> Op:
    result = obsidian_mod.daily()
    return Op(result, result.get("skipped") or "opened today's daily note")


def obsidian_search(root: Path, *, query: str) -> Op:
    result = obsidian_mod.search(query)
    return Op(result, result.get("skipped") or (result.get("stdout") or "(no results)"))


def _session_label(root: Path, session_id: str | None) -> str:
    record = sessions.get(root, session_id) if session_id else None
    name = (record or {}).get("name")
    return str(name) if name else str(session_id)


def _live_peers(root: Path, session_id: str | None) -> list[str]:
    from .. import peers as peers_mod

    out: list[str] = []
    for record in sessions.all_records(root):
        sid = record.get("session_id")
        if (
            not sid
            or sid == session_id
            or record.get("dispatch_id")
            or (not record_live(record, core.is_alive))
        ):
            continue
        name = str(record.get("name") or str(sid)[:8])
        try:
            unread = len(peers_mod.peek(root, str(sid)))
        except Exception:
            unread = 0
        out.append(f"{name}({unread} unread)" if unread else name)
    return sorted(out)


def _owed(
    root: Path, dispatches: list[dict[str, Any]], session_id: str | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from ..vault import live

    owed = live.obligations(root, dispatches=dispatches)
    states = {str(d.get("id")): d.get("state") for d in dispatches}
    launcher = {str(d.get("id")): d.get("launched_by_session") for d in dispatches}

    def row(did: str) -> dict[str, Any]:
        mine = bool(session_id) and launcher.get(did) == session_id
        return {"id": did, "state": states.get(did), "mine": mine}

    asks = [
        {"session_id": n["session_id"], "name": n["name"], "reason": n["reason"]}
        for n in owed["needs_you"]
        if n.get("kind") == "manager"
    ]
    ids: list[str] = []
    for n in owed["needs_you"]:
        if n.get("kind") == "worker":
            ids.append(str(n["id"]))
        elif n.get("kind") == "orphaned":
            ids += str_list(n.get("ids"))
    actions = owed["manager_action"]
    for sid, items in actions.items():
        if session_id is None or sid == session_id:
            ids += [str(i["id"]) for i in items]
    waiting = [row(did) for did in sorted(dict.fromkeys(ids))]
    for w in waiting:
        w["mine"] = w["mine"] or any((str(i["id"]) == w["id"] for i in actions.get(session_id or "", [])))
    return (asks, waiting)


def status_line(root: Path, *, session_id: str | None = None, cost_usd: float | None = None) -> Op:
    running: list[dict[str, Any]] = []
    dispatches = dispatch_mod.load_all(root)
    for d in dispatches:
        if d.get("state") in ("queued", "running"):
            running.append(
                {
                    "id": d.get("id"),
                    "short": d.get("short") or d.get("repo"),
                    "elapsed_min": leases_mod.minutes_ago(d.get("started") or d.get("created")),
                    "cost_usd": round(float(d.get("cost_usd") or 0.0), 2),
                    "tokens": int(d.get("tokens") or 0),
                    "mine": bool(session_id) and d.get("launched_by_session") == session_id,
                }
            )
    asks, waiting = _owed(root, dispatches, session_id)
    if session_id and cost_usd is not None:
        with contextlib.suppress(Exception):
            sessions.record_cost(root, session_id, float(cost_usd), core.today().isoformat())
    record = sessions.get(root, session_id) if session_id else None
    feature = as_dict((record or {}).get("feature"))
    session_cost = cost_usd if cost_usd is not None else (record or {}).get("cost_usd")
    if cost_usd is None and (record or {}).get("cost_source") == "unknown":
        session_cost = None
    label = (record or {}).get("name") or (
        f"{feature_number(feature['id'])} {feature.get('slug')}" if feature.get("id") is not None else ""
    )
    mine: list[str] = []
    others: list[dict[str, str]] = []
    for lease in leases_mod.list_all(root):
        if lease.get("status") not in leases_mod.BLOCKING_STATUSES:
            continue
        if session_id:
            if lease.get("session_id") == session_id:
                mine.append(str(lease.get("area")))
        else:
            others.append(
                {"area": str(lease.get("area")), "holder": _session_label(root, lease.get("session_id"))}
            )
    peers = _live_peers(root, session_id)
    parts: list[str] = []
    if label:
        parts.append(f"[{label} ${float(session_cost):.2f}]" if session_cost is not None else f"[{label}]")
    for r in running:
        burn = (
            f"${r['cost_usd']:.2f}"
            if r["cost_usd"]
            else f"{tokens_mod.compact(r['tokens'])} tok"
            if r["tokens"]
            else ""
        )
        parts.append(
            " ".join((x for x in (f"⚙ {r['id']}", str(r["short"]), f"{r['elapsed_min']}m", burn) if x))
        )
    parts += [f"✋ {a['name']} {a['reason']} waiting on you" for a in asks]
    named = [w for w in waiting if w["mine"]] if session_id else waiting if len(waiting) <= 3 else []
    parts += [f"⏸ {w['id']} {w['state']}" for w in named]
    rest = len(waiting) - len(named)
    if rest:
        parts.append(f"⏸ {rest} {('more ' if named else '')}waiting on you")
    if session_id:
        if mine:
            parts.append("leases: " + ", ".join(mine))
    elif others:
        parts.append("leases held: " + ", ".join((f"{o['area']} ({o['holder']})" for o in others)))
    if peers:
        parts.append("peers: " + ", ".join(peers))
    data: dict[str, Any] = {
        "session": label or None,
        "session_cost_usd": round(float(session_cost), 4) if session_cost is not None else None,
        "running": running,
        "waiting": waiting,
        "asks": asks,
        "peers": peers,
    }
    data["leases"] = mine if session_id else others
    return Op(data, "  ".join(parts))
