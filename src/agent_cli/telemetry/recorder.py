"""The telemetry recorder: builds, validates and appends events, and never raises into its caller.

Explicit callers (supervisor, manager actions) pass the main checkout they resolved themselves and
ignore `AGENT_DISPATCH_ID`. Implicit callers (the pytest plugin, hooks) resolve the sink from the
environment: `AGENT_TELEMETRY=off` records nothing, `AGENT_TELEMETRY_SINK` names the sink, and
inside a dispatch without that variable nothing is recorded, so a worker never writes the
manager's state.

Each event gets one write attempt and, on failure, one loss-file attempt. After three consecutive
failed writes the instance stops for its lifetime and counts what it skips.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import fence, schema
from .sink import Loss, Sink, append

STRIKES = 3


class Recorder:
    """One writer identity per process: `instance` is new per process because host and pid get
    reused, and `seq` rises by one for every event this instance tries to record."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reset()

    def _reset(self) -> None:
        self.pid = os.getpid()
        self.host = socket.gethostname()
        self.instance = str(uuid.uuid4())
        self.seq = 0
        self.failures = 0
        self.disabled = False
        self.counts = {"attempted": 0, "written": 0, "lost": 0, "skipped": 0}

    def writer(self, seq: int) -> dict[str, Any]:
        return {"host": self.host, "pid": self.pid, "instance": self.instance, "seq": seq}

    def record(
        self,
        sink: Sink | None,
        kind: str,
        *,
        event_id: str | None = None,
        parent_id: str | None = None,
        ts: datetime | None = None,
        duration_s: float | None = None,
        outcome: str | None = None,
        session: str | None = None,
        feature: str | None = None,
        dispatch: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        cost_usd: float | None = None,
        cost_source: str | None = None,
        commit: str | None = None,
        source: str = "live",
        data: Mapping[str, Any] | None = None,
    ) -> str | None:
        """Record one event; returns its id when written, else None. `sink` None means the main
        checkout could not be resolved, a `no_root` loss."""
        try:
            with self._lock:
                if os.getpid() != self.pid:
                    self._reset()
                self.counts["attempted"] += 1
                self.seq += 1
                if self.disabled:
                    self.counts["skipped"] += 1
                    return None
                if sink is None:
                    self.counts["lost"] += 1
                    return None
                if not fence.allows(sink.directory):
                    self.counts["lost"] += 1
                    fence.note(sink.directory, kind)
                    return None
                when = ts or datetime.now(UTC)
                event = {
                    "schema": schema.SCHEMA_VERSION,
                    "event_id": event_id or str(uuid.uuid4()),
                    "parent_id": parent_id,
                    "kind": kind,
                    "ts": when.isoformat(),
                    "duration_s": duration_s,
                    "outcome": outcome,
                    "writer": self.writer(self.seq),
                    "session": session,
                    "feature": feature,
                    "dispatch": dispatch,
                    "provider": provider,
                    "model": model,
                    "cost_usd": cost_usd,
                    "cost_source": cost_source,
                    "commit": commit,
                    "source": source,
                    "data": dict(data or {}),
                }
                return self._write(sink, event, when)
        except Exception:  # noqa: BLE001 — the recorder never raises into its caller
            self.counts["lost"] += 1
            return None

    def _write(self, sink: Sink, event: dict[str, Any], when: datetime) -> str | None:
        line = None
        if when.tzinfo is not None and schema.validate(event) is None:
            try:
                encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                line = (encoded + "\n").encode()
            except ValueError, TypeError:
                line = None
        if line is None:
            self.counts["lost"] += 1
            self._loss(sink, event["kind"], "refused")
            return None
        try:
            append(sink.day(when.astimezone(UTC).date()), line)
        except Loss as loss:
            reason = loss.kind
        except Exception as exc:  # noqa: BLE001 — every write failure is a loss, never an error
            reason = type(exc).__name__
        else:
            self.counts["written"] += 1
            self.failures = 0
            return event["event_id"]
        self.counts["lost"] += 1
        self._loss(sink, event["kind"], reason)
        if reason == "oversize":  # a refusal, not a failed write
            return None
        self.failures += 1
        if self.failures >= STRIKES:
            self.disabled = True
            self._loss(sink, "disabled", reason)
        return None

    def _loss(self, sink: Sink, kind_lost: str, reason: str) -> None:
        """One attempt at a loss line, under the same lock rule; its own failure is silent."""
        line = {
            "ts": datetime.now(UTC).isoformat(),
            "writer": self.writer(self.seq),
            "kind_lost": kind_lost,
            "reason": reason,
        }
        try:
            append(sink.loss, (json.dumps(line, separators=(",", ":")) + "\n").encode())
        except Exception:  # noqa: BLE001 — an unobservable loss; health says it is not counted
            return


_process: Recorder | None = None


def process_recorder() -> Recorder:
    """This process's recorder. It renews its identity itself after a fork."""
    global _process
    if _process is None:
        _process = Recorder()
    return _process


def record(main: Path, kind: str, **fields: Any) -> str | None:
    """Explicit recording into `<main>/.agent/telemetry`, where `main` is the main checkout the
    caller resolved itself (`sink_root`). Ignores the environment."""
    try:
        return process_recorder().record(Sink.for_main(main), kind, **fields)
    except Exception:  # noqa: BLE001 — a bad field name too must not reach the caller
        return None


def sink_root(path: Path) -> Path:
    """The main checkout of `path`'s git common dir: the one sink root for every worktree.
    Raises when `path` is not in a git checkout."""
    from ..worktree import main_checkout

    return main_checkout(path)


def main_of(root: Path) -> Path:
    """`sink_root`, or `root` itself outside git: the checkout whose `.agent` holds the events."""
    from ..core import AgentError

    try:
        return sink_root(root)
    except AgentError, OSError:
        return root


def resolve(env: Mapping[str, str] | None = None, cwd: Path | None = None) -> tuple[Sink | None, str]:
    """Implicit routing: `(sink, why)`. `why` is `sink`, `main`, `off`, `dispatch` or `no_root`."""
    env = os.environ if env is None else env
    if env.get("AGENT_TELEMETRY", "").strip().lower() == "off":
        return None, "off"
    named = env.get("AGENT_TELEMETRY_SINK", "")
    if named:
        return Sink.from_directory(Path(named)), "sink"
    if env.get("AGENT_DISPATCH_ID", ""):
        return None, "dispatch"
    try:
        return Sink.for_main(sink_root(cwd or Path.cwd())), "main"
    except Exception:  # noqa: BLE001 — no git root: a loss, not an error
        return None, "no_root"


def record_implicit(
    kind: str, *, env: Mapping[str, str] | None = None, cwd: Path | None = None, **fields: Any
) -> str | None:
    """Implicit recording, routed by `resolve`. `AGENT_TELEMETRY_PARENT` is the default parent."""
    try:
        env = os.environ if env is None else env
        sink, why = resolve(env, cwd)
        if sink is None and why != "no_root":
            return None
        if fields.get("parent_id") is None and env.get("AGENT_TELEMETRY_PARENT"):
            fields["parent_id"] = env["AGENT_TELEMETRY_PARENT"]
        return process_recorder().record(sink, kind, **fields)
    except Exception:  # noqa: BLE001 — the recorder never raises into its caller
        return None
