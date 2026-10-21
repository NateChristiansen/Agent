"""Detached dispatch supervisor: launch, record writes, deadlines, cancel and collect.

`driver.py` owns the Claude drive loop and imports record writers from this module inside
functions so test monkeypatches of `dispatch.supervisor` remain visible. Neither side resolves
the other at import time.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    CanUseTool,
    ClaudeSDKClient,
    PermissionResult,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from .. import features as features_mod
from .. import maintenance, sessions
from .. import repos as repos_mod
from ..core import AgentError, atomic_write, is_alive, iso, now
from ..permissions import shown_tool
from ..records.narrow import as_dict
from ..vault import live
from . import client_version, holdings
from . import verification_gate as gate
from .driver import detach, note, run_worker
from .evaluate import evaluate, queue_auto_resume
from .handoffs import write_handoff
from .preflight import check_verification_preflight, verification_preflight
from .prompt import compose_followup, compose_restart
from .resume_mode import resumes
from .rules import (
    build_options,
    codex_options_preview,
    decide,
    options_preview,
    worker_env,
    write_worker_files,
)
from .state import (
    file,
    load,
    release_repo_lease,
    save,
    stored_envelope,
)
from .terminal import finalize
from .worktrees import AttemptClock, run_worktree_setup


def inbox_path(root: Path, dispatch_id: str) -> Path:
    """Where `dispatch say` leaves a message for a running worker. A file rather than a FIFO,
    so writing to it never blocks when no supervisor is reading. A steer sent a moment
    too early still gets picked up."""
    return file(root, dispatch_id, ".inbox")


def say(root: Path, *, dispatch_id: str, message: str) -> dict[str, Any]:
    """Queue a steer on the live SDK connection inside the current turn (see ADR-0009). Persist it as
    a manager turn too, so follow-up composition and handoffs preserve the complete ordered thread.
    """
    text = (message or "").strip()
    if not text:
        raise AgentError("nothing to say; pass a message")
    d = load(root, dispatch_id)
    if d["state"] not in ("queued", "running"):
        raise AgentError(
            f"dispatch {dispatch_id} is {d['state']}; `say` only reaches a running worker. "
            f"Continue a stopped one with the dispatch tool: action resume, dispatch_id {dispatch_id}, `answer`."
        )
    # A record can say `running` long after the supervisor that was reading the inbox died.
    # Nothing else ever rewrites the state in that case. Queuing a message there
    # looks like it was delivered and is read by nobody, so refuse and name the command that
    # reconciles the record instead.
    if d.get("supervisor_pid") and not is_alive(d["supervisor_pid"]):
        raise AgentError(
            f"dispatch {dispatch_id} says {d['state']} but its supervisor (pid "
            f"{d['supervisor_pid']}) is gone, so nothing would read the message. Reconcile it "
            f"first with the dispatch tool, action collect, dispatch_id {dispatch_id}."
        )
    path = inbox_path(root, dispatch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    path.write_text(f"{existing}\n\n{text}".strip() + "\n", encoding="utf-8")
    # The supervisor writes this record from another process under `record_guard`. A bare
    # load-then-save lost that race and the steer never reached the thread.
    with gate.record_guard(root, dispatch_id) as latest:
        latest["thread"].append({"ts": iso(now()), "from": "manager", "text": text, "steer": True})
        save(root, latest)
    return {"id": dispatch_id, "queued": text, "inbox": str(path), "pending": bool(existing)}


def worker_stop(root: Path, payload: dict[str, Any]) -> str:
    """Stop / SessionEnd inside a worker. Stores the transcript path on the dispatch json and
    routes the capture into the feature's .agent/raw/ with dispatch_id stamped. Collect stays
    the supervisor's job.

    One implementation, two callers: `rules.sdk_hooks` registers it as an
    in-process `ClaudeAgentOptions.hooks` callback on the supervisor's own client. That is how
    every real worker reaches it now. `hooks.worker_stop` keeps the `bin/agent hook
    worker-stop` command form for anything driving a worker without the SDK host.
    """
    from .. import capture as capture_mod

    sid = str(payload.get("session_id") or "")
    dispatch_id = os.environ.get("AGENT_DISPATCH_ID") or ""
    if not dispatch_id and sid:
        record = sessions.get(root, sid)
        dispatch_id = str((record or {}).get("dispatch_id") or "")
    if dispatch_id and payload.get("transcript_path"):
        try:
            load(root, dispatch_id)  # a missing or unreadable record is tolerated, as before
        except AgentError:
            dispatch_id = ""
    if dispatch_id and payload.get("transcript_path"):
        with gate.record_guard(root, dispatch_id) as d:  # a failed write still surfaces
            d["transcript_path"] = str(payload["transcript_path"])
            save(root, d)
    kind = capture_mod.EVENT_TO_KIND.get(str(payload.get("hook_event_name") or ""), "stop")
    try:
        capture_mod.capture(root, kind, payload, sid or None)
    except AgentError:
        pass
    return ""


# ------------------------------------------------------------------ the permission callback


def _input_head(tool_input: Any) -> str:
    """Fixed event input for a fall-through decision.

    The callback receives arbitrary tool arguments. Event records feed `dispatch show` and
    handoffs, so they must never retain those arguments. The worker still receives the full
    diagnostic separately in `PermissionResultDeny` below.
    """
    del tool_input
    return "[tool arguments omitted]"


def _event_reason(decision: str) -> str:
    """Decision-only event reason; never persist a diagnostic built from tool input."""
    if decision == "allow":
        return "Fall-through callback allowed this call."
    return "Fall-through callback denied this call; detailed diagnostic returned to worker."


def record_permission_event(root: Path, d: dict[str, Any], event: dict[str, Any]) -> None:
    """Append one event to the record, re-reading immediately first. The manager process
    writes this same file (`say`, `cancel`), so the copy the supervisor is holding is stale."""
    with gate.record_guard(root, d["id"]) as latest:
        latest["permission_events"] = (latest.get("permission_events") or []) + [event]
        save(root, latest)


def permission_callback(root: Path, d: dict[str, Any], repo: repos_mod.Repo) -> CanUseTool:
    """Record and deny a tool call that falls through worker permission rules. The control protocol
    blocks the worker until this callback answers, so persist the rule reason, subagent identity
    and tool-use id before answering.

    Transport `permission_denials` remains an independent after-the-fact cross-check. Do not merge
    it with these first-hand events. The worker stops with `question` on denial.
    """

    async def can_use_tool(
        tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResult:
        decision, reason = decide(tool_name, tool_input, d, repo, root)
        event = {
            "ts": iso(now()),
            "tool": shown_tool(tool_name),
            "input": _input_head(tool_input),
            "input_omitted": True,
            "agent_id": context.agent_id,
            "tool_use_id": context.tool_use_id,
            "decision": decision,
            "reason": _event_reason(decision),
        }
        try:
            record_permission_event(root, d, event)
        except Exception as exc:  # noqa: BLE001 — the record is the point, but the worker is
            # sitting inside the tool call waiting for this: a failed write must not wedge it
            sys.stderr.write(f"note: could not record a permission event for {d['id']}: {exc}\n")
        if decision == "allow":
            return PermissionResultAllow()
        from ..hooks.decision_log import worker_denial

        command = str(tool_input.get("command") or "") if tool_name == "Bash" else ""
        command = command or tool_name
        worker_denial(root, str(d["id"]), "claude", "worker-permission", command, reason)
        return PermissionResultDeny(message=reason)

    return can_use_tool


def start(root: Path, d: dict[str, Any], *, watch: bool) -> dict[str, Any]:
    if watch:
        return run(root, dispatch_id=d["id"])
    with gate.record_guard(root, d["id"]) as current:
        return detach(root, {**current, "allocator_pid": None})


popen = subprocess.Popen  # the supervisor spawner; tests replace it to assert the detach


def run(root: Path, *, dispatch_id: str) -> dict[str, Any]:
    """The supervisor loop: launch, wait with timeout, evaluate, auto-resume once per A7, finalize
    (handoff, log, render, notify). Called detached by `new`/`resume` or inline with --watch."""
    reconcile_allocations(root)
    reconcile(root)
    d = load(root, dispatch_id)
    if d["state"] != "queued":
        raise AgentError(f"dispatch {dispatch_id} is {d['state']}, not queued; use resume or collect")
    feat = None
    outcome: dict[str, Any] = {}
    try:
        feat = features_mod.resolve(root, str(d["feature"]))
        while True:
            # Detached queues and automatic retries must observe the current registry too.
            try:
                rp = repos_mod.load_registry(root).require(d["repo"])
                check_verification_preflight(verification_preflight(rp, no_checks=d.get("no_checks", False)))
                maintenance.check(root, feat.number, "auto_resume" if int(d["attempt"]) > 1 else "launch")
            except AgentError as exc:
                return finalize(root, d, feat, {"state": "blocked", "error": str(exc)})
            d = _launch_attempt(root, d, feat, rp)
            d = load(root, d["id"])
            outcome = evaluate(root, d, rp)
            reason = outcome.get("auto_resume")
            if reason and d["attempt"] < d["max_attempts"]:
                with gate.record_guard(root, d["id"]) as current:
                    if current["state"] != "cancelled" and not gate.requires_lease(root, current):
                        d = queue_auto_resume(root, current, feat, outcome, reason)
                        continue
            if outcome["state"] == "done":
                gate.execute(root, d["id"], d["attempt"])
            return finalize(root, d, feat, outcome)
    except Exception as exc:  # noqa: BLE001 — cleanup first; unconfirmed cleanup retains the lease
        try:
            settled = gate.settle(root, dispatch_id, cancel=True, retry_busy=True)
        except gate.TerminalBusy:
            settled = False  # left running under a dead supervisor, which `collect` finalizes
        if settled:
            _reap_dispatch_tree(root, dispatch_id)
            current = load(root, dispatch_id)
            if feat is not None:
                finalize(
                    root,
                    current,
                    feat,
                    {**outcome, "state": "failed", "error": f"supervisor failed ({type(exc).__name__})"},
                )
            else:
                with gate.terminal_guard(root, dispatch_id) as current:
                    current["supervisor_pid"] = None
                    save(root, current)
            release_repo_lease(root, current)
        raise
    finally:
        # Never let the legacy process reaper replace the verifier's own cleanup protocol.
        if not gate.requires_lease(root, load(root, dispatch_id)):
            _reap_dispatch_tree(root, dispatch_id)


def _reap_dispatch_tree(root: Path, dispatch_id: str) -> None:
    """What this dispatch left running, reaped as the supervisor ends (the reasoning is on `procs.reap_dispatch`)."""
    from .. import procs

    result = procs.reap_dispatch(root, dispatch_id)
    if result["terminated"]:
        sys.stderr.write(f"note: reaped {result['terminated']} left by {dispatch_id}\n")
    if result.get("unverified_pids"):
        sys.stderr.write(f"warning: {dispatch_id} cleanup unverified for PIDs {result['unverified_pids']}\n")


def _launch_attempt(
    root: Path, d: dict[str, Any], feat: features_mod.Feature, rp: repos_mod.Repo
) -> dict[str, Any]:
    with gate.record_guard(root, d["id"]) as d:
        if d["state"] == "cancelled":
            return d
        if d["state"] != "queued":
            raise AgentError("dispatch attempt is already running or terminal")
        ts = iso(now())
        d["state"] = "running"
        d["started"] = d.get("started") or ts
        d["attempt_started"] = ts
        err = file(root, d["id"], ".err")
        d["err_offset"] = err.stat().st_size if err.exists() else 0  # this attempt's stderr starts here
        d["err_offsets"] = {**as_dict(d.get("err_offsets")), str(d["attempt"]): d["err_offset"]}
        if not d.get("start_sha") and Path(d.get("worktree") or "").is_dir():
            # The commit this dispatch began from, kept across resumes: the convention gate asks
            # for a reason only for comments this dispatch added, not ones already on the branch.
            d["start_sha"] = (
                repos_mod.git_or_none(["rev-parse", "HEAD"], Path(d["worktree"])) or ""
            ).strip() or None
        d.update(finished=None, exit_code=None, timed_out=False)
        # cleared here and written when the attempt ends, so `cancel` in another process can tell
        # "the supervisor acted on my cancel" from "the supervisor has not noticed yet"
        d["stop_reason"] = None
        d["supervisor_pid"], d["allocator_pid"] = os.getpid(), None
        d["error"] = None
        d["terminal_committed"] = False
        if d.get("setup_pending"):
            d["worker_cleanup_pending"] = True
        _hold_repo_lease(root, d, feat)
        save(root, d)
    clock = AttemptClock(root, d)
    if d.pop("setup_pending", False):
        # Install dependencies in the detached supervisor before the first worker attempt so launch
        # returns promptly and tests start with dependencies present. Use the same explicit
        # environment as the worker so postinstall controls prevent unwanted browser downloads and
        # setup timeouts.
        setup = clock.env(run_worktree_setup, root, rp, Path(d["worktree"]), worker_env(root, d, rp))
        with gate.record_guard(root, d["id"]) as d:
            d["setup"] = setup
            d.pop("setup_pending", None)
            d["worker_cleanup_pending"] = False
            save(root, d)
    # regenerate the settings/mcp/schema files every attempt, so a resume after a harness
    # fix (a widened allow list, a new contract line) runs with the current rules
    mcp = write_worker_files(root, d, rp)
    write_handoff(root, d, feat)
    live.write_quietly(root)
    resume = resumes(d)
    if resume:
        text = compose_followup(d)
    else:
        original = file(root, d["id"], ".prompt.md").read_text(encoding="utf-8")
        text = compose_restart(d, original) if d["attempt"] > 1 else original
    if d["attempt"] > 1:
        atomic_write(file(root, d["id"], f".prompt.{d['attempt']}.md"), text)
    provider = str(d.get("provider") or "claude")
    options = build_options(root, d, rp, resume=resume, mcp=mcp) if provider == "claude" else None
    preview = options_preview(options) if options is not None else codex_options_preview(d, rp, resume=resume)
    client = client_version.record(provider, preview, root=root)
    with gate.record_guard(root, d["id"]) as d:
        if d["state"] == "cancelled":
            return d
        if d["attempt"] > 1 and not resume:
            with file(root, d["id"], ".err").open("a", encoding="utf-8") as err:
                note(
                    err,
                    "fresh start: previous attempt established no observable session; replaying full work order",
                )
        d["options"] = preview
        d["client_versions"] = {
            **as_dict(d.get("client_versions")),
            str(d["attempt"]): client,
        }
        d["worker_cleanup_pending"] = True
        d["worker_cleanup_confirmed"] = False
        save(root, d)
    if provider == "codex":
        from .codex_transport import run_codex

        outcome = run_codex(
            root,
            d,
            rp,
            text=text,
            resume=resume,
            mcp_servers=mcp.config().get("mcpServers", {}),
            inbox=inbox_path(root, d["id"]),
            timeout=float(d["timeout_minutes"]) * 60.0,
        )
    else:
        assert options is not None
        outcome = run_worker(
            root,
            d,
            options,
            first_message=text,
            inbox=inbox_path(root, d["id"]),
            timeout=float(d["timeout_minutes"]) * 60.0,
        )
    with gate.record_guard(root, d["id"]) as latest:
        latest["worker_cleanup_pending"] = outcome.get("stop_reason") == "cleanup_failed"
        if not latest["worker_cleanup_pending"]:
            latest["claude_pid"] = None
            latest["worker_cleanup_confirmed"] = True
        latest["last_result"] = stored_envelope(outcome.get("last_result"))
        # Once a CLI session was observed, a later transport failure does not erase it.
        latest["session_established"] = bool(latest.get("session_established")) or bool(
            outcome.get("session_established")
        )
        latest["exit_code"] = outcome.get("exit_code")
        latest["timed_out"] = bool(outcome.get("timed_out"))
        latest["stop_reason"] = outcome.get("stop_reason")
        latest["estimated_cost_usd"] = float(outcome.get("estimated_cost_usd") or 0.0)
        if outcome.get("resolved_model"):
            latest["resolved_model"] = outcome["resolved_model"]
        if outcome.get("codex_thread_id"):
            latest["codex_thread_id"] = outcome["codex_thread_id"]
        latest["finished"] = iso(now())
        save(root, latest)
    live.write_quietly(root)
    return latest


def _hold_repo_lease(root: Path, d: dict[str, Any], feat: features_mod.Feature) -> None:
    """Transfer the worktree lease and stored repo slot to the detached supervisor pid. Reusing
    `dispatch:<id>` touches the same holdings, preserving the launch reservation that closes
    preview/launch races. Touch the worktree first. Refusal is fatal: running without holdings
    could overlap another worker or unfinished verifier.
    """
    note = f"dispatch {d['id']} (supervisor, attempt {d['attempt']})"
    holdings.hold(root, d, note=note)


# Tests replace this with a factory that returns a scripted `Transport`, so the whole
# supervisor loop runs with no `claude` process at all. `None` means the SDK builds its own
# subprocess transport, which is what every real dispatch uses.
transport_factory: Callable[..., Any] | None = None


def persist_result(root: Path, dispatch_id: str, envelope: dict[str, Any]) -> None:
    """Load-modify-save so a cancel written by another process is not clobbered."""
    with gate.record_guard(root, dispatch_id) as latest:
        latest["last_result"] = stored_envelope(envelope)
        save(root, latest)


def persist_tokens(root: Path, dispatch_id: str, tokens: int, requests: list[str] | None = None) -> None:
    """The running token count, written as the worker streams. Load-modify-save
    for the same reason `persist_result` is: another process may have written the record
    since. Cost is not set here. It exists only in the result message, at the end."""
    with gate.record_guard(root, dispatch_id) as latest:
        latest["tokens"] = int(tokens)
        if requests is not None:
            latest["token_requests"] = requests
        save(root, latest)


def remember_worker_pid(root: Path, d: dict[str, Any], client: ClaudeSDKClient) -> None:
    """The claude process the SDK spawned, when there is one. Best effort through a private
    attribute: it is only used for `ps` and for `cancel`'s last-resort kill, so losing it to
    an SDK rename costs a fallback, not the feature."""
    process = getattr(getattr(client, "_transport", None), "_process", None)
    pid = getattr(process, "pid", None)
    if pid:
        with gate.record_guard(root, d["id"]) as latest:
            latest["claude_pid"] = int(pid)
            latest["claude_pid_identity"] = gate.worker_identity(int(pid))
            save(root, latest)


# Separate lifecycle entry points share the same guarded terminal commit.
from .recovery import cancel, collect, reconcile, reconcile_allocations  # noqa: E402

__all__ = [
    "cancel",
    "collect",
    "inbox_path",
    "permission_callback",
    "persist_result",
    "persist_tokens",
    "popen",
    "record_permission_event",
    "remember_worker_pid",
    "run",
    "say",
    "start",
    "transport_factory",
    "worker_stop",
]
