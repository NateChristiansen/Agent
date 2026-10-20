# ruff: noqa
from __future__ import annotations
import json
from datetime import date
from pathlib import Path
from typing import Any
from ..core import agent_path, now

PENDING_DIR = agent_path(Path(), "journal", "pending")
KINDS = ("feature-close", "dispatch-return", "adr", "pr", "landing")


def _path(root: Path, day: date) -> Path:
    return root / PENDING_DIR / f"{day.isoformat()}.jsonl"


def _receipt(root: Path, day: date) -> Path:
    return root / PENDING_DIR / f"{day.isoformat()}.cleared"


def record(
    root: Path, kind: str, ref: str, detail: str = "", day: date | None = None, key: str | None = None
) -> bool:
    if kind not in KINDS:
        return False
    try:
        when = day or now().date()
        path = _path(root, when)
        if key is not None and (
            any((item.get("key") == key for item in read(root, when))) or key in _cleared(root, when)
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
    except Exception:
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
    keys = [str(item["key"]) for item in read(root, day) if item.get("key")]
    try:
        if keys:
            with _receipt(root, day).open("a", encoding="utf-8") as fh:
                fh.write("".join((f"{k}\n" for k in keys)))
        _path(root, day).unlink(missing_ok=True)
    except OSError:
        pass


def summary(root: Path, day: date) -> str:
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
    return (
        f"journal not written for {day.isoformat()}; since then: "
        + "; ".join(parts)
        + f". Write it now with `/journal {day.isoformat()}`."
    )


def fingerprint(root: Path, day: date) -> str:
    import hashlib

    items = read(root, day)
    if not items:
        return ""
    rows = sorted((json.dumps([i.get("kind"), i.get("ref"), i.get("ts")], ensure_ascii=False) for i in items))
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()[:16]
