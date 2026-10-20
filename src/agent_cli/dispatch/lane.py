"""Serialize verification and isolate each run's temporary harnesses.

Supervisor verification and reverify take `verify:<short>` then one numbered lane below
`dispatch.max_concurrent_verify` (default 1). Supervisor acquisition waits up to
`dispatch.verify_wait_timeout_s` (default 1800 s) with no short guard held; reverify refuses a busy
lane immediately. Command timeouts start only after acquisition.

Evidence stays under `.agent/dispatches/<id>.verification/<run_id>/`. `PYTEST_DEBUG_TEMPROOT`
points to a fresh directory outside all checkouts so temporary harnesses cannot resolve
real main as their own root. Windows prefers a short drive-root Temp directory to avoid path limits,
falling back to system temp if creation fails; POSIX uses system temp. Record its path in
`pytest-temproot`; remove it only after cleanup is
confirmed. Keep `TMPDIR` unchanged because telemetry stores loss records there. Release lanes only
after cleanup settles.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import shutil
import stat
import tempfile
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from .. import leases as leases_mod
from .. import repos as repos_mod
from ..core import AgentError, agent_path, atomic_write
from ..launcher import is_windows
from ..records.narrow import as_dict, as_int, dict_list, dig
from . import telemetry_events
from .verification_evidence import Binding

WAIT_TIMEOUT_S = 1800.0
POLL_S = 1.0
TEMP = "pytest-temproot"

# The supervisor's wait reads this clock and sleeps through this function, so a test can pass
# 1800 s without waiting them.
_clock = time.monotonic
_sleep = time.sleep


class LaneBusy(AgentError):
    """Reverify found every verification lane held; nothing was checked out or recorded."""


def lane_area(short: str, k: int) -> str:
    return f"verify:{short}" if k == 0 else f"verify:{short}/{k}"


def lanes_of(rp: repos_mod.Repo) -> int:
    return as_int(rp.dispatch.get("max_concurrent_verify"), 1) or 1


def wait_limit(rp: repos_mod.Repo) -> float:
    raw = rp.dispatch.get("verify_wait_timeout_s")
    if not raw:
        return WAIT_TIMEOUT_S
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise AgentError(f"{rp.short}: dispatch.verify_wait_timeout_s must be a positive number; got {raw!r}")
    try:
        return float(raw)
    except OverflowError:
        raise AgentError(f"{rp.short}: dispatch.verify_wait_timeout_s is too large; got {raw!r}") from None


def run_area(binding: Binding) -> str:
    return agent_path(
        PurePosixPath(), "dispatches", f"{binding.dispatch_id}.verification", binding.run_id
    ).as_posix()


def _prefix(binding: Binding) -> str:
    """`av-` and a digest of the dispatch and run ids. The full ids put git's object paths under a
    Windows verifier's pytest temp dirs past the path limit, and pushes to local remotes failed."""
    digest = hashlib.sha256(f"{binding.dispatch_id}\0{binding.run_id}".encode()).hexdigest()[:16]
    return f"av-{digest}-"


def _temp(root: Path, binding: Binding) -> Path | None:
    """The run's recorded temp root, or None when none was made. Raises on a record that does
    not name this run's own temp root."""
    record = root.resolve() / run_area(binding) / TEMP
    if not record.exists():
        return None
    temp = Path(record.read_text().strip())
    if not temp.is_absolute() or not temp.name.startswith(_prefix(binding)):
        raise ValueError(f"{record} does not name this run's temp root")
    return temp


def temp_parent(worktree: Path) -> Path | None:
    """Prefer short Windows fixture paths; use system temp if the drive root is unavailable."""
    if not is_windows():
        return None
    parent = Path(worktree.anchor) / "Temp"
    try:
        parent.mkdir(exist_ok=True)
    except OSError:
        return None
    return parent


def prepare(root: Path, worktree: Path, **kwargs: Any):
    """`verification.prepare`, then the run's temp root, its record in the run area, and the
    verifier's `PYTEST_DEBUG_TEMPROOT` pointing at it."""
    from . import verification

    prepared = verification.prepare(root, worktree, **kwargs)
    temp = Path(
        tempfile.mkdtemp(prefix=_prefix(prepared.binding), dir=temp_parent(prepared.worktree))
    ).resolve()
    try:
        atomic_write(root.resolve() / run_area(prepared.binding) / TEMP, f"{temp}\n")
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return dataclasses.replace(
        prepared, environment={**prepared.environment, "PYTEST_DEBUG_TEMPROOT": str(temp)}
    )


def clear(root: Path, binding: Binding) -> bool:
    """Remove the run's temp root and nothing else; True once it is gone. Never raises."""
    try:
        temp = _temp(root, binding)
    except Exception:  # noqa: BLE001 — an unreadable record cannot prove the temp root is gone
        return False
    if temp is None:
        return True
    shutil.rmtree(temp, onexc=_writable_retry)
    return not temp.exists() and not temp.is_symlink()


def _writable_retry(func: Callable[..., Any], path: str, _exc: BaseException) -> None:
    """Retry Windows deletion after clearing a regular file's read-only bit; git objects can otherwise
    keep a temp root and its lane held. Never chmod links or their targets. Leave other failures
    for the caller's existence check.
    """
    try:
        if os.path.islink(path):
            return
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)
    except OSError:
        pass


def run_busy(root: Path, binding: Binding) -> bool:
    """The run's verifier may still act, or its temp root is still there."""
    from . import verification

    try:
        if verification.load_status(root, binding).requires_lease:
            return True
        temp = _temp(root, binding)
        return temp is not None and (temp.exists() or temp.is_symlink())
    except Exception:  # noqa: BLE001 — unreadable verifier state cannot free its lane
        return True


def _binding(d: dict[str, Any]) -> Binding | None:
    from .verification_gate import metadata

    value = metadata(d)
    return value[0] if value is not None and value[1] == "run" else None


def blocks(root: Path, d: dict[str, Any]) -> bool:
    """A supervisor's lane protects its run until the verifier's cleanup is confirmed and its
    temp root is gone, whatever the holder's pid or touch says."""
    try:
        binding = _binding(d)
    except Exception:  # noqa: BLE001 — malformed verification state cannot free a lane
        return True
    return binding is not None and run_busy(root, binding)


def tidy(root: Path, d: dict[str, Any]) -> None:
    """Remove the temp root of `d`'s supervisor run once its verifier's cleanup is confirmed."""
    from . import verification

    try:
        binding = _binding(d)
        if binding is not None and not verification.load_status(root, binding).requires_lease:
            clear(root, binding)
    except Exception:  # noqa: BLE001, S110 — a kept temp root keeps the lane; the reconciler retries
        pass


def take_now(
    root: Path, d: dict[str, Any], sid: str, note: str, **marks: Any
) -> tuple[str, list[tuple[str, str]]]:
    """One non-waiting try at a lane: the lane taken and what was newly taken. `LaneBusy` names
    every holder."""
    from .holdings import pick

    taken: list[tuple[str, str]] = []
    limit = lanes_of(repos_mod.load_registry(root).require(d["repo"]))
    try:
        k = pick(root, d, sid, note, limit, taken, lane_area, "verification lane", **marks)
    except AgentError:
        held = [
            (lane_area(d["short"], k), leases_mod.get(root, lane_area(d["short"], k))) for k in range(limit)
        ]
        names = ", ".join(f"{area} is held by {(lease or {}).get('session_id')}" for area, lease in held)
        raise LaneBusy(f"verification lane busy: {names}; try again once that verification ends") from None
    return lane_area(d["short"], k), taken


def concurrent_workers(root: Path, short: str) -> int:
    """Live repo slots in `short` right now, this dispatch's included."""
    prefix = f"repo-slot:{short}/".lower()
    return sum(
        1
        for lease in leases_mod.list_all(root)
        if str(lease.get("area") or "").lower().startswith(prefix)
        and lease.get("status") in leases_mod.BLOCKING_STATUSES
    )


def _stale(d: dict[str, Any], attempt: int) -> bool:
    return d["state"] == "cancelled" or d["attempt"] != attempt or bool(d.get("terminal_committed"))


def give_up(
    root: Path,
    dispatch_id: str,
    attempt: int,
    outcome: str,
    reason: str,
    waited: float,
    rerun: dict[str, Any] | None = None,
) -> None:
    """Record that no verifier starts for this attempt, with why, under the record guard. `rerun`
    is the head, effective config digest and extras a failed certificate ran with, captured when
    it began, so `dispatch reverify` reruns exactly that ."""
    from .state import save
    from .verification_gate import record_guard

    with record_guard(root, dispatch_id) as d:
        if d["attempt"] != attempt or d.get("terminal_committed") or d.get("verification") is not None:
            return
        d["verification_result"] = {"status": "incomplete", "reasons": [reason]}
        d["verification_wait"] = {
            "attempt": attempt,
            "outcome": outcome,
            "reason": reason,
            "verify_wait_s": round(waited, 3),
            "concurrent_workers": concurrent_workers(root, d["short"]),
            # A lane timeout or a failed certificate run can be rerun later (`dispatch reverify`):
            # the phase-5 dogfood found a certificate timed out under load and not reverifiable.
            **(rerun if rerun is not None else _rerunnable(root, d) if outcome == "timeout" else {}),
        }
        save(root, d)


def rerun_binding(run_dir: Path, extra: tuple[str, ...]) -> dict[str, Any] | None:
    """What a failed certificate run actually verified, from its own evidence: the head and the
    effective config digest bound into its terminal snapshot, with the selection's extras. None
    when the evidence cannot be read, and then no rerun is offered (inputs read
    elsewhere could describe different commands from the ones that ran)."""
    import json

    from .verification_evidence import Evidence

    try:
        evidence = Evidence.from_dict(json.loads((run_dir / "terminal.json").read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001 — no readable evidence, no rerun binding
        return None
    b = evidence.binding
    return {"head": b.final_sha, "config_digest": b.config_digest, "extra": list(extra)}


def _rerunnable(root: Path, d: dict[str, Any]) -> dict[str, str]:
    """The head and config a timed-out wait would have verified, so `dispatch reverify` can run
    them later. Empty when either cannot be read: reverify then refuses as before."""
    from .verification_evidence import config_digest
    from .verification_gate import worktree_head

    try:
        rp = repos_mod.load_registry(root).require(d["repo"])
        return {"head": worktree_head(Path(d["worktree"])), "config_digest": config_digest(rp.verification)}
    except Exception:  # noqa: BLE001 — the wait's own record must still be written
        return {}


def await_lane(root: Path, dispatch_id: str, attempt: int) -> tuple[str, float] | None:
    """The supervisor's wait for a lane, holding no short guard. Returns the lane and the seconds
    waited, or None once a cancel or the wait limit means no verifier starts (`give_up`)."""
    from .state import load

    d = load(root, dispatch_id)
    rp = repos_mod.load_registry(root).require(d["repo"])
    started = _clock()
    deadline = started + wait_limit(rp)
    while True:
        d = load(root, dispatch_id)
        if _stale(d, attempt):
            give_up(
                root,
                dispatch_id,
                attempt,
                "cancelled",
                "cancelled before the verifier started",
                _clock() - started,
            )
            return None
        try:
            area, _ = take_now(root, d, f"dispatch:{dispatch_id}", f"verify {dispatch_id}")
        except LaneBusy as exc:
            if _clock() >= deadline:
                reason = f"no verification lane within {wait_limit(rp):g} s; {exc}"
                give_up(root, dispatch_id, attempt, "timeout", reason, _clock() - started)
                return None
            _sleep(POLL_S)
            continue
        telemetry_events.transition(root, d, "verifying", wait_s=round(_clock() - started, 3))
        return area, _clock() - started


def finish(root: Path, binding: Binding, area: str, sid: str, cleanup_confirmed: bool) -> None:
    """Give the lane back once the command has ended: its temp root first, and only when the
    verifier's cleanup is confirmed; otherwise the lane stays held."""
    try:
        if cleanup_confirmed and clear(root, binding):
            leases_mod.release(root, area, sid)
    except Exception:  # noqa: BLE001, S110 — a kept lane is freed by the reconciler
        pass


def reconcile_one(root: Path, lease: dict[str, Any]) -> bool:
    """Free a lane whose holder died, once its verifier's cleanup is confirmed: the temp root is
    removed first. Never raises."""
    from . import verification
    from .state import load

    try:
        sid, area = str(lease["session_id"]), str(lease["area"])
        if sid.startswith("dispatch:"):
            binding = _binding(load(root, sid.removeprefix("dispatch:")))
        else:
            _, dispatch_id, run_id = sid.split(":", 2)
            rows = dict_list(load(root, dispatch_id).get("reverify_runs"))
            # unchecked on purpose: Binding validates each field
            raw: dict[str, Any] | None = next(
                (as_dict(r.get("binding")) for r in rows if dig(r, "binding", "run_id") == run_id), None
            )
            binding = Binding(**raw) if raw else None
        if binding is not None and not verification.load_status(root, binding).requires_lease:
            clear(root, binding)
        return leases_mod.release(root, area, sid)
    except Exception:  # noqa: BLE001 — a failed pass retries at the next new, collect or start
        return False


RECORD = "record:"


@contextmanager
def record_lane(
    root: Path, rp: repos_mod.Repo, run_id: str, wait_s: float, settled: Callable[[], bool] = lambda: True
) -> Generator[str]:
    """Hold one verification lane for a pass-record run, waiting at most `wait_s`, so landing and
    dispatch checks share the same resource cap. A dead holder releases only after `settled()`
    confirms cleanup; reconciliation retains capacity while owned processes may remain.
    """
    from .holdings import pick

    sid = f"{RECORD}{run_id}"
    holder = {"short": rp.short, "repo": rp.name}
    deadline = _clock() + wait_s
    while True:
        try:
            k = pick(
                root, holder, sid, f"pass record {run_id}", lanes_of(rp), [], lane_area, "verification lane"
            )
            break
        except AgentError as exc:
            if _clock() >= deadline:
                raise AgentError(
                    f"no verification lane within {wait_s:g} s for the pass record: {exc}"
                ) from None
            _sleep(POLL_S)
    area = lane_area(rp.short, k)
    try:
        yield area
    finally:
        if settled():
            leases_mod.release(root, area, sid)
