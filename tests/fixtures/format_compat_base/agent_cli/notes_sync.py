# ruff: noqa
from __future__ import annotations
import contextlib
import json
import os
import re
import socket
import subprocess
import time
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from . import config_file, core, notes_claim_guard, procs
from .notes import read_notes_root
from .notes_claim_guard import NOT_CLAIMS, NOT_LABELS
from .records.narrow import as_dict, as_dict_or_none, as_feature_id, dig

SYNC_DIR = "agent-sync"
STOP_BUDGET_S = 25.0
PULL_BUDGET_S = 12.0
CLEANUP_RESERVE_S = 5.0
LOCK_WAIT_S = 5.0
FETCH_CAP_S = 8.0
PUSH_CAP_S = 12.0
LOCAL_CAP_S = 5.0
SSH = "ssh -o BatchMode=yes -o ConnectTimeout=5"
OPERATIONS = ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD")


@dataclass
class Result:
    status: str
    detail: str = ""
    count: int = 0
    converged: bool = False
    head: str = ""
    links: list[dict[str, str]] | None = None
    incoming: bool = False


class _StateError(Exception):
    pass


class Repo:
    def __init__(self, path: Path, budget: float) -> None:
        self.path = path
        self.end = time.monotonic() + budget
        self.cleanup_end: float | None = None
        self.env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        self.env.setdefault("GCM_INTERACTIVE", "never")

    def left(self) -> float:
        return self.end - time.monotonic()

    def git(
        self, *args: str, cap: float = LOCAL_CAP_S, cleanup: bool = False
    ) -> subprocess.CompletedProcess[str] | None:
        if cleanup:
            if self.cleanup_end is None:
                self.cleanup_end = time.monotonic() + CLEANUP_RESERVE_S
            timeout = self.cleanup_end - time.monotonic()
        else:
            timeout = min(cap, self.left())
        if timeout <= 0:
            return None
        try:
            return procs.run_bounded(["git", *args], timeout=timeout, cwd=self.path, env=self.env)
        except OSError, subprocess.SubprocessError:
            return None

    def out(self, *args: str, cleanup: bool = False) -> str | None:
        r = self.git(*args, cleanup=cleanup)
        return r.stdout.strip() if r is not None and r.returncode == 0 else None


def reason(r: subprocess.CompletedProcess[str] | None) -> str:
    if r is None:
        return "timed out or could not run"
    text = (r.stderr or r.stdout).strip()
    return text.splitlines()[-1] if text else f"exit {r.returncode}"


def open_repo(root: Path, budget: float) -> tuple[Repo, Path, Path] | Result:
    notes = read_notes_root(root)
    if notes is None or not (notes / ".git").exists():
        return Result("none")
    repo = Repo(notes.resolve(), budget)
    dirs = repo.out("rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir")
    if dirs is None or len(dirs.splitlines()) != 2:
        return Result("none")
    gitdir, common = (Path(p) for p in dirs.splitlines())
    if "GIT_SSH_COMMAND" not in repo.env and repo.out("config", "core.sshCommand") is None:
        repo.env["GIT_SSH_COMMAND"] = SSH
    return (repo, gitdir, common)


@contextlib.contextmanager
def repo_lock(common: Path, wait: float) -> Generator[None]:
    if os.name == "nt":
        from . import win_lock

        fd = win_lock.open_lock_file(common, (SYNC_DIR, "lock"))
        try:
            with win_lock.exclusive(fd, timeout=max(wait, 0)):
                yield
        finally:
            os.close(fd)
        return
    import fcntl

    (common / SYNC_DIR).mkdir(parents=True, exist_ok=True)
    with (common / SYNC_DIR / "lock").open("a") as fh:
        end = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise TimeoutError("notes sync lock is busy") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _state_path(common: Path) -> Path:
    return common / SYNC_DIR / "state.json"


def _read_state(common: Path) -> dict[str, Any]:
    path = _state_path(common)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise _StateError(f"cannot read {path}: {exc}") from None
    try:
        data = as_dict_or_none(json.loads(text))
    except ValueError as exc:
        raise _StateError(f"{path} is not valid JSON: {exc}") from None
    if data is None:
        raise _StateError(f"{path} is not a JSON object")
    for key in ("halt", "failure", "conflict"):
        if key in data and (not isinstance(data[key], dict)):
            raise _StateError(f"{path}: `{key}` is not an object")
    if "halt" in data and (not isinstance(dig(data, "halt", "detail"), str)):
        raise _StateError(f"{path}: `halt` has no detail")
    return data


def _write_state(common: Path, state: dict[str, Any]) -> None:
    core.atomic_write(_state_path(common), json.dumps(state, indent=2, sort_keys=True) + "\n")


def _fail(common: Path, state: dict[str, Any], status: str, detail: str) -> Result:
    old = as_dict(state.get("failure"))
    same = old.get("status") == status and old.get("detail") == detail
    state["failure"] = {
        "id": old["id"] if same and old.get("id") else uuid.uuid4().hex[:12],
        "status": status,
        "detail": detail,
        "at": core.iso(core.now()),
    }
    state["attempts"] = int(state.get("attempts") or 0) + 1
    _write_state(common, state)
    return Result(status, detail)


def _converged(repo: Repo, common: Path, state: dict[str, Any], result: Result) -> Result:
    head, upstream = (repo.out("rev-parse", "HEAD"), repo.out("rev-parse", "@{u}"))
    stale = [key for key in ("failure", "conflict", "attempts") if key in state]
    result.converged = head is not None and head == upstream
    if result.converged:
        result.head = head or ""
    if result.converged and stale:
        for key in stale:
            state.pop(key)
        _write_state(common, state)
    return result


def standing_failure(root: Path) -> dict[str, Any] | None:
    try:
        opened = open_repo(root, LOCAL_CAP_S)
        if isinstance(opened, Result):
            return None
        state = _read_state(opened[2])
    except _StateError as exc:
        return {"id": "state-invalid", "status": "error", "detail": str(exc)}
    except Exception:
        return None
    if state.get("halt"):
        return {
            "id": "halt-" + str(state["halt"].get("at", "")),
            "status": "halted",
            "detail": state["halt"]["detail"],
        }
    return state.get("failure")


def upstream_of(repo: Repo) -> tuple[str, str] | str:
    late = "git did not answer in the notes repo in time, so its upstream is unknown"
    head = repo.git("symbolic-ref", "-q", "--short", "HEAD")
    if head is None:
        return late
    branch = head.stdout.strip() if head.returncode == 0 else ""
    if not branch:
        return "HEAD is detached"
    found: list[str] = []
    for key in ("remote", "merge"):
        r = repo.git("config", f"branch.{branch}.{key}")
        if r is None:
            return late
        found.append(r.stdout.strip() if r.returncode == 0 else "")
    remote, merge = found
    if not remote or not merge:
        return f"branch {branch} has no upstream"
    if remote == ".":
        return f"branch {branch} tracks a local branch"
    push_remote = repo.out("config", f"branch.{branch}.pushRemote") or repo.out(
        "config", "remote.pushDefault"
    )
    if push_remote and push_remote != remote:
        return f"pushes go to {push_remote}, not the upstream remote {remote}"
    return (remote, merge)


def _busy(repo: Repo, gitdir: Path) -> str | None:
    for name in OPERATIONS:
        if (gitdir / name).exists():
            return f"a git operation is in progress ({name})"
    unmerged = repo.out("ls-files", "-u")
    if unmerged is None:
        return "git ls-files failed"
    return "the index has unmerged entries" if unmerged else None


def _counts(repo: Repo) -> tuple[int, int] | None:
    text = repo.out("rev-list", "--left-right", "--count", "HEAD...@{u}")
    try:
        ahead, behind = (int(n) for n in (text or "").split())
    except ValueError:
        return None
    return (ahead, behind)


def _rebase(repo: Repo, gitdir: Path, common: Path, state: dict[str, Any]) -> Result | None:
    local, upstream = (repo.out("rev-parse", "HEAD"), repo.out("rev-parse", "@{u}"))
    if local is None or upstream is None:
        return Result("error", "could not read HEAD or upstream before the rebase")
    pair = {"local": local, "upstream": upstream}
    if state.get("conflict") == pair:
        return Result("conflict", "unchanged since the last rebase conflict; resolve it by hand")
    if busy := _busy(repo, gitdir):
        return _fail(common, state, "busy", busy)
    dirty = repo.out("status", "--porcelain")
    if dirty is None:
        return Result("error", "git status failed before the rebase")
    if dirty:
        return Result("deferred", "notes changed during the sync; the next trigger rebases")
    r = repo.git("rebase", "--quiet", "--no-autostash", "@{u}")
    if r is not None and r.returncode == 0:
        state.pop("conflict", None)
        return None
    aborted = True
    if any(((gitdir / n).exists() for n in ("rebase-merge", "rebase-apply"))):
        a = repo.git("rebase", "--abort", cleanup=True)
        aborted = a is not None and a.returncode == 0
    head = repo.out("rev-parse", "HEAD", cleanup=True)
    if not aborted or head != local or any(((gitdir / n).exists() for n in ("rebase-merge", "rebase-apply"))):
        detail = f"rebase onto upstream failed and could not be undone; the commit before it was {local}. Check `git -C {repo.path} status`, then run `agent notes-sync clear`"
        state["halt"] = {"detail": detail, "saved_head": local, "at": core.iso(core.now())}
        return _fail(common, state, "halted", detail)
    state["conflict"] = pair
    return _fail(
        common, state, "conflict", f"rebase conflict with upstream ({reason(r)}); the local commit is kept"
    )


def _start(root: Path, budget: float, want: tuple[str, ...]) -> tuple[Repo, Path, Path] | Result:
    mode = config_file.notes_sync_mode(root)
    if mode not in want:
        return Result("off", f"notes_sync is {mode}")
    return open_repo(root, budget)


def _guarded(repo: Repo, common: Path, body: Any) -> Result:
    try:
        with repo_lock(common, min(LOCK_WAIT_S, repo.left())):
            return body()
    except TimeoutError:
        return Result("locked", "another session holds the notes sync lock")
    except _StateError as exc:
        return Result("error", f"{exc}; fix or remove it, nothing syncs until then")


def _never_raises(fn: Any) -> Any:

    def wrapper(*args: Any, **kwargs: Any) -> Result:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            return Result("error", f"{type(exc).__name__}: {exc}")

    wrapper.__doc__ = fn.__doc__
    return wrapper


LAST_FILE = "last.json"
ERRORS = ("error", "offline", "conflict", "halted", "busy", "config", "claims")
OKS = ("current", "pulled", "pushed", "ahead")
UNTOLD = ("off", "none", "current")
MAX_DETAIL = 300


def _last_path(root: Path) -> Path | None:
    from . import git_discovery

    notes = read_notes_root(root)
    if notes is None:
        return None
    try:
        common = git_discovery.common_dir(notes.resolve())
    except Exception:
        return None
    if common is None:
        out = Repo(notes.resolve(), LOCAL_CAP_S).out(
            "rev-parse", "--path-format=absolute", "--git-common-dir"
        )
        if not out:
            return None
        common = Path(out)
    return common / SYNC_DIR / LAST_FILE


def last_outcomes(root: Path) -> dict[str, Any]:
    path = _last_path(root)
    try:
        return as_dict(json.loads(path.read_text(encoding="utf-8"))) if path else {}
    except Exception:
        return {}


def note_outcome(
    root: Path, op: str, status: str, detail: str = "", *, count: int = 0, seconds: float = 0.0
) -> None:
    if status in ("off", "none"):
        return
    try:
        from .hooks.decision_log import redact

        at = core.iso(core.now())
        row: dict[str, Any] = {"op": op, "at": at, "status": status, "detail": redact(detail)[:MAX_DETAIL]}
        path = _last_path(root)
        if path is not None:
            kept = last_outcomes(root)
            prior = as_dict(kept.get("last_attempt")).get("seq")
            row["seq"] = (prior if isinstance(prior, int) else 0) + 1
            kept["last_attempt"] = row
            if status in OKS:
                kept["last_ok"] = row
            elif status in ERRORS:
                kept["last_error"] = row
            path.parent.mkdir(parents=True, exist_ok=True)
            core.atomic_write(path, json.dumps(kept, indent=2, sort_keys=True) + "\n")
        if status not in UNTOLD:
            from .telemetry import attention as events

            events.emit(
                root,
                "notes.sync",
                op=op,
                status=status,
                count=count,
                detail=row["detail"] or None,
                seconds=round(seconds, 3),
            )
    except Exception:
        pass


def _observed(op: str) -> Any:

    def decorate(fn: Any) -> Any:

        def wrapper(root: Path, *args: Any, **kwargs: Any) -> Result:
            started = time.monotonic()
            r: Result = fn(root, *args, **kwargs)
            note_outcome(root, op, r.status, r.detail, count=r.count, seconds=time.monotonic() - started)
            return r

        wrapper.__doc__ = fn.__doc__
        return wrapper

    return decorate


@_observed("pull")
@_never_raises
def pull(root: Path) -> Result:
    opened = _start(root, PULL_BUDGET_S, ("pull", "on"))
    if isinstance(opened, Result):
        return opened
    repo, gitdir, common = opened

    def body() -> Result:
        state = _read_state(common)
        if state.get("halt"):
            return Result("halted", state["halt"]["detail"])
        up = upstream_of(repo)
        if isinstance(up, str):
            return _fail(common, state, "config", up)
        if busy := _busy(repo, gitdir):
            return _fail(common, state, "busy", busy)
        r = repo.git("fetch", "--quiet", up[0], cap=FETCH_CAP_S)
        if r is None or r.returncode:
            return _fail(common, state, "offline", f"fetch failed: {reason(r)}")
        counts, dirty = (_counts(repo), repo.out("status", "--porcelain"))
        if counts is None or dirty is None:
            return Result("error", "could not read the notes repo state")
        ahead, behind = counts
        if dirty:
            return Result("deferred", "uncommitted notes changes; the pull waits for Stop")
        if behind and (not ahead):
            m = repo.git("merge", "--ff-only", "--no-autostash", "--quiet", "@{u}")
            if m is None or m.returncode:
                return _fail(common, state, "error", f"fast-forward failed: {reason(m)}")
            return _converged(repo, common, state, Result("pulled", count=behind, incoming=True))
        if behind:
            if failed := _rebase(repo, gitdir, common, state):
                return failed
            _write_state(common, state)
            return Result(
                "pulled", f"{ahead} local commits rebased; Stop publishes them", behind, incoming=True
            )
        if ahead:
            return Result("ahead", f"{ahead} local commits wait for Stop", ahead)
        return _converged(repo, common, state, Result("current"))

    return _guarded(repo, common, body)


SUBJECT_GROUPS = 6
_FEATURE_DIR = re.compile("^(\\d{3,})-")


def staged_groups(paths: list[str]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for path in paths:
        parts = path.strip().split("/")
        if not parts or not parts[0]:
            continue
        group = parts[0]
        for i, part in enumerate(parts[:-1]):
            if i and parts[i - 1] == "features" and (m := _FEATURE_DIR.match(part)):
                group = m.group(1)
        counts[group] = counts.get(group, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def machine_name(root: Path) -> str:
    try:
        from .dispatch.machine import resolve

        tag = resolve(config_file.machine_root(root)).tag
    except Exception:
        tag = None
    return tag or socket.gethostname()


def _message(root: Path, session: dict[str, Any] | None, staged: list[str]) -> tuple[str, str]:
    groups = staged_groups(staged)
    shown = ", ".join((f"{name} ({n})" for name, n in groups[:SUBJECT_GROUPS]))
    if len(groups) > SUBJECT_GROUPS:
        shown += f", +{len(groups) - SUBJECT_GROUPS} more"
    session = session or {}
    feature = as_dict(session.get("feature"))
    feature_id = as_feature_id(feature.get("id"))
    label = f"{feature_id:03d}-{feature['slug']}" if feature_id and feature.get("slug") else "unmapped"
    who = session.get("name") or session.get("session_id") or "no session"
    files = len(staged)
    count = f"{files} file{('' if files == 1 else 's')}"
    body = f"Stop of session {who} ({label}) on {machine_name(root)}. {count}."
    return (f"notes: auto-sync {shown or 'notes'}", body)


@_observed("sync")
@_never_raises
def sync(root: Path, session: dict[str, Any] | None = None) -> Result:
    opened = _start(root, STOP_BUDGET_S, ("on",))
    if isinstance(opened, Result):
        return opened
    repo, gitdir, common = opened

    def body() -> Result:
        state = _read_state(common)
        if state.get("halt"):
            return Result("halted", state["halt"]["detail"])
        up = upstream_of(repo)
        if isinstance(up, str):
            return _fail(common, state, "config", up)
        if busy := _busy(repo, gitdir):
            return _fail(common, state, "busy", busy)
        if why := notes_claim_guard.refusal(repo):
            return _fail(common, state, "claims", why)
        with contextlib.suppress(OSError):
            from .notes import ensure_hygiene

            ensure_hygiene(repo.path)
        dirty = repo.out("status", "--porcelain")
        if dirty is None:
            return Result("error", "git status failed")
        if dirty:
            if (a := repo.git("add", "-A", "--", ".", NOT_CLAIMS, NOT_LABELS)) is None or a.returncode:
                return _fail(common, state, "error", f"git add failed: {reason(a)}")
            staged = repo.out("diff", "--cached", "--name-only")
            if staged is None:
                return Result("error", "could not read the staged changes")
            if why := notes_claim_guard.refusal(repo, staged):
                return _fail(common, state, "claims", why)
            if staged:
                subject, body_text = _message(root, session, staged.splitlines())
                c = repo.git("commit", "--quiet", "-m", subject, "-m", body_text)
                if c is None or c.returncode:
                    return _fail(common, state, "error", f"commit failed: {reason(c)}")
        counts = _counts(repo)
        if counts is None:
            return Result("error", "could not count commits against upstream")
        if counts[0] == 0:
            return _converged(repo, common, state, Result("current"))
        return _publish(repo, gitdir, common, state, up)

    return _guarded(repo, common, body)


def _publish(repo: Repo, gitdir: Path, common: Path, state: dict[str, Any], up: tuple[str, str]) -> Result:
    rebased: list[bool] = []
    result = _publish_once(repo, gitdir, common, state, up, rebased)
    result.incoming = result.incoming or any(rebased)
    return result


def _publish_once(
    repo: Repo, gitdir: Path, common: Path, state: dict[str, Any], up: tuple[str, str], rebased: list[bool]
) -> Result:
    remote, branch = up
    for attempt in (1, 2):
        r = repo.git("fetch", "--quiet", remote, cap=FETCH_CAP_S)
        if r is None or r.returncode:
            return _fail(common, state, "offline", f"fetch failed: {reason(r)}")
        counts = _counts(repo)
        if counts is None:
            return Result("error", "could not count commits against upstream")
        if counts[1]:
            if failed := _rebase(repo, gitdir, common, state):
                return failed
            rebased.append(True)
        dirty = repo.out("status", "--porcelain")
        if dirty is None:
            return Result("error", "git status failed before the push")
        if dirty:
            _write_state(common, state)
            return Result("deferred", "notes changed during the sync; the next Stop publishes them")
        p = repo.git("push", "--quiet", remote, f"HEAD:{branch}", cap=PUSH_CAP_S)
        if p is not None and p.returncode == 0:
            return _converged(repo, common, state, Result("pushed", count=counts[0]))
        rejected = p is not None and any(
            (w in (p.stderr or "") for w in ("rejected", "fetch first", "non-fast-forward"))
        )
        if not rejected or attempt == 2:
            return _fail(common, state, "offline" if p is None else "error", f"push failed: {reason(p)}")
    return Result("error", "unreachable")


def clear_halt(root: Path) -> str:
    opened = open_repo(root, PULL_BUDGET_S)
    if isinstance(opened, Result):
        raise core.AgentError("no linked notes repo with git")
    repo, gitdir, common = opened
    with repo_lock(common, LOCK_WAIT_S):
        if busy := _busy(repo, gitdir):
            raise core.AgentError(f"not cleared: {busy}; finish or abort it first")
        try:
            state = _read_state(common)
        except _StateError as exc:
            raise core.AgentError(f"not cleared: {exc}; inspect it, then remove it by hand") from None
        had = bool(state.pop("halt", None))
        for key in ("conflict", "failure", "attempts"):
            state.pop(key, None)
        _write_state(common, state)
    return "halt cleared; the next trigger syncs again" if had else "no halt was recorded"
