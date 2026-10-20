"""Dispatch lifecycle telemetry: the `dispatch.*` events, recorded explicitly.

The supervisor and the manager's dispatch actions know the harness root they run in, so they
record into `sink_root(root)` and never route from the environment: the supervisor runs with
`AGENT_DISPATCH_ID` set, which would silence the implicit entry point. Every function here
returns quietly on any failure, so telemetry can never change a dispatch's outcome or record.
"""

from __future__ import annotations

import functools
import uuid
from pathlib import Path
from typing import Any

from .. import telemetry
from ..core import agent_path
from ..records.narrow import as_dict, as_dict_or_none, dict_list

SINK_ENV = "AGENT_TELEMETRY_SINK"
PARENT_ENV = "AGENT_TELEMETRY_PARENT"
ROUTING_KEYS = (SINK_ENV, PARENT_ENV)
COST_EVIDENCE = ("provider", "estimate")


def main_for(root: Path) -> Path | None:
    """The main checkout whose `.agent/telemetry` is the sink, or None when it cannot be found.
    `root` must be a checkout root of its own (a main checkout or a linked worktree): a directory
    with no `.git` that merely sits inside some other checkout is not that checkout's harness."""
    try:
        if not (root / ".git").exists():
            return None
        return telemetry.sink_root(root)
    except Exception:  # noqa: BLE001 — no git root is a `no_root` loss, never an error
        return None


def sink_dir(main: Path) -> Path:
    return agent_path(main, "telemetry")


def new_event_id() -> str:
    return str(uuid.uuid4())


def _feature(value: Any) -> str | None:
    """The feature number as `worker_env` spells it (`001`)."""
    if value is None:
        return None
    try:
        return f"{int(value):03d}"
    except TypeError, ValueError:
        return str(value)


def emit(root: Path, d: dict[str, Any], kind: str, **fields: Any) -> str | None:
    """Record one `dispatch.*` event for `d`; returns its id when written."""
    try:
        common = {
            "dispatch": str(d["id"]),
            "provider": str(d.get("provider") or "claude"),
            "model": str(d["model"]) if d.get("model") else None,
            "feature": _feature(d.get("feature")),
        }
        main = main_for(root)
        if main is None:
            return telemetry.process_recorder().record(None, kind, **common, **fields)
        return telemetry.record(main, kind, **common, **fields)
    except Exception:  # noqa: BLE001 — the recorder never raises; neither does its caller here
        return None


PHASES = (
    "model-active",
    "awaiting-answer",
    "awaiting-verification",
    "verifying",
    "awaiting-landing",
    "ended",
    "landed",
)


@functools.cache
def harness_revision() -> str | None:
    """The commit of the harness code running this process, or None outside a git checkout."""
    import subprocess

    here = Path(__file__).resolve().parent
    proc = subprocess.run(
        ["git", "-C", str(here), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and len(sha) in (40, 64) else None


def transition(root: Path, d: dict[str, Any], phase: str, **data: Any) -> None:
    """One append-only lifecycle transition: the phase a dispatch's attempt enters,
    with its attempt, resolved base and the harness revision running it. Records are overwritten
    attempt by attempt; these events are not, so a resumed attempt keeps its earlier waits.
    `dispatch spans` reads them back."""
    if phase not in PHASES:
        return
    emit(
        root,
        d,
        "dispatch.transition",
        outcome=phase,
        commit=harness_revision(),
        data={
            "phase": phase,
            "attempt": int(d.get("attempt") or 1),
            "base": str(d.get("start_sha") or d.get("base") or "") or None,
            **data,
        },
    )


def launch_kind(d: dict[str, Any]) -> str:
    """`fresh` for a first attempt, `auto_resume` when the attempt before this one queued it
    after a timeout, else `resume` (the manager's answer)."""
    attempt = int(d.get("attempt") or 1)
    if attempt <= 1:
        return "fresh"
    previous = attempt - 1
    for row in dict_list(d.get("auto_resumes")):
        if row.get("attempt") == previous:
            return "auto_resume"
    return "resume"


def launch(root: Path, d: dict[str, Any]) -> None:
    emit(
        root,
        d,
        "dispatch.launch",
        outcome=launch_kind(d),
        data={
            "launch": launch_kind(d),
            "attempt": int(d.get("attempt") or 1),
            "row": d.get("row"),
            "budget_usd": float(d.get("budget_usd") or 0.0),
            "base": str(d.get("base") or "") or None,
            "effort": d.get("effort"),
            "escalated": bool(d.get("escalated_from")),
        },
    )


def setup_timing(attempt: int, created: dict[str, Any]) -> dict[str, Any]:
    """What the manager side knows of an attempt's setup, kept on the record for the supervisor's
    `dispatch.setup` event: the worktree step's time, from `ensure_worktree`/`reuse_worktree`."""
    return {"attempt": attempt, "worktree_s": created.get("worktree_s"), "reused": not created.get("created")}


def setup(root: Path, d: dict[str, Any], timing: dict[str, Any], env_s: float | None) -> None:
    worktree_s = timing.get("worktree_s")
    total = sum(x for x in (worktree_s, env_s) if isinstance(x, (int, float)))
    emit(
        root,
        d,
        "dispatch.setup",
        duration_s=total,
        data={
            "worktree_s": worktree_s,
            "env_s": env_s,
            "reused": bool(timing.get("reused")),
            "attempt": int(d.get("attempt") or 1),
        },
    )


def preflight(
    root: Path,
    d: dict[str, Any],
    findings: list[dict[str, Any]],
    seconds: float,
    source: str,
    attempt: int | None = None,
) -> None:
    """`attempt` is the attempt the preflight gates, when it is not yet `d`'s own (a resume)."""
    verdicts = [str(f.get("verdict")) for f in findings]
    emit(
        root,
        d,
        "dispatch.preflight",
        duration_s=seconds,
        outcome="refused" if any(v not in ("allowed", "warning") for v in verdicts) else "passed",
        data={
            "allowed": verdicts.count("allowed"),
            "denied": verdicts.count("denied"),
            "unmatched": verdicts.count("no-rule"),
            "source": source,
            "attempt": attempt if attempt is not None else int(d.get("attempt") or 1),
        },
    )


UNASSESSED = ("failed", "incomplete", "unverified", "exempt")


def verification(root: Path, d: dict[str, Any], assessed: str | None) -> None:
    """The verification event for `d`'s attempt, called at its terminal commit before the save.

    When `gate.execute` prepared a verifier for this attempt, the event goes under the id that
    verifier was given as its parent, whatever the terminal state, and only once: the stash's
    `recorded` marker is set here and persisted by the caller's guarded save, so a crash in
    between repeats the same id rather than losing it. `assessed` is acceptance's status when it
    ran; otherwise the recorded status stands, and never `verified`. With no prepared verifier, an
    assessed attempt still gets an event under a new id."""
    stash = as_dict_or_none(d.get("verification_telemetry"))
    attempt = int(d.get("attempt") or 1)
    if not (stash is not None and stash.get("attempt") == d.get("attempt") and stash.get("event_id")):
        if assessed is not None:
            emit(
                root,
                d,
                "dispatch.verification",
                outcome=assessed,
                data={"run_id": None, "attempt": attempt, **_lane_wait(d)},
            )
        return
    if stash.get("recorded") == stash["event_id"]:
        return
    status = assessed
    if status is None:
        recorded = as_dict(d.get("verification_result")).get("status")
        status = recorded if recorded in UNASSESSED else "incomplete"
    emit(
        root,
        d,
        "dispatch.verification",
        event_id=stash["event_id"],
        duration_s=stash.get("duration_s"),
        outcome=status,
        data={"run_id": stash.get("run_id"), "attempt": attempt, **_lane_wait(d)},
    )
    stash["recorded"] = stash["event_id"]


def _lane_wait(d: dict[str, Any]) -> dict[str, Any]:
    """The attempt's verification-lane wait and the repo's live workers then."""
    wait = as_dict_or_none(d.get("verification_wait"))
    if wait is None or wait.get("attempt") != d.get("attempt"):
        return {}
    return {"verify_wait_s": wait.get("verify_wait_s"), "concurrent_workers": wait.get("concurrent_workers")}


def note_cost_evidence(d: dict[str, Any], outcome: dict[str, Any]) -> None:
    """Keep what backs the cost just added for `d`'s current attempt in `d["cost_evidence"]`,
    keyed by attempt. An attempt with no evidence (no result, a crash) gets no entry."""
    evidence = as_dict(d.get("cost_evidence"))
    key = str(d["attempt"])
    if outcome.get("cost_evidence") in COST_EVIDENCE:
        evidence[key] = outcome["cost_evidence"]
    else:
        evidence.pop(key, None)
    d["cost_evidence"] = evidence


def cost_figure(d: dict[str, Any]) -> tuple[float | None, str]:
    """`(cost_usd, cost_source)` for a terminal event. Every attempt the record's cumulative cost
    covers needs its own evidence; one missing attempt makes the figure `unknown` and null."""
    evidence = as_dict(d.get("cost_evidence"))
    attempts = int(d.get("attempt") or 0)
    sources = [evidence.get(str(n)) for n in range(1, attempts + 1)]
    if not sources or any(s not in COST_EVIDENCE for s in sources):
        return None, "unknown"
    try:
        amount = float(d.get("cost_usd") or 0.0)
    except TypeError, ValueError:
        return None, "unknown"
    return amount, "provider" if all(s == "provider" for s in sources) else "estimate"


def terminal(root: Path, d: dict[str, Any]) -> None:
    cost, source = cost_figure(d)
    emit(
        root,
        d,
        "dispatch.terminal",
        outcome=str(d.get("state")),
        cost_usd=cost,
        cost_source=source,
        data={"turns": int(d.get("num_turns") or 0), "attempt": int(d.get("attempt") or 1)},
    )


def reverify(
    root: Path,
    d: dict[str, Any],
    *,
    event_id: str,
    run_id: str | None,
    status: str,
    worktree_s: float | None,
    verify_s: float | None,
) -> None:
    emit(
        root,
        d,
        "dispatch.reverify",
        event_id=event_id,
        duration_s=verify_s,
        outcome=status,
        data={
            "run_id": run_id,
            "worktree_s": worktree_s,
            # a reverify checkout runs no `dispatch.setup` commands
            "env_s": None,
            "verify_s": verify_s,
            "attempt": int(d.get("attempt") or 1),
        },
    )


def ack(root: Path, d: dict[str, Any]) -> None:
    emit(
        root,
        d,
        "dispatch.ack",
        outcome="ack",
        data={"state": str(d.get("state")), "noted": bool(as_dict(d.get("actioned")).get("note"))},
    )


def strip_routing(env: dict[str, str]) -> dict[str, str]:
    """Drop the telemetry routing keys, whatever supplied them; returns `env` for chaining."""
    for key in ROUTING_KEYS:
        env.pop(key, None)
    return env
