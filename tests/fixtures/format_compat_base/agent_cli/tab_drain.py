# ruff: noqa
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
except ImportError:
    fcntl = None
TABS_DIR = core.agent_path(Path(), "tabs")
INTERVAL_S = 3.0
RETRY_LIMIT = 3
MAX_EVALS_PER_MINUTE = 24
MATCH_VERSION = 4
"Version retry semantics so incompatible saved attempts get fresh retries. Stable suffix matching\nsurvives remaps and renames; failed evals do not spend a label's retries (`EVAL_FAILURE_LIMIT`).\n"
RESUMED_FIELD = "resumed"
"Set on a Codex session record by its first resume (hooks/activity.py). A reset for that thread\nis refused from then on, here and where it is queued; one already being painted when the flag is\nset is followed by a restore (`tab_reset`)."
RESULT_RE = re.compile("AGENT_TAB_RESULTS:([A-Za-z0-9,_.-]*)")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def drain_dir(root: Path) -> Path:
    return root / TABS_DIR


_LOCK_POLL_SLEEP = time.sleep


@contextlib.contextmanager
def _locked(root: Path, wait: float | None = None) -> Generator[None]:
    base = drain_dir(root)
    base.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
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
    safe = "".join((ch if ch.isalnum() or ch in "-_." else "_" for ch in identity))
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
        "at": time.time(),
        "nonce": uuid.uuid4().hex,
    }
    previous = read_entry(path)
    same = all(
        (
            previous.get(field) == entry[field]
            for field in ("label", "match", "token", "generated_name", "show_after", "before_label")
        )
    )
    if same and previous.get("match_version") == MATCH_VERSION and (bool(previous.get("reset")) == reset):
        entry["at"] = previous.get("at", entry["at"])
    deadline = time.monotonic() + REQUEST_BUDGET_S
    core.atomic_write(path, json.dumps(entry, ensure_ascii=False) + "\n")
    try:
        with _locked(root, wait=REQUEST_LOCK_WAIT_S):
            result = _request_locked(root, key, entry)
    except TimeoutError:
        result = {"started": False, "reason": "busy", "saved": True}
    if result.get("started"):
        return result
    woke = wake(root, wait=max(0.0, deadline - time.monotonic()))
    if result.get("reason") == "busy":
        return {**result, "started": woke}
    return {**result, "woke": True} if woke else result


REQUEST_LOCK_WAIT_S = 1.0
REQUEST_BUDGET_S = 2.0
"A request's whole wait for `drainer.lock`: its own turn (`REQUEST_LOCK_WAIT_S`), then its\nlevel check (`wake`) in what remains."
PROCESSED_FIELD = "processed"
'In the drainer\'s `state.json`: each request key\'s `nonce` that a decision has read. A runner\nrecords the nonces it read before deriving the facts it decided on, so\nevery request saved later is unprocessed; a request records its own when it decides\n"unchanged", "unpaintable" or "plugin" itself. A saved request whose nonce is not recorded is\nunprocessed, and `wake` starts a runner for it.'
WAKE_ROUNDS = 3


def _unprocessed(root: Path, state: dict[str, Any]) -> list[str]:
    processed = as_dict(state.get(PROCESSED_FIELD))
    return [key for key, nonce in _request_nonces(root).items() if processed.get(key) != nonce]


def _mark_processed(state: dict[str, Any], nonces: dict[str, Any]) -> None:
    state[PROCESSED_FIELD] = {**as_dict(state.get(PROCESSED_FIELD)), **nonces}


def wake(root: Path, wait: float = REQUEST_LOCK_WAIT_S) -> bool:
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
                return False
        return False
    except Exception as exc:
        sys.stderr.write(f"note: tab wake failed ({exc})\n")
        return False


def _request_locked(root: Path, key: str, entry: dict[str, Any]) -> dict[str, Any]:
    reset = entry["reset"]
    state = _state(root)
    if not reset and tab_plugin.active(root):
        if _snapshot_failed(root):
            return {"started": _take_over(root, state), "reason": "plugin yielding"}
        _mark_processed(state, {key: entry["nonce"]})
        _save_state(root, state)
        if core.is_alive(state.get("runner")):
            return {"started": False, "reason": "plugin"}
        standby = _start_runner(root, state, not_before=tab_plugin.expires_at(root))
        return {"started": False, "reason": "plugin", "standby": standby}
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
        if _displayed(state, tab_plugin.heartbeat(root), key) == wanted and (not target.get("stale_pending")):
            decided = "unchanged"
        elif (
            attempts.get("label") == wanted
            and attempts.get("match_version") == MATCH_VERSION
            and (attempts.get("count", 0) >= RETRY_LIMIT)
        ):
            decided = "unpaintable"
        if decided:
            _mark_processed(state, {key: entry["nonce"]})
            _save_state(root, state)
            return {"started": False, "reason": decided}
    if _awake(state):
        return {"started": True, "queued": True}
    try:
        _start_runner(root, state, raise_errors=True)
    except OSError as exc:
        return {"started": False, "reason": str(exc)}
    return {"started": True, "queued": True}


def _snapshot_failed(root: Path) -> bool:
    from .vault import live

    return live.yield_due(root)


def _take_over(root: Path, state: dict[str, Any]) -> bool:
    if _awake(state):
        return False
    return _start_runner(root, state)


DERIVE_DEADLINE_S = 5.0
"How long a runner waits for one derivation of the facts. A shared fact\nstore that stalls (every reader of it stalls alike) leaves the facts unknown for now: the\nrunner paints the `?` forms and looks again every `UNKNOWN_RETRY_S`, while that one derivation\ngoes on in its thread; it paints the derived labels once it returns (`_derive`)."
DERIVE_STALL_S = DERIVE_DEADLINE_S + 2.0
"A runner deriving the facts for longer than this is stalled past its own deadline: a request or\nthe guard starts a fresh runner, and the stalled one exits when it\nfinds its token gone."


def _awake(state: dict[str, Any]) -> bool:
    if not core.is_alive(state.get("runner")):
        return False
    asleep = state.get("sleeping_until")
    if isinstance(asleep, (int, float)) and asleep > time.time() + INTERVAL_S:
        return False
    since = state.get("deriving_since")
    return not (isinstance(since, (int, float)) and time.monotonic() - since > DERIVE_STALL_S)


def on_snapshot_failure(root: Path) -> None:
    from . import obsidian_run
    from .obsidian_tabs import enabled

    try:
        if not enabled(root):
            return
        with _locked(root, wait=REQUEST_LOCK_WAIT_S):
            if obsidian_run.running():
                _take_over(root, _state(root))
    except TimeoutError:
        pass
    except Exception as exc:
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
    state["deriving_since"] = None
    _save_state(root, state)
    return True


def spawn(argv: list[str], root: Path) -> subprocess.Popen[Any]:
    return _spawn(argv, root)


def _spawn(argv: list[str], root: Path) -> subprocess.Popen[Any]:
    common: dict[str, Any] = {
        "cwd": root,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform != "win32":
        return subprocess.Popen(argv, start_new_session=True, **common)
    hidden = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        return subprocess.Popen(argv, creationflags=hidden | subprocess.CREATE_BREAKAWAY_FROM_JOB, **common)
    except OSError:
        return subprocess.Popen(argv, creationflags=hidden, **common)


def _claim(state: dict[str, Any], token: str | None) -> bool:
    if token is None:
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
        + "; const leaves = app.workspace.getLeavesOfType('terminal:terminal'); const found = []; for (const e of entries) { const exact = [], token = []; for (const leaf of leaves) { const v = leaf.view; const s = v && v.state; if (!s) continue; const shown = String((leaf.getDisplayText && leaf.getDisplayText()) || ''); const user = String(s.userTitle || ''); const eq = (x, name) => !!name && (x === name || x.endsWith(' ' + name)); if (eq(user, e.match) || eq(user, e.generated_name) || (e.generated && (eq(shown, e.match) || eq(shown, e.generated_name)))) exact.push(v); else if (e.token && user.endsWith(e.token)) token.push(v); } const matches = exact.length ? exact : token; if (matches.length !== 1) { if (matches.length > 1) found.push('ambiguous.' + e.key); continue; } const v = matches[0]; if (String(v.state.userTitle || '') !== e.label) { const c = structuredClone(v.state); c.userTitle = e.label; v.state = c; } found.push(e.key); } return 'AGENT_TAB_RESULTS:' + found.join(','); })()"
    )


EVAL_LOCK = "eval.lock"
"Serialize all Obsidian evals: drainer batches, first claims, resets and dispatch-open. Keep this\nlock separate from `drainer.lock` and release that lock while painting, so hook requests never wait\nbehind evals.\n"
STDERR_TAIL = 300
INSTALLER_OUTDATED = "Your Obsidian installer is out of date"
"Printed on stdout by an Obsidian whose installer predates its app package. That installer has\nno console CLI, so every eval reloads the app package first: 5 to 24 s against a 10 s timeout, and a call that prints only this banner exits 0 with no result line."
INSTALLER_REMEDY = "update the Obsidian installer from https://obsidian.md/download"


@dataclass
class EvalRun:
    stdout: str | None
    ended: str
    returncode: int | None = None
    stderr: str = ""
    installer_outdated: bool = False

    @property
    def failed(self) -> bool:
        return self.stdout is None

    def diagnostic(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ended": self.ended, "returncode": self.returncode, "stderr": self.stderr}
        if self.installer_outdated:
            out["installer_outdated"] = True
        return out


def eval_lock(root: Path, wait: float) -> contextlib.AbstractContextManager[None]:
    from .tab_reset import exclusive

    return exclusive(root, EVAL_LOCK, time.monotonic() + wait)


def run_eval(root: Path | None, code: str, timeout: float, *, source: str, wait: float = 10.0) -> EvalRun:
    from . import obsidian_run
    from .dispatch.opener import obsidian_cli

    exe = obsidian_cli()
    if exe is None:
        return EvalRun(None, "no-cli")

    def once() -> EvalRun:
        try:
            result = obsidian_run.run([exe, "eval", f"code={code}"], timeout, creationflags=NO_WINDOW)
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
    except TimeoutError:
        run = EvalRun(None, "lock-busy")
    except OSError:
        run = once()
    if source != "drain":
        record_metric(
            root, {"at": time.time(), "source": source, "eval_failed": run.failed, **run.diagnostic()}
        )
    return run


def _text(value: object) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value or "")


def record_metric(root: Path, row: dict[str, Any]) -> None:
    try:
        drain_dir(root).mkdir(parents=True, exist_ok=True)
        with (drain_dir(root) / "metrics.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def failure_rate(root: Path, window_s: float = 86400.0, now: float | None = None) -> dict[str, Any]:
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
    return any((row.get("installer_outdated") for row in _recent_evals(root, window_s, now)))


def _recent_evals(root: Path, window_s: float, now: float | None) -> list[dict[str, Any]]:
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
        if isinstance(at, (int, float)) and at >= cutoff and ("eval_failed" in row):
            rows.append(row)
    return rows


drain_root: Path | None = None
"The root this process drains, set by `run`: the evals it makes (its batches and the resets it\napplies) take that root's `EVAL_LOCK`. None outside a drainer."
last_run: EvalRun | None = None
"The drainer's last batch eval, for that batch's metrics row."


def _eval(entries: list[dict[str, Any]], timeout: float = 10) -> set[str] | None:
    global last_run
    run = last_run = run_eval(drain_root, _batch_code(entries), timeout, source="drain")
    if run.ended == "no-cli":
        return set()
    if run.stdout is None:
        return None
    match = RESULT_RE.search(run.stdout)
    if not match:
        run.ended = "no-result-line"
        return None
    return set(match.group(1).split(",")) - {""}


EVAL_FAILURE_LIMIT = 5
"Bound consecutive whole-batch eval failures without consuming a label's retry cap. Measured failure\nbursts span two to four batches before recovery; keep transient failures retryable.\n"


def _apply(root: Path, entries: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    from . import tab_reset

    others = [entry for entry in entries if not entry.get("reset") and _deadline_current(root, entry)]
    result: set[str] | None = _eval(others) if others else set()
    unanswered: set[str] = {entry["key"] for entry in others} if result is None else set()
    found = set(result or ())
    for entry in entries:
        if entry.get("reset"):
            found |= tab_reset.apply(root, entry)
    return (found, unanswered)


def _deadline_current(root: Path, entry: dict[str, Any]) -> bool:
    from .vault.live import approval_show_after

    after = entry.get("show_after")
    if (
        entry.get("kind") != "manager"
        or not isinstance(after, (int, float))
        or time.time() < after
        or (not str(entry.get("label") or "").startswith("✋"))
    ):
        return True
    record = sessions.get(root, str(entry.get("identity") or "")) or {}
    return _epoch(approval_show_after(record)) == after


MAX_REARMS = 4
"Waits in a row in which a standby runner may find the plugin active with a request still\nunpainted. Each waits out one heartbeat (about 31 s), so after about two minutes with no paint the\nplugin is taken as unhealthy for those requests and the runner paints them."
MAX_EXPIRY_WAITS = 3
"Expiry waits in a row (`_next_expiry`) that end with neither a paint nor a renewal: the\nsnapshot's `valid_until` has not moved, yet it has not passed. A wake that finds it moved later is\nprogress, since the writer is alive, and costs nothing here."
MAX_LIFETIME_S = 24 * 60 * 60.0
"How long one runner may keep renewing its expiry waits: a writer that renews for a day still\nends the runner, and the exit is recorded in `metrics.jsonl` (`event: lifetime-cap`)."
UNKNOWN_RETRY_S = 5.0
"While a failure stands and the facts cannot be read, how often a runner reads them again."
YIELD_POLL_S = 1.0
MAX_YIELD_POLLS = 10
"While a snapshot write has failed and the plugin, still active, owes a paint, a runner looks\nagain every `YIELD_POLL_S` for the plugin's `yielded`, at most this many times (10 s); the plugin\npolls every 2 s. Past that the usual standby waits apply."
MAX_WAIT_S = 120.0
"The longest single wait. A heartbeat wait is about 31 s. An expiry wait is taken only when the\nsnapshot expires within this: a running harness's snapshot (`live.VALID_RUNNING_S`, 15 s), whose\nrenewal stopping is the case that matters. An idle snapshot stands for 12 h; no runner sleeps\nthat long, and the next request paints its stale form (`_presented`)."


def _outstanding(root: Path, state: dict[str, Any]) -> list[dict[str, Any]]:
    found = as_dict(tab_plugin.heartbeat(root).get("painted"))
    published: dict[str, Any] = {} if _snapshot_failed(root) else as_dict(_snapshot(root)["tabs"])
    now = time.time()
    return [
        e
        for e in _owed(root, state)
        if not e.get("reset")
        and e["key"] in found
        and (not _shows_published(as_dict(published.get(e["key"])), found[e["key"]], now))
    ]


def _shows_published(tab: dict[str, Any], shown: Any, now: float) -> bool:
    label = tab.get("label")
    if not isinstance(label, str) or not isinstance(shown, str):
        return False
    wanted = label
    after, end = (_epoch(tab.get("show_after")), _epoch(tab.get("show_until")))
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
    from .vault import live

    failed_before = live.write_failed(root) is not None
    try:
        snap = as_dict(json.loads(live.path(root).read_text(encoding="utf-8")))
    except OSError, ValueError:
        snap = {}
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
    except Exception:
        derived = None
    if derived is None:
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
                except BaseException as exc:
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
    view = _view_now(root)
    token = _CURRENT_VIEW.set((root, view))
    try:
        yield view
    finally:
        _CURRENT_VIEW.reset(token)


_stale_reads: dict[Path, obsidian_tabs.StaleReads] = {}


def _presented(root: Path, entry: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
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
    expired = expired and (not tab.get("owner_question"))
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
    from .vault import live

    asked = entry.get("at")
    observed, until = (snapshot["observed_at"], snapshot["valid_until"])
    if until is None or not isinstance(asked, (int, float)):
        return until
    window = until - observed if observed is not None else live.VALID_RUNNING_S
    return max(float(asked) + window, until)


def _targets(root: Path) -> dict[str, dict[str, Any]]:
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
    painted = painted_of(state)
    attempts = attempts_of(state)
    beat = tab_plugin.heartbeat(root)
    out: list[dict[str, Any]] = []
    for key, entry in _targets(root).items():
        if entry.get("source") != "snapshot" and entry.get("kind") == "manager":
            try:
                record = sessions.get(root, str(entry.get("identity") or ""))
            except core.AgentError, OSError:
                continue
            pid = record.get("pid") if record else None
            if entry.get("reset"):
                gone = (
                    record is None
                    or bool(record.get(RESUMED_FIELD))
                    or dig(record, "activity", "state") != "ended"
                )
            else:
                gone = record is None or bool(pid and (not core.is_alive(pid)))
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
            continue
        if displayed == entry.get("label"):
            continue
        attempt = attempts.get(key) or {}
        if (
            attempt.get("label") == entry.get("label")
            and attempt.get("match_version") == entry.get("match_version")
            and (attempt.get("count", 0) >= RETRY_LIMIT)
        ):
            continue
        prior = painted.get(key) or {}
        if name := prior.get("name"):
            entry = {**entry, "match": name}
        out.append(entry)
    state["painted"] = painted
    state["attempts"] = attempts
    return out


def _next_expiry(root: Path, state: dict[str, Any]) -> float | None:
    from .obsidian_tabs import STALE_MARK

    beat = tab_plugin.heartbeat(root)
    now = time.time()
    due: float | None = None
    for key, entry in _targets(root).items():
        until = entry.get("valid_until")
        label = _displayed(state, beat, key)
        for field, current_label in (("show_after", "before_label"), ("show_until", "label")):
            deadline = entry.get(field)
            if isinstance(deadline, (int, float)) and now < deadline and (label == entry.get(current_label)):
                wake_at = min(deadline, now + MAX_WAIT_S)
                due = wake_at if due is None else min(due, wake_at)
        if entry.get("reset") or entry.get("owner_question") or (not isinstance(until, (int, float))):
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
            if not live.yield_due(root):
                return False
            with _locked(root, wait=REQUEST_LOCK_WAIT_S):
                return obsidian_run.running() and _take_over(root, _state(root))
        token = _CURRENT_VIEW.set((root, snapshot))
        try:
            return _guard_locked(root)
        finally:
            _CURRENT_VIEW.reset(token)
    except TimeoutError:
        return False
    except Exception as exc:
        sys.stderr.write(f"note: tab expiry guard failed ({exc})\n")
        return False


def _guard_locked(root: Path) -> bool:
    from . import obsidian_run

    with _locked(root, wait=REQUEST_LOCK_WAIT_S):
        state = _state(root)
        owed = _owed(root, state)
        if core.is_alive(state.get("runner")) and (not owed or _awake(state)):
            return False
        if not owed and _next_expiry(root, state) is None:
            return False
        if not obsidian_run.running():
            return False
        return _start_runner(root, state)


def _pending(
    root: Path, state: dict[str, Any], *, past_plugin: set[str] | None = None
) -> list[dict[str, Any]]:
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
            if entry["key"] in painted:
                painted[entry["key"]] = {**painted[entry["key"]], "label": None}
            continue
        key = entry["key"]
        if f"ambiguous.{key}" in found:
            marker = drain_dir(root) / f"ambiguous-{key}.json"
            if not marker.exists():
                core.atomic_write(marker, json.dumps({"key": key, "at": time.time()}) + "\n")
                sys.stderr.write(f"note: ambiguous Obsidian tab match for {key}; repaint refused\n")
        if key in found:
            from .obsidian_tabs import label_name

            name = label_name(str(entry["label"]))
            painted[key] = {"label": entry["label"], "name": name, "at": time.time()}
            attempts.pop(key, None)
            if (
                entry.get("kind") == "manager"
                and (not entry.get("reset"))
                and (entry.get("source") != "snapshot")
            ):
                sid = str(entry["identity"])
                with contextlib.suppress(core.AgentError, OSError):
                    sessions.set_field(root, sid, "tab_display_name", name)
                    sessions.set_field(root, sid, "tab_label", entry["label"])
        else:
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
    waited_for: float | None = None
    started = time.time()
    while True:
        wait_until: float | None = None
        with _locked(root):
            state = _state(root)
            if not _claim(state, token):
                return
            state["deriving_since"] = time.monotonic()
            _save_state(root, state)
            baseline = _request_nonces(root)
        with _derived_once(root) as view, _locked(root):
            state = _state(root)
            if not _claim(state, token):
                return
            state["deriving_since"] = None
            _mark_processed(state, baseline)
            if state.get("runner") != os.getpid() or state.get("sleeping_until") is not None:
                state["runner"] = os.getpid()
                state["sleeping_until"] = None
                _save_state(root, state)
            if not enabled(root):
                _release(root, state)
                return
            entries = _pending(root, state, past_plugin=forced)
            plugin = tab_plugin.active(root)
            if not entries and (not forced) and plugin:
                owed = _outstanding(root, state)
                if owed and yield_polls < MAX_YIELD_POLLS and _snapshot_failed(root):
                    wait_until, yield_polls = (time.time() + YIELD_POLL_S, yield_polls + 1)
                elif owed and rearms < MAX_REARMS:
                    wait_until, rearms = (tab_plugin.expires_at(root), rearms + 1)
                elif owed:
                    forced = {entry["key"] for entry in owed}
                    entries = _pending(root, state, past_plugin=forced)
            elif not entries and (not plugin) and view.get("unknown") and obsidian_run.running():
                if time.time() - started >= MAX_LIFETIME_S:
                    record_metric(root, {"at": time.time(), "source": "drain", "event": "lifetime-cap"})
                else:
                    wait_until = time.time() + UNKNOWN_RETRY_S
            elif (
                not entries and (not plugin) and (expiry_waits < MAX_EXPIRY_WAITS) and obsidian_run.running()
            ):
                expiry = _next_expiry(root, state)
                if expiry is not None and expiry <= time.time():
                    expiry = None
                if expiry is not None and time.time() - started >= MAX_LIFETIME_S:
                    record_metric(root, {"at": time.time(), "source": "drain", "event": "lifetime-cap"})
                elif expiry is not None:
                    renewed = waited_for is not None and expiry > waited_for
                    expiry_waits = 0 if renewed else expiry_waits + 1
                    wait_until, waited_for = (expiry + 0.5, expiry)
            if wait_until is not None:
                state["sleeping_until"] = wait_until
                _save_state(root, state)
                delay = 0.0
            elif not obsidian_run.running():
                _release(root, state)
                return
            elif not entries:
                _release(root, state)
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
                "found": sum((entry["key"] in found for entry in entries)),
                "eval_failed": bool(unanswered),
                "painted": [
                    {"key": entry["key"], "label": entry["label"]}
                    for entry in entries
                    if entry["key"] in found
                ],
                "max_delay_s": round(max((time.time() - float(e.get("at") or 0) for e in entries)), 3),
                "source": "drain",
                **diagnostic,
            }
            record_metric(root, metric)
            if failures >= EVAL_FAILURE_LIMIT:
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
