"""Shared ToolSpec and Ctx types, aliases and helpers for MCP tool specifications."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .. import sessions
from ..core import AgentError
from ..ops import Op

Profile = Literal["manager", "worker"]
LogKind = Literal["note", "decision", "deviation", "surprise", "measurement", "dead-end", "blocker"]
WorkerLogKind = Literal["dead-end", "surprise", "measurement", "blocker"]
FeatureStatus = Literal["proposed", "active", "blocked", "done", "abandoned"]
OpenStatus = Literal["proposed", "active", "blocked"]
TerminalStatus = Literal["done", "abandoned"]
AdrStatus = Literal["proposed", "accepted", "superseded", "deprecated"]
DispatchAction = Literal[
    "new",
    "list",
    "show",
    "spans",
    "resume",
    "cancel",
    "abandon",
    "ack",
    "say",
    "collect",
    "collect-files",
    "reverify",
    "cleanup",
    "reconcile",
    "restore",
    "discard-files",
    "watch",
    "ledger",
    "plan-review",
    "consume",
]
#: `dispatch_read`'s actions: the read-only ones, which `dispatch` keeps as aliases for one release.
DispatchReadAction = Literal["show", "watch", "list"]
DispatchLedgerBy = Literal["pr", "feature", "week"]
LeaseAction = Literal["take", "release", "list", "wait", "clear"]

WORKER_KINDS = ("dead-end", "surprise", "measurement", "blocker")
SESSION_HINT = "pass session_id (the session_id line of the session-start block)"

FEATURE_REF = "`feature` accepts 1, 001, 001-slug, slug, or `current` (this session's mapped feature)."
SESSION_ARG = "`session_id` is only needed when the server cannot see your session; it is on the session_id line of the session-start block."


@dataclass
class ToolSpec:
    """One tool: `fn(ctx, **params) -> Op`, plus what the model sees. `fn`'s first parameter
    is the server context and is hidden from the schema. The rest become the input schema."""

    fn: Callable[..., Op]
    title: str
    description: str
    read_only: bool = False
    idempotent: bool = False
    destructive: bool = False


@dataclass
class Ctx:
    """What a running server knows: the vault root, the profile, and, for a worker, the
    dispatch it belongs to."""

    root: Path
    profile: str
    feature: str | None = None  # worker: pinned from AGENT_FEATURE
    dispatch_id: str | None = None
    dispatch_tasks: list[int] | None = None  # worker: the plan tasks its dispatch covers
    _pid_cache: dict[str, str | None] = field(default_factory=dict[str, str | None])

    def session_id(self, explicit: str | None = None, *, required: bool = False) -> str | None:
        """B3 order: explicit → by-pid file for CLAUDE_PID / our parent / its parent → env
        CLAUDE_CODE_SESSION_ID / CLAUDE_SESSION_ID → None (or an error naming session_id)."""
        if explicit:
            return explicit
        sid = self._by_pid()
        if sid:
            return sid
        for var in sessions.ENV_VARS:
            value = os.environ.get(var)
            if value:
                return value
        if required:
            raise AgentError(f"no session id: {SESSION_HINT}")
        return None

    def session_for(self, feature: str, explicit: str | None) -> str | None:
        """Session id for resolving `current`; explicit feature refs need none."""
        sid = self.session_id(explicit)
        if sid is None and feature.strip() == "current":
            raise AgentError(
                f"feature is `current` but there is no session id: {SESSION_HINT}; name the feature explicitly, "
                "or map the intended session with `agent feature use <n>`"
            )
        return sid

    def _by_pid(self) -> str | None:
        # the one resolver, shared with hooks.py and ops.py (sessions.candidate_pids)
        for pid in sessions.candidate_pids():
            key = str(pid)
            if key not in self._pid_cache:
                self._pid_cache[key] = sessions.read_by_pid(self.root, pid)
            if self._pid_cache[key]:
                return self._pid_cache[key]
        return None


def dispatch_require(value: Any, name: str, action: str) -> Any:
    """`AgentError` naming what an action needs, instead of a bare KeyError/TypeError the
    model cannot act on."""
    if value is None:
        raise AgentError(f"dispatch action={action!r} needs `{name}`")
    return value
