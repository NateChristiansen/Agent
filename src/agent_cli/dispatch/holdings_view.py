"""What a dispatch holds, as `dispatch show` and `new --dry-run` print it: the worktree lease, the
repo slot and the verify lanes. Read with no guard and no write."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import leases as leases_mod
from .. import repos as repos_mod


def _lease(root: Path, area: str) -> dict[str, Any]:
    held = leases_mod.get(root, area)
    if held is None:
        return {"area": area, "holder": None, "status": "free"}
    return {"area": area, "holder": held.get("session_id"), "status": leases_mod.status(held, root)}


def _lanes(root: Path, short: str) -> list[dict[str, Any]]:
    return [
        _lease(root, str(lease["area"]))
        for lease in leases_mod.list_all(root)
        if str(lease.get("area")) == f"verify:{short}"
        or str(lease.get("area")).startswith(f"verify:{short}/")
    ]


def status(root: Path, d: dict[str, Any]) -> dict[str, Any]:
    """What `dispatch show` names: the worktree lease, the stored slot and the verify lane."""
    from .allocation import worktree_area
    from .holdings import REVERIFY, slot_area

    short, slot = str(d.get("short") or ""), d.get("slot")
    mine = (f"dispatch:{d['id']}", f"{REVERIFY}{d['id']}:")
    return {
        "worktree_lease": _lease(root, worktree_area(short, d["worktree"])) if d.get("worktree") else None,
        "slot": _lease(root, slot_area(short, slot)) if type(slot) is int else None,
        "verify_lanes": [lane for lane in _lanes(root, short) if str(lane["holder"]).startswith(mine)],
    }


def _held(row: dict[str, Any] | None) -> str:
    if row is None:
        return "none stored"
    return f"{row['area']} " + (f"held by {row['holder']} ({row['status']})" if row["holder"] else "free")


def status_lines(st: dict[str, Any] | None) -> list[str]:
    if not st:
        return []
    lanes = ", ".join(_held(lane) for lane in st["verify_lanes"]) or "none held"
    return [
        f"  worktree lease: {_held(st['worktree_lease'])}",
        f"  repo slot: {_held(st['slot'])}",
        f"  verify lane: {lanes}",
    ]


def preview(root: Path, rp: repos_mod.Repo, worktree: Path) -> dict[str, Any]:
    """`new --dry-run`'s holdings: the repo slots and retired repo lease (`holdings.preview`), the
    planned worktree's lease and the lanes."""
    from .allocation import worktree_area
    from .holdings import preview as slots

    return {
        **slots(root, rp),
        "worktree_lease": _lease(root, worktree_area(rp.short, worktree)),
        "verify_lanes": _lanes(root, rp.short),
    }


def preview_lines(p: dict[str, Any]) -> list[str]:
    from .holdings import preview_lines as slot_lines

    lines = slot_lines(p)
    if "worktree_lease" in p:
        lines.append(f"  worktree lease: {_held(p['worktree_lease'])}")
        lines.append(f"  verify lanes: {', '.join(_held(x) for x in p['verify_lanes']) or 'none held'}")
    return lines
