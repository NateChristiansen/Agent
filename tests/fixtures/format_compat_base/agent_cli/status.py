# ruff: noqa
from __future__ import annotations
import hashlib
import json
import re
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any
from . import adrs as adrs_mod
from . import blocked as blocked_mod
from . import features as features_mod
from . import frontmatter, maintenance
from . import log as log_mod
from . import repos as repos_mod
from .capture import UNASSIGNED
from .core import (
    GENERATED_VIEWS,
    AgentError,
    agent_display,
    agent_path,
    atomic_write,
    coerce_date,
    iso,
    now,
    today,
    write_if_changed,
)
from .records.narrow import as_dict_or_none, as_list, dig, str_list
from .tickets import tickets_of

STATUS_FILE = GENERATED_VIEWS["status"].as_posix()
STATUS_JSON = agent_path(Path(), "status.json")
RENDER_FAILURE = agent_path(Path(), "render-failure.json")
ADR_INDEX = GENERATED_VIEWS["adrs"]
DECISION_INDEX = Path("decisions") / "README.md"
HASH_RE = re.compile("hash:([0-9a-f]{12})")
CLOSED_WINDOW_DAYS = 14
ORDER = {"active": 0, "blocked": 1, "proposed": 2}


def collect(root: Path) -> dict[str, Any]:
    registry = None
    try:
        registry = repos_mod.load_registry(root)
    except AgentError:
        registry = None
    feats: list[dict[str, Any]] = []
    parked = set(maintenance.parked(root))
    dispatches = _dispatches(root)
    landed = _landed_tasks(root, dispatches)
    for f in features_mod.load_all(root):
        last = log_mod.last_entry(f) if f.log_path.exists() else None
        tasks = f.tasks()
        task_items = features_mod.list_tasks(f.body)
        open_indexes = {t.index for t in task_items if t.is_open}
        landed_open = sorted(landed.get(f.id, set()) & open_indexes)
        blocked_work = blocked_mod.active_work(root, f, dispatches) if f.status == "blocked" else []
        since = blocked_mod.since(f) if f.status == "blocked" else None
        shorts: list[str] = []
        for name in str_list(f.meta.get("repos")):
            repo = registry.find(name) if registry else None
            shorts.append(repo.short if repo else name)
        feats.append(
            {
                "id": f.id,
                "dir": f.rel_dir(root),
                "slug": f.slug,
                "title": f.meta.get("title", ""),
                "status": f.status,
                "parked": f"{f.id:03d}" in parked,
                "repos": list(f.meta.get("repos") or []),
                "repo_shorts": shorts,
                "tickets": tickets_of(f.meta),
                "adrs": list(f.meta.get("adrs") or []),
                "opened": coerce_date(f.meta.get("opened")),
                "closed": coerce_date(f.meta.get("closed")),
                "last_log": {"ts": last.ts, "kind": last.kind} if last else None,
                "next_task": tasks["next"],
                "landed_unticked": landed_open,
                "open_tasks": tasks["open"],
                "open_task_items": [{"index": t.index, "text": t.text} for t in task_items if t.is_open],
                "done_tasks": tasks["done"],
                "blocked_since": {"ts": since[0], "reason": since[1]} if since else None,
                "blocked_work": blocked_work,
                "_hash_src": frontmatter.join(f.meta, "")
                + (last.ts + last.kind if last else "")
                + "|".join(tasks["open_items"])
                + json.dumps([(t.index, t.state, t.text) for t in task_items])
                + f"|open={tasks['open']}|done={tasks['done']}|parked={f'{f.id:03d}' in parked}"
                + f"|landed={landed_open}"
                + (f"|blocked={since}|work={blocked_work}" if f.status == "blocked" else ""),
            }
        )
    adr_list: list[dict[str, Any]] = []
    for a in adrs_mod.load_all(root):
        adr_list.append(
            {
                "id": a.id,
                "slug": a.slug,
                "file": a.path.relative_to(root).as_posix(),
                "title": a.meta.get("title", ""),
                "status": a.meta.get("status"),
                "date": coerce_date(a.meta.get("date")),
                "feature": a.meta.get("feature"),
                "_hash_src": frontmatter.join(a.meta, ""),
            }
        )
    decisions: list[dict[str, Any]] = []
    if adrs_mod.notes_ready(root):
        for a in adrs_mod.load_all(root, "notes"):
            decisions.append(
                {
                    "id": a.id,
                    "slug": a.slug,
                    "file": a.path.relative_to(root).as_posix(),
                    "title": a.meta.get("title", ""),
                    "status": a.meta.get("status"),
                    "date": coerce_date(a.meta.get("date")),
                    "feature": a.meta.get("feature"),
                    "_hash_src": frontmatter.join(a.meta, ""),
                }
            )
    unassigned_dir = root / UNASSIGNED
    files = sorted(unassigned_dir.glob("*.jsonl")) if unassigned_dir.exists() else []
    unassigned = {
        "files": len(files),
        "latest": files[-1].stem if files else None,
        "_names": [p.name for p in files],
    }
    return {"features": feats, "adrs": adr_list, "decisions": decisions, "unassigned_raw": unassigned}


def _dispatches(root: Path) -> list[dict[str, Any]]:
    from .dispatch import state as dispatch_state

    try:
        return dispatch_state.load_all(root)
    except AgentError, OSError:
        return []


def _landed_tasks(root: Path, records: list[dict[str, Any]] | None = None) -> dict[int, set[int]]:
    out: dict[int, set[int]] = {}
    for d in _dispatches(root) if records is None else records:
        feature = d.get("feature")
        if dig(d, "integration", "state") != "landed" or not isinstance(feature, int):
            continue
        out.setdefault(feature, set()).update((t for t in as_list(d.get("tasks")) if isinstance(t, int)))
    return out


def _landed_mark(indexes: object) -> str:
    landed = [t for t in as_list(indexes) if isinstance(t, int)]
    return f"#{', #'.join((str(t) for t in landed))} landed, unticked; " if landed else ""


def source_hash(data: dict[str, Any]) -> str:
    h = hashlib.sha256()
    for f in data["features"]:
        h.update(f["_hash_src"].encode("utf-8"))
    for a in data["adrs"]:
        h.update(a["_hash_src"].encode("utf-8"))
    for a in data["decisions"]:
        h.update(a["_hash_src"].encode("utf-8"))
    h.update("|".join(data["unassigned_raw"].get("_names") or []).encode("utf-8"))
    return h.hexdigest()[:12]


def adr_hash(data: dict[str, Any]) -> str:
    h = hashlib.sha256()
    for a in data["adrs"]:
        h.update(a["_hash_src"].encode("utf-8"))
    return h.hexdigest()[:12]


def decision_hash(data: dict[str, Any]) -> str:
    h = hashlib.sha256()
    for a in data["decisions"]:
        h.update(a["_hash_src"].encode("utf-8"))
    return h.hexdigest()[:12]


def render(root: Path) -> dict[str, Any]:
    from . import notes as notes_mod

    notes_mod.unlink_local_generated(root)
    data = collect(root)
    digest = source_hash(data)
    ts = now()
    write_if_changed(root / STATUS_FILE, render_status_md(data, digest))
    write_if_changed(root / ADR_INDEX, render_adr_index(data, adr_hash(data)))
    if adrs_mod.notes_ready(root):
        write_if_changed(root / DECISION_INDEX, render_decision_index(data, decision_hash(data)))
    payload = {
        "rendered_at": iso(ts),
        "source_hash": digest,
        "features": [_strip(f) for f in data["features"]],
        "adrs": [_strip(a) for a in data["adrs"]],
        "decisions": [_strip(a) for a in data["decisions"]],
        "unassigned_raw": _strip(data["unassigned_raw"]),
    }
    atomic_write(root / STATUS_JSON, json.dumps(payload, indent=2, default=str) + "\n")
    from . import vault
    from .dispatch import handoffs as handoffs_mod
    from .notes_writer import writes_shared_notes

    if writes_shared_notes(root):
        handoffs_mod.sync_live_fields(root)
    open_count = sum((1 for f in data["features"] if f["status"] in features_mod.OPEN_STATUSES))
    current_adrs = sum(
        (1 for a in (*data["adrs"], *data["decisions"]) if a["status"] in ("proposed", "accepted"))
    )
    vault.render(root, open_count, current_adrs)
    from .vault import live

    try:
        live.write(root)
    except Exception as exc:
        live.record_failure(root, exc)
        raise AgentError(f"live snapshot {live.path(root)} not written: {exc}") from exc
    (root / RENDER_FAILURE).unlink(missing_ok=True)
    return payload


def render_quietly(root: Path, source: str = "write") -> bool:
    try:
        render(root)
    except Exception as exc:
        record_render_failure(root, source, exc)
        sys.stderr.write(f"note: write done, but re-render failed: {exc}. Run `bin/agent doctor`.\n")
        return False
    return True


def record_render_failure(root: Path, source: str, exc: BaseException) -> None:
    try:
        from .hooks.decision_log import redact

        error = redact(f"{type(exc).__name__}: {exc}")
        row = {"at": iso(now()), "source": source, "error": error}
        atomic_write(root / RENDER_FAILURE, json.dumps(row, indent=2) + "\n")
        from .telemetry import attention as events

        events.emit(root, "status.render", source=source, error=error)
    except Exception:
        pass


def render_failure(root: Path) -> dict[str, Any] | None:
    try:
        return as_dict_or_none(json.loads((root / RENDER_FAILURE).read_text(encoding="utf-8")))
    except OSError, ValueError:
        return None


def _strip(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if not k.startswith("_")}


def _stamped(path: Path) -> str | None:
    try:
        m = HASH_RE.search(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    return m.group(1) if m else None


def _live_state(root: Path) -> str:
    from datetime import datetime
    from .vault import live

    unreadable_before = live.failures_unreadable(root)
    failed_before = live.failures_only(root) is not None
    try:
        snap = as_dict_or_none(json.loads(live.path(root).read_text(encoding="utf-8")))
    except OSError, ValueError:
        snap = None
    if snap is None:
        return "missing or unreadable"
    until = snap.get("valid_until")
    try:
        deadline = datetime.fromisoformat(str(until))
    except ValueError:
        return "no valid_until"
    if deadline.tzinfo is None:
        deadline = deadline.astimezone()
    if deadline <= now():
        return f"expired at {until}"
    if unreadable := (unreadable_before or live.failures_unreadable(root)):
        return f"the failure record cannot be read ({unreadable})"
    if failed_before or live.failures_only(root) is not None:
        return "a later rewrite failed"
    return live.stale_intent(root) or "current"


def _json_generation(path: Path) -> str | None:
    try:
        data = as_dict_or_none(json.loads(path.read_text(encoding="utf-8")))
    except OSError, ValueError:
        return None
    value = data.get("source_hash") if data else None
    return value if isinstance(value, str) else None


def check(root: Path) -> dict[str, Any]:
    data = collect(root)
    expected = source_hash(data)
    views: dict[str, dict[str, Any]] = {}

    def view(name: str, want: str, got: str | None) -> None:
        views[name] = {"fresh": got == want, "expected": want, "found": got}

    status_path = root / STATUS_FILE
    found = _stamped(status_path)
    view(STATUS_FILE, expected, found)
    view(STATUS_JSON.as_posix(), expected, _json_generation(root / STATUS_JSON))
    if data["adrs"] or (root / ADR_INDEX).exists():
        view(ADR_INDEX.as_posix(), adr_hash(data), _stamped(root / ADR_INDEX))
    if adrs_mod.notes_ready(root):
        view(DECISION_INDEX.as_posix(), decision_hash(data), _stamped(root / DECISION_INDEX))
    from .vault import live

    state = _live_state(root)
    views[live.path(root).relative_to(root).as_posix()] = {
        "fresh": state == "current",
        "expected": "a readable snapshot within its valid_until",
        "found": state,
    }
    from . import tab_plugin

    if tab_plugin.outdated(root):
        views[tab_plugin.HEARTBEAT.as_posix()] = {
            "fresh": False,
            "expected": f"plugin protocol {tab_plugin.YIELD_PROTOCOL}",
            "found": tab_plugin.OUTDATED,
        }
    elif problem := tab_plugin.read_problem(root):
        views[tab_plugin.HEARTBEAT.as_posix()] = {
            "fresh": False,
            "expected": "a plugin that reads its sources",
            "found": f"plugin cannot read {problem}",
        }
    stale = [name for name, row in views.items() if not row["fresh"]]
    if not status_path.exists():
        reason: str | None = "STATUS.md missing"
    else:
        reason = f"stale: {', '.join(stale)}" if stale else None
    return {"fresh": not stale, "expected": expected, "found": found, "reason": reason, "views": views}


def render_status_md(data: dict[str, Any], digest: str) -> str:
    open_feats = sorted(
        [f for f in data["features"] if f["status"] in features_mod.OPEN_STATUSES],
        key=lambda f: (ORDER.get(f["status"], 9), f["id"]),
    )
    cutoff = today() - timedelta(days=CLOSED_WINDOW_DAYS)
    closed_feats = sorted(
        [
            f
            for f in data["features"]
            if f["status"] in features_mod.TERMINAL_STATUSES and f["closed"] and (f["closed"] >= cutoff)
        ],
        key=lambda f: (f["closed"], f["id"]),
        reverse=True,
    )
    lines = [
        f"<!-- GENERATED by `agent status render` — do not edit; hash:{digest} -->",
        "# STATUS",
        "",
        f"## Open features ({len(open_feats)})",
        "",
    ]
    if open_feats:
        lines.append("| # | Title | Status | Repos | Tickets | Opened | Last log | Next task |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for f in open_feats:
            lines.append(
                "| "
                + " | ".join(
                    [
                        f"[{f['id']:03d}]({f['dir']}/plan.md)",
                        _cell(f["title"]),
                        _cell(_status_cell(f)),
                        ", ".join(f["repo_shorts"]) or "—",
                        ", ".join(f["tickets"]) or "—",
                        str(f["opened"]),
                        _last_log(f["last_log"]),
                        "— (parked: see the plan's restart note)"
                        if f.get("parked")
                        else _cell(_landed_mark(f.get("landed_unticked")) + _next_cell(f)),
                    ]
                )
                + " |"
            )
    else:
        lines.append("_nothing in flight_")
    lines += ["", f"## Closed in last {CLOSED_WINDOW_DAYS} days ({len(closed_feats)})", ""]
    if closed_feats:
        lines.append("| # | Title | Status | Closed |")
        lines.append("|---|---|---|---|")
        for f in closed_feats:
            lines.append(
                f"| [{f['id']:03d}]({f['dir']}/plan.md) | {_cell(f['title'])} | {f['status']} | {f['closed']} |"
            )
    else:
        lines.append("_none_")
    u = data["unassigned_raw"]
    lines += ["", "## Unassigned raw captures", ""]
    if u["files"]:
        lines.append(
            f"{agent_display('raw', 'unassigned')}: {u['files']} file(s), latest {u['latest']} — the session that produced them adopts them by mapping a feature with the `feature_use` tool."
        )
    else:
        lines.append("_none_")
    lines.append("")
    return "\n".join(lines)


def render_adr_index(data: dict[str, Any], digest: str) -> str:
    lines = [f"<!-- GENERATED by `agent status render` — do not edit; hash:{digest} -->", "# ADRs", ""]
    if not data["adrs"]:
        lines.append("_none yet — create one with the `adr_new` tool, scope `harness`_")
    else:
        lines.append("| ADR | Title | Status | Date |")
        lines.append("|---|---|---|---|")
        for a in data["adrs"]:
            name = Path(a["file"]).name
            lines.append(f"| [{a['id']:04d}]({name}) | {_cell(a['title'])} | {a['status']} | {a['date']} |")
    lines.append("")
    return "\n".join(lines)


def render_decision_index(data: dict[str, Any], digest: str) -> str:
    lines = [
        f"<!-- GENERATED by `agent status render` — do not edit; hash:{digest} -->",
        "# Work decisions",
        "",
    ]
    if not data["decisions"]:
        lines.append("_none yet — create one with the `adr_new` tool_")
    else:
        lines += ["| Decision | Title | Status | Date | Feature |", "|---|---|---|---|---|"]
        for a in data["decisions"]:
            name = Path(a["file"]).name
            feature = f"{a['feature']:03d}" if a["feature"] else "—"
            lines.append(
                f"| [{a['id']:04d}]({name}) | {_cell(a['title'])} | {a['status']} | {a['date']} | {feature} |"
            )
    lines.append("")
    return "\n".join(lines)


READY_TO_CLOSE = "active, 0 open: ready to close"


def _status_cell(f: dict[str, Any]) -> str:
    parked = " (parked)" if f.get("parked") else ""
    if f["status"] != "blocked":
        return str(f["status"]) + parked
    since = as_dict_or_none(f.get("blocked_since"))
    work = [str(w) for w in as_list(f.get("blocked_work"))]
    text = ("blocked? ⚠ active work: " + "; ".join(work) if work else "blocked") + parked
    if since:
        text += f" (blocked since {since.get('ts')}: {since.get('reason')})"
    return text


def _next_cell(f: dict[str, Any]) -> str:
    if f["status"] == "active" and (not f["open_tasks"]) and f["done_tasks"]:
        return READY_TO_CLOSE
    return f["next_task"] or "—"


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _last_log(entry: dict[str, Any] | None) -> str:
    if not entry:
        return "—"
    return f"{entry['ts'][5:]} {entry['kind']}"


def show(root: Path) -> str:
    path = root / STATUS_FILE
    if not path.exists():
        raise AgentError("STATUS.md is missing; run `agent status render`", 1)
    return path.read_text(encoding="utf-8")
