"""Rerun supervisor verification for a terminal dispatch without launching model work. collect does
not rerun checks; resume starts another model attempt.

Use a fresh detached checkout at the worker head bound in the evidence. Read but never edit the
worker worktree; require unchanged HEAD and report uncommitted changes as unverified. Append a new
reverify_runs binding and keep all old evidence. The terminal state and historical result are never
regenerated as verified.
"""

from __future__ import annotations

import os
import secrets
import shutil
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .. import repos
from ..core import AgentError, iso, now
from ..records.narrow import as_dict, as_list_or_none, dict_list, str_list
from . import holdings, notice_producers, telemetry_events, test_select
from .allocation import git_admin
from .state import TERMINAL_STATES, save
from .verification_evidence import Binding, assess, binding_of

CHECKOUT_TIMEOUT = 60.0
CLEANUP_TIMEOUT = 30.0


def _git(cwd: Path, *args: str, timeout: float = 5.0) -> tuple[int, str]:
    from .verification_process import BASE_ENV, capture, system_git

    git = system_git()
    if git is None:
        raise AgentError("Git is unavailable for verification recovery")
    env = {
        **BASE_ENV,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    # No repository hooks: `worktree add` would run a post-checkout hook from the worker's commit.
    config = ["-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}"]
    code, output = capture([git, *config, *args], cwd, env, timeout, 1024 * 1024)
    return code, output.decode("utf-8", "replace").strip()


def _run_rows(d: dict[str, Any]) -> list[dict[str, object]]:
    raw = as_list_or_none(d.get("reverify_runs") or [])
    rows = dict_list(raw)
    if raw is None or len(rows) != len(raw):
        raise ValueError("invalid reverify_runs")
    return rows


def run_bindings(d: dict[str, Any]) -> list[Binding]:
    """Every rerun binding recorded on the dispatch, oldest first. Raises on a malformed row."""
    rows = _run_rows(d)
    # A row without a binding records a checkout that failed before any verifier was prepared.
    return [binding_of(row["binding"]) for row in rows if row.get("binding") is not None]


def cleanup_pending(d: dict[str, Any]) -> bool:
    return any(row.get("cleanup_pending") for row in dict_list(d.get("reverify_runs")))


def _refuse_active(root: Path, d: dict[str, Any]) -> None:
    from . import verification
    from .verification_gate import metadata

    try:
        value = metadata(d)
        bindings = [value[0]] if value is not None and value[1] == "run" else []
        active = any(verification.load_status(root, b).requires_lease for b in bindings + run_bindings(d))
    except Exception as exc:  # noqa: BLE001 — unreadable state cannot prove nothing is running
        raise AgentError(
            f"dispatch {d['id']} has unreadable verification state ({type(exc).__name__}); refused"
        ) from None
    if active:
        raise AgentError(f"dispatch {d['id']} has a verification still active or awaiting cleanup")
    if cleanup_pending(d):
        raise AgentError(
            f"dispatch {d['id']} has a verification run in progress or a checkout whose cleanup failed"
        )


def _worker_tree(worktree: Path, launch_base: str | None) -> dict[str, Any]:
    """What the worker's worktree holds beyond its commits. Reported, never verified."""
    from . import verification

    try:
        tree = verification.observe_tree(worktree, launch_base=launch_base)
    except Exception as exc:  # noqa: BLE001 — a report, not a gate; name the class only
        return {"observed": False, "reason": type(exc).__name__, "verified": False}
    return {
        "observed": True,
        "head": tree.head,
        "tracked_clean": tree.tracked_clean,
        "index_clean": tree.index_clean,
        "verified": False,
    }


def _abort_checkout(
    root: Path, d: dict[str, Any], worktree: Path, holder: Path, checkout: Path, reason: str, run_id: str
) -> None:
    """Undo a checkout that failed before its verifier ran. The run's pending row goes and the
    holdings are released only when nothing is left behind; otherwise the row, binding-less and
    cleanup_pending, explains it."""
    from .verification_gate import record_guard

    removed = not checkout.exists()
    if not removed:
        try:
            with git_admin(root, d["short"]):
                code, _ = _git(
                    worktree, "worktree", "remove", "--force", str(checkout), timeout=CLEANUP_TIMEOUT
                )
            removed = code == 0 and not checkout.exists()
        except Exception:  # noqa: BLE001 — an unconfirmed removal keeps the lease
            removed = False
    with record_guard(root, d["id"]) as current:
        rows = _run_rows(current)
        if removed:
            current["reverify_runs"] = [row for row in rows if row.get("run_id") != run_id]
        for row in rows:
            if row.get("run_id") == run_id:
                row["result"] = {"status": "unverified", "reasons": [reason]}
        save(root, current)
    if removed:
        shutil.rmtree(holder, ignore_errors=True)
        holdings.reverify_release(root, current, run_id)


def _in_flight(root: Path, dispatch_id: str, row: dict[str, Any]) -> bool:
    """The run's own process may still be working on it: its row has no outcome yet and a live
    process holds its lane."""
    from .. import leases as leases_mod
    from ..core import is_alive

    if row.get("finished") or as_dict(row.get("result")).get("reasons") != ["verification pending"]:
        return False
    sid = f"{holdings.REVERIFY}{dispatch_id}:{row.get('run_id')}"
    return any(
        lease.get("session_id") == sid and is_alive(lease.get("pid")) for lease in leases_mod.list_all(root)
    )


def _holder_of(dispatch_id: str, checkout: Path) -> Path | None:
    """The `tempfile.mkdtemp` holder `_rerun` made for `checkout`, or None unless the checkout is
    `<temp>/<id>-reverify-*/tree` with no symlink or junction below the temp directory."""
    from ..links import is_link

    holder = checkout.parent
    if (
        not checkout.is_absolute()
        or checkout.name != "tree"
        or not holder.name.startswith(f"{dispatch_id}-reverify-")
        or is_link(holder)
        or is_link(checkout)
    ):
        return None
    temp = Path(tempfile.gettempdir()).resolve()
    if holder.parent.resolve() != temp or holder.resolve() != temp / holder.name:
        return None
    return holder


def _finish_one(root: Path, d: dict[str, Any], row: dict[str, Any]) -> bool:
    """Finish one stuck run's cleanup, each step confirmed before the next. False leaves the row
    pending and every holding in place."""
    from . import lane, verification
    from .verification_gate import record_guard

    run_id = str(row["run_id"])
    if _in_flight(root, d["id"], row):
        return False
    binding = binding_of(row["binding"]) if row.get("binding") is not None else None
    if binding is not None:
        status = verification.load_status(root, binding)
        if status.requires_lease or not status.cleanup_confirmed:
            return False
    checkout = Path(str(row["checkout"]))
    holder = _holder_of(d["id"], checkout)
    if holder is None:
        return False
    if checkout.exists():
        worktree = Path(d["worktree"])
        cwd = worktree if worktree.is_dir() else repos.load_registry(root).require(d["repo"]).abs_path(root)
        with git_admin(root, d["short"]):
            code, _ = _git(cwd, "worktree", "remove", "--force", str(checkout), timeout=CLEANUP_TIMEOUT)
        if code or checkout.exists():
            return False
    if binding is not None and not lane.clear(root, binding):
        return False
    shutil.rmtree(holder, ignore_errors=True)
    if holder.exists():
        return False
    with record_guard(root, d["id"]) as current:
        stored = next((r for r in _run_rows(current) if r.get("run_id") == run_id), None)
        if stored is None or not stored.get("cleanup_pending") or stored.get("binding") != row.get("binding"):
            return False
        stored.update(cleanup_pending=False, cleanup_finished=iso(now()))
        save(root, current)
        holdings.reverify_release(root, current, run_id)
    return run_id not in holdings.reverify_held(root, d["id"])


def finish_cleanup(root: Path, dispatch_id: str) -> list[str]:
    """Finish the cleanup of every reverify run whose cleanup failed and whose verifier is provably
    done, then clear its row and free its holdings; free what a finished run still holds. Returns
    the run ids finished. Never raises: a run it cannot finish stays pending or keeps its holdings,
    and the next caller retries it."""
    from .state import load

    finished: list[str] = []
    try:
        d = load(root, dispatch_id)
        rows = _run_rows(d)
        held = holdings.reverify_held(root, dispatch_id)
    except Exception:  # noqa: BLE001 — an unreadable record is refused by the caller's own checks
        return finished
    for row in rows:
        run_id = str(row.get("run_id"))
        try:
            if row.get("cleanup_pending"):
                done = _finish_one(root, d, row)
            elif run_id in held:
                done = holdings.reverify_free(root, dispatch_id, run_id)
            else:
                continue
            if done:
                finished.append(run_id)
        except Exception:  # noqa: BLE001, S112 — a run left pending is retried by the next caller
            continue
    return finished


def finish_stuck(root: Path, ds: list[dict[str, Any]]) -> None:
    """`finish_cleanup` for each of `ds` with a run whose cleanup is pending or that still holds a
    lease."""
    for d in ds:
        try:
            stuck = cleanup_pending(d) or bool(holdings.reverify_held(root, d["id"]))
        except Exception:  # noqa: BLE001, S112 — unreadable leases are retried by the next caller
            continue
        if stuck:
            finish_cleanup(root, d["id"])


def reverify(root: Path, dispatch_id: str) -> dict[str, Any]:
    """Rerun the supervisor checks for a terminal dispatch's recorded head. Returns the run row.

    Once its event id is allocated, a run records one `dispatch.reverify` event on every path,
    a raise included: the stored outcome when it got that far, else `incomplete`."""
    span: dict[str, Any] = {"event_id": None, "status": "incomplete"}
    span["caller"] = notice_producers.caller_session(root)
    try:
        return _rerun(root, dispatch_id, span)
    except BaseException:
        notice_producers.reverify_raised(root, dispatch_id, span.get("run_id"), span["caller"])
        raise
    finally:
        if span["event_id"] is not None:
            telemetry_events.reverify(
                root,
                span["d"],
                event_id=span["event_id"],
                run_id=span.get("run_id"),
                status=span["status"],
                worktree_s=span.get("worktree_s"),
                verify_s=span.get("verify_s"),
            )


def recorded_binding(root: Path, d: dict[str, Any]) -> Binding:
    from .verification_gate import metadata

    if d.get("state") not in TERMINAL_STATES or not d.get("terminal_committed"):
        raise AgentError(f"dispatch {d['id']} is not terminal; verification recovery refused")
    _refuse_active(root, d)
    value = metadata(d)
    if value is not None:
        return value[0]
    # A finished attempt that never got a lane: the wait recorded the head and
    # config it would have verified, so a rerun checks that head instead of needing a resume.
    wait = as_dict(d.get("verification_wait"))
    if (
        wait.get("outcome") in ("timeout", "failed")
        and wait.get("attempt") == d.get("attempt")
        and wait.get("head")
    ):
        return Binding(
            dispatch_id=str(d["id"]),
            attempt=int(d["attempt"]),
            final_sha=str(wait["head"]),
            config_digest=str(wait["config_digest"]),
            run_id="lane-timeout",
        )
    raise AgentError(f"dispatch {d['id']} has no recorded verification binding to rerun")


def _rerun(root: Path, dispatch_id: str, span: dict[str, Any]) -> dict[str, Any]:
    """The checks and the clean checkout run outside the records guard; the
    guard is held only to take the holdings and record the run's pending row, which keeps a
    resume or a second reverify out until the run's final save."""
    from . import lane, verification
    from .options import verifier_env
    from .state import load
    from .verification_gate import record_guard, worktree_head

    finish_cleanup(root, dispatch_id)
    d = load(root, dispatch_id)
    recorded = recorded_binding(root, d)
    head = recorded.final_sha
    worktree = Path(d["worktree"])
    if not worktree.is_dir():
        raise AgentError(f"dispatch {dispatch_id}'s worktree is gone; its head cannot be checked")
    if worktree_head(worktree) != head:
        raise AgentError(f"dispatch {dispatch_id}'s worktree HEAD differs from the recorded head {head}")
    code, _ = _git(worktree, "cat-file", "-e", f"{head}^{{commit}}")
    if code:
        raise AgentError(f"recorded head {head} is missing from the repository")
    repo = repos.load_registry(root).require(d["repo"])
    config = repo.verification
    if config is None:
        raise AgentError(f"repo {d['repo']} has no runnable verification configured")
    if recorded.run_id == "lane-timeout":  # rerun exactly what the wait recorded
        from .test_select import with_extra
        from .verification_evidence import config_digest

        config = with_extra(config, str_list(as_dict(d.get("verification_wait")).get("extra")))
        if config_digest(config) != recorded.config_digest:
            raise AgentError(
                f"dispatch {dispatch_id}'s verification configuration changed since the recorded attempt; "
                "resume it or launch fresh instead"
            )
    selection = None
    if test_select.policy_of(d) == 2:  # recovery runs the attempt's selection, never the suite (see ADR-0045)
        selection = test_select.select_for(worktree, d, head)
        if selection.scope == "refused":
            raise AgentError(f"dispatch {dispatch_id} has no scoped selection to rerun: {selection.reason}")
        config = test_select.config_for(config, selection)
    launch_base = d.get("start_sha")
    worker_tree = _worker_tree(worktree, launch_base)
    run_id = secrets.token_hex(16)
    holder = Path(tempfile.mkdtemp(prefix=f"{dispatch_id}-reverify-"))
    checkout = holder / "tree"
    try:
        with record_guard(root, dispatch_id) as d:
            if recorded_binding(root, d) != recorded:
                raise AgentError(
                    f"dispatch {dispatch_id} changed while reverify checked it; nothing was changed"
                )
            taken = holdings.reverify_take(root, d, run_id, checkout)
            try:
                if selection is not None:
                    # Acceptance re-derives and compares the republished selection.
                    test_select.publish(d, selection, head_sha=head, observed_at=None)
                started = iso(now())
                d.setdefault("reverify_runs", []).append(
                    {
                        "run_id": run_id,
                        "binding": None,
                        "checkout": str(checkout),
                        "started": started,
                        "observed_at": started,
                        "base_sha": launch_base,
                        "head_sha": head,
                        **(
                            {"unmapped_paths": list(selection.unmapped_paths)}
                            if selection is not None and selection.unmapped_paths is not None
                            else {}
                        ),
                        "result": {"status": "incomplete", "reasons": ["verification pending"]},
                        "cleanup_pending": True,
                    }
                )
                save(root, d)
            except BaseException:
                holdings.rollback(root, taken)
                raise
    except BaseException:
        shutil.rmtree(holder, ignore_errors=True)
        raise
    event_id = telemetry_events.new_event_id()
    span.update(event_id=event_id, d=dict(d))
    main = telemetry_events.main_for(root)
    try:
        checkout_started = time.monotonic()
        with git_admin(root, d["short"]):
            code, output = _git(
                worktree, "worktree", "add", "--detach", str(checkout), head, timeout=CHECKOUT_TIMEOUT
            )
        span["worktree_s"] = round(time.monotonic() - checkout_started, 3)
        if code:
            raise AgentError(f"clean verification checkout failed: {output[-300:]}")
        prepared = lane.prepare(
            root,
            checkout,
            dispatch_id=d["id"],
            attempt=d["attempt"],
            final_sha=head,
            config=config,
            environment=verifier_env(repo),
            launch_base=launch_base,
            telemetry_sink=telemetry_events.sink_dir(main) if main is not None else None,
            telemetry_parent=event_id,
            run_id=run_id,
        )
    except BaseException as exc:
        why = f"checkout failed ({type(exc).__name__})"
        _abort_checkout(root, d, worktree, holder, checkout, why, run_id)
        raise
    span["run_id"] = prepared.binding.run_id
    row: dict[str, Any] = {}
    with record_guard(root, dispatch_id) as d:
        for stored in dict_list(d.get("reverify_runs")):
            if stored.get("run_id") == run_id:
                stored.update(
                    binding=asdict(prepared.binding),
                    replaces_binding=asdict(recorded),
                    final_sha=head,
                    worker_tree=worker_tree,
                    run_area=lane.run_area(prepared.binding),
                )
                row = stored
        save(root, d)
    verify_started = time.monotonic()
    handle = verification.launch_prepared(prepared)
    status = verification.wait(handle)
    span["verify_s"] = round(time.monotonic() - verify_started, 3)
    try:
        tree = verification.observe_tree(checkout, launch_base=launch_base)
        output = ""
        try:
            with (root / lane.run_area(prepared.binding) / "output.log").open("rb") as log:
                output = log.read(verification.LOG_LIMIT).decode("utf-8", errors="replace")
        except FileNotFoundError:
            pass
        assessment = assess(
            status.evidence,
            expected=prepared.binding,
            config=config,
            current_tree=tree,
            output=output,
        )
        result = {"status": assessment.status, "reasons": list(assessment.reasons)}
    except Exception as exc:  # noqa: BLE001 — diagnostics name the class, never command output
        result = {
            "status": "unverified",
            "reasons": [f"verification evidence unavailable ({type(exc).__name__})"],
        }
    try:
        with git_admin(root, d["short"]):
            code, _ = _git(worktree, "worktree", "remove", "--force", str(checkout), timeout=CLEANUP_TIMEOUT)
    except AgentError:
        code = 1
    cleaned = (
        code == 0
        and not checkout.exists()
        and status.cleanup_confirmed
        and lane.clear(root, prepared.binding)
    )
    if cleaned:
        shutil.rmtree(holder, ignore_errors=True)
    with record_guard(root, dispatch_id) as d:
        for stored in dict_list(d.get("reverify_runs")):
            if stored.get("run_id") == run_id:
                stored.update(result=result, finished=iso(now()), cleanup_pending=not cleaned)
                row = stored
        nid = notice_producers.reverify_complete(
            root, d, prepared.binding.run_id, str(result["status"]), cleaned, span["caller"]
        )
        save(root, d)
        span.update(status=str(result["status"]), d=dict(d))
        if cleaned:
            holdings.reverify_release(root, d, run_id)
    if nid is not None:
        notice_producers.deliver(root, dispatch_id, nid)
    return row
