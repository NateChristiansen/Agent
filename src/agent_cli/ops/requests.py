"""Request ops: new, capture, use, show, list, complete and promote, one implementation behind the
`request` CLI and the typed `request` tool. Mapping, closing and cleanup reuse the feature ops."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import capture as capture_mod
from .. import features as features_mod
from .. import log as log_mod
from .. import obsidian as obsidian_mod
from .. import requests as requests_mod
from .. import sessions
from ..core import table
from ._shared import Op, render_views
from .feature_close import close_record
from .features import _feature_dict, feature_use  # pyright: ignore[reportPrivateUsage]


def _resolve(root: Path, ref: str, session_id: str | None) -> features_mod.Feature:
    return requests_mod.require(features_mod.resolve(root, ref, session_id))


def _data(root: Path, f: features_mod.Feature) -> dict[str, Any]:
    return {**_feature_dict(root, f), **requests_mod.row(root, f)}


def _created(root: Path, f: features_mod.Feature, use: bool, session_id: str | None, verb: str) -> Op:
    data = _data(root, f)
    tail = ""
    if use:
        sid = sessions.resolve_session_id(root, session_id)
        sessions.set_record(root, sid, feature={"id": f.id, "slug": f.slug, "dir": f.rel_dir(root)})
        data["session_id"] = sid
        data["adopted"] = capture_mod.adopt(root, f, sid)["moved"]
        tail = f" (session {sid} mapped" + (
            f", {data['adopted']} capture(s) adopted)" if data["adopted"] else ")"
        )
    log_mod.append(
        f,
        "decision",
        f"opened: {f.meta.get('category')} request — {requests_mod.short(requests_mod.ask_of(f))}",
    )
    render_views(root)
    obsidian_mod.auto_open(root, f"{f.rel_dir(root)}/plan.md")
    return Op(data, f"{verb} {f.label} {f.rel_dir(root)} [{f.status}]{tail}")


def request_new(
    root: Path,
    *,
    slug: str,
    title: str,
    ask: str,
    category: str,
    repos: list[str] | None = None,
    use: bool = True,
    session_id: str | None = None,
) -> Op:
    """An active request the owner asked for now; maps the calling manager by default."""
    if use:
        sessions.resolve_session_id(root, session_id)  # refuse before an id is claimed
    f = requests_mod.new(root, slug=slug, title=title, ask=ask, category=category, repos=repos)
    return _created(root, f, use, session_id, "created")


def request_capture(
    root: Path,
    *,
    slug: str,
    title: str,
    ask: str,
    category: str,
    repos: list[str] | None = None,
    use: bool = False,
    session_id: str | None = None,
) -> Op:
    """A bug or idea for later: `proposed`, and the session keeps its mapping unless `use`."""
    if use:
        sessions.resolve_session_id(root, session_id)
    f = requests_mod.capture(root, slug=slug, title=title, ask=ask, category=category, repos=repos)
    op = _created(root, f, use, session_id, "captured")
    op.text += (
        "\nnot authorized to execute: set it active (feature_set status=active) only when the owner asks"
    )
    return op


def request_use(root: Path, *, request: str, session_id: str | None = None, switch: bool = False) -> Op:
    f = _resolve(root, request, session_id)
    op = feature_use(root, feature=str(f.id), session_id=session_id, switch=switch)
    return Op({**op.data, "request": requests_mod.row(root, f)}, op.text)


def request_show(root: Path, *, request: str = "current", session_id: str | None = None) -> Op:
    f = _resolve(root, request, session_id)
    data = _data(root, f)
    data["ask"] = requests_mod.ask_of(f)
    data["result"] = requests_mod.result_of(f) or None
    data["unfinished_work"] = requests_mod.unfinished_work(root, f)
    entries = log_mod.parse(f.log_path)[-5:] if f.log_path.exists() else []
    data["recent_log"] = [{"ts": e.ts, "kind": e.kind, "text": e.text} for e in entries]
    lines = [
        f"{f.label} {f.slug} — {data['title']} [{f.status}] {data['category']}",
        f"ask: {data['ask']}",
        f"result: {data['result'] or '—'}",
        f"next: {data['next_action']}",
    ]
    if data["open_tasks"] or data["done_tasks"]:
        lines.append(f"tasks: {data['open_tasks']} open, {data['done_tasks']} done")
    if data["unfinished_work"]:
        lines.append("dispatch work owed: " + "; ".join(data["unfinished_work"]))
    return Op(data, "\n".join(lines))


def request_list(root: Path, *, include_closed: bool = False) -> Op:
    """Active and captured requests; resolved ones too with `include_closed`."""
    reqs = [f for f in features_mod.load_all(root) if f.is_request and (include_closed or f.is_open)]
    rows = [requests_mod.row(root, f) for f in reqs]
    text = table(
        [[r["number"], r["status"], r["category"] or "", r["ask"], r["next_action"]] for r in rows],
        ["request", "status", "category", "ask", "next"],
    )
    return Op(rows, text)


def request_complete(
    root: Path,
    *,
    request: str,
    result: str,
    status: str = "done",
    note: str | None = None,
    session_id: str | None = None,
) -> Op:
    """Record the delivered result and close the request; refused while its dispatches owe work."""
    f = _resolve(root, request, session_id)
    return close_record(root, f, status=status, note=note, session_id=session_id, result=result)


def request_promote(root: Path, *, request: str, reason: str, session_id: str | None = None) -> Op:
    f = _resolve(root, request, session_id)
    requests_mod.promote(root, f, reason)
    render_views(root)
    return Op(
        _feature_dict(root, f),
        f"promoted Request {f.number} to feature {f.number} {f.slug} [{f.status}]; "
        "add Decisions, Verification and tasks before work beyond the agreed ask",
    )
