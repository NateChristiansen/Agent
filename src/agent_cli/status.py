"""Generated vault views, ADR indexes and .agent/status.json."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from . import adrs as adrs_mod
from . import blocked as blocked_mod
from . import features as features_mod
from . import frontmatter, maintenance
from . import log as log_mod
from . import repos as repos_mod
from . import requests as requests_mod
from .capture import UNASSIGNED
from .core import (
    GENERATED_VIEWS,
    AgentError,
    agent_path,
    atomic_write,
    coerce_date,
    iso,
    now,
    write_if_changed,
)
from .records.narrow import as_dict_or_none, as_list, dig, str_list
from .tickets import tickets_of

STATUS_JSON = agent_path(Path(), "status.json")
RENDER_FAILURE = agent_path(Path(), "render-failure.json")  # the last failed render, until one succeeds
ADR_INDEX = GENERATED_VIEWS["adrs"]
DECISION_INDEX = Path("decisions") / "README.md"
HASH_RE = re.compile(r"hash:([0-9a-f]{12})")


def collect(root: Path) -> dict[str, Any]:
    """Gather everything the generated files are built from."""
    registry = None
    try:
        registry = repos_mod.load_registry(root)
    except AgentError:
        registry = None
    feats: list[dict[str, Any]] = []
    # Requests share the store but not the feature views: they go to `requests`, which feature
    # counts, cards and the session digest's feature list never read.
    reqs: list[dict[str, Any]] = []
    parked = set(maintenance.parked(root))
    dispatches = _dispatches(root)
    landed = _landed_tasks(root, dispatches)
    for f in features_mod.load_all(root):
        last = log_mod.last_entry(f) if f.log_path.exists() else None
        tasks = f.tasks()
        task_items = features_mod.list_tasks(f.body)
        open_indexes = {t.index for t in task_items if t.is_open}
        landed_open = sorted(landed.get(f.id, set()) & open_indexes)
        # Flag work since a blocked status and show its logged time and reason. Keep status changes
        # an owner choice.
        blocked_work = blocked_mod.active_work(root, f, dispatches) if f.status == "blocked" else []
        since = blocked_mod.since(f) if f.status == "blocked" else None
        shorts: list[str] = []
        for name in str_list(f.meta.get("repos")):
            repo = registry.find(name) if registry else None
            shorts.append(repo.short if repo else name)
        entry: dict[str, Any] = {
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
            "blocker": since._asdict() if since else None,
            "blocked_work": blocked_work,
            "_hash_src": (
                frontmatter.join(f.meta, "")
                + (last.ts + last.kind if last else "")
                + "|".join(tasks["open_items"])
                + json.dumps([(t.index, t.state, t.text) for t in task_items])
                + f"|open={tasks['open']}|done={tasks['done']}|parked={f'{f.id:03d}' in parked}"
                + f"|landed={landed_open}"
                + (f"|blocked={since}|work={blocked_work}" if f.status == "blocked" else "")
            ),
        }
        if f.is_request:
            view = requests_mod.row(root, f)
            entry.update({k: view[k] for k in ("label", "category", "ask", "has_result", "next_action")})
            entry["record_kind"] = features_mod.REQUEST_KIND
            entry["_hash_src"] += json.dumps(view, default=str)
            reqs.append(entry)
        else:
            feats.append(entry)
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
    return {
        "features": feats,
        "requests": reqs,
        "adrs": adr_list,
        "decisions": decisions,
        "unassigned_raw": unassigned,
    }


def _dispatches(root: Path) -> list[dict[str, Any]]:
    from .dispatch import state as dispatch_state

    try:
        return dispatch_state.load_all(root)
    except AgentError, OSError:
        return []


def _landed_tasks(root: Path, records: list[dict[str, Any]] | None = None) -> dict[int, set[int]]:
    """Feature id -> the task indexes of its harness dispatches that have landed on main. A
    landing is not acceptance, so nothing ticks them; Dashboard marks the ones still open, so "how
    close are we" shows work that landed and waits on a tick."""
    out: dict[int, set[int]] = {}
    for d in _dispatches(root) if records is None else records:
        feature = d.get("feature")
        if dig(d, "integration", "state") != "landed" or not isinstance(feature, int):
            continue
        out.setdefault(feature, set()).update(t for t in as_list(d.get("tasks")) if isinstance(t, int))
    return out


def source_hash(data: dict[str, Any]) -> str:
    h = hashlib.sha256()
    for f in data["features"]:
        h.update(f["_hash_src"].encode("utf-8"))
    for a in data["adrs"]:
        h.update(a["_hash_src"].encode("utf-8"))
    for a in data["decisions"]:
        h.update(a["_hash_src"].encode("utf-8"))
    h.update("|".join(data["unassigned_raw"].get("_names") or []).encode("utf-8"))
    for r in data["requests"]:
        h.update(r["_hash_src"].encode("utf-8"))
    return h.hexdigest()[:12]


def adr_hash(data: dict[str, Any]) -> str:
    """Hash only public ADR inputs. Feature plans and logs are private (see ADR-0012) and must not
    churn the public index or make its digest irreproducible.
    """
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
    """Regenerate every derived file. Idempotent: a file is only written when its content
    changes. The markdown carries no wall-clock stamp, so re-rendering never churns git.
    The render time lives in .agent/status.json (gitignored)."""
    from . import notes as notes_mod

    # a symlinked root file is invisible to Obsidian's watcher, which then saves its stale copy
    # back over this render; write plain files instead
    notes_mod.unlink_local_generated(root)
    (root / GENERATED_VIEWS["status"]).unlink(missing_ok=True)
    data = collect(root)
    digest = source_hash(data)
    ts = now()
    write_if_changed(root / ADR_INDEX, render_adr_index(data, adr_hash(data)))
    if adrs_mod.notes_ready(root):
        write_if_changed(root / DECISION_INDEX, render_decision_index(data, decision_hash(data)))
    payload = {
        "rendered_at": iso(ts),
        "source_hash": digest,
        "features": [_strip(f) for f in data["features"]],
        "requests": [_strip(r) for r in data["requests"]],
        "adrs": [_strip(a) for a in data["adrs"]],
        "decisions": [_strip(a) for a in data["decisions"]],
        "unassigned_raw": _strip(data["unassigned_raw"]),
    }
    atomic_write(root / STATUS_JSON, json.dumps(payload, indent=2, default=str) + "\n")
    from . import vault
    from .dispatch import handoffs as handoffs_mod

    # Before the Bases: the Workers view filters on `live`, which an ack, an abandon or a
    # feature closing changes without rewriting the handoff. Only the checkout
    # that owns the shared notes writes them (worktree and probe renders churned every handoff).
    from .notes_writer import writes_shared_notes

    if writes_shared_notes(root):
        handoffs_mod.sync_live_fields(root)
    open_count = sum(1 for f in data["features"] if f["status"] in features_mod.OPEN_STATUSES)
    current_adrs = sum(
        1 for a in (*data["adrs"], *data["decisions"]) if a["status"] in ("proposed", "accepted")
    )
    vault.render(root, open_count, current_adrs)
    from .vault import live

    # an ack, an abandon or a feature closing changes who waits on the owner. The
    # snapshot is a required view: its failure fails the render, after every other view was
    # written, so the failure record stands and the caller hears it (`write_quietly` swallowed it and the
    # record of an earlier failure was deleted).
    try:
        live.write(root)
    except Exception as exc:
        live.record_failure(root, exc)
        raise AgentError(f"live snapshot {live.path(root)} not written: {exc}") from exc
    (root / RENDER_FAILURE).unlink(missing_ok=True)  # every view is current again
    return payload


def render_quietly(root: Path, source: str = "write") -> bool:
    """Render views after a write; report failure without making an already-persisted write look
    failed. A retry could duplicate that write. Return whether rendering succeeds and retain
    failures for doctor because hook stderr may be unseen.
    """
    try:
        render(root)
    except Exception as exc:  # noqa: BLE001
        record_render_failure(root, source, exc)
        sys.stderr.write(f"note: write done, but re-render failed: {exc}. Run `bin/agent doctor`.\n")
        return False
    return True


def record_render_failure(root: Path, source: str, exc: BaseException) -> None:
    """Keep the last failed render for doctor and telemetry. A successful render clears the standing
    failure; telemetry retains history. Never raises.
    """
    try:
        from .hooks.decision_log import redact

        error = redact(f"{type(exc).__name__}: {exc}")
        row = {"at": iso(now()), "source": source, "error": error}
        atomic_write(root / RENDER_FAILURE, json.dumps(row, indent=2) + "\n")
        from .telemetry import attention as events

        events.emit(root, "status.render", source=source, error=error)
    except Exception:  # noqa: BLE001, S110 — keeping the failure never raises in its place
        pass


def render_failure(root: Path) -> dict[str, Any] | None:
    """The last render failure when no render has succeeded since, else None."""
    try:
        return as_dict_or_none(json.loads((root / RENDER_FAILURE).read_text(encoding="utf-8")))
    except OSError, ValueError:
        return None


def _strip(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if not k.startswith("_")}


def _stamped(path: Path) -> str | None:
    """The `hash:` a generated markdown file carries, or None when it is missing or has none."""
    try:
        m = HASH_RE.search(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    return m.group(1) if m else None


def _live_state(root: Path) -> str:
    """`current`, or why the snapshot is not: `missing or unreadable`, `no valid_until`,
    `expired at <valid_until>`, `a later rewrite failed`, or that a write did not finish
    (`live.stale_intent`). A snapshot past its `valid_until`
    is a stale view, as every surface shows it, and so is
    one a rewrite failed after: the facts moved on without it, whatever its deadline says
    (`live.write_failed`)."""
    from datetime import datetime

    from .vault import live

    # the failure record is read before the bytes as well as after: a
    # success that published and cleared it between the two would make older bytes current
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
    # every good write clears the record, so one standing is newer than this snapshot; a record
    # folder that cannot be listed stands too, with its reason
    if unreadable := unreadable_before or live.failures_unreadable(root):
        return f"the failure record cannot be read ({unreadable})"
    if failed_before or live.failures_only(root) is not None:
        return "a later rewrite failed"
    # an intent: a write in flight stays current, so status does not flicker; one older
    # than `live.INTENT_STALE_S` with nobody publishing is a writer that died before publishing
    return live.stale_intent(root) or "current"


def _json_generation(path: Path) -> str | None:
    try:
        data = as_dict_or_none(json.loads(path.read_text(encoding="utf-8")))
    except OSError, ValueError:
        return None
    value = data.get("source_hash") if data else None
    return value if isinstance(value, str) else None


def check(root: Path) -> dict[str, Any]:
    """Check snapshot and index generations, and require readable, unexpired live state.

    `fresh` is false if any view is stale; `expected` and `found` describe status.json.
    """
    data = collect(root)
    expected = source_hash(data)
    views: dict[str, dict[str, Any]] = {}

    def view(name: str, want: str, got: str | None) -> None:
        views[name] = {"fresh": got == want, "expected": want, "found": got}

    found = _json_generation(root / STATUS_JSON)
    view(STATUS_JSON.as_posix(), expected, found)
    if data["adrs"] or (root / ADR_INDEX).exists():
        view(ADR_INDEX.as_posix(), adr_hash(data), _stamped(root / ADR_INDEX))
    if adrs_mod.notes_ready(root):
        view(DECISION_INDEX.as_posix(), decision_hash(data), _stamped(root / DECISION_INDEX))
    # The live snapshot has no source digest (it is rebuilt from dispatch and session state on
    # every write, and carries its own `valid_until`), so it is checked for being there, readable
    # and within that deadline: a missing, unreadable or expired `live.json` is a stale view.
    from .vault import live

    state = _live_state(root)
    views[live.path(root).relative_to(root).as_posix()] = {
        "fresh": state == "current",
        "expected": "a readable snapshot within its valid_until",
        "found": state,
    }
    # A running plugin build that cannot yield may repaint a stale snapshot over the fallback:
    # a view that may be wrong, until `agent setup` updates it.
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
    reason = f"stale: {', '.join(stale)}" if stale else None
    return {
        "fresh": not stale,
        "expected": expected,
        "found": found,
        "reason": reason,
        "views": views,
    }


def render_adr_index(data: dict[str, Any], digest: str) -> str:
    lines = [
        f"<!-- GENERATED by `agent status render` — do not edit; hash:{digest} -->",
        "# ADRs",
        "",
    ]
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


def _cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")
