"""Dispatch-owned verifier publication, acceptance and cleanup (see ADR-0018).

The binding comes from guarded dispatch state, never worker output. Recovery reads durable
results and never executes commands. Malformed lifecycle state retains the repository lease.
"""

from __future__ import annotations

import os
import secrets
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .. import repos, verify_record
from ..core import AgentError, is_alive, iso, now
from ..records.narrow import as_dict_or_none, as_str
from . import certify, no_diff, telemetry_events, test_select
from .allocation import GUARD_TIMEOUT_S, allocating
from .guards import area_guard
from .state import load, save
from .verification_evidence import (
    INFRASTRUCTURE,
    Assessment,
    Binding,
    Evidence,
    assess,
    binding_of,
    config_digest,
)

EXEMPTION = "explicit dispatch --no-checks"
CLEANUP_WAIT = 8.0
TERMINAL_RETRY_SECONDS = 60.0


class TerminalBusy(AgentError):
    """Terminal publication exhausted its retry budget without taking the guard."""


@contextmanager
def terminal_guard(root: Path, dispatch_id: str, *, budget: float | None = None):
    """Retry acquisition only; errors inside the guarded operation are never retried. Every
    supervisor in a repo shares one record area, so the supervisor's own guards use this too."""
    deadline = time.monotonic() + (TERMINAL_RETRY_SECONDS if budget is None else budget)
    delay = 0.05
    while True:
        guard = record_guard(
            root, dispatch_id, timeout=max(0, min(GUARD_TIMEOUT_S, deadline - time.monotonic()))
        )
        try:
            current = guard.__enter__()
        except AgentError as exc:
            if "guard is busy" not in str(exc):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TerminalBusy(str(exc)) from exc
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 1.0)
        else:
            break
    try:
        yield current
    finally:
        guard.__exit__(None, None, None)


@contextmanager
def record_guard(root: Path, dispatch_id: str, *, timeout: float | None = None):
    d = load(root, dispatch_id)
    area = f"dispatch-records:{d['short']}" if d.get("short") else f"dispatch:{dispatch_id}"
    with area_guard(root, area, timeout=GUARD_TIMEOUT_S if timeout is None else timeout):
        current = load(root, dispatch_id)
        if current.get("short") != d.get("short") or current.get("repo") != d.get("repo"):
            raise AgentError("dispatch repository identity changed")
        yield current


def metadata(d: dict[str, Any]) -> tuple[Binding, str] | None:
    if d.get("verification") is None:
        return None
    raw = as_dict_or_none(d.get("verification"))
    if (
        raw is None
        or set(raw) != {"schema_version", "binding", "mode"}
        or type(raw["schema_version"]) is not int
        or raw["schema_version"] != 1
        or raw["mode"] not in ("run", "exempt", "record")
        or as_dict_or_none(raw["binding"]) is None
    ):
        raise ValueError("invalid dispatch verification metadata")
    return binding_of(raw["binding"]), as_str(raw["mode"])


def worker_identity(pid: int) -> dict[str, Any]:
    """Taken once `pid` is known, so the worker started no later than `since`."""
    return {"pid": pid, "since": time.time()}


def worker_since(d: dict[str, Any]) -> float | None:
    """The recorded start bound for the current `claude_pid`; None for a record without one,
    or one written for another pid, which leaves the number alone to decide."""
    identity = as_dict_or_none(d.get("claude_pid_identity"))
    if identity is None or identity.get("pid") != d.get("claude_pid"):
        return None
    since = identity.get("since")
    return float(since) if isinstance(since, (int, float)) and not isinstance(since, bool) else None


def worker_seen(d: dict[str, Any], supervised: bool) -> str | None:
    """`process_identity` of the recorded worker, "unknown" when `ps` cannot tell, and None when
    no check applies: a live supervisor still holds the pid, a legacy record has no start bound,
    and Windows checks birth times in its own reap."""
    since = worker_since(d)
    if supervised or since is None or os.name == "nt":
        return None
    from .verification_status import process_identity

    return process_identity(d["claude_pid"], since) or "unknown"


def worker_running(d: dict[str, Any]) -> bool:
    from .verification_status import process_gone

    pid = d.get("claude_pid")
    if not pid:
        return False
    if type(pid) is not int or pid <= 1:
        raise ValueError("invalid worker process identity")
    return not process_gone(pid, worker_since(d))


def requires_lease(root: Path, d: dict[str, Any]) -> bool:
    """Fail closed, including malformed state; worker completion is not verifier completion."""
    from . import verification
    from .verification_store import Store

    try:
        value = metadata(d)
        if value is not None:
            binding, mode = value
            if mode == "run" and verification.load_status(root, binding).requires_lease:
                return True
            if mode in ("exempt", "record"):
                store = Store(root, binding)
                try:
                    evidence = Evidence.from_dict(store.read("terminal.json"))
                    if evidence.binding != binding or evidence.state != "exempt":
                        return True
                finally:
                    store.close()
        if (
            d.get("stop_reason") == "cleanup_failed" and d.get("worker_cleanup_confirmed") is not True
        ) or d.get("worker_cleanup_pending"):
            return True
        # A verification-only rerun (reverify.py) holds the lease while it runs, and keeps it
        # when its clean checkout could not be removed.
        from .reverify import cleanup_pending, run_bindings

        if cleanup_pending(d) or any(
            verification.load_status(root, b).requires_lease for b in run_bindings(d)
        ):
            return True
        return worker_running(d)
    except Exception:  # noqa: BLE001 — missing/unreadable/malformed state cannot free a lease
        return True


def lease_blocks(root: Path, lease: dict[str, Any]) -> bool:
    from .holdings import HELD_AREAS, REVERIFY, record_unsettled, run_blocks, slot_released

    sid = str(lease.get("session_id") or "")
    if sid.startswith(REVERIFY):
        return run_blocks(root, lease)
    if sid.startswith("record:"):  # a live holder's lease is live anyway; a dead one's needs the check
        return not is_alive(lease.get("pid")) and record_unsettled(root, sid)
    if not str(lease.get("area") or "").lower().startswith(HELD_AREAS) or not sid.startswith("dispatch:"):
        return False
    try:
        d = load(root, sid.removeprefix("dispatch:"))
    except Exception:  # noqa: BLE001 — no runtime record may exist during initial creation
        from .state import file

        return file(root, sid.removeprefix("dispatch:"), ".json").exists()
    if str(lease.get("area") or "").lower().startswith("verify:"):
        from .lane import blocks

        return blocks(root, d)
    if str(lease.get("area") or "").lower().startswith("repo-slot:") and slot_released(d):
        return False  # the worker is gone and the slot was given back (`holdings.release_slot`)
    return (
        requires_lease(root, d)
        or allocating(d)
        or bool(d.get("holdings_held"))
        or (
            d.get("state") in ("queued", "running")
            and is_alive(d.get("supervisor_pid"))
            and not d.get("terminal_committed")
        )
    )


def worktree_head(worktree: Path) -> str:
    from .verification_process import BASE_ENV, capture, system_git

    git = system_git()
    if git is None:
        raise AgentError("Git is unavailable for verification binding")
    env = {
        **BASE_ENV,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }
    code, output = capture(
        [git, "-c", "core.fsmonitor=false", "rev-parse", "--verify", "HEAD"], worktree, env, 2, 1024
    )
    if code:
        raise AgentError("verification HEAD observation failed")
    return output.decode("ascii").strip()


def stale(d: dict[str, Any], attempt: int) -> bool:
    return d["state"] == "cancelled" or d["attempt"] != attempt or bool(d.get("terminal_committed"))


def publish(root: Path, d: dict[str, Any], binding: Binding, mode: str, event_id: str) -> None:
    d["verification"] = {"schema_version": 1, "binding": asdict(binding), "mode": mode}
    d["verification_result"] = {"status": "incomplete", "reasons": ["verification pending"]}
    d["verification_telemetry"] = {
        "attempt": d["attempt"],
        "event_id": event_id,
        "run_id": binding.run_id,
        "duration_s": None,
    }
    save(root, d)


def execute(root: Path, dispatch_id: str, expected_attempt: int) -> None:
    """Only the active supervisor calls this; binding publication precedes subprocess launch. A
    runnable verifier first waits for a verification lane with no guard held,
    and cancellation is checked again once the lane is taken."""
    from . import lane, verification
    from .holdings import hold, release_slot
    from .options import verifier_env
    from .verification_store import Store

    with terminal_guard(root, dispatch_id) as d:
        if stale(d, expected_attempt) or d.get("verification") is not None:
            return  # never silently rerun a published run
        hold(root, d)
        repo = repos.load_registry(root).require(d["repo"])
        config = repo.verification
        if config is None and d.get("no_checks") is not True:
            d["verification_result"] = {
                "status": "unverified",
                "reasons": ["runnable verification is not configured"],
            }
            save(root, d)
            return
    # Checked for `--no-checks` reviews too: an unchanged review is the stronger, observed no-diff
    # exemption.
    unchanged = no_diff.eligible(Path(d["worktree"]), d.get("start_sha"))
    if unchanged is not None:
        event_id = telemetry_events.new_event_id()
        with terminal_guard(root, dispatch_id) as d:
            if stale(d, expected_attempt) or d.get("verification") is not None:
                return
            assert unchanged.head is not None
            binding = Binding(
                d["id"], d["attempt"], unchanged.head, config_digest(config), secrets.token_hex(16)
            )
            store = Store(root, binding, create=True)
            try:
                store.evidence(Evidence(1, binding, "exempt", None, None, (), 0, no_diff.NO_DIFF))
            finally:
                store.close()
            d["no_diff"] = {
                "attempt": d["attempt"],
                "start_sha": d["start_sha"],
                "relied_on": no_diff.relied_on(root, Path(d["worktree"]), d["start_sha"]),
            }
            publish(root, d, binding, "exempt", event_id)
        return
    if d.get("no_checks") is True:
        head, event_id = worktree_head(Path(d["worktree"])), telemetry_events.new_event_id()
        with terminal_guard(root, dispatch_id) as d:
            if stale(d, expected_attempt) or d.get("verification") is not None:
                return
            binding = Binding(d["id"], d["attempt"], head, config_digest(config), secrets.token_hex(16))
            store = Store(root, binding, create=True)
            try:
                store.evidence(Evidence(1, binding, "exempt", None, None, (), 0, EXEMPTION))
            finally:
                store.close()
            publish(root, d, binding, "exempt", event_id)
        return
    selector = test_select.selector_for(d)
    reused = config and verify_record.reusable(root, Path(d["worktree"]), d.get("start_sha"), selector)
    if reused is not None:
        head, pointer = reused
        event_id = telemetry_events.new_event_id()
        with terminal_guard(root, dispatch_id) as d:
            if stale(d, expected_attempt) or d.get("verification") is not None:
                return
            binding = Binding(d["id"], d["attempt"], head, config_digest(config), secrets.token_hex(16))
            store = Store(root, binding, create=True)
            try:
                store.evidence(Evidence(1, binding, "exempt", None, None, (), 0, pointer))
            finally:
                store.close()
            publish(root, d, binding, "record", event_id)
        return
    release_slot(root, dispatch_id, expected_attempt)
    if config is not None and certify.certify(root, dispatch_id, expected_attempt, d, config):
        return
    acquired = lane.await_lane(root, dispatch_id, expected_attempt)
    if acquired is None:
        return
    area, waited = acquired
    handle = prepared = None
    started = 0.0
    try:
        head = worktree_head(Path(d["worktree"]))
        selection = test_select.select_for(Path(d["worktree"]), d, head)
        if selection.scope == "refused":  # policy 2 never falls back to the whole suite (see ADR-0045)
            with terminal_guard(root, dispatch_id) as current:
                if not stale(current, expected_attempt):
                    test_select.publish(current, selection, head_sha=head, observed_at=iso(now()))
                    save(root, current)
            why = f"no scoped selection: {selection.reason}"
            lane.give_up(root, dispatch_id, expected_attempt, "incomplete", why, waited)
            return
        with terminal_guard(root, dispatch_id) as d:
            if stale(d, expected_attempt) or d.get("verification") is not None:
                lane.give_up(
                    root,
                    dispatch_id,
                    expected_attempt,
                    "cancelled",
                    "cancelled before the verifier started",
                    waited,
                )
                return
            assert config is not None
            event_id, main = telemetry_events.new_event_id(), telemetry_events.main_for(root)
            prepared = lane.prepare(
                root,
                Path(d["worktree"]),
                dispatch_id=d["id"],
                attempt=d["attempt"],
                final_sha=head,
                config=test_select.config_for(config, selection),
                environment=verifier_env(repo),
                launch_base=d.get("start_sha"),
                telemetry_sink=telemetry_events.sink_dir(main) if main is not None else None,
                telemetry_parent=event_id,
            )
            test_select.publish(d, selection, head_sha=head, observed_at=iso(now()))
            d["run_area"] = lane.run_area(prepared.binding)
            d["verification_wait"] = {
                "attempt": expected_attempt,
                "outcome": "acquired",
                "reason": None,
                "verify_wait_s": round(waited, 3),
                "concurrent_workers": lane.concurrent_workers(root, d["short"]),
            }
            publish(root, d, prepared.binding, "run", event_id)
            started = time.monotonic()
            handle = verification.launch_prepared(prepared)
    finally:
        if handle is None:
            from .. import leases

            if prepared is None or lane.clear(root, prepared.binding):
                leases.release(root, area, f"dispatch:{dispatch_id}")
    assert handle is not None and prepared is not None
    status = verification.wait(handle)
    _stash_duration(root, dispatch_id, expected_attempt, time.monotonic() - started)
    lane.finish(root, prepared.binding, area, f"dispatch:{dispatch_id}", status.cleanup_confirmed)


def _stash_duration(root: Path, dispatch_id: str, attempt: int, seconds: float) -> None:
    """The verifier's launch-to-result time for the terminal event; best effort (null on failure)."""
    try:
        with terminal_guard(root, dispatch_id) as d:
            stash = as_dict_or_none(d.get("verification_telemetry"))
            if stash is not None and stash.get("attempt") == attempt:
                stash["duration_s"] = round(seconds, 3)
                save(root, d)
    except Exception:  # noqa: BLE001 — telemetry never changes a dispatch's outcome
        return


def acceptance(root: Path, d: dict[str, Any]) -> Assessment:
    """Assess a fresh snapshot; terminal publication fences it by record revision."""
    from . import lane, verification
    from .verification_store import Store

    try:
        output = lifecycle_reason = ""
        value = metadata(d)
        wait = as_dict_or_none(d.get("verification_wait"))
        if value is None and wait is not None and wait.get("attempt") == d["attempt"] and wait.get("reason"):
            status = (
                "failed"
                if wait.get("outcome") == "failed" and not str(wait["reason"]).startswith(INFRASTRUCTURE)
                else "incomplete"
            )
            return Assessment(status, (str(wait["reason"]),))
        if value is None:
            return Assessment("unverified", ("supervisor verification evidence is missing",))
        binding, mode = value
        if binding.dispatch_id != d["id"] or binding.attempt != d["attempt"]:
            return Assessment("unverified", ("verification belongs to another dispatch or attempt",))
        repo = repos.load_registry(root).require(d["repo"])
        config = repo.verification
        if mode == "run":
            status = verification.load_status(root, binding)
            if status.requires_lease:
                return Assessment("incomplete", ("verifier activity or cleanup is unconfirmed",))
            evidence = status.evidence
            lifecycle_reason = str(status.lifecycle.get("reason") or "")
            if lifecycle_reason == "guardian_interrupted" and status.lifecycle.get("command_pid") is not None:
                lifecycle_reason = ""  # It may have died after activation; do not infer a startup fault.
            if evidence is not None and evidence.state != "running":
                try:
                    with (root / lane.run_area(binding) / "output.log").open("rb") as log:
                        output = log.read(verification.LOG_LIMIT).decode("utf-8", errors="replace")
                except FileNotFoundError:
                    pass
        else:
            store = Store(root, binding)
            try:
                evidence = store.read("terminal.json")
            finally:
                store.close()
        if mode == "record":
            return _record_acceptance(root, d, binding, evidence)
        no_diff_now = as_dict_or_none(d.get("no_diff")) or {}
        if mode == "exempt" and d.get("no_checks") is True and no_diff_now.get("attempt") != d["attempt"]:
            # An explicit exemption certifies nothing about the tree; it binds HEAD only, and a
            # review that reuses a dirty build worktree stays exempt.
            if worktree_head(Path(d["worktree"])) != binding.final_sha:
                return Assessment("unverified", ("HEAD differs from the exempt review binding",))
            return assess(
                evidence, expected=binding, config=config, current_tree=None, authorized_exemption=EXEMPTION
            )
        if mode == "exempt":
            # The automatic no-diff exemption granted itself, so it must still hold now.
            seen = no_diff.observe(Path(d["worktree"]), d.get("start_sha"))
            if seen.head != binding.final_sha:
                return Assessment("unverified", ("HEAD differs from the exempt review binding",))
            if not seen.clean:
                return Assessment(
                    "unverified", (f"the worktree changed after the exemption ({seen.reason})",)
                )
            authorized = no_diff.NO_DIFF if d.get("start_sha") and seen.head == d["start_sha"] else None
            return assess(
                evidence, expected=binding, config=config, current_tree=None, authorized_exemption=authorized
            )
        tree = verification.observe_tree(Path(d["worktree"]), launch_base=d.get("start_sha"))
        if not tree.clean_at(binding.final_sha):
            return Assessment(
                "unverified", ("fresh HEAD, tracked files or index differ from the verified commit",)
            )
        assessment = assess(
            evidence,
            expected=binding,
            config=None
            if config is None
            else test_select.accepted(Path(d["worktree"]), d, binding.final_sha, config),
            current_tree=tree,
            authorized_exemption=EXEMPTION if d.get("no_checks") is True else None,
            output=output,
            lifecycle_reason=lifecycle_reason,
        )
        scoped = test_select.label(d)
        return Assessment("verified", (scoped,)) if assessment.status == "verified" and scoped else assessment
    except Exception as exc:  # noqa: BLE001 — diagnostics name the class, never command/env values
        return Assessment("unverified", (f"verification evidence unavailable ({type(exc).__name__})",))


def _record_acceptance(root: Path, d: dict[str, Any], binding: Binding, raw: Any) -> Assessment:
    """The supervisor relied on a full pass record for this head's tree; it must still apply,
    and the worktree must still be clean at that head."""
    from . import verification

    evidence = Evidence.from_dict(raw)
    if evidence.binding != binding or evidence.state != "exempt":
        return Assessment("unverified", ("the reused record's evidence does not match this attempt",))
    tree = verification.observe_tree(Path(d["worktree"]), launch_base=d.get("start_sha"))
    if not tree.clean_at(binding.final_sha):
        return Assessment(
            "unverified", ("fresh HEAD, tracked files or index differ from the recorded commit",)
        )
    found, why = verify_record.lookup(root, binding.final_sha)
    if found is None or verify_record.pointer(found) != evidence.exemption:
        return Assessment("unverified", (f"the full pass record no longer applies: {why}",))
    return Assessment("verified", (evidence.exemption or "",))


def settle(
    root: Path, dispatch_id: str, *, cancel: bool, timeout: float = CLEANUP_WAIT, retry_busy: bool = False
) -> bool:
    """Bounded recovery/cancellation. Never rerun checks or infer success from worker output.
    With `retry_busy`, a busy guard is retried as `terminal_guard` does, then raises `TerminalBusy`."""
    from . import verification

    deadline = time.monotonic() + timeout
    while True:
        guard = terminal_guard(root, dispatch_id) if retry_busy else record_guard(root, dispatch_id)
        with guard as d:
            if allocating(d):
                return False
            try:
                pid = d.get("claude_pid")
                supervised = is_alive(d.get("supervisor_pid"))
                if pid and (cancel or not supervised):
                    from .verification_process import kill_group, kill_worker

                    if type(pid) is not int or pid <= 1:
                        raise ValueError("invalid worker process identity")
                    seen = worker_seen(d, supervised)
                    if seen == "unknown":
                        d["worker_cleanup_pending"] = True
                    elif seen == "reused" or (kill_worker if supervised else kill_group)(pid):
                        d["claude_pid"] = None
                        d["worker_cleanup_pending"] = False
                        d["worker_cleanup_confirmed"] = True
                    else:
                        d["worker_cleanup_pending"] = True
                value = metadata(d)
                if value is not None and value[1] == "run":
                    binding = value[0]
                    if cancel:
                        verification.request_cancel(root, binding)
                    verification.recover(root, binding)
                pending = requires_lease(root, d)
                d["verification_cleanup_pending"] = pending
                save(root, d)
            except Exception:  # noqa: BLE001 — failure retains protected lease and visible pending state
                d["verification_cleanup_pending"] = True
                save(root, d)
                return False
        if not pending:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
