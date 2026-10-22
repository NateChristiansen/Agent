"""The reconciliation ledger: what a feature still owes a decision on, kept as CLI-written
`decision` entries in its notes-synced, union-merged `log.md`.

An entry is two lines. The first is the canonical header `reconcile <verb> <subject>: <summary>`;
the second is one JSON object, the only part read. Verbs: `source` (a result that raises items),
`decide` (dispositions for items of one source), `capture` (intent to add a task for an item) and
`approve` (lifts the launch hold on a captured task). Only `append(..., reserved=True)` can write a
reserved header, and `head` is the one test both the writer guard and the parser use.

The fold reads the entries as a set: duplicates collapse by `eid`, sources by (source, G), the
highest version is current, and two generations at one version fail closed under a
`conflict:` pseudo-generation. Decisions form heads through `prev`; one head, or several equal
ones, is effective. Nothing here depends on log order or timestamps except a subagent source's
own version.

Sources register only from persisted intents (`reconcile_pending` on a dispatch record or land
journal, the journal's `obligations.reconcile`). Old history has neither, so it never becomes
owed; `ledger` heals pending intents before folding.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import features as features_mod
from .core import AgentError, iso, now
from .records.narrow import as_dict, as_dict_or_none, as_list, as_list_or_none

RESERVED = "reconcile "
VERBS = ("source", "decide", "capture", "approve")
SOURCE_KINDS = ("dispatch", "accept", "landing", "subagent", "manual")
DISPOSITIONS = ("task", "new-task", "no-task", "waiting", "not-accepted")
PENDING = "pending"
OBLIGATION = "reconcile"
INTENT = "reconcile_pending"
# Dispatch states whose result raises items; questions, probes and owner-closed ones do not.
RAISING_STATES = ("done", "blocked", "failed")

_HEADER = re.compile(r"^reconcile (" + "|".join(VERBS) + r") (\S+?):(?: .*)?$")
_SOURCE = re.compile(r"^(" + "|".join(SOURCE_KINDS) + r"):\S+$")
_EID = re.compile(r"^[0-9a-f]{32}$")
_WAITS_ON = re.compile(r"^(owner|external|manager|feature:\d{3}|dispatch:\S+)$")
_CAP = re.compile(r"\bcap ([0-9a-f]{12})\b")
_TASK_ITEM = re.compile(r"^task#(\d+)$")


def head(text: str) -> str:
    return text.lstrip().split("\n", 1)[0].strip().casefold()


def is_reserved(text: str) -> bool:
    return head(text).startswith(RESERVED)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def cap_marker(eid: str) -> str:
    return f"cap {eid[:12]}"


# ---------------------------------------------------------------- source builders


def dispatch_generation(d: dict[str, Any]) -> str:
    from .records.dispatch import result_of

    return f"{d['attempt']}:" + _sha(_canonical(result_of(d)))


def dispatch_source(d: dict[str, Any]) -> dict[str, Any] | None:
    """The `dispatch:<id>` intent for a finished attempt, or None when its result raises nothing."""
    from .records.dispatch import result_of

    if d.get("kind") == "probe" or d.get("state") not in RAISING_STATES:
        return None
    findings = as_list(result_of(d).get("findings"))
    return {
        "feature": d["feature"],
        "source": f"dispatch:{d['id']}",
        "G": dispatch_generation(d),
        "ver": [int(d["attempt"])],
        "items": ["result", *(f"F{i}" for i in range(1, len(findings) + 1))],
    }


def accept_source(
    d: dict[str, Any], journal_id: str | None, *, feature: features_mod.Feature
) -> dict[str, Any] | None:
    """The `accept:<id>` intent for a landing (`journal_id`) or a done result with no diff
    (None): one `task#n` item per listed task still open in `feature`, or None when there is none."""
    open_now = {t.index for t in features_mod.list_tasks(feature.body) if t.is_open}
    listed = [n for n in as_list(d.get("tasks")) if isinstance(n, int) and n in open_now]
    if not listed:
        return None
    return {
        "feature": d["feature"],
        "source": f"accept:{d['id']}",
        "G": f"{dispatch_generation(d)}@{journal_id or 'nodiff'}",
        "ver": [int(d["attempt"]), journal_id or ""],
        "items": [f"task#{n}" for n in sorted(set(listed))],
    }


def landing_source(journal_id: str, feature: int) -> dict[str, Any]:
    """The `landing:<journal>` intent for a landed branch that has no dispatch record."""
    return {
        "feature": feature,
        "source": f"landing:{journal_id}",
        "G": journal_id,
        "ver": [],
        "items": ["landing"],
    }


def subagent_source(
    feature: int, agent_id: str, message: str | None, session: str | None = None
) -> dict[str, Any]:
    """The `subagent:<agent_id>` source for one SubagentStop in `session`. Its version is its
    entry's own (`at`, `eid`); without a message, G is the agent id alone."""
    return {
        "feature": feature,
        "source": f"subagent:{agent_id}",
        "G": _sha(message) if message is not None else agent_id,
        "ver": [],
        "items": ["result"],
        **({"session": session} if session else {}),
    }


# ---------------------------------------------------------------- records


@dataclass(frozen=True)
class Rec:
    verb: str
    eid: str
    at: str
    data: dict[str, Any]
    ts: str


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
        raise ValueError(f"{what} must be a non-empty single line")
    return value


def _check_decision(row: dict[str, Any]) -> None:
    _text(row.get("item"), "item")
    disposition = row.get("disposition")
    if disposition not in DISPOSITIONS:
        raise ValueError(f"disposition {disposition!r}")
    prev = as_list_or_none(row.get("prev"))
    if prev is None or not all(isinstance(p, str) and _EID.match(p) for p in prev):
        raise ValueError("prev must be a list of eids")
    if disposition == "task":
        if type(row.get("task")) is not int or row["task"] < 1:
            raise ValueError("task disposition needs a task number")
    elif disposition == "new-task":
        if not isinstance(row.get("capture"), str) or not _EID.match(row["capture"]):
            raise ValueError("new-task needs its capture eid")
        if type(row.get("task")) is not int or row["task"] < 1:
            raise ValueError("new-task needs the captured task number")
    elif disposition in ("no-task", "not-accepted"):
        _text(row.get("reason"), "reason")
        if disposition == "not-accepted" and not _TASK_ITEM.match(str(row["item"])):
            raise ValueError("not-accepted applies to task#n items only")
    elif disposition == "waiting":
        if not _WAITS_ON.match(str(row.get("on"))):
            raise ValueError("waiting needs on: owner, external, manager, feature:NNN or dispatch:<id>")
        _text(row.get("until"), "until")


def _check(verb: str, subject: str, data: dict[str, Any]) -> None:
    if not isinstance(data.get("eid"), str) or not _EID.match(data["eid"]):
        raise ValueError("eid")
    at = data.get("at")
    if not isinstance(at, str) or datetime.fromisoformat(at).tzinfo is None:
        raise ValueError("at must be an ISO time with a zone")
    if verb == "approve":
        if data.get("of") != subject or not _EID.match(str(subject)):
            raise ValueError("approve names its capture eid")
        _text(data.get("evidence"), "evidence")
        return
    if data.get("source") != subject or not _SOURCE.match(subject):
        raise ValueError("subject does not match the source")
    _text(data.get("G"), "G")
    if verb == "source":
        ver = as_list_or_none(data.get("ver"))
        if ver is None or not all(isinstance(v, (int, str)) and not isinstance(v, bool) for v in ver):
            raise ValueError("ver")
        items = as_list_or_none(data.get("items"))
        if not items or len(set(map(str, items))) != len(items):
            raise ValueError("items")
        for item in items:
            _text(item, "item")
    elif verb == "capture":
        _text(data.get("item"), "item")
        _text(data.get("text"), "text")
    else:
        rows = as_list_or_none(data.get("decisions"))
        if not rows:
            raise ValueError("decisions")
        seen: set[str] = set()
        for value in rows:
            narrowed = as_dict_or_none(value)
            if narrowed is None:
                raise ValueError("decision row")
            row: dict[str, Any] = dict(narrowed)
            _check_decision(row)
            if str(row["item"]) in seen:
                raise ValueError("one decision per item per entry")
            seen.add(str(row["item"]))


def parse_entry(entry: Any) -> Rec | dict[str, Any] | None:
    """A `Rec`, a malformed-entry report, or None for an entry that is not reserved."""
    text: str = entry.text
    if not is_reserved(text):
        return None
    first = text.split("\n", 1)[0]
    bad = {"ts": entry.ts, "kind": entry.kind, "head": first[:120]}
    if entry.kind != "decision":
        return {**bad, "why": "reserved text outside a decision entry"}
    lines = text.split("\n")
    m = _HEADER.match(first)
    if m is None:
        return {**bad, "why": "non-canonical header"}
    if len(lines) != 2:
        return {**bad, "why": "an entry is the header and exactly one JSON line"}
    try:
        loaded = as_dict_or_none(json.loads(lines[1]))
        if loaded is None:
            raise ValueError("not an object")
        data: dict[str, Any] = dict(loaded)
        _check(m.group(1), m.group(2), data)
    except (ValueError, TypeError) as exc:
        return {**bad, "why": f"invalid JSON line: {exc}"}
    return Rec(verb=m.group(1), eid=str(data["eid"]), at=str(data["at"]), data=data, ts=entry.ts)


# ---------------------------------------------------------------- fold


@dataclass
class Item:
    source: str
    G: str
    item: str
    state: str  # owed | waiting | settled
    reason: str = ""
    heads: list[str] = field(default_factory=list[str])
    decision: dict[str, Any] | None = None
    conflict: list[str] = field(default_factory=list[str])

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"source": self.source, "G": self.G, "item": self.item, "state": self.state}
        if self.reason:
            out["reason"] = self.reason
        if self.decision is not None:
            out["decision"] = self.decision
        if self.conflict:
            out["conflict"] = self.conflict
        return out


@dataclass
class Ledger:
    on: bool
    items: list[Item]
    malformed: list[dict[str, Any]]
    holds: dict[int, str]
    captures: dict[str, dict[str, Any]]
    approvals: dict[str, str]
    # The session a source's current generation records, when exactly one does.
    sessions: dict[str, str] = field(default_factory=dict[str, str])

    @property
    def owed(self) -> list[Item]:
        return [i for i in self.items if i.state == "owed"]

    @property
    def waiting(self) -> list[Item]:
        return [i for i in self.items if i.state == "waiting"]

    @property
    def conflicts(self) -> list[Item]:
        return [i for i in self.items if i.conflict and i.state == "owed"]

    def item(self, source: str, item: str) -> Item | None:
        return next((i for i in self.items if i.source == source and i.item == item), None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "on": self.on,
            "owed": [i.as_dict() for i in self.owed],
            "waiting": [i.as_dict() for i in self.waiting],
            "conflicts": [i.as_dict() for i in self.conflicts],
            "malformed": self.malformed,
            "holds": [{"task": n, "cap": c} for n, c in sorted(self.holds.items())],
        }


def _version(rec: Rec) -> tuple[tuple[int, Any], ...]:
    if rec.data["source"].startswith("subagent:"):
        at = datetime.fromisoformat(rec.at).astimezone(UTC).isoformat()
        return ((1, at), (1, rec.eid))
    return tuple((0, v) if isinstance(v, int) else (1, v) for v in rec.data["ver"])


def _dedupe(recs: list[Rec], malformed: list[dict[str, Any]]) -> list[Rec]:
    by_eid: dict[str, set[str]] = {}
    first: dict[str, Rec] = {}
    for rec in recs:
        by_eid.setdefault(rec.eid, set()).add(rec.verb + _canonical(rec.data))
        first.setdefault(rec.eid, rec)
    out: list[Rec] = []
    for eid in sorted(by_eid):
        if len(by_eid[eid]) == 1:
            out.append(first[eid])
        else:
            malformed.append({"eid": eid, "why": "one eid with different contents"})
    return out


def fold(entries: list[Any], tasks: list[features_mod.Task]) -> Ledger:
    """The ledger as a pure function of the set of log entries and the plan's tasks."""
    recs: list[Rec] = []
    malformed: list[dict[str, Any]] = []
    for entry in entries:
        parsed = parse_entry(entry)
        if isinstance(parsed, Rec):
            recs.append(parsed)
        elif parsed is not None:
            malformed.append(parsed)
    recs = _dedupe(recs, malformed)
    malformed.sort(key=_canonical)

    generations: dict[str, dict[str, tuple[tuple[Any, ...], set[str]]]] = {}
    recorded: dict[tuple[str, str], set[str]] = {}
    for rec in (r for r in recs if r.verb == "source"):
        gens = generations.setdefault(rec.data["source"], {})
        ver, items = gens.get(rec.data["G"], ((), set[str]()))
        gens[rec.data["G"]] = (max(ver, _version(rec)), items | set(rec.data["items"]))
        if isinstance(rec.data.get("session"), str) and rec.data["session"]:
            recorded.setdefault((rec.data["source"], rec.data["G"]), set()).add(rec.data["session"])

    current: dict[str, tuple[str, list[str], list[str]]] = {}
    sessions: dict[str, str] = {}
    for source, gens in generations.items():
        top = max(v for v, _ in gens.values())
        at_top = sorted(g for g, (v, _) in gens.items() if v == top)
        items = sorted(set[str]().union(*(gens[g][1] for g in at_top)))
        if len(at_top) == 1:
            current[source] = (at_top[0], items, [])
            if len(named := recorded.get((source, at_top[0]), set())) == 1:
                sessions[source] = next(iter(named))
        else:
            current[source] = ("conflict:" + _sha("\n".join(at_top)), items, at_top)

    groups: dict[tuple[str, str], list[tuple[str, dict[str, Any]]]] = {}
    for rec in (r for r in recs if r.verb == "decide"):
        source = rec.data["source"]
        if source not in current or current[source][0] != rec.data["G"]:
            continue  # a decision for another generation is history
        for row in rec.data["decisions"]:
            groups.setdefault((source, row["item"]), []).append((rec.eid, row))

    captures = {r.eid: r.data for r in recs if r.verb == "capture"}
    approvals = {r.data["of"]: r.eid for r in recs if r.verb == "approve"}
    by_index = {t.index: t for t in tasks}

    items_out: list[Item] = []
    for source in sorted(current):
        gen, items, conflict = current[source]
        for name in items:
            item = Item(source, gen, name, "owed", conflict=conflict)
            if conflict:
                item.reason = "generations conflict at one version"
            rows = groups.get((source, name), [])
            prevs = {p for _, row in rows for p in row["prev"]}
            heads = sorted((eid, row) for eid, row in rows if eid not in prevs)
            item.heads = [eid for eid, _ in heads]
            payloads = {_canonical({k: v for k, v in row.items() if k != "prev"}) for _, row in heads}
            task_match = _TASK_ITEM.match(name)
            ticked = (
                task_match is not None
                and (t := by_index.get(int(task_match.group(1)))) is not None
                and t.done
            )
            if ticked:
                item.state, item.reason = "settled", "task ticked"
            elif len(payloads) > 1:
                item.reason = "decisions disagree; decide again to replace every head"
            elif rows and not heads:
                item.reason = "decisions replace each other in a cycle"
            elif heads:
                row = heads[0][1]
                item.decision = {k: v for k, v in row.items() if k != "prev"}
                item.state = "waiting" if row["disposition"] == "waiting" else "settled"
                item.reason = ""
            items_out.append(item)

    _check_captures(items_out, captures, by_index)

    approved = {eid[:12] for eid in approvals}
    holds: dict[int, str] = {}
    for task in tasks:
        if task.is_open:
            for cap in _CAP.findall(task.text):
                if cap not in approved:
                    holds[task.index] = cap
    return Ledger(bool(current), items_out, malformed, holds, captures, approvals, sessions)


def _check_captures(
    items: list[Item], captures: dict[str, dict[str, Any]], by_index: dict[int, features_mod.Task]
) -> None:
    """A `new-task` decision settles only when its task carries the capture's marker and no other
    capture with different text claims the same task number."""
    claims: dict[int, set[tuple[str, str]]] = {}
    for item in items:
        d = item.decision
        if d is not None and d["disposition"] == "new-task":
            text = str(captures.get(d["capture"], {}).get("text", ""))
            claims.setdefault(d["task"], set()).add((d["capture"], text))
    for item in items:
        d = item.decision
        if d is None or d["disposition"] != "new-task" or item.state != "settled":
            continue
        task = by_index.get(d["task"])
        if len({text for _, text in claims[d["task"]]}) > 1:
            item.state, item.reason = "owed", f"captures with different text claim task #{d['task']}"
        elif d["capture"] not in captures:
            item.state, item.reason = "owed", "the capture entry is missing"
        elif task is None or cap_marker(d["capture"]) not in task.text:
            item.state, item.reason = "owed", f"task #{d['task']} does not carry its capture marker"


# ---------------------------------------------------------------- reading


def _feature(root: Path, feature: features_mod.Feature | str | int) -> features_mod.Feature:
    ref = feature.number if isinstance(feature, features_mod.Feature) else str(feature)
    return features_mod.resolve(root, ref)


def _fold_feature(feature: features_mod.Feature) -> Ledger:
    from . import log as log_mod

    entries = log_mod.parse(feature.log_path) if feature.log_path.exists() else []
    return fold(entries, features_mod.list_tasks(feature.body))


def ledger(root: Path, feature: features_mod.Feature | str | int, *, heal: bool = True) -> Ledger:
    """The feature's ledger, re-read from disk. With `heal`, pending intents on this machine's
    dispatch records and land journals are appended and cleared first."""
    f = _feature(root, feature)
    if heal:
        heal_intents(root, f)
    return _fold_feature(f)


# ---------------------------------------------------------------- writing


def _write(
    feature: features_mod.Feature, verb: str, subject: str, summary: str, data: dict[str, Any]
) -> dict[str, Any]:
    from . import log as log_mod

    data = {**data, "eid": data.get("eid") or uuid.uuid4().hex, "at": iso(now())}
    _check(verb, subject, data)
    line = " ".join(" ".join(summary.splitlines()).split()) or verb
    text = f"reconcile {verb} {subject}: {line}\n{json.dumps(data, sort_keys=True)}"
    log_mod.append(feature, "decision", text, reserved=True)
    return data


def _registered(feature: features_mod.Feature) -> set[tuple[str, str]]:
    from . import log as log_mod

    if not feature.log_path.exists():
        return set()
    out: set[tuple[str, str]] = set()
    for entry in log_mod.parse(feature.log_path):
        rec = parse_entry(entry)
        if isinstance(rec, Rec) and rec.verb == "source":
            out.add((rec.data["source"], rec.data["G"]))
    return out


def append_source(feature: features_mod.Feature, pending: dict[str, Any]) -> bool:
    """Append a source intent unless its (source, G) is already in the log. False when it was."""
    if (pending["source"], pending["G"]) in _registered(feature):
        return False
    data = {k: pending[k] for k in ("source", "G", "ver", "items")}
    if isinstance(pending.get("session"), str) and pending["session"]:
        data["session"] = pending["session"]
    _write(feature, "source", pending["source"], f"{len(pending['items'])} item(s) to reconcile", data)
    return True


def heal_intents(root: Path, feature: features_mod.Feature) -> list[str]:
    """Append each pending source intent for `feature` once, then clear it. Returns the sources
    appended. A crash between append and clear leaves a present source, which the next heal only
    clears; a concurrent duplicate collapses in the fold."""
    appended: list[str] = []
    appended += _heal_dispatches(root, feature)
    appended += _heal_journals(root, feature)
    return appended


def _mine(row: Any, feature: features_mod.Feature, default: Any) -> bool:
    return as_dict(row).get("feature", default) == feature.id


def _heal_dispatches(root: Path, feature: features_mod.Feature) -> list[str]:
    from .dispatch import state

    appended: list[str] = []
    for d in state.for_feature(root, feature):
        pending = [as_dict(p) for p in as_list(d.get(INTENT))]
        if not pending:
            continue
        for p in pending:
            if append_source(feature, p):
                appended.append(str(p["source"]))
        for _ in range(3):
            current = state.load(root, d["id"])
            left = [
                p
                for p in as_list(current.get(INTENT))
                if _canonical(p) not in {_canonical(q) for q in pending}
            ]
            if left:
                current[INTENT] = left
            else:
                current.pop(INTENT, None)
            try:
                state.save(root, current)
                break
            except AgentError:
                continue  # a stale revision: reload and clear again
    return appended


def _heal_journals(root: Path, feature: features_mod.Feature) -> list[str]:
    from . import land_journal as jn
    from .land import main_of

    try:
        main = main_of(root)
        journals = jn.every(main)
    except AgentError, OSError:
        return []
    appended: list[str] = []
    owing = [
        j
        for j in journals
        if j.get("state") == "done" and as_dict(j.get("obligations")).get(OBLIGATION) == PENDING
    ]
    for journal in owing:
        mine = [as_dict(p) for p in as_list(journal.get(INTENT)) if _mine(p, feature, None)]
        for p in mine:
            if append_source(feature, p):
                appended.append(str(p["source"]))
        _settle_journal(root, main, journal["id"])
    return appended


def _settle_journal(root: Path, main: Path, journal_id: str) -> None:
    """Mark the journal's `reconcile` obligation done once every feature's log holds its sources.
    Skipped while a landing holds the land lock; the next heal tries again."""
    from . import land_journal as jn

    try:
        with jn.flock(main):
            journal = jn.read(main, journal_id)
            obligations = as_dict(journal.get("obligations"))
            if obligations.get(OBLIGATION) != PENDING:
                return
            registered: dict[int, set[tuple[str, str]]] = {}
            for p in (as_dict(p) for p in as_list(journal.get(INTENT))):
                fid = p.get("feature")
                if not isinstance(fid, int):
                    return
                if fid not in registered:
                    registered[fid] = _registered(_feature(root, fid))
                if (p.get("source"), p.get("G")) not in registered[fid]:
                    return
            journal["obligations"] = {**obligations, OBLIGATION: "done"}
            jn.write(main, journal)
    except AgentError, OSError:
        return


def _require_current(led: Ledger, source: str, item: str) -> Item:
    found = led.item(source, item)
    if found is None:
        raise AgentError(f"{source} has no current item {item!r}; list the ledger with the `reconcile` tool")
    return found


def decide(
    root: Path, feature: features_mod.Feature | str | int, source: str, rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """One `decide` entry for items of one source, against the current fold. `prev` is filled
    with each item's current heads, so the write replaces every head it saw."""
    f = _feature(root, feature)
    led = ledger(root, f)
    out_rows: list[dict[str, Any]] = []
    gen = ""
    for row in rows:
        item = _require_current(led, source, str(row.get("item")))
        gen = item.G
        clean = {k: v for k, v in row.items() if v is not None and k not in ("source", "prev")}
        if clean.get("disposition") == "new-task":
            raise AgentError("new-task goes through capture: give `text` and the tool captures the task")
        if clean.get("disposition") == "task" and clean.get("task") not in {
            t.index for t in features_mod.list_tasks(f.body)
        }:
            raise AgentError(f"no task #{clean.get('task')} in {f.number}")
        out_rows.append({**clean, "prev": item.heads})
    if not out_rows:
        raise AgentError("no decisions to write")
    summary = ", ".join(f"{r['item']} {r['disposition']}" for r in out_rows)
    try:
        return _write(f, "decide", source, summary, {"source": source, "G": gen, "decisions": out_rows})
    except ValueError as exc:
        raise AgentError(f"invalid decision: {exc}") from None


def capture(
    root: Path, feature: features_mod.Feature | str | int, *, source: str, item: str, text: str
) -> dict[str, Any]:
    """Capture a new task for one item: the `capture` entry, then the task (unless one already
    carries `cap <eid12>`), then the `new-task` decision. Each step is idempotent, so a rerun after
    a crash resumes where it stopped and creates exactly one task."""
    f = _feature(root, feature)
    led = ledger(root, f)
    found = _require_current(led, source, item)
    if (
        found.decision is not None
        and found.decision.get("disposition") == "new-task"
        and found.state == "settled"
    ):
        return {"capture": found.decision["capture"], "task": found.decision["task"], "changed": False}
    mine = sorted(
        eid
        for eid, c in led.captures.items()
        if c["source"] == source and c["G"] == found.G and c["item"] == item
    )
    if mine:
        eid = mine[0]
    else:
        features_mod.one_line(text, "a captured task")
        try:
            eid = _write(
                f,
                "capture",
                source,
                f"{item}: {text}",
                {"source": source, "G": found.G, "item": item, "text": text},
            )["eid"]
        except ValueError as exc:
            raise AgentError(f"invalid capture: {exc}") from None
    task_text = str(led.captures[eid]["text"]) if eid in led.captures else text
    f = _feature(root, f)
    marker = cap_marker(eid)
    index = next((t.index for t in features_mod.list_tasks(f.body) if marker in t.text), None)
    if index is None:
        f.body, index = features_mod.add_task_at(
            f.body, features_mod.with_note(task_text, f"captured: {source} {item}, {marker}")
        )
        f.save()
    row = {"item": item, "disposition": "new-task", "capture": eid, "task": index, "prev": found.heads}
    _write(
        f, "decide", source, f"{item} new-task #{index}", {"source": source, "G": found.G, "decisions": [row]}
    )
    return {"capture": eid, "task": index, "changed": True}


def approve(
    root: Path, feature: features_mod.Feature | str | int, *, of: str, evidence: str
) -> dict[str, Any]:
    """Lift the launch hold on a captured task. `of` is the capture eid or its 12-character prefix.
    The manager can write this itself: an audited speed bump, not owner authorization."""
    f = _feature(root, feature)
    led = ledger(root, f, heal=False)
    hits = [eid for eid in led.captures if eid.startswith(of.strip())] if len(of.strip()) >= 12 else []
    if len(hits) != 1:
        raise AgentError(f"{of!r} names no single capture; give the capture eid or its 12-character prefix")
    eid = hits[0]
    if eid in led.approvals:
        return {"of": eid, "approve": led.approvals[eid], "changed": False}
    try:
        data = _write(f, "approve", eid, evidence, {"of": eid, "evidence": evidence})
    except ValueError as exc:
        raise AgentError(f"invalid approval: {exc}") from None
    return {"of": eid, "approve": data["eid"], "changed": True}


def add_manual(root: Path, feature: features_mod.Feature | str | int, items: list[str]) -> dict[str, Any]:
    """A `manual:<eid>` source holding the given items."""
    f = _feature(root, feature)
    names = [" ".join(i.split()) for i in items]
    if not names or any(not n for n in names) or len(set(names)) != len(names):
        raise AgentError("add needs distinct, non-empty item descriptions")
    eid = uuid.uuid4().hex
    source = f"manual:{eid}"
    data = _write(
        f,
        "source",
        source,
        f"{len(names)} item(s) added",
        {"eid": eid, "source": source, "G": eid, "ver": [], "items": names},
    )
    return {"source": source, "items": names, "eid": data["eid"]}
