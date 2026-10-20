# ruff: noqa
from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from .. import capture as capture_mod
from .. import features as features_mod
from .. import log as log_mod
from .. import obsidian as obsidian_mod
from .. import sessions
from .. import tickets as tickets_mod
from ..core import AgentError, agent_display, iso, now, table
from ..records.narrow import as_dict, str_list
from ._shared import Op, render_views


def _feature_dict(root: Path, f: features_mod.Feature) -> dict[str, Any]:
    tasks = f.tasks()
    return {
        "id": f.id,
        "number": f.number,
        "slug": f.slug,
        "dir": f.rel_dir(root),
        "title": f.meta.get("title"),
        "status": f.status,
        "opened": f.meta.get("opened"),
        "closed": f.meta.get("closed"),
        "tickets": tickets_mod.tickets_of(f.meta),
        "repos": f.meta.get("repos") or [],
        "adrs": f.meta.get("adrs") or [],
        "tags": f.meta.get("tags") or [],
        "links": f.meta.get("links") or [],
        "open_tasks": tasks["open"],
        "done_tasks": tasks["done"],
        "superseded_tasks": tasks["superseded"],
        "next_task": tasks["next"],
    }


def _warn_unmapped_session(root: Path, session_id: str | None = None) -> None:
    try:
        sid = sessions.resolve_session_id(root, session_id)
    except AgentError:
        return
    if (sessions.get(root, sid) or {}).get("feature"):
        return
    sys.stderr.write(
        f"warning: session {sid} is mapped to no feature, so its captures land in {agent_display('raw', 'unassigned')}/. Map it with the `feature_use` tool (which adopts them) before doing the work.\n"
    )


def feature_new(
    root: Path,
    *,
    slug: str,
    title: str,
    status: str = "active",
    tickets: list[str] | None = None,
    repos: list[str] | None = None,
    tags: list[str] | None = None,
    use: bool = False,
    session_id: str | None = None,
) -> Op:
    f = features_mod.new(root, slug, title, status, list(tickets or []), list(repos or []), list(tags or []))
    result = _feature_dict(root, f)
    if use:
        sid = sessions.resolve_session_id(root, session_id)
        sessions.set_record(root, sid, feature={"id": f.id, "slug": f.slug, "dir": f.rel_dir(root)})
        result["session_id"] = sid
        result["adopted"] = capture_mod.adopt(root, f, sid)["moved"]
    else:
        _warn_unmapped_session(root, session_id)
    render_views(root)
    obsidian_mod.auto_open(root, f"{f.rel_dir(root)}/plan.md")
    tail = f" (session {result['session_id']} mapped)" if use else ""
    if use and result["adopted"]:
        tail += f" ({result['adopted']} capture(s) adopted)"
    return Op(result, f"created {f.rel_dir(root)}{tail}")


def feature_list(root: Path, *, status: list[str] | None = None, include_closed: bool = False) -> Op:
    feats = features_mod.load_all(root)
    if status:
        feats = [f for f in feats if f.status in status]
    elif not include_closed:
        feats = [f for f in feats if f.is_open]
    rows = [[f.number, f.slug, f.status, str(f.meta.get("title", ""))] for f in feats]
    return Op([_feature_dict(root, f) for f in feats], table(rows, ["#", "slug", "status", "title"]))


def feature_show(root: Path, *, feature: str = "current", session_id: str | None = None) -> Op:
    f = features_mod.resolve(root, feature, session_id)
    entries = log_mod.parse(f.log_path)[-5:] if f.log_path.exists() else []
    data = _feature_dict(root, f)
    data["recent_log"] = [{"ts": e.ts, "kind": e.kind, "text": e.text} for e in entries]
    lines = [
        f"{f.number} {f.slug} — {data['title']} [{f.status}]",
        f"opened {data['opened']}  closed {data['closed']}",
    ]
    lines.append(
        f"tickets {data['tickets']}  repos {data['repos']}  adrs {data['adrs']}  tags {data['tags']}"
    )
    superseded = f", {data['superseded_tasks']} superseded" if data["superseded_tasks"] else ""
    lines.append(
        f"tasks: {data['open_tasks']} open, {data['done_tasks']} done{superseded}; next: {data['next_task'] or '—'}"
    )
    if entries:
        lines.append("recent log:")
        for e in entries:
            lines.append(f"  {e.ts} {e.kind}: {(e.text.splitlines()[0] if e.text else '')}")
    return Op(data, "\n".join(lines))


def _mapping_work(root: Path, before: dict[str, Any]) -> bool:
    from ..dispatch import state, verdicts
    from ..vault.live import running_dispatch

    old = as_dict(before.get("feature")).get("id")
    if old is None:
        return False
    current = features_mod.resolve(root, str(old))
    if not current.is_open:
        return False
    for dispatch in state.load_all(root):
        if dispatch.get("feature") != old or not state.is_live(dispatch):
            continue
        if running_dispatch(dispatch) or not verdicts.is_read(dispatch):
            return True
    return False


def feature_use(root: Path, *, feature: str, session_id: str | None = None, switch: bool = False) -> Op:
    if os.environ.get("CLAUDE_AGENT_ID"):
        raise AgentError("a subagent cannot remap the manager session with feature use")
    f = features_mod.resolve(root, feature, session_id)
    sid = sessions.resolve_session_id(root, session_id)
    from ..hooks.naming import OVERRIDABLE_SOURCES, desired_title, live_name, taken_names

    before = sessions.get(root, sid) or {}
    old = as_dict(before.get("feature"))
    changing = old.get("id") != f.id
    peer = as_dict(before.get("ask_guard")).get("machine_prompt") or before.get("feature_peer_delivered")
    if changing and (not switch) and (peer or _mapping_work(root, before)):
        raise AgentError(
            "`feature_use` would change ownership with open work or after a peer message. Keep the mapping for inspection, answers or cleanup; pass explicit `feature` or `dispatch_id` arguments. Only on the owner's ask, retry with `switch=true`."
        )
    old_generated = desired_title(before, sid, taken=taken_names(root, sid))
    live, source = live_name(before.get("pid"))
    record = sessions.set_record(root, sid, feature={"id": f.id, "slug": f.slug, "dir": f.rel_dir(root)})
    if record.get("provider") != "codex" and (not record.get("dispatch_id")):
        if live and source == "user":
            sessions.set_field(root, sid, "name", live)
        elif old_generated and (
            (live or before.get("name")) == old_generated
            or before.get("name") == old_generated
            or (live is not None and source in OVERRIDABLE_SOURCES)
        ):
            new_name = desired_title(record, sid, taken=taken_names(root, sid))
            if new_name and new_name != old_generated:
                if not any((record.get(field) for field in ("tab_display_name", "tab_name", "tab_label"))):
                    sessions.set_field(root, sid, "tab_name", old_generated)
                sessions.set_field(root, sid, "name", new_name)
                sessions.set_field(root, sid, "pending_client_name", old_generated)
                former = [*str_list(record.get("former_names") or []), old_generated]
                sessions.set_field(root, sid, "former_names", former)
        from ..hooks.activity import refresh_tab

        refresh_tab(root, sid)
        record = sessions.get(root, sid) or record
    adopted = capture_mod.adopt(root, f, sid)["moved"]
    if changing and old.get("id") is not None:
        previous = features_mod.resolve(root, str(old["id"]))
        line = f"session `{sid}` mapped from `{previous.number}` to `{f.number}`; switch={str(switch).lower()}, peer context={str(bool(peer)).lower()}."
        log_mod.append(f, "note", line)
        log_mod.append(previous, "note", line)
    render_views(root)
    obsidian_mod.auto_open(root, f"{f.rel_dir(root)}/plan.md")
    tail = f" ({adopted} capture(s) adopted)" if adopted else ""
    checked, check_text = _pickup_check(root, f)
    return Op(
        {**record, "adopted": adopted, "task_check": checked},
        f"session {sid} → {f.number} {f.slug}{tail}{check_text}",
    )


def feature_current(root: Path, *, session_id: str | None = None) -> Op:
    f = features_mod.resolve(root, "current", session_id)
    return Op(_feature_dict(root, f), f"{f.number} {f.slug} — {f.meta.get('title')} [{f.status}]")


def feature_set(
    root: Path,
    *,
    feature: str,
    session_id: str | None = None,
    status: str | None = None,
    title: str | None = None,
    add_ticket: list[str] | None = None,
    remove_ticket: list[str] | None = None,
    add_repo: list[str] | None = None,
    remove_repo: list[str] | None = None,
    add_adr: list[int] | None = None,
    remove_adr: list[int] | None = None,
    add_tag: list[str] | None = None,
    remove_tag: list[str] | None = None,
    reason: str | None = None,
) -> Op:
    f = features_mod.resolve(root, feature, session_id)
    before = f.status
    features_mod.set_fields(
        root,
        f,
        status=status,
        title=title,
        add_ticket=add_ticket,
        remove_ticket=remove_ticket,
        add_repo=add_repo,
        remove_repo=remove_repo,
        add_adr=add_adr,
        remove_adr=remove_adr,
        add_tag=add_tag,
        remove_tag=remove_tag,
        reason=reason,
    )
    render_views(root)
    text = f"updated {f.number} {f.slug}"
    if f.status != before:
        text += f"; status {before} → {f.status}, logged"
    return Op(_feature_dict(root, f), text)


def raw_preserve(root: Path, *, feature: str = "current", session_id: str | None = None) -> Op:
    f = features_mod.resolve(root, feature, session_id)
    copied = capture_mod.preserve_transcripts(root, f)
    result = {"feature": f.id, "copied": [p.relative_to(root).as_posix() for p in copied]}
    raw_rel = f.raw_dir.relative_to(root).as_posix()
    return Op(result, f"preserved {len(copied)} transcript(s) into {raw_rel}/transcripts/")


def _task_dicts(f: features_mod.Feature) -> list[dict[str, Any]]:
    return [
        {"index": t.index, "done": t.done, "state": t.state, "text": t.text}
        for t in features_mod.list_tasks(f.body)
    ]


_TASK_BOX = {"done": "[x]", "superseded": "[-]", "open": "[ ]"}


def _task_lines(tasks: list[dict[str, Any]]) -> str:
    if not tasks:
        return "(no tasks)"
    return "\n".join((f"{_TASK_BOX[t['state']]} #{t['index']} {t['text']}" for t in tasks))


def task_list(
    root: Path, *, feature: str = "current", open_only: bool = False, session_id: str | None = None
) -> Op:
    f = features_mod.resolve(root, feature, session_id)
    tasks = _task_dicts(f)
    if open_only:
        tasks = [t for t in tasks if t["state"] == "open"]
    return Op({"feature": f.id, "tasks": tasks}, f"{f.number} {f.slug}\n" + _task_lines(tasks))


def _task_toggle(
    root: Path, feature: str, task: str, done: bool, note: str | None, session_id: str | None
) -> Op:
    f = features_mod.resolve(root, feature, session_id)
    t = features_mod.resolve_task(f.body, task)
    if t.superseded and done:
        raise AgentError(f"#{t.index} is superseded; reopen it with task_undo first if it was done after all")
    if t.done == done and (not t.superseded):
        return Op(
            {"feature": f.id, "task": t.index, "changed": False},
            f"#{t.index} already {('done' if done else 'open')}: {t.text}",
        )
    f.body = features_mod.set_task(f.body, t, done, note)
    f.save()
    hint = ""
    if done and f.status == "blocked" and f.log_path.exists():
        from .. import blocked, log

        log.append(f, "note", blocked.tick_entry(t.index))
        hint = f" (feature {f.number} is blocked; {blocked.CLEAR_HINT})"
    render_views(root)
    verb = "done" if done else "reopened"
    return Op(
        {"feature": f.id, "task": t.index, "changed": True, "text": t.text},
        f"{verb} #{t.index}: {t.text}{hint}",
    )


def task_done(
    root: Path, *, task: str, feature: str = "current", note: str | None = None, session_id: str | None = None
) -> Op:
    op = _task_toggle(root, feature, task, True, note, session_id)
    if op.data.get("changed"):
        _task_event(root, "task.done", op, session_id)
    return op


def _task_event(root: Path, kind: str, op: Op, session_id: str | None) -> None:
    from ..telemetry import attention as events

    events.emit(
        root,
        kind,
        session=session_id,
        feature=events.feature_ref(op.data.get("feature")),
        index=op.data.get("task"),
    )
    if session_id and sessions.get(root, session_id) is not None:
        sessions.update_record(
            root,
            session_id,
            lambda r: {
                **r,
                "feature_task_at": {**as_dict(r.get("feature_task_at")), str(op.data["feature"]): iso(now())},
            },
        )


def task_undo(root: Path, *, task: str, feature: str = "current", session_id: str | None = None) -> Op:
    return _task_toggle(root, feature, task, False, None, session_id)


def task_supersede(
    root: Path, *, task: str, reason: str, link: str, feature: str = "current", session_id: str | None = None
) -> Op:
    f = features_mod.resolve(root, feature, session_id)
    t = features_mod.resolve_task(f.body, task)
    if t.superseded:
        return Op(
            {"feature": f.id, "task": t.index, "changed": False}, f"#{t.index} already superseded: {t.text}"
        )
    f.body = features_mod.supersede_task(f.body, t, reason, link)
    f.save()
    render_views(root)
    return Op(
        {"feature": f.id, "task": t.index, "changed": True, "text": t.text, "reason": reason, "link": link},
        f"superseded #{t.index}: {t.text}",
    )


def task_check(root: Path, *, feature: str = "current", session_id: str | None = None) -> Op:
    from .. import task_check as task_check_mod

    f = features_mod.resolve(root, feature, session_id)
    result = task_check_mod.check(root, f)
    return Op(result, task_check_mod.render(result))


def _pickup_check(root: Path, f: features_mod.Feature) -> tuple[dict[str, Any] | None, str]:
    from .. import task_check as task_check_mod

    try:
        result = task_check_mod.check(root, f)
    except (AgentError, OSError, subprocess.SubprocessError) as exc:
        return (None, f"\ntask check skipped: {exc}")
    return (result, "\n" + task_check_mod.render(result) if result["open"] else "")


def task_add(root: Path, *, text: str, feature: str = "current", session_id: str | None = None) -> Op:
    f = features_mod.resolve(root, feature, session_id)
    f.body, index = features_mod.add_task_at(f.body, text)
    f.save()
    render_views(root)
    op = Op({"feature": f.id, "task": index, "text": text.strip()}, f"added #{index}: {text.strip()}")
    _task_event(root, "task.add", op, session_id)
    return op


def log(root: Path, *, kind: str, text: str, feature: str = "current", session_id: str | None = None) -> Op:
    f = features_mod.resolve(root, feature, session_id)
    entry = log_mod.append(f, kind, text)
    render_views(root)
    return Op(
        {"feature": f.id, "ts": entry.ts, "kind": entry.kind, "text": entry.text},
        f"{f.number} {entry.ts} — {entry.kind}",
    )
