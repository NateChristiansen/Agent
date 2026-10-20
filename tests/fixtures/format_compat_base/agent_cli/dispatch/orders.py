# ruff: noqa
from __future__ import annotations
import hashlib
import json
import re
import uuid
from contextlib import nullcontext, suppress
from pathlib import Path
from typing import Any
from .. import notes, notes_writer
from ..core import AgentError, agent_path, atomic_write
from ..records.narrow import as_dict
from .guards import area_guard
from .state import file, load


def _path(root: Path, dispatch_id: str) -> Path:
    if not re.fullmatch("[A-Za-z0-9][A-Za-z0-9._-]{0,127}", dispatch_id):
        raise AgentError("invalid work-order preview id")
    return agent_path(root, "work-order-previews", f"{dispatch_id}.json")


def new_id() -> str:
    return f"preview-{uuid.uuid4().hex}"


def prepared_worktrees(root: Path, *, excluding: str) -> set[str]:
    reserved: set[str] = set()
    for path in _path(root, excluding).parent.glob("preview-*.json"):
        if path.stem == excluding:
            continue
        try:
            saved = preview(root, path.stem)
            worktree = as_dict(saved.get("choices")).get("worktree")
            launched = saved.get("launched_as")
            active = bool(launched) and (not load(root, str(launched)).get("terminal_committed"))
            if (not launched or active) and isinstance(worktree, str):
                reserved.add(worktree)
        except AgentError:
            continue
    return reserved


def preview(root: Path, dispatch_id: str, *, replace_invalid: bool = False) -> dict[str, Any]:
    try:
        saved = as_dict(json.loads(_path(root, dispatch_id).read_text(encoding="utf-8")))
        if (
            not isinstance(saved.get("nonce"), str)
            or not saved.get("sha256")
            or (not isinstance(saved.get("choices"), dict))
        ):
            raise ValueError("invalid preview manifest")
        return saved
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        if replace_invalid:
            return {}
        raise AgentError("work-order preview unreadable; preview again before launch") from exc


def _binding(d: dict[str, Any], text: str) -> dict[str, Any]:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    choices = {
        key: d.get(key)
        for key in (
            "base",
            "branch",
            "worktree",
            "provider",
            "row",
            "model",
            "effort",
            "budget_usd",
            "timeout_minutes",
            "max_turns",
            "tasks",
            "mcp",
            "no_checks",
            "writes",
            "max_attempts",
        )
    }
    if d.get("preview_id"):
        choices.update({key: d.get(key) for key in ("repo", "feature_dir", "kind", "escalated_from")})
    return {"nonce": d["nonce"], "sha256": digest, "choices": choices}


def check(root: Path, d: dict[str, Any], text: str) -> None:
    identity = str(d.get("preview_id") or d["id"])
    saved = preview(root, identity)
    if saved:
        if saved.get("launched_as") and saved["launched_as"] != d["id"]:
            raise AgentError("work-order preview already launched; preview again")
        if {k: saved[k] for k in ("nonce", "sha256", "choices")} != _binding(d, text):
            raise AgentError("work order or launch choices changed; preview again for approval")
        verify(root, f"{d['feature_dir']}/orders/{identity}.md", str(saved["sha256"]))
    elif d.get("preview_id"):
        raise AgentError("work-order preview missing; preview again before launch")


def publish(
    root: Path, d: dict[str, Any], text: str, *, dry_run: bool, allocated: bool = False
) -> dict[str, Any]:
    if dry_run:
        d.setdefault("preview_id", new_id())
    identity = str(d.get("preview_id") or d["id"])
    with nullcontext() if dry_run else area_guard(root, f"work-order:{identity}"):
        return _publish(root, d, text, dry_run=dry_run, allocated=allocated)


def _publish(root: Path, d: dict[str, Any], text: str, *, dry_run: bool, allocated: bool) -> dict[str, Any]:
    from ..ops.harness import link

    identity = str(d.get("preview_id") or d["id"])
    relative = f"{d['feature_dir']}/orders/{identity}.md"
    record = _binding(d, text)
    digest = record["sha256"]
    if not allocated and file(root, str(d["id"]), ".json").exists():
        raise AgentError("this dispatch was allocated while composing; preview again")
    saved = preview(root, identity)
    if saved:
        check(root, d, text)
        url = str(link(root, target=relative).data["url"])
    else:
        try:
            if notes.read_notes_root(root) is None:
                raise AgentError("no notes root is configured")
            url = str(link(root, target=relative).data["url"])
            notes_writer.write_work_order(root, relative, text)
            if d.get("preview_id"):
                record["preview_id"] = d["preview_id"]
            atomic_write(_path(root, identity), json.dumps(record, sort_keys=True) + "\n")
        except (AgentError, OSError) as exc:
            if dry_run:
                with suppress(OSError):
                    _path(root, identity).unlink(missing_ok=True)
            return {"work_order_url": None, "work_order_reason": str(exc)}
    if dry_run:
        atomic_write(_path(root, str(d["id"])), json.dumps(saved or record, sort_keys=True) + "\n")
    elif d.get("preview_id"):
        record = {**(saved or record), "launched_as": d["id"]}
        atomic_write(_path(root, identity), json.dumps(record, sort_keys=True) + "\n")
    return {
        **({"preview_id": d["preview_id"]} if d.get("preview_id") else {}),
        "work_order": relative,
        "work_order_url": url,
        "work_order_sha256": digest,
    }


def verify(root: Path, relative: str, digest: str) -> None:
    try:
        actual = hashlib.sha256((root / relative).read_bytes()).hexdigest()
    except OSError as exc:
        raise AgentError("work-order file unreadable; preview again for approval") from exc
    if actual != digest:
        raise AgentError("work-order file changed; preview again for approval")
