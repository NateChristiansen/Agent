"""Replay a window of hook events through the tab label code and count the wrong icons (
Verification, "The icon replay"; rev 4 A10, rev 6 C9).

Usage:
  uv run python scripts/icon_replay.py --code <tree> --evidence <audit>/evidence \\
      [--features <notes>/features] [--json OUT]
  uv run python scripts/icon_replay.py --code <tree> --state <harness>/.agent \\
      --since 2026-10-14T00:00:00Z --until 2026-10-15T00:00:00Z [--features ...] [--json OUT]

`--code` is a source tree whose `src/agent_cli` is imported in place of the installed one: this
worktree for the "after" numbers, or a `git archive <base> src` extraction for "before". Read-only:
the label code runs against an empty temporary root with every state read stubbed from the input.

Input, either:
- `--evidence`: an audit's frozen capture (`manifest.json` plus `captures/*.gz`): telemetry,
  session and dispatch records, and the manager transcripts (Claude and Codex) it names;
- `--state`: a live `.agent` directory (`telemetry/events/*.jsonl`, `sessions/`, `dispatches/`),
  with each manager's transcript found under `~/.claude/projects` or `~/.codex/sessions`.

What the label code is fed (the hook side). Per manager session, in time order:
- each `activity.transition`, turned back into the hook payload its `cause` names and passed
  through the tree's own `hooks.activity._state_for`;
- `workflow.owner_question` and its `_cleared` event, as `owner_question_pending`;
- every dispatch's lifecycle: running from `dispatch.launch` to `dispatch.terminal`, then its
  recorded final state, actioned from its `dispatch.ack`.
At each moment the tree's `hooks.activity.desired_label` gives the label, and its first token
the glyph. Closed features come from `--features` (each plan's `status` and `closed` date, or
the window's `feature.close` time); without it none count as closed.

The ground truth, from the transcripts only (never from the hooks):
- ✋ an AskUserQuestion (Claude), `request_user_input` (Codex) or ExitPlanMode still waiting for
  its result, or an `owner_question` the owner has not answered with a prompt of their own;
- ⚙️ a turn in progress: Claude from a prompt to its `turn_duration`/`stop_hook_summary` (or an
  interrupt), Codex from `task_started` to `task_complete`;
- ⏳ a worker the session launched is running. The manager's transcript never sees a worker end,
  so this one interval comes from the supervisor's `dispatch.launch`/`dispatch.terminal`;
- ✅ otherwise. Backgrounded commands are left out of both sides: telemetry does not carry them.

Reported: seconds and segments where the shown glyph differs from the expected one, by kind
(`expected → shown (why)`), per session and in total. No transcript text is written out.
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import importlib
import json
import os
import re
import sys
import tempfile
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HAND, WAIT, WORK, REST = "✋", "⏳", "⚙️", "✅"
WAKES = {"peer_wake", "dispatch_wake"}
PROMPTS = {"owner_prompt", "machine_prompt"}
OWNER_QUESTION_RE = re.compile(r"owner_question")


def ts(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp()


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat()


# ------------------------------------------------------------------ input


@dataclass
class Window:
    start: float
    end: float
    telemetry: list[dict[str, Any]]
    sessions: dict[str, dict[str, Any]]
    dispatches: dict[str, dict[str, Any]]
    transcripts: dict[str, tuple[str, list[dict[str, Any]]]]  # sid -> (client, records)


def _jsonl(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def load_evidence(evidence: Path) -> Window:
    manifest = json.loads((evidence / "manifest.json").read_text(encoding="utf-8"))
    start, end = (ts(x) for x in manifest["window"])
    capture_end = ts(manifest.get("capture_end"))
    assert start is not None and end is not None
    window = Window(start, min(end, capture_end or end), [], {}, {}, {})

    def read(item: dict[str, Any]) -> str:
        return gzip.decompress((evidence / "captures" / item["capture"]).read_bytes()).decode("utf-8")

    for item in manifest["inputs"]:
        kind, name = item["kind"], Path(item["path"]).name
        if kind == "telemetry":
            window.telemetry += _jsonl(read(item))
        elif kind == "session":
            window.sessions[Path(name).stem] = json.loads(read(item))
        elif kind == "dispatch":
            window.dispatches[Path(name).stem] = json.loads(read(item))
        elif kind == "claude":
            window.transcripts[Path(name).stem] = ("claude", _jsonl(read(item)))
        elif kind == "codex":
            sid = re.sub(r"^rollout-[0-9T:-]+-", "", Path(name).stem)
            window.transcripts[sid] = ("codex", _jsonl(read(item)))
    return window


def load_state(state: Path, since: float, until: float) -> Window:
    window = Window(since, until, [], {}, {}, {})
    for path in sorted((state / "telemetry" / "events").glob("*.jsonl")):
        window.telemetry += _jsonl(path.read_text(encoding="utf-8"))
    for path in (state / "sessions").glob("*.json"):
        with contextlib.suppress(OSError, ValueError):
            window.sessions[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    for path in (state / "dispatches").glob("*.json"):
        with contextlib.suppress(OSError, ValueError):
            window.dispatches[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    home = Path.home()
    for sid in window.sessions:
        claude = next(iter((home / ".claude" / "projects").glob(f"*/{sid}.jsonl")), None)
        codex = next(iter((home / ".codex" / "sessions").glob(f"*/*/*/rollout-*-{sid}.jsonl")), None)
        if claude is not None:
            window.transcripts[sid] = ("claude", _jsonl(claude.read_text(encoding="utf-8")))
        elif codex is not None:
            window.transcripts[sid] = ("codex", _jsonl(codex.read_text(encoding="utf-8")))
    return window


def closed_features(features: Path | None, telemetry: list[dict[str, Any]]) -> dict[int, float]:
    """Feature id -> when it closed (epoch s): the window's `feature.close` time, else the start
    of the plan's `closed:` date."""
    out: dict[int, float] = {}
    if features is not None:
        for plan in features.glob("*/plan.md"):
            head = plan.read_text(encoding="utf-8").split("\n---", 1)[0]
            status = re.search(r"^status:\s*(\S+)", head, re.M)
            if not status or status.group(1) not in ("done", "abandoned"):
                continue
            fid = re.search(r"^id:\s*(\d+)", head, re.M)
            day = re.search(r"^closed:\s*(\d{4}-\d{2}-\d{2})", head, re.M)
            if fid:
                out[int(fid.group(1))] = ts(f"{day.group(1)}T00:00:00+00:00") if day else 0.0  # type: ignore[assignment]
    for row in telemetry:
        if row.get("kind") == "feature.close" and str(row.get("feature") or "").isdigit():
            at = ts(row.get("ts"))
            if at is not None:
                out[int(row["feature"])] = at
    return out


# ------------------------------------------------------------------ ground truth (transcripts)


@dataclass
class Truth:
    """Intervals from the transcript: turns, open questions."""

    turns: list[tuple[float, float]] = field(default_factory=list)
    hands: list[tuple[float, float]] = field(default_factory=list)
    first: float = 0.0
    last: float = 0.0


def _blocks(record: dict[str, Any]) -> list[dict[str, Any]]:
    content = (record.get("message") or {}).get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _text(record: dict[str, Any]) -> str:
    content = (record.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    return " ".join(str(b.get("text") or "") for b in _blocks(record) if b.get("type") == "text")


def claude_truth(records: list[dict[str, Any]]) -> Truth:
    truth = Truth()
    rows = sorted(
        ((t, r) for r in records if not r.get("isSidechain") and (t := ts(r.get("timestamp"))) is not None),
        key=lambda x: x[0],
    )
    if not rows:
        return truth
    truth.first, truth.last = rows[0][0], rows[-1][0]
    turn_at: float | None = None
    # A user record starts a turn only once the model answers it: a compaction summary, a `!`
    # command or a local slash command is written as a user record and runs no turn. So a turn
    # starts at the last such record before the next assistant record.
    prompt_at: float | None = None
    open_tools: dict[str, float] = {}
    question_at: float | None = None
    for t, r in rows:
        kind = r.get("type")
        if kind == "user":
            results = [b for b in _blocks(r) if b.get("type") == "tool_result"]
            for b in results:
                began = open_tools.pop(str(b.get("tool_use_id")), None)
                if began is not None:
                    truth.hands.append((began, t))
            if results:
                continue
            if _text(r).startswith("[Request interrupted"):
                if turn_at is not None:
                    truth.turns.append((turn_at, t))
                turn_at = prompt_at = None
                continue
            origin = r.get("origin")
            if question_at is not None and isinstance(origin, dict) and origin.get("kind") == "human":
                truth.hands.append((question_at, t))
                question_at = None
            if turn_at is None:
                prompt_at = t
        elif kind == "assistant":
            if turn_at is None:
                turn_at = prompt_at if prompt_at is not None else t
                prompt_at = None
            for b in _blocks(r):
                if b.get("type") != "tool_use":
                    continue
                name = str(b.get("name") or "")
                if name in ("AskUserQuestion", "ExitPlanMode"):
                    open_tools[str(b.get("id"))] = t
                elif name.startswith("mcp__agent") and OWNER_QUESTION_RE.search(
                    name + json.dumps(b.get("input"))
                ):
                    question_at = t if question_at is None else question_at
        elif kind == "system" and r.get("subtype") in ("turn_duration", "stop_hook_summary"):
            if turn_at is not None:
                truth.turns.append((turn_at, t))
            turn_at = None
    end = truth.last
    if turn_at is not None:
        truth.turns.append((turn_at, end))
    truth.hands += [(began, end) for began in open_tools.values()]
    if question_at is not None:
        truth.hands.append((question_at, end))
    return truth


def codex_truth(records: list[dict[str, Any]]) -> Truth:
    truth = Truth()
    rows = sorted(
        ((t, r) for r in records if (t := ts(r.get("timestamp"))) is not None),
        key=lambda x: x[0],
    )
    if not rows:
        return truth
    truth.first, truth.last = rows[0][0], rows[-1][0]
    turn_at: float | None = None
    open_calls: dict[str, float] = {}
    question_at: float | None = None
    for t, r in rows:
        raw = r.get("payload")
        p: dict[str, Any] = raw if isinstance(raw, dict) else {}
        kind = p.get("type")
        if kind == "task_started":
            trigger = (p.get("turn_attribution") or {}).get("turn_trigger")
            if question_at is not None and trigger == "user":
                truth.hands.append((question_at, t))
                question_at = None
            if turn_at is None:
                turn_at = t
        elif kind in ("task_complete", "turn_aborted"):
            if turn_at is not None:
                truth.turns.append((turn_at, t))
            turn_at = None
        elif kind == "function_call":
            name = str(p.get("name") or "")
            if name == "request_user_input":
                open_calls[str(p.get("call_id"))] = t
            elif name == "request_user_input_async":
                question_at = t if question_at is None else question_at
        elif kind == "function_call_output":
            began = open_calls.pop(str(p.get("call_id")), None)
            if began is not None:
                truth.hands.append((began, t))
        elif kind == "custom_tool_call" and re.search(r"owner_question\w*\s*\(", str(p.get("input") or "")):
            question_at = t if question_at is None else question_at
    end = truth.last
    if turn_at is not None:
        truth.turns.append((turn_at, end))
    truth.hands += [(began, end) for began in open_calls.values()]
    if question_at is not None:
        truth.hands.append((question_at, end))
    return truth


def inside(intervals: list[tuple[float, float]], t: float) -> bool:
    return any(a <= t < b for a, b in intervals)


# ------------------------------------------------------------------ the hook side


def payload_for(cause: str) -> dict[str, Any]:
    """The hook payload an `activity.transition` cause names (`hooks/attention.observe`)."""
    event, _, tool = cause.partition(":")
    if cause in WAKES:
        from agent_cli.ops.peer_wake import WAKE_TEXTS

        text = next(iter(WAKE_TEXTS.values()), "")
        return {"hook_event_name": "UserPromptSubmit", "prompt": text, "_wake_accepted": True}
    if cause in PROMPTS:
        return {"hook_event_name": "UserPromptSubmit", "prompt": "replayed"}
    payload: dict[str, Any] = {"hook_event_name": event}
    if tool:
        payload["tool_name"] = tool
    return payload


def lifecycles(window: Window) -> dict[str, list[tuple[float, str, float | None]]]:
    """Dispatch id -> [(at, "launch"|"terminal"|"ack", None)], from telemetry, in time order."""
    out: dict[str, list[tuple[float, str, float | None]]] = defaultdict(list)
    for row in window.telemetry:
        kind = str(row.get("kind") or "")
        did = row.get("dispatch")
        at = ts(row.get("ts"))
        if not did or at is None:
            continue
        if kind in ("dispatch.launch", "dispatch.terminal", "dispatch.ack"):
            out[str(did)].append((at, kind.split(".")[1], None))
    for events in out.values():
        events.sort()
    return out


def dispatch_at(
    record: dict[str, Any], events: list[tuple[float, str, float | None]], t: float
) -> dict[str, Any] | None:
    """The record as it stood at `t`: running between a launch and its terminal, then its final
    state with `finished` and `actioned_at` from the lifecycle. None before it existed."""
    d = {k: v for k, v in record.items() if k not in ("actioned_at", "actioned", "finished")}
    seen = [e for e in events if e[0] <= t]
    if events:
        if not seen:
            return None
        launched = [e[0] for e in seen if e[1] == "launch"]
        ended = [e[0] for e in seen if e[1] == "terminal"]
        acked = [e[0] for e in seen if e[1] == "ack"]
        if launched and (not ended or launched[-1] > ended[-1]):
            d["state"] = "running"
            return d
        if ended:
            d["finished"] = iso(ended[-1])
        if acked:
            d["actioned_at"] = iso(acked[-1])
            d["actioned"] = True
        return d
    started, finished = ts(record.get("attempt_started") or record.get("started")), ts(record.get("finished"))
    if started is None or t < started:
        return None
    if finished is None or t < finished:
        d["state"] = "running"
        return d
    d["finished"] = record.get("finished")
    acked_at = ts(record.get("actioned_at"))
    if acked_at is not None and acked_at <= t:
        d["actioned_at"], d["actioned"] = record.get("actioned_at"), record.get("actioned", True)
    return d


@contextlib.contextmanager
def stubbed(root: Path, view: dict[str, Any]) -> Iterator[None]:
    """Every state read the label code makes, answered from `view` (dispatches, the session's
    record, closed features) instead of the root."""
    import agent_cli.dispatch as dispatch_mod
    import agent_cli.dispatch.state as dispatch_state
    import agent_cli.hooks.naming as naming
    import agent_cli.vault.live as live

    saved: list[tuple[Any, str, Any]] = []

    def put(obj: Any, name: str, value: Any) -> None:
        if hasattr(obj, name):
            saved.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

    put(dispatch_mod, "load_all", lambda *a, **k: view["dispatches"])
    put(dispatch_state, "load_all", lambda *a, **k: view["dispatches"])
    put(dispatch_mod, "closed_feature_ids", lambda *a, **k: view["closed"])
    put(dispatch_state, "closed_feature_ids", lambda *a, **k: view["closed"])
    put(naming, "live_name", lambda *a, **k: (None, None))
    # Lifecycle events establish liveness at the replay instant. The saved supervisor may
    # have ended long ago, and its present-day PID says nothing about that interval.
    put(live, "running_dispatch", lambda d: d.get("state") in ("queued", "running"))
    with contextlib.suppress(ImportError):
        import agent_cli.vault.managers as managers

        put(managers, "live_manager_records", lambda *a, **k: [(view["record"], {})])
    try:
        yield
    finally:
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)


# ------------------------------------------------------------------ replay


@dataclass
class Tally:
    seconds: float = 0.0
    wrong_s: float = 0.0
    segments: int = 0
    wrong_segments: int = 0
    kinds: dict[str, list[float]] = field(default_factory=lambda: defaultdict(lambda: [0.0, 0]))

    def add(self, expected: str, shown: str, why: str, span: float) -> None:
        self.seconds += span
        self.segments += 1
        if expected != shown:
            self.wrong_s += span
            self.wrong_segments += 1
            row = self.kinds[f"{expected} → {shown} ({why})"]
            row[0] += span
            row[1] += 1


def replay(window: Window, closed_at: dict[int, float]) -> dict[str, Any]:
    """The replay, against an empty temporary root with tabs off; the environment is restored."""
    saved = {k: os.environ.get(k) for k in ("AGENT_ROOT", "AGENT_NO_TABS")}
    with tempfile.TemporaryDirectory(prefix="icon-replay-") as tmp:
        root = Path(tmp)
        (root / ".agent").mkdir()
        os.environ["AGENT_ROOT"], os.environ["AGENT_NO_TABS"] = str(root), "1"
        try:
            return _replay(window, closed_at, root)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def _replay(window: Window, closed_at: dict[int, float], root: Path) -> dict[str, Any]:
    # by module name: `agent_cli.hooks` re-exports the `activity` hook function under that name
    activity_mod = importlib.import_module("agent_cli.hooks.activity")
    state_for = activity_mod._state_for  # noqa: SLF001 — the replay feeds the hooks' own rule
    desired_label = activity_mod.desired_label
    by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in window.telemetry:
        if row.get("session") and row.get("kind") in (
            "activity.transition",
            "workflow.owner_question",
            "workflow.owner_question_cleared",
        ):
            by_session[str(row["session"])].append(row)
    life = lifecycles(window)
    total = Tally()
    sessions_out: dict[str, Any] = {}
    for sid, rows in sorted(by_session.items()):
        session = window.sessions.get(sid) or {}
        if session.get("dispatch_id") or sid not in window.transcripts:
            continue
        client, records = window.transcripts[sid]
        truth = claude_truth(records) if client == "claude" else codex_truth(records)
        rows.sort(key=lambda r: r.get("ts") or "")
        mine = {did: rec for did, rec in window.dispatches.items() if rec.get("launched_by_session") == sid}
        running: list[tuple[float, float]] = []
        for did in mine:
            events = life.get(did, [])
            launched = None
            for at, what, _ in events:
                if what == "launch":
                    launched = at
                elif what == "terminal" and launched is not None:
                    running.append((launched, at))
                    launched = None
            if launched is not None:
                running.append((launched, window.end))
        first_event = ts(rows[0].get("ts")) or window.start
        start = max(window.start, truth.first, first_event)
        end = min(window.end, truth.last)
        if end <= start:
            continue
        points = {start, end}
        for row in rows:
            if (at := ts(row.get("ts"))) is not None and start < at < end:
                points.add(at)
        for a, b in truth.turns + truth.hands + running:
            points |= {p for p in (a, b) if start < p < end}
        for did in window.dispatches:
            points |= {at for at, _, _ in life.get(did, []) if start < at < end}
        points |= {at for at in closed_at.values() if start < at < end}
        timeline = sorted(points)
        tally = Tally()
        current: str | None = None
        question = False
        cause = ""
        cursor = 0
        record_base = {
            "session_id": sid,
            "name": session.get("name") or f"mgr-replay-{sid[:6]}",
            "provider": session.get("provider") or client,
            "feature": session.get("feature"),
            "pid": 1,
        }
        for i, t in enumerate(timeline[:-1]):
            span = timeline[i + 1] - t
            while cursor < len(rows) and (ts(rows[cursor].get("ts")) or 0) <= t:
                row = rows[cursor]
                cursor += 1
                kind = row.get("kind")
                if kind == "workflow.owner_question":
                    question = True
                elif kind == "workflow.owner_question_cleared":
                    question = False
                else:
                    data = row.get("data") or {}
                    cause = str(data.get("cause") or "")
                    nxt = state_for(payload_for(cause), current)
                    current = nxt if nxt is not None else current
            if current is None or current == "ended":
                continue
            dispatches = [
                d
                for did, rec in window.dispatches.items()
                if (d := dispatch_at(rec, life.get(did, []), t)) is not None
            ]
            closed = {fid for fid, at in closed_at.items() if at <= t}
            record = {
                **record_base,
                "activity": {"state": current, "at": iso(t)},
                "owner_question_pending": question,
            }
            view = {"dispatches": dispatches, "closed": closed, "record": record}
            with stubbed(root, view):
                label = desired_label(root, sid, record)
            shown = label[2].split(" ", 1)[0] if label else REST
            if inside(truth.hands, t):
                expected = HAND
            elif inside(truth.turns, t):
                expected = WORK
            elif inside(running, t):
                expected = WAIT
            else:
                expected = REST
            tally.add(expected, shown, _why(shown, current, cause, question, dispatches, sid), span)
            total.add(expected, shown, _why(shown, current, cause, question, dispatches, sid), span)
        sessions_out[sid] = _summary(tally) | {"client": client}
    return {"total": _summary(total), "sessions": sessions_out}


def _why(
    shown: str, state: str, cause: str, question: bool, dispatches: list[dict[str, Any]], sid: str
) -> str:
    if shown == HAND:
        if question:
            return "owner_question pending"
        return f"activity waiting_on_you after {cause.split(':')[0] or '?'}"
    if shown == WAIT:
        mine = [d for d in dispatches if d.get("launched_by_session") == sid]
        if any(d.get("state") in ("queued", "running") for d in mine):
            return "worker running"
        return "finished worker not handled"
    return f"activity {state}"


def _summary(t: Tally) -> dict[str, Any]:
    kinds = sorted(t.kinds.items(), key=lambda kv: -kv[1][0])
    return {
        "seconds": round(t.seconds),
        "wrong_seconds": round(t.wrong_s),
        "wrong_share": round(t.wrong_s / t.seconds, 4) if t.seconds else 0.0,
        "segments": t.segments,
        "wrong_segments": t.wrong_segments,
        "kinds": {k: {"seconds": round(v[0]), "segments": int(v[1])} for k, v in kinds},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--code", type=Path, required=True, help="tree whose src/agent_cli to run")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--evidence", type=Path, help="an audit's frozen evidence directory")
    source.add_argument("--state", type=Path, help="a live .agent directory")
    parser.add_argument("--since", help="window start (ISO), with --state")
    parser.add_argument("--until", help="window end (ISO), with --state")
    parser.add_argument("--features", type=Path, help="the notes' features directory (closed features)")
    parser.add_argument("--json", type=Path, help="write the full result here")
    args = parser.parse_args(argv)
    sys.path.insert(0, str((args.code / "src").resolve()))
    import agent_cli

    loaded_from = Path(agent_cli.__file__).resolve()
    if not loaded_from.is_relative_to((args.code / "src").resolve()):
        parser.error(f"agent_cli came from {loaded_from}, not --code")
    if args.evidence:
        window = load_evidence(args.evidence)
    else:
        since, until = ts(args.since), ts(args.until)
        if since is None or until is None:
            parser.error("--state needs --since and --until")
        window = load_state(args.state, since, until)
    result = replay(window, closed_features(args.features, window.telemetry))
    result["window"] = [iso(window.start), iso(window.end)]
    result["code"] = str(args.code)
    if args.json:
        args.json.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    total = result["total"]
    print(
        f"{total['wrong_seconds']} of {total['seconds']} s wrong ({total['wrong_share']:.1%}); "
        f"{total['wrong_segments']} of {total['segments']} segments"
    )
    for kind, row in total["kinds"].items():
        print(f"  {row['seconds']:>8} s  {row['segments']:>5}  {kind}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
