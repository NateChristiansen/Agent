"""Run one dispatched Codex attempt through the app-server protocol."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any

from .. import permissions, prices, repos
from ..core import AgentError, iso, now
from ..records.narrow import as_dict, as_dict_or_none, dig
from . import notice_producers, spend, streams
from . import state as state_mod
from . import verification_gate as gate
from .codex_flood import Flood
from .codex_hooks import isolated_server
from .codex_normalize import codex_output_schema, final_text, result_envelope, usage_counts
from .codex_permissions import git_metadata_refusal
from .codex_policy import policy_for
from .codex_policy import writable_roots as _writable_roots
from .codex_rpc import CodexAppServer
from .options import RETURN_SCHEMA, max_tool_items, worker_env
from .rules import dispatch_rules
from .state import file, save


def run_codex(
    root: Path,
    d: dict[str, Any],
    repo: repos.Repo,
    *,
    text: str,
    resume: bool,
    mcp_servers: dict[str, Any],
    inbox: Path,
    timeout: float,
) -> dict[str, Any]:
    return asyncio.run(
        _run_codex(
            root, d, repo, text=text, resume=resume, mcp_servers=mcp_servers, inbox=inbox, timeout=timeout
        )
    )


async def _run_codex(
    root: Path,
    d: dict[str, Any],
    repo: repos.Repo,
    *,
    text: str,
    resume: bool,
    mcp_servers: dict[str, Any],
    inbox: Path,
    timeout: float,
) -> dict[str, Any]:
    from .driver import cancel_requested, take_inbox
    from .supervisor import persist_result, persist_tokens

    prices.require_price(d.get("model"))
    env = worker_env(root, d, repo)
    if os.environ.get("CODEX_HOME"):
        env["CODEX_HOME"] = os.environ["CODEX_HOME"]
    # No PYTHONPATH: it pointed every command the worker ran at the manager's `src`, so a Codex
    # worker's pytest tested main instead of its candidate. The policy hook imports
    # the harness on its own (`codex_permissions.hook_config` runs `python -I`).
    worktree = Path(d["worktree"])
    writable_roots = _writable_roots(worktree)
    policy = policy_for(d, repo, writable=writable_roots)
    sandbox_caches(env, writable_roots)
    trust_worktree(env, worktree)
    out_path = file(root, d["id"], ".out")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    streams.keep_previous(root, d)
    first_prompt = (
        text
        if resume
        else (
            (root / state_mod.WORKER_CONTRACT).read_text(encoding="utf-8")
            + "\n\nCodex tool note: use apply_patch for file edits. Shell redirection and printf writes do not "
            "match the dispatch Bash allow rules. For git add and git commit, request the shell "
            "tool's sandbox escalation on the first call: Codex protects Git metadata under "
            "workspace-write, and the dispatch host checks and approves these two commands "
            "against its existing rules. To undo your own probe edit, `git restore -- <path>` (or "
            "`git checkout -- <path>`) with the same escalation is approved for unprotected paths in "
            "this dispatch's writes. Any other escalation or patch approval is declined automatically "
            "by the dispatch host; Codex reports that as 'rejected by user', but no person saw it. A "
            "declined escalation is a denial; stop then and say which command and why.\n\n" + text
        )
    )
    deadline = time.monotonic() + timeout
    result: dict[str, Any] | None = None
    stop_reason = "error"
    established = False
    timed_out = False
    estimated_cost = 0.0
    cumulative_tokens = -1
    active_model = str(d.get("model") or "")
    thread_id = str(d.get("codex_thread_id") or "") if resume else ""
    if resume and not thread_id:
        raise AgentError(
            f"dispatch {d['id']} asks to resume a Codex thread but records no codex_thread_id; a "
            "resume-framed prompt would start a fresh thread without the work order. Restart the "
            "attempt instead: it reconstructs the full contract."
        )
    denials: list[str] = []
    tool_items: set[str] = set()
    agent_messages: set[str] = set()
    tool_item_cap = max_tool_items(d, repo)
    spent_before, budget_basis = prior_spend(d)
    server: CodexAppServer | None = None
    failure_recorded = False
    git_approvals = 0
    turn_count = 0
    final_answer: str | None = None
    turn_id = ""
    rules, _hidden = dispatch_rules(root, repo, d)
    thread_config = {
        # Required for the injected worker hook. isolated_server refuses foreign hooks and
        # verifies that only the generated policy and natively trusted product hooks, pinned
        # to the dispatch base, are enabled first.
        "bypass_hook_trust": True,
        "mcp_servers": _codex_mcp_servers(mcp_servers, rules["allow"], rules["deny"]),
        # Child threads have separate token usage and are outside this attempt's
        # soft budget accounting until the supervisor handles them explicitly.
        "agents": {"enabled": False},
        "features": {"multi_agent": False},
    }

    def take_usage(info: dict[str, Any]) -> str:
        """Count one usage update into the estimate: "counted", "unpriced", or "" for a stale or
        malformed update."""
        nonlocal cumulative_tokens, estimated_cost
        row = usage_counts(info)
        if row is None or row[1] <= cumulative_tokens:
            return ""
        usage, cumulative_tokens = row
        priced = prices.price_usage(active_model, usage)
        if priced is None:
            return "unpriced"
        estimated_cost += priced
        with contextlib.suppress(Exception):
            persist_tokens(root, d["id"], cumulative_tokens)
        persist_estimate(root, d["id"], estimated_cost)
        return "counted"

    def take_pricing(method: object, info: dict[str, Any]) -> str:
        """Apply a reroute or a usage update, in arrival order, so usage is priced at the model
        that served it. Returns what `take_usage` does, or "" for a reroute."""
        nonlocal active_model
        if method == "model/rerouted":
            active_model = str(info.get("toModel") or active_model)
            return ""
        return take_usage(info) if method == "thread/tokenUsage/updated" else ""

    async def deny_request(request: dict[str, Any]) -> dict[str, Any]:
        nonlocal git_approvals
        method = str(request.get("method") or "")
        params = as_dict_or_none(request.get("params"))
        refused_by = ""
        if method == "item/commandExecution/requestApproval" and params is not None:
            refused_by = git_metadata_refusal(root, d["id"], params)
            if not refused_by:
                git_approvals += 1
                return {"decision": "accept"}
        denials.append(method)
        # The generated hook and the worktree sandbox handle routine tools. Any other escalation
        # or interactive request is declined here, by the harness: Codex tells the worker the
        # user rejected it, so the decline is recorded as the host's own.
        await asyncio.to_thread(_host_decline, root, d, method, request, params, refused_by)
        if method == "item/tool/requestUserInput":
            return {"answers": {}}
        return {"decision": "decline"}

    try:
        async with isolated_server(
            root, repo, worktree, env=env, handle_request=deny_request, policy=policy
        ) as (
            rpc,
            hooks,
        ):
            thread_config["hooks"] = hooks
            server = rpc
            with out_path.open("w", encoding="utf-8", buffering=1) as out:
                if resume and thread_id:
                    await rpc.call(
                        "thread/resume",
                        {
                            "threadId": thread_id,
                            "cwd": str(worktree),
                            "model": active_model,
                            **policy.thread_params(),
                            "config": thread_config,
                        },
                    )
                else:
                    started = await rpc.call(
                        "thread/start",
                        {
                            "cwd": str(worktree),
                            "model": active_model,
                            **policy.thread_params(),
                            "config": thread_config,
                        },
                    )
                    thread_id = str(dig(started, "thread", "id") or "")
                    if not thread_id:
                        raise AgentError("Codex thread/start returned no thread id")
                    with gate.record_guard(root, d["id"]) as latest:
                        latest["codex_thread_id"] = thread_id
                        latest["session_established"] = True
                        latest["claude_pid"] = rpc.process.pid if rpc.process else None
                        save(root, latest)
                established = True
                while True:
                    params: dict[str, Any] = {
                        "threadId": thread_id,
                        "input": [{"type": "text", "text": first_prompt}],
                        "cwd": str(worktree),
                        **policy.turn_params(),
                        "outputSchema": codex_output_schema(RETURN_SCHEMA),
                    }
                    if d.get("effort"):
                        params["effort"] = d["effort"]
                    started_turn = await rpc.call("turn/start", params)
                    turn_id = str(dig(started_turn, "turn", "id") or "")
                    if not turn_id:
                        raise AgentError("Codex turn/start returned no turn id")
                    turn_count += 1
                    if cumulative_tokens < 0 and (guess := prompt_estimate(active_model, first_prompt)):
                        persist_estimate(root, d["id"], guess)
                    final_answer = None
                    flood = Flood()
                    result = None  # an answered question's result must not stand in for this turn's
                    question_deadline: float | None = None
                    delivered_messages: list[str] = []
                    while True:
                        clock = time.monotonic()
                        if clock > deadline or cancel_requested(root, d["id"]):
                            timed_out = clock > deadline
                            stop_reason = "timeout" if timed_out else "cancelled"
                            with contextlib.suppress(Exception):
                                await rpc.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
                            await _drain_interrupted(rpc, out, take_pricing)
                            break
                        message = take_inbox(inbox)
                        if message:
                            await rpc.call(
                                "turn/steer",
                                {
                                    "threadId": thread_id,
                                    "input": [{"type": "text", "text": message}],
                                    "expectedTurnId": turn_id,
                                },
                            )
                            delivered_messages.append(message)
                        try:
                            event = await asyncio.wait_for(rpc.events.get(), timeout=1)
                        except TimeoutError:
                            if question_deadline is not None and clock > question_deadline:
                                stop_reason = "idle"
                                break
                            continue
                        method = event.get("method")
                        info = as_dict(event.get("params"))
                        if method == "item/agentMessage/delta":
                            if (verdict := flood.take(str(info.get("delta") or ""))) == "stop":
                                err_path = file(root, d["id"], ".err")
                                stop_reason = await flood.stop(rpc, thread_id, turn_id, err_path)
                                break
                            if verdict == "skip":
                                continue  # counted, not written: the stream file stays bounded
                        out.write(json.dumps(event, default=str) + "\n")
                        if method == "connection/closed":
                            stop_reason = "error"
                            failure_recorded = True
                            _transport_failure(
                                root,
                                str(d["id"]),
                                str(info.get("error") or "Codex app-server closed its output"),
                                rpc,
                            )
                            break
                        if method == "model/rerouted":
                            take_pricing(method, info)
                        elif method == "thread/tokenUsage/updated" and (taken := take_pricing(method, info)):
                            if taken == "unpriced":
                                stop_reason = "pricing_unknown"
                                with contextlib.suppress(Exception):
                                    await rpc.call(
                                        "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}
                                    )
                                break
                            if spent_before + estimated_cost >= float(d["budget_usd"]):
                                stop_reason = "budget_exhausted"
                                with contextlib.suppress(Exception):
                                    await rpc.call(
                                        "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}
                                    )
                                break
                        elif method == "item/completed":
                            item = as_dict_or_none(info.get("item"))
                            answer = final_text(item) if item is not None else None
                            if answer is not None:
                                final_answer = answer
                            if item is not None and _count_item(item, tool_items, agent_messages):
                                if len(tool_items) >= tool_item_cap:
                                    stop_reason = "max_tool_items"
                                    with contextlib.suppress(Exception):
                                        await rpc.call(
                                            "turn/interrupt", {"threadId": thread_id, "turnId": turn_id}
                                        )
                                    break
                        elif method == "turn/completed":
                            completed = as_dict_or_none(info.get("turn"))
                            status = (
                                str(completed.get("status") or "failed")
                                if completed is not None
                                else "failed"
                            )
                            result = result_envelope(
                                session_id=str(d["session_id"]),
                                text=final_answer,
                                cost_usd=estimated_cost if cumulative_tokens >= 0 else None,
                                num_turns=turn_count,
                                status=status,
                                denials=denials,
                            )
                            result["git_metadata_approvals"] = git_approvals
                            persist_result(root, d["id"], result)
                            if dig(result, "structured_output", "status") == "question":
                                question_deadline = time.monotonic() + state_mod.QUESTION_IDLE_SECONDS
                                await asyncio.to_thread(notice_producers.question_hold, root, str(d["id"]))
                                break
                            stop_reason = "result"
                            break
                    if stop_reason != "error" and stop_reason != "idle" and stop_reason != "result":
                        break
                    if stop_reason == "result" or stop_reason == "idle":
                        break
                    # A question can be answered during the same connected session.
                    if question_deadline is not None:
                        # Surface every question before retaining an acknowledged early steer.
                        first_prompt = "\n\n".join(delivered_messages)
                        while not first_prompt and time.monotonic() < question_deadline:
                            first_prompt = take_inbox(inbox)
                            if first_prompt:
                                break
                            await asyncio.sleep(1)
                        if first_prompt:
                            await asyncio.to_thread(notice_producers.answered, root, str(d["id"]))
                            continue
                        stop_reason = "idle"
                    break
    except Exception as exc:  # noqa: BLE001 - supervisor receives a failed outcome
        if not failure_recorded:  # a closed connection already wrote its one entry
            _transport_failure(root, str(d["id"]), f"{type(exc).__name__}: {exc}", server)
        stop_reason = "error"
    if stop_reason == "pricing_unknown":
        with file(root, d["id"], ".err").open("a", encoding="utf-8") as err:
            err.write(f"Codex cost estimate unavailable for {active_model}; turn interrupted\n")
    if result is None and stop_reason in (
        "budget_exhausted",
        "max_tool_items",
        "timeout",
        "cancelled",
        "degenerate_output",
    ):
        cost_basis = ""
        if stop_reason in ("cancelled", "timeout") and cumulative_tokens < 0 and turn_count:
            guess = prompt_estimate(active_model, first_prompt)
            if guess is not None:
                estimated_cost, cost_basis = guess, PROMPT_ESTIMATE_BASIS
        result = result_envelope(
            session_id=str(d["session_id"]),
            text=final_answer,
            cost_usd=estimated_cost if cumulative_tokens >= 0 or cost_basis else None,
            num_turns=turn_count,
            status=stop_reason,
            denials=denials,
        )
        if cost_basis:
            result["cost_basis"] = cost_basis
        result["git_metadata_approvals"] = git_approvals
        persist_result(root, d["id"], result)
    if result is not None:
        # Separate units: protocol turns (`num_turns`), tool items against their cap, and agent
        # messages, which no cap counts. The budget figure says what earlier attempts added.
        result["codex_limits"] = {
            "protocol_turns": turn_count,
            "tool_items": len(tool_items),
            "max_tool_items": tool_item_cap,
            "agent_messages": len(agent_messages),
            "budget_spent_before_usd": spent_before,
            "budget_basis": budget_basis,
        }
        with contextlib.suppress(Exception):
            persist_result(root, d["id"], result)
    return {
        "last_result": result,
        "session_established": established,
        "exit_code": 0 if result is not None and stop_reason in ("result", "idle") else 1,
        "timed_out": timed_out,
        "stop_reason": stop_reason,
        "estimated_cost_usd": estimated_cost,
        "resolved_model": active_model,
        "codex_thread_id": thread_id,
    }


INTERRUPT_DRAIN_SECONDS = 2.0
"""How long a cancel or timeout reads on after `turn/interrupt` for the turn to close. Kept well
inside `CANCEL_GRACE_SECONDS`, after which `cancel` stops waiting and kills the app-server."""

PROMPT_ESTIMATE_BASIS = (
    "rough prompt-only estimate: the app-server reported no usage before the interrupt, so this "
    "prices only the prompt sent, as fresh input at four bytes a token"
)


async def _drain_interrupted(rpc: CodexAppServer, out: Any, take_pricing: Any) -> None:
    """Read until the interrupted turn completes, counting any usage it reports at the model it
    was rerouted to, so a cancel records what the turn spent as Claude's interrupt result does.
    Bounded by `INTERRUPT_DRAIN_SECONDS`; a server that sends nothing more costs only the wait."""
    until = time.monotonic() + INTERRUPT_DRAIN_SECONDS
    while (left := until - time.monotonic()) > 0:
        try:
            event = await asyncio.wait_for(rpc.events.get(), timeout=left)
        except TimeoutError:
            return
        out.write(json.dumps(event, default=str) + "\n")
        method = event.get("method")
        if method in ("turn/completed", "connection/closed"):
            return
        take_pricing(method, as_dict(event.get("params")))


def persist_estimate(root: Path, dispatch_id: str, cost: float) -> None:
    """The running estimate, on the record as it is known. `cancel` finalizes from the record
    once its grace runs out, and on Windows the supervisor is still tearing the app-server tree
    down then (or is killed), so an estimate held only in memory was recorded as $0.0."""
    with contextlib.suppress(Exception), gate.record_guard(root, dispatch_id) as latest:
        latest["estimated_cost_usd"] = cost
        save(root, latest)


def prompt_estimate(model: str, prompt: str) -> float | None:
    """`PROMPT_ESTIMATE_BASIS`. Codex's own instructions, tool schemas and any output are left
    out, and cached input is priced as fresh, so it can land on either side of the true cost."""
    return prices.price_usage(model, {"input_tokens": len(prompt.encode("utf-8")) // 4, "output_tokens": 0})


HOST_DECLINE_REASON = (
    "declined automatically by the dispatch host (no owner saw it): a headless Codex worker gets no "
    "escalation beyond git add/commit and worktree restores of its declared paths"
)


def _host_decline(
    root: Path,
    d: dict[str, Any],
    method: str,
    request: dict[str, Any],
    params: dict[str, Any] | None,
    refused_by: str = "",
) -> None:
    """Record an approval request the host declined, with its origin, kind, attempt and request id,
    on the record and in telemetry, never as an owner decision. A command escalation also records
    the check that refused it and the request's `cwd` and `kind`. Best effort: a failed record
    must not turn a decline into an accept."""
    from ..hooks.decision_log import MAX_REASON, shown, worker_denial
    from .supervisor import record_permission_event

    detail = ""
    if params is not None:
        detail = str(params.get("command") or params.get("reason") or params.get("message") or "")
    event: dict[str, Any] = {
        "ts": iso(now()),
        "origin": "host",
        "kind": method,
        "attempt": int(d.get("attempt") or 0),
        "request_id": request.get("id"),
        "decision": "decline",
        "reason": HOST_DECLINE_REASON,
        "detail": shown(detail)[:MAX_REASON],
    }
    if refused_by and params is not None:
        event["refused_by"] = refused_by
        cwd, worktree = str(params.get("cwd")), str(d.get("worktree") or "")
        if worktree and cwd.startswith(worktree):
            cwd = "<worktree>" + cwd[len(worktree) :]
        event["request_cwd"] = shown(cwd)[:MAX_REASON]
        event["request_kind"] = shown(str(params.get("kind")))[:64]
    if method == MCP_APPROVAL_METHOD and params is not None:
        # Field names only, so a declined MCP approval shows which shape Codex sent.
        meta = as_dict_or_none(params.get("_meta")) or {}
        event["request_fields"] = [shown(str(k))[:64] for k in sorted(params)][:24]
        event["meta_fields"] = [shown(str(k))[:64] for k in sorted(meta)][:24]
    with contextlib.suppress(Exception):
        record_permission_event(root, d, event)
    with contextlib.suppress(Exception):
        worker_denial(root, str(d["id"]), "codex", "host-decline", method, HOST_DECLINE_REASON)


MCP_APPROVAL_METHOD = "mcpServer/elicitation/request"


TOOL_ITEM_TYPES = frozenset({"commandExecution", "fileChange", "mcpToolCall"})


def _count_item(item: dict[str, Any], tool_items: set[str], agent_messages: set[str]) -> bool:
    """Count one completed item by id, so a repeated completion event counts once and parallel
    tools each count. True when it was a new tool item."""
    item_id = str(item.get("id") or "")
    kind = item.get("type")
    if kind == "agentMessage" and item_id:
        agent_messages.add(item_id)
    if kind not in TOOL_ITEM_TYPES or not item_id or item_id in tool_items:
        return False
    tool_items.add(item_id)
    return True


def prior_spend(d: dict[str, Any]) -> tuple[float, str]:
    """What earlier attempts spent, against which this attempt's estimate is checked, and its
    basis. The budget is the total across attempts (`spend.require_budget_left`). A record whose
    history is unresolved or predates the accounting rule counts nothing earlier and says so,
    as the resume check does: such a record is never refused on cost."""
    accounting = str(d.get("cost_accounting") or "")
    if accounting in (spend.N1, spend.NORMALIZED):
        return float(d.get("cost_usd") or 0), accounting
    return 0.0, f"unknown ({accounting or 'no accounting state'}): earlier attempts not counted"


def _transport_failure(root: Path, dispatch_id: str, message: str, server: CodexAppServer | None) -> None:
    """Record a transport failure once in the dispatch's `.err`, with the app-server's stderr tail."""
    from ..hooks.decision_log import redact_all

    tail = ""
    with contextlib.suppress(Exception):  # the failure is recorded even when the tail is not
        tail = server.stderr_tail() if server is not None else ""
    with file(root, dispatch_id, ".err").open("a", encoding="utf-8") as err:
        err.write(f"Codex transport error: {redact_all(message)}\n")
        if tail:
            # Bounded by `STDERR_TAIL` and redacted: an app-server may log request headers.
            err.write("Codex app-server stderr (tail):\n" + redact_all(tail) + "\n")


def approved_mcp_tools(server: str, allow: list[str], deny: list[str]) -> list[str]:
    """The tools of `server` this dispatch grants by an exact `mcp__<server>__<tool>` allow rule
    that no deny rule covers. A `mcp__<server>__*` grant names no tool, so it approves none."""
    if permissions.mcp_server_ambiguous(server):
        return []
    prefix = f"mcp__{server}__"
    return sorted(
        {
            rule[len(prefix) :]
            for rule in allow
            if permissions.mcp_canonical(server, rule)
            and rule != prefix + "*"
            and permissions.mcp_verdict(rule, allow, deny)[0] == "allowed"
        }
    )


def _codex_mcp_servers(
    raw: dict[str, Any], allow: list[str] | None = None, deny: list[str] | None = None
) -> dict[str, dict[str, Any]]:
    """Translate resolved MCP definitions into Codex config keys. Each granted tool is approved
    per tool (`tools.<tool>.approval_mode = "approve"`), so Codex runs it without an approval
    request the host would decline; every other tool keeps the server's prompting default."""
    servers: dict[str, dict[str, Any]] = {}
    for name, item in raw.items():
        value = as_dict_or_none(item)
        if value is None:
            continue
        server: dict[str, object]
        if isinstance(value.get("command"), str):
            server = {key: value[key] for key in ("command", "args", "env", "cwd") if key in value}
        elif isinstance(value.get("url"), str):
            server = {"url": value["url"]}
        else:
            continue
        server["required"] = True
        if tools := approved_mcp_tools(name, allow or [], deny or []):
            server["tools"] = {tool: {"approval_mode": "approve"} for tool in tools}
        servers[name] = server
    return servers


def sandbox_caches(env: dict[str, str], writable_roots: list[str]) -> None:
    """Put tool caches under writable shared Git metadata so sandboxed worktrees can reuse them
    without tracking them. Home caches may be outside the sandbox. Explicit repository UV_CACHE_DIR
    wins.
    """
    env.setdefault("UV_CACHE_DIR", str(Path(writable_roots[-1]) / "agent-uv-cache"))


def trust_worktree(env: dict[str, str], worktree: Path) -> None:
    """Trust only this worktree in command-scope Git config: the Windows sandbox account can differ
    from the owner and trigger dubious-ownership checks. Use GIT_CONFIG_PARAMETERS because Codex
    filters inherited names containing KEY; an orphaned GIT_CONFIG_COUNT would break Git.
    """
    value = "safe.directory=" + worktree.resolve().as_posix()
    quoted = "'" + value.replace("'", "'\\''") + "'"
    prior = env.get("GIT_CONFIG_PARAMETERS", "")
    env["GIT_CONFIG_PARAMETERS"] = f"{prior} {quoted}" if prior else quoted
