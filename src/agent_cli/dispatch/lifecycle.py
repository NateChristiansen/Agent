"""Create fresh dispatches and resume owner turns."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any, Literal

from .. import features as features_mod
from .. import launcher, maintenance, prices, sessions, worker_mcp
from .. import log as log_mod
from .. import repos as repos_mod
from ..core import AgentError, atomic_write, iso, now
from ..records.narrow import as_dict, as_str
from ..repos_validate import check_timeout_minutes, resolve_timeout
from . import (
    allocation,
    holdings,
    holdings_view,
    machine,
    notice_producers,
    orders,
    telemetry_events,
    test_select,
    write_scope,
)
from . import rubric as rubric_mod
from . import spend as spend_mod
from . import verification_gate as gate
from .handoffs import dispatch_log_text, write_handoff
from .notify import default_channels
from .preflight import answer_preflight, check_verification_preflight, verification_preflight
from .prompt import compose_prompt, last_manager_message
from .rules import (
    build_settings,
    check_preflight,
    launch_options_preview,
    preflight,
    verifier_env,
    write_worker_files,
)
from .state import (
    DISPATCH_KINDS,
    MAX_ATTEMPTS,
    WORKER_CONTRACT,
    branch_name,
    file,
    first_line,
    handoff_path,
    load,
    make_nonce,
    render,
    save,
    worktree_path,
)
from .supervisor import start
from .worktrees import ensure_worktree, git_commits, launch_base, reuse_worktree, worktree_dirty


def _launching_session(root: Path, session_id: str | None) -> str | None:
    try:
        return sessions.resolve_session_id(root, session_id)
    except AgentError:
        return None


def new(
    root: Path,
    *,
    repo: str,
    feature: str,
    prompt: str,
    tasks: tuple[str, ...] | list[str] = (),
    budget: float | None = None,
    budget_reason: str | None = None,
    max_turns: int | None = None,
    timeout_minutes: float | None = None,
    model: str | None = None,
    effort: str | None = None,
    choice_reason: str | None = None,
    just_go: bool = False,
    branch: str | None = None,
    base: str | None = None,
    target: str | None = None,
    dry_run: bool = False,
    preview_id: str | None = None,
    watch: bool = False,
    session_id: str | None = None,
    worker_session_id: str | None = None,
    allow_unmatched: bool = False,
    mcp: list[str] | tuple[str, ...] | None = None,
    no_checks: bool = False,
    escalate_from: str | None = None,
    kind: str = "work",
    provider: str = rubric_mod.CLAUDE,
    row: str | None = None,
    writes: list[str] | None = None,
    max_attempts: int | None = None,
    task_check: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create and start a dispatch (A1 `new`). `task_check` is the summary of the pickup check
    the caller ran before launch; it is kept on the record and never gates. `provider` and `row`
    pick the model, effort and
    budget (`rubric.py`). `kind` is `work` or `probe`: a probe's return is the result, never an
    ask, and its spend is counted apart. `session_id` is the manager's session (for
    `feature="current"`); `worker_session_id` pins the worker's uuid (tests). `allow_unmatched`
    overrides the preflight refusal below (see ADR-0008), the one rule a launch may pass
    unverified;
    say why in the preview. `mcp` names agent.yaml `worker_mcp:` profiles (worker_mcp.py).
    `no_checks` exempts report checks and verification; permissions stay as configured.
    `escalate_from` reuses branch, worktree and effort; `max_turns` and `timeout_minutes` override
    repo values; `writes` reserves paths on the board (`write_scope.reserve`). `max_attempts`
    may lower the automatic-retry cap from `MAX_ATTEMPTS` (1 queues no timeout retry)."""
    if not prompt or not prompt.strip():
        raise AgentError("the work order is empty; pass `prompt`")
    if max_attempts is not None and (type(max_attempts) is not int or not 1 <= max_attempts <= MAX_ATTEMPTS):
        raise AgentError(f"max_attempts must be an integer from 1 to {MAX_ATTEMPTS}; got {max_attempts!r}")
    attempt_limit = MAX_ATTEMPTS if max_attempts is None else max_attempts
    passed_timeout = check_timeout_minutes(timeout_minutes) if timeout_minutes is not None else None
    mcp_profiles = [str(m) for m in (mcp or [])]
    worker_mcp.require(root, mcp_profiles)
    if effort is not None and effort not in repos_mod.EFFORT_LEVELS:
        raise AgentError(f"--effort must be one of {', '.join(repos_mod.EFFORT_LEVELS)}; got {effort!r}")
    row_obj = rubric_mod.select_row(provider, row)
    if row_obj is not None and row_obj.name == "review":
        # A review writes nothing: an omitted `writes` would reserve the whole repo, and an omitted
        # target and base would cut the review from main instead of the build tip.
        if writes is None:
            writes = []
        if not (target or base or escalate_from):
            raise AgentError("a review needs `target` (the artifact branch or commit) or `base`")
    if max_turns is not None and int(max_turns) <= 0:
        raise AgentError("--max-turns must be a positive integer")
    if kind not in DISPATCH_KINDS:
        raise AgentError(f"kind must be one of {', '.join(DISPATCH_KINDS)}; got {kind!r}")
    old: dict[str, Any] | None = None
    if escalate_from:
        old = load(root, escalate_from)
        if old["state"] in ("queued", "running"):
            raise AgentError(f"{escalate_from} is {old['state']}; wait for it or cancel it first")
        if branch or base or target:
            raise AgentError(
                "--escalate-from reuses the old dispatch's branch and base; do not also pass --branch/--base/--target"
            )
        feature = str(old["feature"])
    feat = features_mod.resolve(root, feature, session_id)
    if not feat.is_open:
        raise AgentError(f"feature {feat.number} is {feat.status}; dispatch only into open features")
    maintenance.check(root, feat.number, "escalate" if escalate_from else "launch")
    registry = repos_mod.load_registry(root)
    rp = registry.require(repo)
    if old is not None and old.get("repo") != rp.name:
        raise AgentError(
            f"{escalate_from} was dispatched to {old.get('repo')}, not {rp.name}; pass that repo to escalate"
        )
    if rp.name not in (feat.meta.get("repos") or []):
        raise AgentError(
            f"{rp.short} is not in feature {feat.number}'s repos; add it with "
            f"the feature_set tool (feature {feat.id}, add_repo [{rp.short!r}]) first"
        )
    task_objs: list[features_mod.Task] = []
    for ref in tasks:
        t = features_mod.resolve_task(feat.body, str(ref))
        if not t.is_open:
            raise AgentError(f"task #{t.index} is already {t.state}: {t.text}")
        if t.index not in [x.index for x in task_objs]:
            task_objs.append(t)
    applied_timeout, timeout_source = resolve_timeout(rp, passed_timeout)
    default_budget, budget_usd, budget_source = rubric_mod.launch_budget(rp, row_obj, budget, budget_reason)
    parent_id = escalate_from if old is not None and effort is None else None
    if old is not None:
        model, effort = rubric_mod.escalate(old, rp, model, effort)
    base_ref = str(old["base"]) if old is not None else launch_base(root, rp, base, row=row, target=target)
    model, effort, choice_reason, effort_source = rubric_mod.resolve(
        rp, provider, row_obj, model, effort, choice_reason, parent_id
    )
    if provider == rubric_mod.CODEX:
        prices.require_price(model)
    channels = default_channels()
    if not (root / WORKER_CONTRACT).exists():
        raise AgentError(f"{WORKER_CONTRACT} is missing; the worker contract is part of the harness")
    launcher.require(root)
    verification = verification_preflight(rp, no_checks=no_checks)
    if not dry_run:
        check_verification_preflight(verification)
    tag = machine.launch_label(root, rp)  # ADR-0036: refuses with no valid label or no {suffix}
    seq, dispatch_id = allocation.allocate(root, feat, rp, tag)
    prepared = orders.preview(root, preview_id or dispatch_id, replace_invalid=dry_run and not preview_id)
    if preview_id and not prepared:
        raise AgentError("work-order preview missing; preview again before launch")
    identity = preview_id or (orders.new_id() if dry_run else prepared.get("preview_id"))
    if dry_run and not preview_id:
        prepared = {}
    nonce = str(prepared.get("nonce") or make_nonce(str(identity or dispatch_id)))
    escalated_from: tuple[str, str] | None = None
    forked_from_session: str | None = None
    if old is not None:
        wt = Path(old["worktree"])
        reused_wt = wt if (wt.exists() and not old.get("worktree_removed")) else None
        br = str(old["branch"])
        if provider != rubric_mod.CODEX:
            forked_from_session = str(old.get("session_id") or "") or None
        if escalate_from is not None and not forked_from_session:
            escalated_from = (escalate_from, last_manager_message(old))
    else:
        suffix = f"-{str(identity).removeprefix('preview-')[:12]}" if identity else ""
        planned = as_dict(prepared.get("choices"))
        br = branch or as_str(planned.get("branch")) or (branch_name(feat, seq, rp, tag=tag) + suffix)
        reused_wt = allocation.reusable(root, feat, rp, tag, br, preview_id=identity)
        fresh = worktree_path(root, rp, feat, seq, tag)
        if suffix:
            prefix = f"{feat.number}-{feat.slug}"
            fresh = fresh.with_name(prefix + suffix + fresh.name.removeprefix(prefix))
        wt = Path(as_str(planned["worktree"])) if planned.get("worktree") else reused_wt or fresh
    sid = worker_session_id or str(uuid.uuid4())
    text = compose_prompt(
        dispatch_id=str(identity or dispatch_id),
        nonce=nonce,
        repo=rp,
        feature=feat,
        prompt=prompt,
        tasks=task_objs,
        worktree=wt,
        branch=br,
        base=base_ref,
        root=root,
        escalated_from=escalated_from,
        forked_from=escalate_from if forked_from_session else None,
    )
    ts = iso(now())
    d: dict[str, Any] = {
        "id": dispatch_id,
        **({"preview_id": identity} if identity else {}),
        "kind": (old or {}).get("kind") or kind,
        "feature": feat.id,
        "feature_slug": feat.slug,
        "feature_dir": feat.rel_dir(root),
        "repo": rp.name,
        "short": rp.short,
        "worktree": str(wt),
        "reused": reused_wt is not None,
        "branch": br,
        "base": base_ref,
        "selection_policy": test_select.stamp(rp.abs_path(root), base_ref, old),  # ADR-0045
        "session_id": sid,
        "nonce": nonce,
        "state": "queued",
        "attempt": 1,
        "max_attempts": attempt_limit,
        "budget_usd": budget_usd,
        "budget_default_usd": default_budget,
        "budget_reason": budget_reason,
        "provider": provider,
        "row": row_obj.name if row_obj is not None else None,
        "writes": write_scope.canonical_writes(writes),
        "task_check": task_check,
        "model": model,
        "effort": effort,
        "effort_source": effort_source,
        "choice_reason": choice_reason,
        "timeout_minutes": applied_timeout,
        "timeout_source": timeout_source,
        "max_turns": int(max_turns) if max_turns is not None else rp.max_turns,
        "tasks": [t.index for t in task_objs],
        "notify": channels,
        "just_go": bool(just_go),
        "mcp": mcp_profiles,
        "no_checks": bool(no_checks),
        "escalated_from": escalate_from or None,
        "launched_by_session": _launching_session(root, session_id),
        "forked_from_session": forked_from_session,
        "supervisor_pid": None,
        "claude_pid": None,
        "started": None,
        "finished": None,
        "exit_code": None,
        "timed_out": False,
        "terminal_committed": False,
        "stop_reason": None,
        "cost_usd": 0.0,
        "cost_accounting": spend_mod.N1,
        "cost_attempts": {},
        "fork_parent_cost_usd": spend_mod.parent_total(old, forked_from_session),
        "tokens": 0,
        "num_turns": 0,
        "last_result": None,
        "transcript_path": None,
        "handoff": handoff_path(feat, rp, seq, tag).relative_to(root).as_posix(),
        **machine.provenance(root, tag, dry_run=True, inspect_claim=dry_run),
        "result": None,
        "thread": [{"ts": ts, "from": "manager", "text": prompt.strip("\n")}],
        "auto_resumes": [],
        "chat_pending": False,
        # No verdict read yet (`verdicts.py`). The key's presence marks a record that
        # never falls back to `chat_pending` for whether its verdict was read.
        "verdict_read": None,
        "worktree_removed": False,
        "error": None,
        "created": ts,
    }
    settings = build_settings(root, rp, worktree=str(wt), mcp=mcp_profiles)
    preflight_started = time.monotonic()
    findings = preflight(root, d, rp, settings)
    if dry_run:
        return {
            **orders.publish(root, d, text, dry_run=True),
            "dry_run": True,
            "id": dispatch_id,
            "preview_id": identity,
            "feature": feat.id,
            "feature_slug": feat.slug,
            "goal": first_line(prompt),
            "repo": rp.name,
            "short": rp.short,
            "worktree": str(wt),
            "reused": reused_wt is not None,
            "branch": br,
            "base": base_ref,
            "provider": provider,
            "row": d["row"],
            "model": d["model"],
            "effort": d["effort"] or "vendor default",
            "effort_source": effort_source,
            "choice_reason": d["choice_reason"],
            "budget_usd": budget_usd,
            "budget_default_usd": default_budget,
            "budget_source": budget_source,
            "budget_reason": budget_reason,
            "timeout_minutes": applied_timeout,
            "timeout_source": timeout_source,
            "max_attempts": d["max_attempts"],
            "tasks": [f"#{t.index} {t.text}" for t in task_objs],
            "notify": channels,
            "mcp": mcp_profiles,
            "no_checks": bool(no_checks),
            "escalated_from": escalate_from or None,
            "forked_from_session": forked_from_session,
            "prompt_bytes": len(text.encode("utf-8")),
            "prompt": text,
            "prompt_lines": len(text.splitlines()),
            "preflight": findings,
            # Names only, never values: a missing toolchain grant (uv, npm, ...) shows here,
            # before launch, not later as `command not found` in a blocked verification log.
            "verification": {**verification, "env": sorted(verifier_env(rp))},
            "options": launch_options_preview(root, d, rp, settings=settings),
            **holdings_view.preview(root, rp, wt),
        }
    check_preflight(findings, allow_unmatched=allow_unmatched)
    holdings.refuse_backpressure(root, rp)
    telemetry_events.preflight(
        root, d, findings, round(time.monotonic() - preflight_started, 3), "work-order"
    )
    # A launch leaves the label claim unread above: `allocation.claim` sets it before the record exists.
    d["install"] = machine.install(root)["token"]
    orders.check(root, d, text)
    d["notice_route"] = notice_producers.new_route(root, d["launched_by_session"])
    with write_scope.reserve(root, rp.name, writes):
        allocation.claim(root, d, feat, rp, tag, seq=seq, reused=reused_wt, escalated=old is not None)
    stage = "work order"
    try:
        publication = orders.publish(root, d, text, dry_run=False, allocated=True)
        if publication.get("work_order_url"):
            d.update(publication)
        stage = "checkout"
        created = (reuse_worktree if reused_wt is not None else ensure_worktree)(root, rp, wt, br, base_ref)
        stage = "setup"
        d["setup_pending"] = bool(created.get("created"))
        d["setup_timing"] = telemetry_events.setup_timing(1, created)
        save(root, d)
        stage = "session"
        if d.get("work_order_url"):
            orders.verify(root, str(d["work_order"]), str(d["work_order_sha256"]))
        atomic_write(file(root, dispatch_id, ".prompt.md"), text, newline="\n")
        sessions.set_record(
            root,
            sid,
            feature={"id": feat.id, "slug": feat.slug, "dir": feat.rel_dir(root)},
            cwd=str(wt),
            name=f"dispatch {dispatch_id}",
            dispatch_id=dispatch_id,
        )
        stage = "worker files"
        write_worker_files(root, d, rp, settings)
        stage = "handoff"
        write_handoff(root, d, feat)
        log_mod.append(feat, "dispatch", dispatch_log_text(d, budget_reason, "just-go" if just_go else "yes"))
        render(root)
        stage = "supervisor start"
        return start(root, d, watch=watch)
    except BaseException as exc:
        allocation.fail(root, d, stage, exc)
        raise


# ------------------------------------------------------------------ public: resume / cancel


def resume(
    root: Path,
    *,
    dispatch_id: str,
    answer: str,
    answer_by: Literal["manager", "owner"] = "owner",
    budget: float | None = None,
    budget_reason: str | None = None,
    timeout_minutes: float | None = None,
    watch: bool = False,
    allow_unmatched: bool = False,
    again: bool = False,
    writes: list[str] | None = None,
) -> dict[str, Any]:
    d = _prepare_resume(
        root,
        dispatch_id=dispatch_id,
        answer=answer,
        answer_by=answer_by,
        budget=budget,
        budget_reason=budget_reason,
        timeout_minutes=timeout_minutes,
        allow_unmatched=allow_unmatched,
        again=again,
        writes=writes,
    )
    return start(root, d, watch=watch)


def _prepare_resume(
    root: Path,
    *,
    dispatch_id: str,
    answer: str,
    answer_by: Literal["manager", "owner"] = "owner",
    budget: float | None = None,
    budget_reason: str | None = None,
    timeout_minutes: float | None = None,
    allow_unmatched: bool = False,
    again: bool = False,
    writes: list[str] | None = None,
) -> dict[str, Any]:
    """Send an answer to a stopped dispatch, preflighted like a work order.
    `allow_unmatched` waves commands; `timeout_minutes` and `budget` replace limits.
    A fourth attempt needs `again=True` or escalation with `new --escalate-from`."""
    d = load(root, dispatch_id)
    rp = repos_mod.load_registry(root).require(d["repo"])
    check_verification_preflight(verification_preflight(rp, no_checks=d.get("no_checks", False)))
    # The board lock first, as a new launch takes it (one lock order).
    with (
        write_scope.board_lock(root),
        allocation.alloc_guard(root, d["short"]),
        gate.record_guard(root, dispatch_id) as d,
    ):
        if gate.requires_lease(root, d):
            raise AgentError("dispatch cleanup is incomplete; collect it before resuming")
        rp = repos_mod.load_registry(root).require(d["repo"])
        check_verification_preflight(verification_preflight(rp, no_checks=d.get("no_checks", False)))
        if d["state"] in ("queued", "running"):
            raise AgentError(f"dispatch {dispatch_id} is {d['state']}; wait for it or cancel it first")
        if d.get("provider") == rubric_mod.CODEX:
            prices.require_price(d.get("model"))
        next_attempt = int(d["attempt"]) + 1
        if next_attempt >= 4 and not again:
            escalate_cmd = (
                f"the dispatch tool, action new, repo {d['short']}, escalate_from {dispatch_id}, and a prompt"
            )
            raise AgentError(
                f"resuming {dispatch_id} again would be attempt {next_attempt}. Past "
                "three rounds on the same question is thrash, not progress — a fresh dispatch with a "
                f"stricter bar beats another resume. Escalate instead: {escalate_cmd}  "
                "(or pass again=True to resume anyway)"
            )
        if not answer or not answer.strip():
            raise AgentError("`answer` is empty")
        passed_timeout = check_timeout_minutes(timeout_minutes) if timeout_minutes is not None else None
        feat = features_mod.resolve(root, str(d["feature"]))
        if not feat.is_open:
            raise AgentError(f"feature {feat.number} is {feat.status}; resume only into open features")
        maintenance.check(root, feat.number, "resume")
        wt = Path(d["worktree"])
        if budget is not None:
            d["budget_usd"] = rubric_mod.resolve_budget(budget, budget_reason, float(d["budget_default_usd"]))
            d["budget_reason"] = budget_reason or d.get("budget_reason")
        spend_mod.require_budget_left(d, root)
        if passed_timeout is not None:
            d["timeout_minutes"] = passed_timeout
            d["timeout_source"] = "passed"
        answer_findings = answer_preflight(root, d, rp, answer)
        check_preflight(answer_findings, allow_unmatched=allow_unmatched)
        undo = write_scope.check_resume(root, d, narrow_to=writes)  # `writes` may only narrow
        try:
            taken = holdings.resume(root, d, note=f"dispatch {dispatch_id} (resume)")
        except Exception:
            undo()
            raise
    try:
        if not wt.exists() or d.get("worktree_removed"):
            created = ensure_worktree(root, rp, wt, d["branch"], d["base"], resume=True)
            d["worktree_removed"] = False
            d["setup_timing"] = telemetry_events.setup_timing(int(d["attempt"]) + 1, created)
        else:
            head = repos_mod.git_or_none(["rev-parse", "--abbrev-ref", "HEAD"], wt)
            if head is not None and head.strip() and head.strip() != d["branch"]:
                raise AgentError(
                    f"worktree {wt} is now on {head.strip()}; a later dispatch reused it — start a "
                    f"fresh dispatch (it reads the branch fresh) or `git worktree add` one for "
                    f"{d['branch']} by hand"
                )
        previous_session = d.get("session_established")
        if previous_session is None:
            previous_session = d.get("last_result") is not None
        if not previous_session and (
            git_commits(wt, str(d.get("start_sha") or d["base"])) or worktree_dirty(wt)
        ):
            raise AgentError(
                f"dispatch {dispatch_id} produced no observable Claude session but changed its "
                "worktree; inspect the branch before starting a fresh worker"
            )
        notice_producers.record_answer(d, answer_by, answer.strip("\n"))  # carries an open question
        d["attempt"] += 1
        d["max_attempts"] = d["attempt"] + 1
        d["state"] = "queued"
        notice_producers.supersede(d, "resume")
        d.update(chat_pending=False, terminal_committed=False, verification=None)
        d["prior_verification"] = d.get("verification_result")
        d["verification_result"] = {"status": "unverified", "reasons": ["new attempt"]}
        d["verification_cleanup_pending"] = False
        d["worker_cleanup_pending"] = False
        d.update(cancel_requested=None, cancel_unverified_pids=[], cancel_inventory_error="")
        d["result"] = d.get("result")
        save(root, d)
    except Exception:
        holdings.rollback(root, taken)
        raise
    finally:
        undo()  # this resume's own reservation: the saved record holds the paths now
    sessions.set_record(root, d["session_id"], dispatch_id=d["id"])
    write_handoff(root, d, feat)
    return d
