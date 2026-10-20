# ruff: noqa
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
from . import holdings
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
from .state import file, load, release_repo_lease, save, stored_envelope
from .terminal import finalize
from .worktrees import AttemptClock, run_worktree_setup


def inbox_path(root: Path, dispatch_id: str) -> Path:
    return file(root, dispatch_id, ".inbox")


def say(root: Path, *, dispatch_id: str, message: str) -> dict[str, Any]:
    text = (message or "").strip()
    if not text:
        raise AgentError("nothing to say; pass a message")
    d = load(root, dispatch_id)
    if d["state"] not in ("queued", "running"):
        raise AgentError(
            f"dispatch {dispatch_id} is {d['state']}; `say` only reaches a running worker. Continue a stopped one with the dispatch tool: action resume, dispatch_id {dispatch_id}, `answer`."
        )
    if d.get("supervisor_pid") and (not is_alive(d["supervisor_pid"])):
        raise AgentError(
            f"dispatch {dispatch_id} says {d['state']} but its supervisor (pid {d['supervisor_pid']}) is gone, so nothing would read the message. Reconcile it first with the dispatch tool, action collect, dispatch_id {dispatch_id}."
        )
    path = inbox_path(root, dispatch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    path.write_text(f"{existing}\n\n{text}".strip() + "\n", encoding="utf-8")
    with gate.record_guard(root, dispatch_id) as latest:
        latest["thread"].append({"ts": iso(now()), "from": "manager", "text": text, "steer": True})
        save(root, latest)
    return {"id": dispatch_id, "queued": text, "inbox": str(path), "pending": bool(existing)}


def worker_stop(root: Path, payload: dict[str, Any]) -> str:
    from .. import capture as capture_mod

    sid = str(payload.get("session_id") or "")
    dispatch_id = os.environ.get("AGENT_DISPATCH_ID") or ""
    if not dispatch_id and sid:
        record = sessions.get(root, sid)
        dispatch_id = str((record or {}).get("dispatch_id") or "")
    if dispatch_id and payload.get("transcript_path"):
        try:
            load(root, dispatch_id)
        except AgentError:
            dispatch_id = ""
    if dispatch_id and payload.get("transcript_path"):
        with gate.record_guard(root, dispatch_id) as d:
            d["transcript_path"] = str(payload["transcript_path"])
            save(root, d)
    kind = capture_mod.EVENT_TO_KIND.get(str(payload.get("hook_event_name") or ""), "stop")
    try:
        capture_mod.capture(root, kind, payload, sid or None)
    except AgentError:
        pass
    return ""


def _input_head(tool_input: Any) -> str:
    del tool_input
    return "[tool arguments omitted]"


def _event_reason(decision: str) -> str:
    if decision == "allow":
        return "Fall-through callback allowed this call."
    return "Fall-through callback denied this call; detailed diagnostic returned to worker."


def record_permission_event(root: Path, d: dict[str, Any], event: dict[str, Any]) -> None:
    with gate.record_guard(root, d["id"]) as latest:
        latest["permission_events"] = (latest.get("permission_events") or []) + [event]
        save(root, latest)


def permission_callback(root: Path, d: dict[str, Any], repo: repos_mod.Repo) -> CanUseTool:

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
        except Exception as exc:
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


popen = subprocess.Popen


def run(root: Path, *, dispatch_id: str) -> dict[str, Any]:
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
                    if current["state"] != "cancelled" and (not gate.requires_lease(root, current)):
                        d = queue_auto_resume(root, current, feat, outcome, reason)
                        continue
            if outcome["state"] == "done":
                gate.execute(root, d["id"], d["attempt"])
            return finalize(root, d, feat, outcome)
    except Exception as exc:
        try:
            settled = gate.settle(root, dispatch_id, cancel=True, retry_busy=True)
        except gate.TerminalBusy:
            settled = False
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
        if not gate.requires_lease(root, load(root, dispatch_id)):
            _reap_dispatch_tree(root, dispatch_id)


def _reap_dispatch_tree(root: Path, dispatch_id: str) -> None:
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
        d["err_offset"] = err.stat().st_size if err.exists() else 0
        d["err_offsets"] = {**as_dict(d.get("err_offsets")), str(d["attempt"]): d["err_offset"]}
        if not d.get("start_sha") and Path(d.get("worktree") or "").is_dir():
            d["start_sha"] = (
                repos_mod.git_or_none(["rev-parse", "HEAD"], Path(d["worktree"])) or ""
            ).strip() or None
        d.update(finished=None, exit_code=None, timed_out=False)
        d["stop_reason"] = None
        d["supervisor_pid"], d["allocator_pid"] = (os.getpid(), None)
        d["error"] = None
        d["terminal_committed"] = False
        if d.get("setup_pending"):
            d["worker_cleanup_pending"] = True
        _hold_repo_lease(root, d, feat)
        save(root, d)
    clock = AttemptClock(root, d)
    if d.pop("setup_pending", False):
        setup = clock.env(run_worktree_setup, root, rp, Path(d["worktree"]), worker_env(root, d, rp))
        with gate.record_guard(root, d["id"]) as d:
            d["setup"] = setup
            d.pop("setup_pending", None)
            d["worker_cleanup_pending"] = False
            save(root, d)
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
    with gate.record_guard(root, d["id"]) as d:
        if d["state"] == "cancelled":
            return d
        if d["attempt"] > 1 and (not resume):
            with file(root, d["id"], ".err").open("a", encoding="utf-8") as err:
                note(
                    err,
                    "fresh start: previous attempt established no observable session; replaying full work order",
                )
        d["options"] = (
            options_preview(options) if options is not None else codex_options_preview(d, rp, resume=resume)
        )
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
    note = f"dispatch {d['id']} (supervisor, attempt {d['attempt']})"
    holdings.hold(root, d, note=note)


transport_factory: Callable[..., Any] | None = None


def persist_result(root: Path, dispatch_id: str, envelope: dict[str, Any]) -> None:
    with gate.record_guard(root, dispatch_id) as latest:
        latest["last_result"] = stored_envelope(envelope)
        save(root, latest)


def persist_tokens(root: Path, dispatch_id: str, tokens: int, requests: list[str] | None = None) -> None:
    with gate.record_guard(root, dispatch_id) as latest:
        latest["tokens"] = int(tokens)
        if requests is not None:
            latest["token_requests"] = requests
        save(root, latest)


def remember_worker_pid(root: Path, d: dict[str, Any], client: ClaudeSDKClient) -> None:
    process = getattr(getattr(client, "_transport", None), "_process", None)
    pid = getattr(process, "pid", None)
    if pid:
        with gate.record_guard(root, d["id"]) as latest:
            latest["claude_pid"] = int(pid)
            save(root, latest)


from .recovery import cancel, collect, reconcile, reconcile_allocations

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
