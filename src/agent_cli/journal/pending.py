"""Find daily milestones missing from the journal's generated block. Session-start and Stop share the
same pending set; a journal write clears only milestones it covers.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from ..core import agent_path, now

PENDING_DIR = agent_path(Path(), "journal", "pending")

# What counts as a milestone. `pr` is recorded when a dispatch record first carries a PR
# number: the point where the work left the machine. `landing` is a branch landing on main
#: a worker's return can be journaled hours before its branch lands.
KINDS = ("feature-close", "dispatch-return", "adr", "pr", "landing")


def _path(root: Path, day: date) -> Path:
    return root / PENDING_DIR / f"{day.isoformat()}.jsonl"


def _receipt(root: Path, day: date) -> Path:
    """The keys of markers a journal write cleared, so a keyed `record` can tell "already
    journaled" from "never written"."""
    return root / PENDING_DIR / f"{day.isoformat()}.cleared"


def record(
    root: Path, kind: str, ref: str, detail: str = "", day: date | None = None, key: str | None = None
) -> bool:
    """Append one milestone; True once it is recorded. Never raises: the caller is closing a
    feature, returning a worker or finishing a landing, and none may fail because this did.

    With `key` it is idempotent: a marker with that key already pending, or already cleared by a
    journal write that day, counts as recorded and nothing is appended. False means the write
    failed and the caller still owes it."""
    if kind not in KINDS:
        return False
    try:
        when = day or now().date()
        path = _path(root, when)
        if key is not None and (
            any(item.get("key") == key for item in read(root, when)) or key in _cleared(root, when)
        ):
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        row: dict[str, Any] = {
            "ts": now().isoformat(timespec="seconds"),
            "kind": kind,
            "ref": ref,
            "detail": detail,
        }
        if key is not None:
            row["key"] = key
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — a milestone marker never breaks its caller
        return False
    return True


def _cleared(root: Path, day: date) -> set[str]:
    try:
        return set(_receipt(root, day).read_text(encoding="utf-8").split())
    except OSError:
        return set()


def read(root: Path, day: date) -> list[dict[str, Any]]:
    path = _path(root, day)
    if not path.exists():
        return []
    items: list[dict[str, Any]] = []
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                items.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return items


def clear(root: Path, day: date) -> None:
    """Drop the day's markers. `journal write` calls this: once the block is written, what was
    pending is now in the note, and a second nudge for the same milestones is noise. Keyed
    markers leave their keys in a receipt first; if that write fails nothing is cleared, since
    a repeated nudge is cheaper than a lost receipt."""
    keys = [str(item["key"]) for item in read(root, day) if item.get("key")]
    try:
        if keys:
            with _receipt(root, day).open("a", encoding="utf-8") as fh:
                fh.write("".join(f"{k}\n" for k in keys))
        _path(root, day).unlink(missing_ok=True)
    except OSError:
        pass


def summary(root: Path, day: date) -> str:
    """The nudge body: what happened, grouped, with the instruction that the answer may be no.

    Empty string when nothing is pending, so the normal case costs no context."""
    items = read(root, day)
    if not items:
        return ""
    by_kind: dict[str, list[str]] = {}
    for item in items:
        ref = str(item.get("ref") or "").strip()
        if not ref:
            continue
        refs = by_kind.setdefault(str(item.get("kind") or ""), [])
        if ref not in refs:
            refs.append(ref)
    if not by_kind:
        return ""
    parts = [f"{kind}: {', '.join(refs)}" for kind, refs in by_kind.items() if refs]
    # Issue the journal instruction whenever pending milestones change so managers record each new
    # batch.
    return (
        f"journal not written for {day.isoformat()}; since then: "
        + "; ".join(parts)
        + f". Write it now with `/journal {day.isoformat()}`."
    )


def fingerprint(root: Path, day: date) -> str:
    """What is pending for `day`, as a short hash; empty when nothing is. A session remembers
    the one it was nudged with, and is nudged again when this changes: a new milestone after a
    nudge, or after a journal write cleared the list (a count could not see "cleared, then one new milestone")."""
    import hashlib

    items = read(root, day)
    if not items:
        return ""
    rows = sorted(json.dumps([i.get("kind"), i.get("ref"), i.get("ts")], ensure_ascii=False) for i in items)
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()[:16]
