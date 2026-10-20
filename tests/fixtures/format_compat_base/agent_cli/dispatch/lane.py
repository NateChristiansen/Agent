# ruff: noqa
from __future__ import annotations
import dataclasses
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
from ..records.narrow import as_dict, as_int, dict_list, dig
from . import telemetry_events
from .verification_evidence import Binding

WAIT_TIMEOUT_S = 1800.0
POLL_S = 1.0
TEMP = "pytest-temproot"
_clock = time.monotonic
_sleep = time.sleep


class LaneBusy(AgentError):
    pass


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
    return f"agent-verify-{binding.dispatch_id}-{binding.run_id}-"


def _temp(root: Path, binding: Binding) -> Path | None:
    record = root.resolve() / run_area(binding) / TEMP
    if not record.exists():
        return None
    temp = Path(record.read_text().strip())
    if not temp.is_absolute() or not temp.name.startswith(_prefix(binding)):
        raise ValueError(f"{record} does not name this run's temp root")
    return temp


def prepare(root: Path, worktree: Path, **kwargs: Any):
    from . import verification

    prepared = verification.prepare(root, worktree, **kwargs)
    temp = Path(tempfile.mkdtemp(prefix=_prefix(prepared.binding))).resolve()
    try:
        atomic_write(root.resolve() / run_area(prepared.binding) / TEMP, f"{temp}\n")
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return dataclasses.replace(
        prepared, environment={**prepared.environment, "PYTEST_DEBUG_TEMPROOT": str(temp)}
    )


def clear(root: Path, binding: Binding) -> bool:
    try:
        temp = _temp(root, binding)
    except Exception:
        return False
    if temp is None:
        return True
    shutil.rmtree(temp, onexc=_writable_retry)
    return not temp.exists() and (not temp.is_symlink())


def _writable_retry(func: Callable[..., Any], path: str, _exc: BaseException) -> None:
    try:
        if os.path.islink(path):
            return
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        func(path)
    except OSError:
        pass


def run_busy(root: Path, binding: Binding) -> bool:
    from . import verification

    try:
        if verification.load_status(root, binding).requires_lease:
            return True
        temp = _temp(root, binding)
        return temp is not None and (temp.exists() or temp.is_symlink())
    except Exception:
        return True


def _binding(d: dict[str, Any]) -> Binding | None:
    from .verification_gate import metadata

    value = metadata(d)
    return value[0] if value is not None and value[1] == "run" else None


def blocks(root: Path, d: dict[str, Any]) -> bool:
    try:
        binding = _binding(d)
    except Exception:
        return True
    return binding is not None and run_busy(root, binding)


def tidy(root: Path, d: dict[str, Any]) -> None:
    from . import verification

    try:
        binding = _binding(d)
        if binding is not None and (not verification.load_status(root, binding).requires_lease):
            clear(root, binding)
    except Exception:
        pass


def take_now(
    root: Path, d: dict[str, Any], sid: str, note: str, **marks: Any
) -> tuple[str, list[tuple[str, str]]]:
    from .holdings import pick

    taken: list[tuple[str, str]] = []
    limit = lanes_of(repos_mod.load_registry(root).require(d["repo"]))
    try:
        k = pick(root, d, sid, note, limit, taken, lane_area, "verification lane", **marks)
    except AgentError:
        held = [
            (lane_area(d["short"], k), leases_mod.get(root, lane_area(d["short"], k))) for k in range(limit)
        ]
        names = ", ".join((f"{area} is held by {(lease or {}).get('session_id')}" for area, lease in held))
        raise LaneBusy(f"verification lane busy: {names}; try again once that verification ends") from None
    return (lane_area(d["short"], k), taken)


def concurrent_workers(root: Path, short: str) -> int:
    prefix = f"repo-slot:{short}/".lower()
    return sum(
        (
            1
            for lease in leases_mod.list_all(root)
            if str(lease.get("area") or "").lower().startswith(prefix)
            and lease.get("status") in leases_mod.BLOCKING_STATUSES
        )
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
            **(rerun if rerun is not None else _rerunnable(root, d) if outcome == "timeout" else {}),
        }
        save(root, d)


def rerun_binding(run_dir: Path, extra: tuple[str, ...]) -> dict[str, Any] | None:
    import json
    from .verification_evidence import Evidence

    try:
        evidence = Evidence.from_dict(json.loads((run_dir / "terminal.json").read_text(encoding="utf-8")))
    except Exception:
        return None
    b = evidence.binding
    return {"head": b.final_sha, "config_digest": b.config_digest, "extra": list(extra)}


def _rerunnable(root: Path, d: dict[str, Any]) -> dict[str, str]:
    from .verification_evidence import config_digest
    from .verification_gate import worktree_head

    try:
        rp = repos_mod.load_registry(root).require(d["repo"])
        return {"head": worktree_head(Path(d["worktree"])), "config_digest": config_digest(rp.verification)}
    except Exception:
        return {}


def await_lane(root: Path, dispatch_id: str, attempt: int) -> tuple[str, float] | None:
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
        return (area, _clock() - started)


def finish(root: Path, binding: Binding, area: str, sid: str, cleanup_confirmed: bool) -> None:
    try:
        if cleanup_confirmed and clear(root, binding):
            leases_mod.release(root, area, sid)
    except Exception:
        pass


def reconcile_one(root: Path, lease: dict[str, Any]) -> bool:
    from . import verification
    from .state import load

    try:
        sid, area = (str(lease["session_id"]), str(lease["area"]))
        if sid.startswith("dispatch:"):
            binding = _binding(load(root, sid.removeprefix("dispatch:")))
        else:
            _, dispatch_id, run_id = sid.split(":", 2)
            rows = dict_list(load(root, dispatch_id).get("reverify_runs"))
            raw: dict[str, Any] | None = next(
                (as_dict(r.get("binding")) for r in rows if dig(r, "binding", "run_id") == run_id), None
            )
            binding = Binding(**raw) if raw else None
        if binding is not None and (not verification.load_status(root, binding).requires_lease):
            clear(root, binding)
        return leases_mod.release(root, area, sid)
    except Exception:
        return False


RECORD = "record:"


@contextmanager
def record_lane(
    root: Path, rp: repos_mod.Repo, run_id: str, wait_s: float, settled: Callable[[], bool] = lambda: True
) -> Generator[str]:
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
