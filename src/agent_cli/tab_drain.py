"""Rate-limited fallback painter for live manager and worker watch tabs.

Hooks save desired entries and return. A detached runner batches paints. Saved requests stay
unprocessed until a decision reads their nonces; every lock releaser checks for unprocessed work,
so Stop events cannot lose a wake during an eval or runner exit.

The agent-terminal-focus plugin paints from `live.json` while its heartbeat is fresh
(`tab_plugin.active`). This fallback records requests without pushing them then. Resets of ended
Codex tabs still push because `live.json` lists only live sessions.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import core, obsidian_tabs, sessions, tab_plugin
from .records.narrow import as_dict, as_list, dig
from .records.tabs import AttemptRow, attempts_of, painted_of

try:
    import fcntl
except ImportError:  # Windows has no flock; Obsidian tabs are best effort there.
    fcntl = None  # type: ignore[assignment]

TABS_DIR = core.agent_path(Path(), "tabs")
INTERVAL_S = 3.0
RETRY_LIMIT = 3
MAX_EVALS_PER_MINUTE = 24
MATCH_VERSION = 4
"""Version retry semantics so incompatible saved attempts get fresh retries. Stable suffix matching
survives remaps and renames; failed evals do not spend a label's retries (`EVAL_FAILURE_LIMIT`).
"""
RESUMED_FIELD = "resumed"
"""Set on a Codex session record by its first resume (hooks/activity.py). A reset for that thread
is refused from then on, here and where it is queued; one already being painted when the flag is
set is followed by a restore (`tab_reset`)."""
RESULT_RE = re.compile(r"AGENT_TAB_RESULTS:([A-Za-z0-9,_.-]*)")
# A Windows helper run from a hook or the drainer never gets a visible console.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def drain_dir(root: Path) -> Path:
    return root / TABS_DIR


_LOCK_POLL_SLEEP = time.sleep  # the real one: a runner's waits may be replaced, a lock poll's not


@contextlib.contextmanager
def _locked(root: Path, wait: float | None = None) -> Generator[None]:
    """Hold `drainer.lock`; raise TimeoutError after `wait` seconds when supplied. Without `wait`, use
    the platform default (10 s on Windows).
    """
    base = drain_dir(root)
    base.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        # No flock on Windows; the harness's byte-range lock gives the same exclusion, so the
        # last read and the exit stay atomic with a new request there too.
        from . import win_lock

        fd = win_lock.open_lock_file(root, (*TABS_DIR.parts, "drainer.lock"))
        try:
            with win_lock.exclusive(fd, timeout=10 if wait is None else wait):
                yield
        finally:
            os.close(fd)
        return
    with (base / "drainer.lock").open("a") as fh:
        if fcntl is not None:
            if wait is None:
                fcntl.flock(fh, fcntl.LOCK_EX)
            else:
                deadline = time.monotonic() + wait
                while True:
                    try:
                        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("drainer.lock is busy") from None
                        _LOCK_POLL_SLEEP(0.02)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_UN)


def read_entry(path: Path) -> dict[str, Any]:
    try:
        return as_dict(json.loads(path.read_text(encoding="utf-8")))
    except OSError, ValueError:
        return {}


def _state(root: Path) -> dict[str, Any]:
    return read_entry(drain_dir(root) / "state.json")


def _save_state(root: Path, state: dict[str, Any]) -> None:
    core.atomic_write(drain_dir(root) / "state.json", json.dumps(state, ensure_ascii=False) + "\n")


def drain_key(kind: str, identity: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in identity)
    return f"{kind}-{safe}"


def request(
    root: Path,
    kind: str,
    identity: str,
    match_name: str,
    label: str,
    *,
    generated: bool = True,
    token: str = "",
    generated_name: str = "",
    reset: bool = False,
    show_after: str | None = None,
    before_label: str | None = None,
) -> dict[str, Any]:
    """Save this tab's latest label, and ensure one detached drainer will apply it.

    `reset` marks the empty label that returns an ended manager's tab to its plain terminal
    title. It is applied while the session stays ended, whatever its pid."""
    from .obsidian_tabs import enabled

    if not enabled(root):
        return {"started": False, "reason": "disabled"}
    if reset and (sessions.get(root, identity) or {}).get(RESUMED_FIELD):
        return {"started": False, "reason": "resumed"}
    key = drain_key(kind, identity)
    path = drain_dir(root) / f"{key}.json"
    entry = {
        "key": key,
        "kind": kind,
        "identity": identity,
        "match": match_name,
        "token": token,
        "generated_name": generated_name,
        "label": label,
        "show_after": _epoch(show_after),
        "before_label": before_label,
        "match_version": MATCH_VERSION,
        "generated": generated,
        "reset": reset,
        "at": time.time(),  # wall clock: validity and display only
        "nonce": uuid.uuid4().hex,  # which request this is: unprocessed until a decision reads it
    }
    previous = read_entry(path)
    same = all(
        previous.get(field) == entry[field]
        for field in ("label", "match", "token", "generated_name", "show_after", "before_label")
    )
    if same and previous.get("match_version") == MATCH_VERSION and bool(previous.get("reset")) == reset:
        entry["at"] = previous.get("at", entry["at"])  # the same request: when it was first asked
    # A request never derives the facts: only runners do. It
    # saves its own file first (one atomic write, safe without the lock), then ensures a runner,
    # waiting for `drainer.lock` at most `REQUEST_LOCK_WAIT_S`, and then checks the level
    # (`wake`) within what is left of `REQUEST_BUDGET_S`, so it returns well inside a hook's
    # time. Past the wait, whoever holds the lock checks the level after releasing it, and sees
    # this request unprocessed.
    deadline = time.monotonic() + REQUEST_BUDGET_S
    core.atomic_write(path, json.dumps(entry, ensure_ascii=False) + "\n")
    try:
        with _locked(root, wait=REQUEST_LOCK_WAIT_S):
            result = _request_locked(root, key, entry)
    except TimeoutError:
        result = {"started": False, "reason": "busy", "saved": True}
    if result.get("started"):
        return result  # a runner holds the token, and checks the level when it leaves it
    woke = wake(root, wait=max(0.0, deadline - time.monotonic()))
    if result.get("reason") == "busy":
        return {**result, "started": woke}
    return {**result, "woke": True} if woke else result


REQUEST_LOCK_WAIT_S = 1.0
REQUEST_BUDGET_S = 2.0
"""A request's whole wait for `drainer.lock`: its own turn (`REQUEST_LOCK_WAIT_S`), then its
level check (`wake`) in what remains."""
PROCESSED_FIELD = "processed"
"""In the drainer's `state.json`: each request key's `nonce` that a decision has read. A runner
records the nonces it read before deriving the facts it decided on, so
every request saved later is unprocessed; a request records its own when it decides
"unchanged", "unpaintable" or "plugin" itself. A saved request whose nonce is not recorded is
unprocessed, and `wake` starts a runner for it."""
WAKE_ROUNDS = 3


def _unprocessed(root: Path, state: dict[str, Any]) -> list[str]:
    """The keys of saved requests no decision has read (`PROCESSED_FIELD`)."""
    processed = as_dict(state.get(PROCESSED_FIELD))
    return [key for key, nonce in _request_nonces(root).items() if processed.get(key) != nonce]


def _mark_processed(state: dict[str, Any], nonces: dict[str, Any]) -> None:
    state[PROCESSED_FIELD] = {**as_dict(state.get(PROCESSED_FIELD)), **nonces}


def wake(root: Path, wait: float = REQUEST_LOCK_WAIT_S) -> bool:
    """The level check: if a saved request is unprocessed and no runner is
    awake (`_awake`), start one. Every process that releases `drainer.lock` calls this right
    after: a request, the expiry guard, a takeover, and a runner leaving its token or going to
    sleep. A busy request calls it after saving. So whoever releases last sees every request
    saved before, and no request waits on an edge it missed. A wait that times out leaves the
    work to the lock's holder, which checks after releasing; after a check that found nothing
    under the lock, the level is read again once released. True if a runner was started.
    Never raises."""
    from .obsidian_tabs import enabled

    try:
        if not enabled(root):
            return False
        for _ in range(WAKE_ROUNDS):
            state = _state(root)
            if _awake(state) or not _unprocessed(root, state):
                return False
            try:
                with _locked(root, wait=wait):
                    state = _state(root)
                    if not _awake(state) and _unprocessed(root, state):
                        return _start_runner(root, state)
            except TimeoutError:
                return False  # the holder checks the level when it releases
        return False
    except Exception as exc:  # noqa: BLE001 — a wake must never fail its caller
        sys.stderr.write(f"note: tab wake failed ({exc})\n")
        return False


def _request_locked(root: Path, key: str, entry: dict[str, Any]) -> dict[str, Any]:
    """`request`'s work under `drainer.lock`: ensure a runner. It decides "already shown" only
    from the published snapshot while that holds the facts (a file read, no derivation); while
    a failure stands it leaves every decision to the runner."""
    reset = entry["reset"]
    state = _state(root)
    if not reset and tab_plugin.active(root):
        # The plugin paints from live.json. What it shows is in its heartbeat,
        # which `_displayed` weighs against this drainer's own record by time.
        # One deferred check: if the plugin stops before it paints
        # this, nothing else would take the request over. A standby runner sleeps once, to
        # when this heartbeat would go stale, then drains as usual: while the plugin lives it
        # finds nothing to push and exits; once it is stale it paints. At most one at a time.
        # A snapshot write has failed, so the plugin is about to yield: a runner starts now and
        # polls for that, not a standby 30 s away.
        if _snapshot_failed(root):
            return {"started": _take_over(root, state), "reason": "plugin yielding"}
        # decided: the plugin paints it, a runner (alive, or the standby below) guards that
        _mark_processed(state, {key: entry["nonce"]})
        _save_state(root, state)
        if core.is_alive(state.get("runner")):
            return {"started": False, "reason": "plugin"}
        standby = _start_runner(root, state, not_before=tab_plugin.expires_at(root))
        return {"started": False, "reason": "plugin", "standby": standby}
    # The request only wakes the drainer: what it paints is the target, the snapshot's label
    # for this key when the snapshot lists it, as the snapshot permits it now. Done when the tab is
    # known to show that; refused when that label was. Judged
    # here only from the published snapshot; while a failure stands, the runner judges.
    snapshot = _snapshot(root)
    if snapshot["authoritative"]:
        token = _CURRENT_VIEW.set((root, snapshot))
        try:
            target = _targets(root).get(key) or {}
        finally:
            _CURRENT_VIEW.reset(token)
        wanted = target.get("label")
        attempts = attempts_of(state).get(key) or {}
        decided = None
        if _displayed(state, tab_plugin.heartbeat(root), key) == wanted and not target.get("stale_pending"):
            decided = "unchanged"
        elif (
            attempts.get("label") == wanted
            and attempts.get("match_version") == MATCH_VERSION
            and attempts.get("count", 0) >= RETRY_LIMIT
        ):
            decided = "unpaintable"
        if decided:
            # only this request's own nonce: a newer one saved for the key stays unprocessed
            _mark_processed(state, {key: entry["nonce"]})
            _save_state(root, state)
            return {"started": False, "reason": decided}
    if _awake(state):
        return {"started": True, "queued": True}
    # A runner asleep in a standby or expiry wait would leave this request for up to its wait;
    # a fresh runner takes the token, and the sleeper exits when it wakes (`run`).
    try:
        _start_runner(root, state, raise_errors=True)
    except OSError as exc:
        return {"started": False, "reason": str(exc)}
    return {"started": True, "queued": True}


def _snapshot_failed(root: Path) -> bool:
    """The plugin is about to yield: a failure stands, or an intent has outlived a write in
    flight (`live.yield_due`; a young intent is painted through)."""
    from .vault import live

    return live.yield_due(root)


def _take_over(root: Path, state: dict[str, Any]) -> bool:
    """Start a runner now unless one is awake already; a sleeping one (a standby, an expiry
    wait) is replaced, and exits when it wakes to find its token gone. Called under `_locked`.
    True if one was started."""
    if _awake(state):
        return False
    return _start_runner(root, state)


DERIVE_DEADLINE_S = 5.0
"""How long a runner waits for one derivation of the facts. A shared fact
store that stalls (every reader of it stalls alike) leaves the facts unknown for now: the
runner paints the `?` forms and looks again every `UNKNOWN_RETRY_S`, while that one derivation
goes on in its thread; it paints the derived labels once it returns (`_derive`)."""
DERIVE_STALL_S = DERIVE_DEADLINE_S + 2.0
"""A runner deriving the facts for longer than this is stalled past its own deadline: a request or
the guard starts a fresh runner, and the stalled one exits when it
finds its token gone."""


def _awake(state: dict[str, Any]) -> bool:
    """A runner is alive and working: not asleep in a long wait (`sleeping_until`) and not
    stalled in a derivation (`deriving_since`, a monotonic time, comparable across processes on
    one machine)."""
    if not core.is_alive(state.get("runner")):
        return False
    asleep = state.get("sleeping_until")
    if isinstance(asleep, (int, float)) and asleep > time.time() + INTERVAL_S:
        return False
    since = state.get("deriving_since")
    return not (isinstance(since, (int, float)) and time.monotonic() - since > DERIVE_STALL_S)


def on_snapshot_failure(root: Path) -> None:
    """A snapshot write failed (`live.write`, `live.record_failure`): the plugin will yield on its
    next poll, within 2 s, and the snapshot no longer holds the facts. When a paint is owed, a
    runner starts now, polls the plugin's heartbeat for the yield (`YIELD_POLL_S`) and paints,
    so an idle harness with no further writes still shows ✋ within seconds. Never raises.

    It derives nothing itself: a fact store slow for its writer is slow
    for this caller too (a watchdog, the writer of the failure), which then started nothing.
    A runner starts unless one is awake, derives within `DERIVE_DEADLINE_S` and exits if
    nothing is owed."""
    from . import obsidian_run
    from .obsidian_tabs import enabled

    try:
        if not enabled(root):
            return
        with _locked(root, wait=REQUEST_LOCK_WAIT_S):
            if obsidian_run.running():
                _take_over(root, _state(root))
    except TimeoutError:
        pass  # the lock's holder checks the level when it releases (`wake`)
    except Exception as exc:  # noqa: BLE001 — a takeover must never fail the snapshot's writer
        sys.stderr.write(f"note: tab takeover failed ({exc})\n")
    wake(root)


def _start_runner(
    root: Path,
    state: dict[str, Any],
    *,
    not_before: float | None = None,
    rearms: int = 0,
    raise_errors: bool = False,
) -> bool:
    """Spawn the detached drainer and record it as the runner; called under `_locked`. With
    `not_before` (epoch seconds) it sleeps until then, once, before its first look; `rearms`
    counts the standby runners before it in this chain (`MAX_REARMS`)."""
    # The runner proves itself by this token, not by the pid Popen returns: on Windows a venv
    # python.exe is a launcher that starts the real interpreter as its child, so the pid
    # recorded here never equalled the drainer's os.getpid() and every drainer exited on
    # its first check.
    token = uuid.uuid4().hex
    argv = [sys.executable, "-m", "agent_cli.tab_drain", str(root), token]
    if not_before is not None:
        argv += [f"{not_before:.3f}", str(rearms)]
    try:
        proc = _spawn(argv, root)
    except OSError:
        if raise_errors:
            raise
        return False
    state["runner"] = proc.pid
    state["runner_token"] = token
    state["sleeping_until"] = not_before
    state["deriving_since"] = None  # a stalled predecessor's mark is not this runner's
    _save_state(root, state)
    return True


def spawn(argv: list[str], root: Path) -> subprocess.Popen[Any]:
    """`_spawn` for another module's detached process (`live`'s intent watchdog)."""
    return _spawn(argv, root)


def _spawn(argv: list[str], root: Path) -> subprocess.Popen[Any]:
    """Start the drainer so it outlives the hook that asked for it."""
    common: dict[str, Any] = {
        "cwd": root,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform != "win32":
        return subprocess.Popen(argv, start_new_session=True, **common)  # noqa: S603 — fixed argv
    # CREATE_NO_WINDOW avoids visible consoles for the detached Windows painter and its evals.
    hidden = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        return subprocess.Popen(argv, creationflags=hidden | subprocess.CREATE_BREAKAWAY_FROM_JOB, **common)  # noqa: S603 — fixed argv
    except OSError:
        return subprocess.Popen(argv, creationflags=hidden, **common)  # noqa: S603 — fixed argv


def _claim(state: dict[str, Any], token: str | None) -> bool:
    """Whether this process is the runner the last request started."""
    if token is None:  # an old invocation without a token proves itself by pid
        return state.get("runner") == os.getpid()
    return state.get("runner_token") == token


def _desired(root: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in drain_dir(root).glob("manager-*.json"):
        entry = read_entry(path)
        if entry.get("key") == path.stem:
            out[path.stem] = entry
    for path in drain_dir(root).glob("watch-*.json"):
        entry = read_entry(path)
        if entry.get("key") == path.stem:
            out[path.stem] = entry
    return out


def _batch_code(entries: list[dict[str, Any]]) -> str:
    """Return keys for tabs found exactly once; an ambiguous name changes no tab."""
    payload = [
        {
            "key": entry["key"],
            "match": entry["match"],
            "token": entry.get("token") or "",
            "generated_name": entry.get("generated_name") or "",
            "label": entry["label"],
            "generated": entry["generated"],
        }
        for entry in entries
    ]
    return (
        "(() => { const entries = "
        + json.dumps(payload, ensure_ascii=False)
        + "; const leaves = app.workspace.getLeavesOfType('terminal:terminal'); const found = []; "
        "for (const e of entries) { const exact = [], token = []; for (const leaf of leaves) { "
        "const v = leaf.view; const s = v && v.state; if (!s) continue; "
        "const shown = String((leaf.getDisplayText && leaf.getDisplayText()) || ''); "
        "const user = String(s.userTitle || ''); "
        # A client can rename the tab while relabelling is off. Both names are exact matches;
        # if they select two leaves, the ambiguity check below refuses to paint either.
        "const eq = (x, name) => !!name && (x === name || x.endsWith(' ' + name)); "
        "if (eq(user, e.match) || eq(user, e.generated_name) || "
        "(e.generated && (eq(shown, e.match) || eq(shown, e.generated_name)))) exact.push(v); "
        "else if (e.token && user.endsWith(e.token)) token.push(v); } "
        "const matches = exact.length ? exact : token; "
        "if (matches.length !== 1) { if (matches.length > 1) found.push('ambiguous.' + e.key); continue; } "
        "const v = matches[0]; "
        "if (String(v.state.userTitle || '') !== e.label) { "
        "const c = structuredClone(v.state); c.userTitle = e.label; v.state = c; } "
        "found.push(e.key); } return 'AGENT_TAB_RESULTS:' + found.join(','); })()"
    )


EVAL_LOCK = "eval.lock"
"""Serialize all Obsidian evals: drainer batches, first claims, resets and dispatch-open. Keep this
lock separate from `drainer.lock` and release that lock while painting, so hook requests never wait
behind evals.
"""
STDERR_TAIL = 300
INSTALLER_OUTDATED = "Your Obsidian installer is out of date"
"""Printed on stdout by an Obsidian whose installer predates its app package. That installer has
no console CLI, so every eval reloads the app package first: 5 to 24 s against a 10 s timeout, and a call that prints only this banner exits 0 with no result line."""
INSTALLER_REMEDY = "update the Obsidian installer from https://obsidian.md/download"


@dataclass
class EvalRun:
    """One `obsidian eval`: `stdout` when it exited 0, else None, and why. `ended` is `exited`
    (it ran to an exit code), `timeout`, `not-running` (no app, so not run), `no-cli`,
    `oserror` or `lock-busy` (another eval held `EVAL_LOCK` past the wait).
    `installer_outdated`: its output carried `INSTALLER_OUTDATED`, however it ended."""

    stdout: str | None
    ended: str
    returncode: int | None = None
    stderr: str = ""
    installer_outdated: bool = False

    @property
    def failed(self) -> bool:
        return self.stdout is None

    def diagnostic(self) -> dict[str, Any]:
        """Explain an eval outcome with its end class, return code and stderr."""
        out: dict[str, Any] = {"ended": self.ended, "returncode": self.returncode, "stderr": self.stderr}
        if self.installer_outdated:
            out["installer_outdated"] = True
        return out


def eval_lock(root: Path, wait: float) -> contextlib.AbstractContextManager[None]:
    """`EVAL_LOCK`, waited on for at most `wait` seconds. Raises TimeoutError."""
    from .tab_reset import exclusive

    return exclusive(root, EVAL_LOCK, time.monotonic() + wait)


def eval_args(code: str) -> list[str]:
    return ["eval", f"code={code}"]


def run_eval(root: Path | None, code: str, timeout: float, *, source: str, wait: float = 10.0) -> EvalRun:
    """Run one `obsidian eval` under `EVAL_LOCK`, and, for every caller but the drainer (whose
    batch row carries the same fields), append a row to `metrics.jsonl`. Never raises. With no
    root there is no lock file to share, so the eval runs unlocked and unrecorded."""
    from . import obsidian_run
    from .dispatch.opener import obsidian_cli

    exe = obsidian_cli()
    if exe is None:
        return EvalRun(None, "no-cli")

    def once() -> EvalRun:
        try:
            # Never against a closed app: the CLI would start Obsidian on our pipes (obsidian_run).
            result = obsidian_run.run([exe, *eval_args(code)], timeout, creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired as exc:
            err = _text(exc.stderr)
            outdated = INSTALLER_OUTDATED in _text(exc.output)
            return EvalRun(None, "timeout", None, err.strip()[-STDERR_TAIL:], outdated)
        except OSError as exc:
            return EvalRun(None, "oserror", None, f"{type(exc).__name__}: {exc}"[-STDERR_TAIL:])
        if result is None:
            return EvalRun(None, "not-running")
        tail = (result.stderr or "").strip()[-STDERR_TAIL:]
        outdated = INSTALLER_OUTDATED in (result.stdout or "")
        stdout = result.stdout if result.returncode == 0 else None
        return EvalRun(stdout, "exited", result.returncode, tail, outdated)

    if root is None:
        return once()
    try:
        with eval_lock(root, wait):
            run = once()
    except TimeoutError:  # before OSError, which it subclasses
        run = EvalRun(None, "lock-busy")
    except OSError:  # The lock file cannot be opened; paint unlocked.
        run = once()
    if source != "drain":
        record_metric(
            root, {"at": time.time(), "source": source, "eval_failed": run.failed, **run.diagnostic()}
        )
    return run


def _text(value: object) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value or "")


def record_metric(root: Path, row: dict[str, Any]) -> None:
    """Append one row to `.agent/tabs/metrics.jsonl`. Never raises."""
    try:
        drain_dir(root).mkdir(parents=True, exist_ok=True)
        with (drain_dir(root) / "metrics.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def failure_rate(root: Path, window_s: float = 86400.0, now: float | None = None) -> dict[str, Any]:
    """Failed evals over every eval in `metrics.jsonl` within the last `window_s`, all callers,
    with the commonest way they ended. `evals` 0 when there were none."""
    evals = failed = 0
    ended: dict[str, int] = {}
    for row in _recent_evals(root, window_s, now):
        evals += 1
        if row.get("eval_failed"):
            failed += 1
            how = str(row.get("ended") or "unrecorded")
            ended[how] = ended.get(how, 0) + 1
    return {"evals": evals, "failed": failed, "ended": ended}


def installer_outdated(root: Path, window_s: float = 86400.0, now: float | None = None) -> bool:
    """Whether any eval within the last `window_s` printed `INSTALLER_OUTDATED`."""
    return any(row.get("installer_outdated") for row in _recent_evals(root, window_s, now))


def _recent_evals(root: Path, window_s: float, now: float | None) -> list[dict[str, Any]]:
    """The eval rows of `metrics.jsonl` within the last `window_s`."""
    cutoff = (now or time.time()) - window_s
    try:
        lines = (drain_dir(root) / "metrics.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            row = as_dict(json.loads(line))
        except ValueError:
            continue
        at = row.get("at")
        if isinstance(at, (int, float)) and at >= cutoff and "eval_failed" in row:
            rows.append(row)
    return rows


drain_root: Path | None = None
"""The root this process drains, set by `run`: the evals it makes (its batches and the resets it
applies) take that root's `EVAL_LOCK`. None outside a drainer."""
last_run: EvalRun | None = None
"""The drainer's last batch eval, for that batch's metrics row."""


def _eval(entries: list[dict[str, Any]], timeout: float = 10) -> set[str] | None:
    """The keys Obsidian found, or None when the eval itself failed: Obsidian did not answer,
    timed out, or returned no result line. A failed eval says nothing about the tabs, so it
    must not count against a label's retry cap (see `EVAL_FAILURE_LIMIT`). No CLI at all is not
    transient and reads as "found none". How it ended is kept in `last_run`.
    A batch whose code is too long for one CLI argument (`obsidian_run.ARG_LIMIT`) is painted in
    several evals, and fails as a whole if any of them fails."""
    global last_run
    found: set[str] = set()
    for chunk in _request_chunks(entries):
        run = last_run = run_eval(drain_root, _batch_code(chunk), timeout, source="drain")
        if run.ended == "no-cli":
            return set()
        if run.stdout is None:
            return None
        match = RESULT_RE.search(run.stdout)
        if not match:
            run.ended = "no-result-line"
            return None
        found |= set(match.group(1).split(",")) - {""}
    return found


def _request_chunks(entries: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """`entries` in order, as the fewest runs whose batch eval fits one CLI argument. An entry too
    large alone is its own chunk, which `obsidian_run.run` refuses."""
    from .obsidian_run import args_fit

    chunks: list[list[dict[str, Any]]] = []
    for entry in entries:
        if chunks and args_fit(eval_args(_batch_code([*chunks[-1], entry]))):
            chunks[-1].append(entry)
        else:
            chunks.append([entry])
    return chunks


EVAL_FAILURE_LIMIT = 5
"""Bound consecutive whole-batch eval failures without consuming a label's retry cap. Measured failure
bursts span two to four batches before recovery; keep transient failures retryable.
"""


def _apply(root: Path, entries: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    """Paint the entries `_pending` selected: `(found, unanswered)`, the keys whose tab was
    painted and the keys of a batch eval that failed. Runs outside the drainer lock, so a reset
    is checked again and painted inside its thread's lock by `tab_reset.apply`, one eval each.
    Resets are rare (one per ended session), so the extra evals stay far under the rate gate."""
    from . import tab_reset

    others = [entry for entry in entries if not entry.get("reset") and _deadline_current(root, entry)]
    result: set[str] | None = _eval(others) if others else set()
    unanswered: set[str] = {entry["key"] for entry in others} if result is None else set()
    found = set(result or ())
    for entry in entries:
        if entry.get("reset"):
            found |= tab_reset.apply(root, entry)
    return found, unanswered


def _deadline_current(root: Path, entry: dict[str, Any]) -> bool:
    """Re-read a delayed permission transition immediately before sending its hand to paint."""
    from .vault.live import approval_show_after

    after = entry.get("show_after")
    if (
        entry.get("kind") != "manager"
        or not isinstance(after, (int, float))
        or time.time() < after
        or not str(entry.get("label") or "").startswith("✋")
    ):
        return True
    record = sessions.get(root, str(entry.get("identity") or "")) or {}
    return _epoch(approval_show_after(record)) == after


MAX_REARMS = 4
"""Waits in a row in which a standby runner may find the plugin active with a request still
unpainted. Each waits out one heartbeat (about 31 s), so after about two minutes with no paint the
plugin is taken as unhealthy for those requests and the runner paints them."""
MAX_EXPIRY_WAITS = 3
"""Expiry waits in a row (`_next_expiry`) that end with neither a paint nor a renewal: the
snapshot's `valid_until` has not moved, yet it has not passed. A wake that finds it moved later is
progress, since the writer is alive, and costs nothing here."""
MAX_LIFETIME_S = 24 * 60 * 60.0
"""How long one runner may keep renewing its expiry waits: a writer that renews for a day still
ends the runner, and the exit is recorded in `metrics.jsonl` (`event: lifetime-cap`)."""
UNKNOWN_RETRY_S = 5.0
"""While a failure stands and the facts cannot be read, how often a runner reads them again."""
YIELD_POLL_S = 1.0
MAX_YIELD_POLLS = 10
"""While a snapshot write has failed and the plugin, still active, owes a paint, a runner looks
again every `YIELD_POLL_S` for the plugin's `yielded`, at most this many times (10 s); the plugin
polls every 2 s. Past that the usual standby waits apply."""
MAX_WAIT_S = 120.0
"""The longest single wait. A heartbeat wait is about 31 s. An expiry wait is taken only when the
snapshot expires within this: a running harness's snapshot (`live.VALID_RUNNING_S`, 15 s), whose
renewal stopping is the case that matters. An idle snapshot stands for 12 h; no runner sleeps
that long, and the next request paints its stale form (`_presented`)."""


def _outstanding(root: Path, state: dict[str, Any]) -> list[dict[str, Any]]:
    """Paints the healthy plugin owes and has not made: targets on tabs the plugin found (its
    heartbeat's `painted` map lists them) that do not show what they should, a saved request
    that wins over a non-authoritative snapshot included. A tab
    the plugin did not find is not owed by it: there is no such tab open.

    Nor is a tab that shows what `live.json` asks for, unless a failure stands and the plugin is
    about to yield (`_snapshot_failed`). Every snapshot write stands an intent while it runs
    (`live.write`), and while one stands this drainer derives its own view (`_view`), which may
    differ from the published one the plugin paints through it: past `valid_until` the plugin
    shows the `?` form and the derived view the plain label. Judged by that view, a plugin doing
    its job read as failing the key at every look and was overruled through an eval; the plugin
    then repainted it, and the two took turns."""
    found = as_dict(tab_plugin.heartbeat(root).get("painted"))
    published: dict[str, Any] = {} if _snapshot_failed(root) else as_dict(_snapshot(root)["tabs"])
    now = time.time()
    return [
        e
        for e in _owed(root, state)
        if not e.get("reset")
        and e["key"] in found
        and not _shows_published(as_dict(published.get(e["key"])), found[e["key"]], now)
    ]


def _shows_published(tab: dict[str, Any], shown: Any, now: float) -> bool:
    """Whether `shown` is the title the plugin paints now for the published entry `tab`: its
    label as the clock selects it (`show_after`, `show_until`), or past `valid_until` that
    label's `?` form, which the plugin confirms over its own reads."""
    label = tab.get("label")
    if not isinstance(label, str) or not isinstance(shown, str):
        return False
    wanted = label
    after, end = _epoch(tab.get("show_after")), _epoch(tab.get("show_until"))
    if after is not None and now < after and isinstance(tab.get("before_label"), str):
        wanted = str(tab["before_label"])
    elif end is not None and now >= end and isinstance(tab.get("after_label"), str):
        wanted = str(tab["after_label"])
    if shown == wanted:
        return True
    until = _epoch(tab.get("valid_until"))
    if tab.get("owner_question") or until is None or now <= until:
        return False
    stale = tab.get("stale_label") if wanted == label else None
    return shown == (stale if isinstance(stale, str) else obsidian_tabs.stale_label(wanted))


def _epoch(stamp: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(stamp)).timestamp()
    except TypeError, ValueError:
        return None


def _snapshot(root: Path) -> dict[str, Any]:
    """Read snapshot times, tabs and authority from `live.json`.

    Read the failure record both before and after the snapshot bytes: a concurrent success can
    clear a failure without refreshing bytes already read. A standing failure invalidates
    authority; `live.PUBLISH_LOCK` orders capture, publication and failure updates.
    """
    from .vault import live

    failed_before = live.write_failed(root) is not None
    try:
        snap = as_dict(json.loads(live.path(root).read_text(encoding="utf-8")))
    except OSError, ValueError:
        snap = {}
    # every good write clears the record, so one standing (or one unreadable) is newer
    authoritative = not failed_before and live.write_failed(root) is None
    return _shaped(snap, authoritative=authoritative)


def _shaped(snap: dict[str, Any], *, authoritative: bool) -> dict[str, Any]:
    tabs: dict[str, dict[str, Any]] = {}
    for item in as_list(snap.get("tabs")):
        tab = as_dict(item)
        tabs[str(tab.get("key"))] = tab
    return {
        "observed_at": _epoch(snap.get("observed_at")),
        "valid_until": _epoch(snap.get("valid_until")),
        "tabs": tabs,
        "authoritative": authoritative,
    }


def _view(root: Path) -> dict[str, Any]:
    """Use authoritative `live.json`, or derive current facts with `live.build` in memory.

    Derivation holds no publication lock because it publishes nothing. One runner paints in read
    order, and hooks wake it for later changes. Within `_derived_once`, callers reuse the view and
    take `drainer.lock` only for comparison and updates.

    Strict reads (`core.strict_reads`) reject malformed records rather than omit them. If
    derivation fails, facts are unknown: tabs use stale `?` labels and `status check` stays unready
    while the failure stands.
    """
    current = _CURRENT_VIEW.get()
    if current is not None and current[0] == root:
        return current[1]
    return _view_now(root)


def _view_now(root: Path) -> dict[str, Any]:
    snapshot = _snapshot(root)
    if snapshot["authoritative"]:
        return snapshot
    try:
        derived = _derive(root)
    except Exception:  # noqa: BLE001 — unknown facts, shown as unknown
        derived = None
    if derived is None:  # failed, or past DERIVE_DEADLINE_S: unknown for now
        return {**snapshot, "valid_until": 0.0, "derived": False, "unknown": True}
    return {**_shaped(as_dict(derived), authoritative=False), "derived": True}


@dataclass
class _Derivation:
    done: threading.Event
    result: dict[str, Any] | None = None
    error: BaseException | None = None


_derivations: dict[tuple[Path, int], _Derivation] = {}
_derivations_lock = threading.Lock()


def _derive(root: Path) -> dict[str, Any] | None:
    """The facts as `live.build` derives them (strict, `core.strict_reads`), or None when they
    are not known within `DERIVE_DEADLINE_S`. The derivation runs in a
    daemon thread (`tab-derive-<caller>`), at most one per runner (the calling thread) and root:
    a call while one is outstanding waits on that one again instead of starting another, so a
    read that never returns holds one thread, not one per pass. Raises what a finished
    derivation raised."""
    from .vault import live

    key = (root, threading.get_ident())
    with _derivations_lock:
        job = _derivations.get(key)
        if job is None:
            job = _derivations[key] = _Derivation(threading.Event())
            started = job

            def work() -> None:
                try:
                    with core.strict_reads():
                        started.result = json.loads(json.dumps(live.build(root), default=str))
                except BaseException as exc:  # noqa: BLE001 — handed to the waiting runner
                    started.error = exc
                finally:
                    started.done.set()

            name = f"tab-derive-{threading.current_thread().name}"
            threading.Thread(target=work, name=name, daemon=True).start()
    if not job.done.wait(DERIVE_DEADLINE_S):
        return None
    with _derivations_lock:
        if _derivations.get(key) is job:
            del _derivations[key]
    if job.error is not None:
        raise job.error
    return job.result


_CURRENT_VIEW: contextvars.ContextVar[tuple[Path, dict[str, Any]] | None] = contextvars.ContextVar(
    "tab_view", default=None
)


@contextlib.contextmanager
def _derived_once(root: Path) -> Generator[dict[str, Any]]:
    """Derive the view now, outside any lock, and let `_view` reuse it within the block."""
    view = _view_now(root)
    token = _CURRENT_VIEW.set((root, view))
    try:
        yield view
    finally:
        _CURRENT_VIEW.reset(token)


_stale_reads: dict[Path, obsidian_tabs.StaleReads] = {}


def _presented(root: Path, entry: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """Use the definitive label within `valid_until`, then its stale `?` form. Prefer the snapshot's
    `stale_label` when it holds this label, otherwise derive it with `obsidian_tabs.stale_label`.
    Keep the original in `requested_label`.
    """
    from .obsidian_tabs import stale_label

    until = entry.get("valid_until", snapshot["valid_until"])
    label = str(entry.get("label") or "")
    now = time.time()
    after = entry.get("show_after")
    if isinstance(after, (int, float)) and now < after and entry.get("before_label"):
        label = str(entry["before_label"])
        entry = {**entry, "label": label}
    end = entry.get("show_until")
    if isinstance(end, (int, float)) and now >= end and entry.get("after_label"):
        label = str(entry["after_label"])
        entry = {**entry, "label": label}
    tab = as_dict(as_dict(snapshot["tabs"]).get(entry["key"]))
    expired = isinstance(until, (int, float)) and now > until and bool(label)
    expired = expired and not tab.get("owner_question")
    reads = _stale_reads.setdefault(root, obsidian_tabs.StaleReads())
    threshold = tab.get("stale_reads")
    required = threshold if isinstance(threshold, int) and threshold > 0 else 1
    if snapshot.get("unknown"):
        required = 1
    if not reads.confirm(str(entry["key"]), label, bool(expired), now, required):
        return {**entry, "stale_pending": bool(expired)}
    stale = tab.get("stale_label") if tab.get("label") == label else None
    return {**entry, "label": str(stale or stale_label(label)), "requested_label": label}


def _request_valid_until(entry: dict[str, Any], snapshot: dict[str, Any]) -> float | None:
    """Keep a request valid for the snapshot's window from its request time, at least through the
    snapshot's own `valid_until`. Without a readable snapshot there is no validity window or stale
    `?` form.
    """
    from .vault import live

    asked = entry.get("at")
    observed, until = snapshot["observed_at"], snapshot["valid_until"]
    if until is None or not isinstance(asked, (int, float)):
        return until
    window = until - observed if observed is not None else live.VALID_RUNNING_S
    return max(float(asked) + window, until)


def _targets(root: Path) -> dict[str, dict[str, Any]]:
    """Select current labels by tab key, using the plugin's validity contract.

    Facts from `_view` determine each listed tab's label; saved requests only wake the runner.
    Requests supply labels for unlisted tabs under the same validity window. A Codex reset wins
    only when `_owed` proves the session ended without resuming.
    """
    snapshot = _view(root)
    saved = _desired(root)
    out: dict[str, dict[str, Any]] = {}
    for key, raw in as_dict(snapshot["tabs"]).items():
        tab = as_dict(raw)
        if not isinstance(tab.get("label"), str) or not tab.get("kind"):
            continue
        asked_at = as_dict(saved.get(key)).get("at")
        entry: dict[str, Any] = {
            "key": key,
            "kind": tab.get("kind"),
            "identity": str(tab.get("identity") or ""),
            "match": str(tab.get("match") or ""),
            "token": str(tab.get("token") or ""),
            "generated_name": str(tab.get("generated_name") or ""),
            "generated": bool(tab.get("generated")),
            "label": tab["label"],
            "valid_until": snapshot["valid_until"],
            "show_after": _epoch(tab.get("show_after")),
            "before_label": tab.get("before_label"),
            "show_until": _epoch(tab.get("show_until")),
            "after_label": tab.get("after_label"),
            "match_version": MATCH_VERSION,
            "reset": False,
            "at": float(asked_at) if isinstance(asked_at, (int, float)) else time.time(),
            "source": "snapshot",
            "owner_question": tab.get("owner_question"),
            "stale_reads": tab.get("stale_reads"),
        }
        out[key] = _presented(root, entry, snapshot)
    for key, entry in saved.items():
        if entry.get("reset"):
            out[key] = entry
        elif key not in out:
            out[key] = _presented(
                root, {**entry, "valid_until": _request_valid_until(entry, snapshot)}, snapshot
            )
    return out


def _displayed(state: dict[str, Any], beat: dict[str, Any], key: str) -> str | None:
    """What the tab is known to show: the newer of the plugin heartbeat's `painted[key]` and
    this drainer's own record of its last paint. None when neither knows."""
    drained = painted_of(state).get(key) or {}
    plugin = as_dict(beat.get("painted"))
    plugin_at = beat.get("at") if isinstance(beat.get("at"), (int, float)) else None
    drained_at = drained.get("at")
    if (
        key in plugin
        and plugin_at is not None
        and (not isinstance(drained_at, (int, float)) or float(plugin_at) >= float(drained_at))
    ):
        return str(plugin[key])
    label = drained.get("label")
    return label if isinstance(label, str) else None


def _owed(root: Path, state: dict[str, Any]) -> list[dict[str, Any]]:
    """Every target (`_targets`) whose tab is not known to show it, less those refused
    `RETRY_LIMIT` times for that label. Saved requests for gone sessions are dropped here."""
    painted = painted_of(state)
    attempts = attempts_of(state)
    beat = tab_plugin.heartbeat(root)
    out: list[dict[str, Any]] = []
    for key, entry in _targets(root).items():
        if entry.get("source") != "snapshot" and entry.get("kind") == "manager":
            try:
                record = sessions.get(root, str(entry.get("identity") or ""))
            except core.AgentError, OSError:
                # unreadable for now: not known gone, so kept, and not
                # painted on this pass; the next pass reads it again
                continue
            pid = record.get("pid") if record else None
            if entry.get("reset"):
                # A reset is for a session that is gone, so a dead pid is expected. A resumed
                # thread owns its tab from then on, even when a late end marks it ended again, so
                # a reset queued before the resume is refused here. One already
                # being painted is handled in `tab_reset.apply`, which restores the tab after it.
                gone = (
                    record is None
                    or bool(record.get(RESUMED_FIELD))
                    or dig(record, "activity", "state") != "ended"
                )
            else:
                gone = record is None or bool(pid and not core.is_alive(pid))
            if gone:
                (drain_dir(root) / f"{key}.json").unlink(missing_ok=True)
                painted.pop(key, None)
                attempts.pop(key, None)
                as_dict(state.get(PROCESSED_FIELD)).pop(key, None)
                continue
        displayed = _displayed(state, beat, key)
        if entry.get("stale_pending") and displayed == obsidian_tabs.stale_label(
            str(entry.get("label") or "")
        ):
            continue  # a new runner keeps the confirmed mark until a fresh read clears it
        if displayed == entry.get("label"):
            continue
        attempt = attempts.get(key) or {}
        if (
            attempt.get("label") == entry.get("label")
            and attempt.get("match_version") == entry.get("match_version")
            and attempt.get("count", 0) >= RETRY_LIMIT
        ):
            continue
        # A tab this drainer renamed is matched by the name it painted, until the next paint.
        prior = painted.get(key) or {}
        if name := prior.get("name"):
            entry = {**entry, "match": name}
        out.append(entry)
    state["painted"] = painted
    state["attempts"] = attempts
    return out


def _next_expiry(root: Path, state: dict[str, Any]) -> float | None:
    """When a tab showing a definitive label (no `?`) from the snapshot must turn stale: now,
    if the snapshot is already past `valid_until`; its
    `valid_until` if that is at most `MAX_WAIT_S` ahead; else None. What a tab shows is
    `_displayed`, whoever painted it, so a label the plugin painted before it stopped is guarded
    like one this drainer painted, and every target counts: a watch tab with no saved request,
    and a saved request the snapshot does not list, under its own validity. The earliest such
    deadline."""
    from .obsidian_tabs import STALE_MARK

    beat = tab_plugin.heartbeat(root)
    now = time.time()
    due: float | None = None
    for key, entry in _targets(root).items():
        until = entry.get("valid_until")
        label = _displayed(state, beat, key)
        for field, current_label in (("show_after", "before_label"), ("show_until", "label")):
            deadline = entry.get(field)
            if isinstance(deadline, (int, float)) and now < deadline and label == entry.get(current_label):
                wake_at = min(deadline, now + MAX_WAIT_S)
                due = wake_at if due is None else min(due, wake_at)
        if entry.get("reset") or entry.get("owner_question") or not isinstance(until, (int, float)):
            continue
        if not label or label.partition(" ")[0].endswith(STALE_MARK):
            continue
        if until <= now:
            wake_at = now + obsidian_tabs.STALE_READ_INTERVAL_S if entry.get("stale_reads") else now
            due = wake_at if due is None else min(due, wake_at)
            continue
        if until - now <= MAX_WAIT_S:
            due = until if due is None else min(due, until)
    return due


def ensure_expiry_guard(root: Path) -> bool:
    """Start a runner when the plugin is not healthy, no runner is alive, and a paint is owed
    (`_owed`) or a definitive label is due to turn stale (`_next_expiry`, now for a snapshot
    already expired). The runner paints what the snapshot permits,
    or sleeps to `valid_until` and repaints the `?` form unless the snapshot was renewed,
    whoever did the last paint. `live.write_quietly` calls this after every write, so
    the snapshot's writer (the supervisor's 5 s heartbeat while a worker runs, and every hook)
    keeps the guard in place: a plugin that stops while a definitive label shows is noticed at
    the first write after its heartbeat goes unhealthy. A healthy plugin repaints expiry
    itself, so then no runner is started. Then, as every lock holder does, it checks the level
    (`wake`). Returns True if a runner was started. Never raises."""
    from .obsidian_tabs import enabled

    if not enabled(root):
        return False
    return _guard(root) or wake(root)


def _guard(root: Path) -> bool:
    from . import obsidian_run
    from .vault import live

    try:
        if tab_plugin.active(root):
            return False
        snapshot = _snapshot(root)
        if not snapshot["authoritative"]:
            # The facts would have to be derived, which only a runner does: a failure or a stale intent
            # stands,
            # so one is started; a young intent is a
            # write in flight, whose own guard follows its publication.
            if not live.yield_due(root):
                return False
            with _locked(root, wait=REQUEST_LOCK_WAIT_S):
                return obsidian_run.running() and _take_over(root, _state(root))
        token = _CURRENT_VIEW.set((root, snapshot))  # the snapshot holds the facts: no derivation
        try:
            return _guard_locked(root)
        finally:
            _CURRENT_VIEW.reset(token)
    except TimeoutError:
        return False  # the lock's holder checks the level when it releases (`wake`)
    except Exception as exc:  # noqa: BLE001 — a guard must never fail the snapshot's writer
        sys.stderr.write(f"note: tab expiry guard failed ({exc})\n")
        return False


def _guard_locked(root: Path) -> bool:
    """`_guard`'s decision from the published snapshot, which holds the facts. Raises
    TimeoutError past the lock's wait."""
    from . import obsidian_run

    with _locked(root, wait=REQUEST_LOCK_WAIT_S):
        state = _state(root)
        owed = _owed(root, state)
        # A runner asleep (a plugin standby, up to 30 s; an expiry wait) would leave a paint
        # owed now, after the plugin yielded or stopped, waiting; a fresh runner takes the
        # token and the sleeper exits on waking, as in `request`. An expiry due later is
        # already the sleeper's to keep.
        if core.is_alive(state.get("runner")) and (not owed or _awake(state)):
            return False
        if not owed and _next_expiry(root, state) is None:
            return False
        if not obsidian_run.running():
            return False  # nothing can be painted; a runner would only exit
        return _start_runner(root, state)


def _pending(
    root: Path, state: dict[str, Any], *, past_plugin: set[str] | None = None
) -> list[dict[str, Any]]:
    """The paints to make now: what is owed (`_owed`). While the plugin is active it paints them
    instead, except resets and the keys in `past_plugin`, which a runner paints itself (see
    `_outstanding`)."""
    owed = _owed(root, state)
    if not tab_plugin.active(root):
        return owed
    allowed = past_plugin or set()
    return [e for e in owed if e.get("reset") or e["key"] in allowed]


def _record(
    root: Path,
    state: dict[str, Any],
    entries: list[dict[str, Any]],
    found: set[str],
    unanswered: set[str] | None = None,
) -> None:
    painted = state.setdefault("painted", {})
    attempts = state.setdefault("attempts", {})
    for entry in entries:
        if entry["key"] in (unanswered or ()):
            # The eval failed, so the tab may or may not read the new label. Forget the painted
            # one as below, but spend none of this label's retries on it.
            if entry["key"] in painted:
                painted[entry["key"]] = {**painted[entry["key"]], "label": None}
            continue
        key = entry["key"]
        if f"ambiguous.{key}" in found:
            # The detached drainer's stderr goes to DEVNULL. Keep one on-disk diagnostic per
            # key rather than emitting a note no manager can see on every retry.
            marker = drain_dir(root) / f"ambiguous-{key}.json"
            if not marker.exists():
                core.atomic_write(marker, json.dumps({"key": key, "at": time.time()}) + "\n")
                sys.stderr.write(f"note: ambiguous Obsidian tab match for {key}; repaint refused\n")
        if key in found:
            from .obsidian_tabs import label_name

            name = label_name(str(entry["label"]))  # after the glyph and any `+N` count
            painted[key] = {"label": entry["label"], "name": name, "at": time.time()}
            attempts.pop(key, None)
            # A reset keeps the display on the record, so `codex resume` can still match it. A
            # paint of the snapshot's own label leaves the record alone, as the plugin does: the
            # record's names are the hooks' (a resumed Codex thread stays unclaimed until its hook
            # claims it), and the snapshot finds the tab by its suffix meanwhile.
            if (
                entry.get("kind") == "manager"
                and not entry.get("reset")
                and entry.get("source") != "snapshot"
            ):
                sid = str(entry["identity"])
                with contextlib.suppress(core.AgentError, OSError):  # a record unreadable for now
                    sessions.set_field(root, sid, "tab_display_name", name)
                    sessions.set_field(root, sid, "tab_label", entry["label"])
        else:
            # An eval can change a tab even when it reports no match. Keep its name for matching
            # but forget its painted label, so the next request paints again.
            if key in painted:
                painted[key] = {**painted[key], "label": None}
            previous: AttemptRow = attempts.get(key) or {}
            same_attempt = previous.get("label") == entry["label"] and previous.get(
                "match_version"
            ) == entry.get("match_version")
            count = previous.get("count", 0) + 1 if same_attempt else 1
            attempts[key] = {
                "label": entry["label"],
                "match_version": entry.get("match_version"),
                "count": count,
            }


def _request_nonces(root: Path) -> dict[str, Any]:
    """Each saved request by key, as its `nonce` (its file's contents for a request saved
    before nonces), so a runner can tell a request it has not seen without a clock."""
    out: dict[str, Any] = {}
    for pattern in ("manager-*.json", "watch-*.json"):
        for path in drain_dir(root).glob(pattern):
            entry = read_entry(path)
            out[path.stem] = entry.get("nonce") or json.dumps(entry, sort_keys=True)
    return out


def _release(root: Path, state: dict[str, Any]) -> None:
    state["runner"] = None
    state["runner_token"] = None
    state["sleeping_until"] = None
    state["deriving_since"] = None
    _save_state(root, state)


def run(root: Path, token: str | None = None, not_before: float | None = None, rearms: int = 0) -> None:
    """Drain until no paintable requests remain. Every state decision holds the shared lock.

    A standby runner (`not_before`, see `request`) first sleeps once until the plugin would stop
    counting as active. If the plugin is still active then and a request it owes is unpainted
    (`_outstanding`), this runner keeps the duty: it waits again, to the new expiry, and looks
    again. After `MAX_REARMS` such waits the plugin is taken as not painting those requests, and
    the runner paints them. The waits happen in this process, so
    no successor launch can fail and leave a paint owed with no runner.

    With the plugin down, a label painted from the current snapshot must turn stale at its
    `valid_until`: when nothing else is pending the runner waits for that and
    repaints the stale form (`_next_expiry`). A wake that finds the deadline renewed waits for
    the new one, one sleep per renewal window, for as long as the writer renews (up to
    `MAX_LIFETIME_S`); only wakes with no renewal count against `MAX_EXPIRY_WAITS`.

    Every wait is recorded as `sleeping_until`, so a request that finds the runner asleep while
    the plugin is down starts a fresh runner instead of queueing behind the wait; this one then
    finds its token gone and exits. No wait is longer than `MAX_WAIT_S`.

    Each pass reads the saved requests' nonces before it derives the facts, and records them
    as processed with its decision (`PROCESSED_FIELD`). When it leaves its token, or goes to
    sleep, it checks the level (`wake`) once the lock is released, so a request saved at any
    point of its last pass starts a runner."""
    try:
        _run(root, token, not_before, rearms)
    finally:
        wake(root)


def _run(root: Path, token: str | None, not_before: float | None, rearms: int) -> None:
    from . import obsidian_run
    from .obsidian_tabs import TABS_OFF_FILE, enabled

    global drain_root, last_run
    drain_root = root
    if not_before is not None:
        time.sleep(min(max(0.0, not_before - time.time()), MAX_WAIT_S))
    forced: set[str] = set()
    expiry_waits = 0
    yield_polls = 0
    waited_for: float | None = None  # the `valid_until` the last expiry wait was for
    started = time.time()
    while True:
        wait_until: float | None = None
        # the facts are derived before the lock, which is held only to compare, update and
        # paint, so a slow read never holds a hook's request;
        # the derivation is marked, so a read that stalls lets another runner take over
        with _locked(root):
            state = _state(root)
            if not _claim(state, token):
                return
            state["deriving_since"] = time.monotonic()
            _save_state(root, state)
            # the requests this pass decides on: read before the facts are, so a request saved
            # after this read stays unprocessed, and one saved before it is in the facts
            baseline = _request_nonces(root)
        with _derived_once(root) as view, _locked(root):
            state = _state(root)
            if not _claim(state, token):
                return  # taken over while this one was deriving
            state["deriving_since"] = None
            _mark_processed(state, baseline)
            if state.get("runner") != os.getpid() or state.get("sleeping_until") is not None:
                # The launcher's pid was recorded; liveness checks must see this process.
                state["runner"] = os.getpid()
                state["sleeping_until"] = None
                _save_state(root, state)
            if not enabled(root):
                _release(root, state)
                return
            entries = _pending(root, state, past_plugin=forced)
            plugin = tab_plugin.active(root)
            if not entries and not forced and plugin:
                owed = _outstanding(root, state)
                if owed and yield_polls < MAX_YIELD_POLLS and _snapshot_failed(root):
                    # the plugin yields on its next poll: look again shortly
                    wait_until, yield_polls = time.time() + YIELD_POLL_S, yield_polls + 1
                elif owed and rearms < MAX_REARMS:
                    wait_until, rearms = tab_plugin.expires_at(root), rearms + 1
                elif owed:
                    forced = {entry["key"] for entry in owed}
                    entries = _pending(root, state, past_plugin=forced)
            elif not entries and not plugin and view.get("unknown") and obsidian_run.running():
                # A failure stands and the facts cannot be read: the tabs show their `?` form,
                # and this runner stays to read them again every UNKNOWN_RETRY_S, so a tab is
                # right again as soon as the reads recover.
                if time.time() - started >= MAX_LIFETIME_S:
                    record_metric(root, {"at": time.time(), "source": "drain", "event": "lifetime-cap"})
                else:
                    wait_until = time.time() + UNKNOWN_RETRY_S
            elif not entries and not plugin and expiry_waits < MAX_EXPIRY_WAITS and obsidian_run.running():
                expiry = _next_expiry(root, state)
                if expiry is not None and expiry <= time.time():
                    expiry = None  # already due, yet nothing owed: refused paints; nothing to wait for
                if expiry is not None and time.time() - started >= MAX_LIFETIME_S:
                    record_metric(root, {"at": time.time(), "source": "drain", "event": "lifetime-cap"})
                elif expiry is not None:
                    # a deadline that moved later is a renewal, so the writer is alive: progress
                    renewed = waited_for is not None and expiry > waited_for
                    expiry_waits = 0 if renewed else expiry_waits + 1
                    wait_until, waited_for = expiry + 0.5, expiry
            if wait_until is not None:
                state["sleeping_until"] = wait_until
                _save_state(root, state)
                delay = 0.0
            elif not obsidian_run.running():
                # With Obsidian closed nothing can be painted: stop, and leave the requests for
                # the drainer the next hook starts. Retrying launched the app on every attempt.
                _release(root, state)
                return
            elif not entries:
                _release(root, state)  # `run` checks the level once this returns
                return
            else:
                due = float(state.get("last_eval") or 0) + INTERVAL_S
                delay = max(0.0, due - time.time())
                if not delay:
                    recent = [t for t in state.get("eval_times", []) if time.time() - t < 60]
                    if len(recent) >= MAX_EVALS_PER_MINUTE:
                        (root / TABS_OFF_FILE).touch()
                        _release(root, state)
                        return
                    now = time.time()
                    state["last_eval"] = now
                    state["eval_times"] = [*recent, now]
                    _save_state(root, state)
        if wait_until is not None:
            # asleep, this runner no longer counts as awake once the wait is long (`_awake`): a
            # request saved during this pass is a fresh runner's, which takes the token
            wake(root)
            time.sleep(min(max(0.0, wait_until - time.time()), MAX_WAIT_S))
            continue
        if delay:
            time.sleep(min(delay, 0.5))
            continue
        last_run = None
        found, unanswered = _apply(root, entries)
        with _locked(root):
            state = _state(root)
            if not _claim(state, token) or not enabled(root):
                return
            _record(root, state, entries, found, unanswered)
            failures = int(state.get("eval_failures") or 0) + 1 if unanswered else 0
            state["eval_failures"] = failures
            _save_state(root, state)
            diagnostic: dict[str, Any] = last_run.diagnostic() if last_run is not None else {}
            metric: dict[str, Any] = {
                "at": time.time(),
                "requested": len(entries),
                "found": sum(entry["key"] in found for entry in entries),
                "eval_failed": bool(unanswered),
                "painted": [
                    {"key": entry["key"], "label": entry["label"]}
                    for entry in entries
                    if entry["key"] in found
                ],
                "max_delay_s": round(max(time.time() - float(e.get("at") or 0) for e in entries), 3),
                "source": "drain",
                # why a failed batch failed: exit code, stderr tail, timeout or exit
                **diagnostic,
            }
            record_metric(root, metric)
            if failures >= EVAL_FAILURE_LIMIT:
                # Obsidian is down, not flaky: stop here, and let the next hook's request start
                # a drainer that tries again. The labels keep their retries.
                state["eval_failures"] = 0
                _release(root, state)
                return


if __name__ == "__main__":
    run(
        Path(sys.argv[1]),
        sys.argv[2] if len(sys.argv) > 2 else None,
        float(sys.argv[3]) if len(sys.argv) > 3 else None,
        int(sys.argv[4]) if len(sys.argv) > 4 else 0,
    )
