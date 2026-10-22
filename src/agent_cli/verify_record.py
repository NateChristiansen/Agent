"""Tree-keyed full pass records: the evidence `agent land` and the pre-push gate require (see
ADR-0041).

A record says that every configured `dispatch.verify` command passed against one commit's tree,
in an isolated detached checkout of that commit (`verify_run`), under a recorded tool and
environment digest. It is reused for any commit with the identical tree under the same digests,
and the requested commit is added to its provenance. Nothing else is full evidence: a scoped,
exempt or no-diff result, a filtered run, or a record whose run evidence does not check out never
satisfies a lookup. There is no partial-tree reuse: a tree that differs in any byte, the lessons
queue included, needs its own run.

Records live in `<main>/.agent/verify/v1/<tree>/<key>.json`, and each run's evidence (the
runner's terminal snapshot, lifecycle and output log) in `<main>/.agent/verify/runs/<run_id>/`.
That is outside the git common dir, which Codex workers can write. Claude workers have no OS
sandbox yet and can write anywhere the user can, so a worker that fabricates a record together
with consistent run evidence is not stopped until worker containment lands; see ADR-0041.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import secrets
import subprocess
import time
from collections.abc import Generator, Iterable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config_file, verify_run
from .core import AgentError, agent_path, atomic_write
from .records.narrow import (
    as_dict,
    as_dict_or_none,
    as_float,
    as_float_or_none,
    as_int,
    dict_list,
    str_list,
)

SCHEMA = 1
#: Bumped when what a record certifies changes; older records then stop applying.
POLICY = 5
#: Tests the `suite` block lists by spawns and by duration.
TOP_TESTS = 20
STORE = agent_path(Path(), "verify")
#: How long a record request waits for the same key's run, and the poll between tries.
RECORD_WAIT_S = 7200.0
KEY_POLL_S = 2.0
#: Environment values that reach the verify commands and can change a test's outcome. Per-session
#: values (session ids, telemetry routing, `AGENT_NOW`) are left out so a record written under
#: `agent land` still applies to a push from the owner's own shell.
DIGEST_ENV = ("LANG", "LC_ALL", "LC_CTYPE", "TZ")
#: Verifier environment names keyed by name only: shell plumbing that locates tools and scratch
#: space. What it can change is keyed directly, as tool versions and git config.
PLUMBING = frozenset({"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TERM"})
#: Per-session values, which never key a record.
SESSION_ONLY = re.compile(
    r"^(?:CLAUDE|AGENT_SESSION|AGENT_TELEMETRY|AGENT_NOW$|AGENT_ROOT$|AGENT_DISPATCH_ID$|AGENT_VERIFY_RUN_ID$"
    r"|CODEX_THREAD|CODEX_SESSION)"
)
_SUMMARY = re.compile(r"^=*\s*(\d+ passed\b.*?)=*$", re.M)
_BAD = re.compile(r"\b[1-9]\d* (?:failed|errors?)\b")
_RUN_ID = re.compile(r"[0-9a-f]{32}")


def main_of(root: Path) -> Path:
    from .worktree import main_checkout

    return main_checkout(root)


def store(root: Path) -> Path:
    return main_of(root) / STORE


def resolve(root: Path, rev: str) -> tuple[str, str]:
    """The commit and tree `rev` names."""
    out: list[str] = []
    for spec in (f"{rev}^{{commit}}", f"{rev}^{{tree}}"):
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", spec],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if rev.startswith("-") or proc.returncode or not proc.stdout.strip():
            raise AgentError(f"{rev} is not a commit")
        out.append(proc.stdout.strip())
    return out[0], out[1]


_VERSIONS: ContextVar[dict[tuple[Any, ...], str] | None] = ContextVar("verify_record_versions", default=None)


@contextmanager
def probe_scope() -> Generator[None]:
    """Within one record request, probe each tool once. Nothing outlives the request, so the
    next one measures the tools again, whatever changed behind an unchanged launcher."""
    from .dispatch.verification_shell import bash_scope

    if _VERSIONS.get() is not None:
        yield
        return
    token = _VERSIONS.set({})
    try:
        with bash_scope():
            yield
    finally:
        _VERSIONS.reset(token)


def _version(argv: list[str], env: Mapping[str, str] | None = None, missing: int | None = None) -> str:
    """The first line `argv` prints; "unavailable" when it cannot start or exits `missing`.
    Inside `probe_scope`, the same argv and environment are probed once."""
    seen = _VERSIONS.get()
    if seen is None:
        return _probe(argv, env, missing)
    key = (tuple(argv), tuple(sorted((os.environ if env is None else env).items())), missing)
    if key not in seen:
        seen[key] = _probe(argv, env, missing)
    return seen[key]


def _probe(argv: list[str], env: Mapping[str, str] | None, missing: int | None) -> str:
    try:
        out = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=None if env is None else dict(env),
        )
    except OSError, subprocess.SubprocessError:
        return "unavailable"
    if missing is not None and out.returncode == missing:
        return "unavailable"
    text = (out.stdout or out.stderr).strip()
    return text.splitlines()[0] if text else "unknown"


def _tool(name: str, env: Mapping[str, str], bash: str | None) -> str:
    """`name --version` for the program the verify commands would run. On Windows they run under
    Git Bash, whose launcher puts Git's own directories ahead of `PATH` and which ignores PATHEXT,
    so the name is looked up and run by that same bash; without one it is unavailable."""
    if os.name != "nt":
        return _version([name, "--version"], env)
    if bash is None:
        return "unavailable"
    lookup = f"command -v {name} >/dev/null || exit 127; exec {name} --version"
    return _version([bash, "-c", lookup], env, missing=127)


def _verifier_bash() -> str | None:
    if os.name != "nt":
        return None
    from .dispatch.verification_shell import find_bash

    try:
        return str(find_bash())
    except AgentError:
        return None


def _file_sha(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def environment(rp: Any = None) -> dict[str, Any]:
    """What a full run depends on outside the tree. `ruff` and `pyright` are pinned by `uv.lock`,
    which the tree carries; these are what the tree cannot pin. Given the harness repo, tools and
    git config are measured under the verify commands' own environment (`verifier_env`), so a
    `dispatch.env` that points `PATH` or `HOME` elsewhere is measured where the commands run."""
    from .dispatch.options import verifier_env

    env: Mapping[str, str] = verifier_env(rp) if rp is not None else os.environ
    home = Path(env.get("HOME") or Path.home())
    xdg = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    bash = _verifier_bash()
    facts = {
        "uv": _tool("uv", env, bash),
        "git": _tool("git", env, bash),
        "python": platform.python_version(),
        "os": platform.system(),
        "arch": platform.machine(),
        "gitconfig_sha256": _file_sha(home / ".gitconfig"),
        "xdg_gitconfig_sha256": _file_sha(xdg / "git" / "config"),
        "env": {k: hashlib.sha256(env[k].encode()).hexdigest()[:16] for k in DIGEST_ENV if k in env},
    }
    return {"facts": facts, "digest": _digest(facts)}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class RecordFailed(AgentError):
    """A full run that executed and did not pass; `run_dir` holds its evidence and log."""

    def __init__(self, message: str, run_dir: Path, category: str = "failed") -> None:
        super().__init__(message)
        self.run_dir = run_dir
        self.category = category  # `failed`, `timeout` or infrastructure `incomplete`


class NotListed(AgentError):
    """The registry was read and lists no repo that is this checkout."""


def harness_repo(root: Path) -> Any:
    """The registered repo that is this harness checkout; records cover it alone. No registry
    file at all, or one that reads and does not list the harness, raises `NotListed`: the cases a
    public or unconfigured clone's pre-push treats as ungated. A registry file that cannot be read
    (malformed, or a link to nothing) raises its own error."""
    from . import repos as repos_mod

    main = main_of(root).resolve()
    registry = config_file.registry_path(root)
    if not (registry.exists() or registry.is_symlink()):
        raise NotListed("no agent.yaml here, so no registry lists this checkout as the harness")
    for rp in repos_mod.load_registry(root).repos:
        try:
            if main_of(rp.abs_path(root)).resolve() == main:
                return rp
        except AgentError, OSError:
            continue
    raise NotListed("no registered repo is this harness checkout; pass records cover the harness only")


def verifier_inputs(rp: Any) -> dict[str, str]:
    """The verify commands' effective environment, by hashed value: the repo's `dispatch.env`, the
    variables its `env_allow` passes through, locale and time zone. Shell plumbing and per-session
    values inherited from the shell are left out (`PLUMBING`, `SESSION_ONLY`), so the key holds
    between the shell that lands and the shell that pushes; a name `dispatch.env` sets explicitly
    is always keyed, `PATH` and `HOME` included."""
    from .dispatch.options import verifier_env

    declared = {str(k) for k in as_dict(rp.dispatch.get("env"))}
    return {
        name: hashlib.sha256(value.encode()).hexdigest()[:16]
        for name, value in sorted(verifier_env(rp).items())
        if name in declared or (name not in PLUMBING and not SESSION_ONLY.match(name))
    }


def policy(rp: Any) -> tuple[str, str]:
    """`(policy digest, runner config digest)`: the verify commands, their timeouts and the
    environment they run with. A change to any of them makes an existing record inapplicable."""
    from .dispatch.verification_evidence import config_digest

    if rp.verification is None:
        raise AgentError(f"{rp.name} has no dispatch.verify commands; a full pass record needs them")
    config = config_digest(rp.verification)
    return _digest([POLICY, config, verifier_inputs(rp)]), config


def _path(root: Path, tree: str, env_digest: str, policy_digest: str) -> Path:
    return store(root) / "v1" / tree / f"{_digest([env_digest, policy_digest])[:24]}.json"


def pointer(record: dict[str, Any]) -> str:
    return f"full pass record {record['run_id']} (tree {record['tree'][:12]})"


def check(
    record: Any, root: Path, *, tree: str, env_digest: str, policy_digest: str, config: str
) -> str | None:
    """Why `record` cannot certify `tree` under these digests, or None when it can. Reads the run
    evidence the record names; a record without consistent evidence fails closed."""
    from .dispatch.verification_evidence import Evidence

    record = as_dict_or_none(record)
    if record is None or record.get("schema") != SCHEMA or record.get("tier") != "full":
        return "not a full pass record of a known schema"
    if record.get("tree") != tree:
        return "recorded for another tree"
    if record.get("env_digest") != env_digest or record.get("policy_digest") != policy_digest:
        return "tools, environment or verify configuration differ from the recorded run"
    run_id = str(record.get("run_id") or "")
    hashes = as_dict_or_none(record.get("evidence"))
    if not _RUN_ID.fullmatch(run_id) or hashes is None:
        return "the record names no run evidence"
    run_dir = store(root) / "runs" / run_id
    for name in verify_run.EVIDENCE_FILES:
        if _file_sha(run_dir / name) != hashes.get(name) or hashes.get(name) is None:
            return f"the run evidence ({name}) is missing or does not match the record"
    if changed := _suite_changed(run_dir, hashes):
        return f"the run evidence ({changed}) is missing or does not match the record"
    try:
        evidence = Evidence.from_dict(json.loads((run_dir / "terminal.json").read_text(encoding="utf-8")))
        life = json.loads((run_dir / "lifecycle.json").read_text(encoding="utf-8"))
        log = (run_dir / "output.log").read_text(encoding="utf-8", errors="replace")
    except OSError, ValueError, TypeError:
        return "the run evidence is unreadable"
    b = evidence.binding
    if (b.dispatch_id, b.run_id, b.final_sha, b.config_digest) != (
        verify_run.RUN_ID,
        run_id,
        record.get("sha"),
        config,
    ):
        return "the run evidence belongs to another run"
    why = _failure(evidence, life, log, as_int(record.get("command_count")))
    if why is None or record.get("retry") is None:
        return why
    # The run failed only on approved timing flakes and its one serial retry passed; re-derived from
    # both runs' evidence, never trusted from the record.
    from . import verify_retry

    why = verify_retry.check(root, record, evidence, life, log)
    retry = as_dict(record.get("retry"))
    if why is None and (
        changed := _suite_changed(run_dir.parent / str(retry.get("run_id")), retry.get("evidence"))
    ):
        return f"the retry evidence ({changed}) is missing or changed"
    return why


def _suite_changed(run_dir: Path, hashes: object) -> str | None:
    """The first hashed suite file in `run_dir` whose bytes differ from its recorded hash."""
    for name, digest in as_dict(hashes).items():
        if name.startswith("suite-") and _file_sha(run_dir / name) != digest:
            return name
    return None


def _failure(
    evidence: Any, life: dict[str, Any], log: str, count: int, names: list[str] | None = None
) -> str | None:
    """Why the runner's evidence is not a clean full pass, or None; `names` are the commands."""
    sha = evidence.binding.final_sha
    failed = [c for c in evidence.commands if c.outcome != "exited" or c.exit_code != 0]
    if failed:
        first = failed[0]
        text = f" `{names[first.index]}`" if names and 0 <= first.index < len(names) else ""
        return (
            f"a command did not pass (command {first.index}{text}: {first.outcome}, exit {first.exit_code})"
        )
    if evidence.state != "complete":
        return f"the run is {evidence.state} ({life.get('reason') or 'no reason recorded'})"
    if not life.get("finished") or life.get("cleanup_confirmed") is not True:
        return "the run's cleanup was not confirmed"
    if not (
        evidence.before and evidence.after and evidence.before.clean_at(sha) and evidence.after.clean_at(sha)
    ):
        return "the checkout was not clean at the commit before and after the run"
    if count < 1 or len(evidence.commands) != count:
        return "the run does not cover every configured command"
    summaries = _SUMMARY.findall(log)
    if not summaries or _BAD.search(summaries[-1]):
        return "the output has no clean pytest summary"
    return None


def lookup(root: Path, rev: str, *, env_digest: str | None = None) -> tuple[dict[str, Any] | None, str]:
    """The record that certifies `rev`'s tree now, or None and why not."""
    rp = harness_repo(root)
    sha, tree = resolve(root, rev)
    env_digest = env_digest or str(environment(rp)["digest"])
    policy_digest, config = policy(rp)
    path = _path(root, tree, env_digest, policy_digest)
    again = f"; run `agent verify record {sha[:12]}`"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"no full pass record for {sha[:12]} (tree {tree[:12]}) under these tools{again}"
    except OSError, ValueError:
        return None, f"the pass record at {path} is unreadable{again}"
    extra = str_list(as_dict(record).get("extra"))
    if extra:  # the run also ran a selection rule's extra commands, so its config included them
        from .dispatch.test_select import with_extra
        from .dispatch.verification_evidence import config_digest

        config = config_digest(with_extra(rp.verification, extra))
    why = check(record, root, tree=tree, env_digest=env_digest, policy_digest=policy_digest, config=config)
    return (record, "ok") if why is None else (None, f"{sha[:12]}: {why}{again}")


@contextmanager
def _key_lock(root: Path, name: str, wait_s: float):
    """Serialize requests for one certificate key: equal tree, environment and policy wait and reuse
    the result; different keys can run concurrently within verification lanes.

    Acquire land lock, key lock, then verification lane, never in reverse order. Windows uses
    win_lock byte-range locking on a reparse-checked file.
    """
    if os.name == "nt":
        from contextlib import ExitStack

        from .win_lock import exclusive, open_lock_file

        store(root).mkdir(parents=True, exist_ok=True)  # the first record has no store yet
        fd = open_lock_file(store(root), ("locks", f"{name}.lock"))
        try:
            with ExitStack() as held:
                try:  # only the acquisition's timeout is this lock's; the body's errors pass through
                    held.enter_context(exclusive(fd, timeout=wait_s))
                except TimeoutError:
                    raise AgentError(
                        f"a pass record run for this tree is still in progress after {wait_s:g} s"
                    ) from None
                yield
        finally:
            os.close(fd)
        return
    import fcntl

    path = store(root) / "locks" / f"{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + wait_s
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise AgentError(
                        f"a pass record run for this tree is still in progress after {wait_s:g} s"
                    ) from None
                time.sleep(KEY_POLL_S)
        yield
    finally:
        os.close(fd)


def record(
    root: Path, rev: str, *, reuse: bool = True, wait_s: float = RECORD_WAIT_S, extra: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Reuse an applicable record for `rev`'s tree (adding `rev` to its provenance) or run one,
    holding a lane. `extra`: commands a selection rule adds; a reused record must have run them.
    Identical requests share one run, failed ones too (`_key_lock`, `_shared_failure`)."""
    with probe_scope():
        return _record(root, rev, reuse=reuse, wait_s=wait_s, extra=extra)


def _record(root: Path, rev: str, *, reuse: bool, wait_s: float, extra: tuple[str, ...]) -> dict[str, Any]:
    sha, tree = resolve(root, rev)
    rp = harness_repo(root)
    key = f"{tree}-{environment(rp)['digest'][:16]}-{policy(rp)[0][:16]}"
    asked = time.time()
    with _key_lock(root, key, wait_s):
        _shared_failure(root, key, asked, extra)
        if reuse:
            found, _ = lookup(root, sha)
            if found is not None and set(extra) <= set(str_list(found.get("extra"))):
                if sha not in [p.get("sha") for p in found["provenance"]]:
                    found["provenance"].append({"sha": sha, "at": _now()})
                    atomic_write(
                        _path(root, tree, found["env_digest"], found["policy_digest"]),
                        json.dumps(found, indent=1),
                    )
                event(
                    root, sha, tree, "reused", run_id=str(found.get("run_id") or ""), suite=found.get("suite")
                )
                return {**found, "reused": True}
        try:
            return {**_run(root, sha, tree, extra), "reused": False}
        except AgentError as exc:
            failed = {
                "at": time.time(),
                "message": str(exc),
                "run_dir": str(exc.run_dir) if isinstance(exc, RecordFailed) else "",
                "extra": list(extra),
                "category": exc.category if isinstance(exc, RecordFailed) else "failed",
            }
            atomic_write(store(root) / "locks" / f"{key}.failed", json.dumps(failed))
            raise


def _shared_failure(root: Path, key: str, asked: float, extra: tuple[str, ...]) -> None:
    """Raise the outcome of the run this request waited on, when that run failed after the
    request was made and covered the commands it asks for (Codex, phase-2 review: two identical
    failing requests ran the suite twice)."""
    try:
        failed = as_dict(json.loads((store(root) / "locks" / f"{key}.failed").read_text(encoding="utf-8")))
    except OSError, ValueError:
        return
    if not set(extra) <= set(str_list(failed.get("extra"))):
        return
    if as_float(failed.get("at")) < asked:
        return
    message = f"the identical run this request waited on failed: {failed.get('message')}"
    if failed.get("run_dir"):
        raise RecordFailed(message, Path(str(failed["run_dir"])), str(failed.get("category") or "failed"))
    raise AgentError(message)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def event(
    root: Path,
    sha: str,
    tree: str,
    outcome: str,
    *,
    run_id: str = "",
    failed: list[str] | tuple[str, ...] = (),
    retried: list[str] | tuple[str, ...] = (),
    suite: object = None,
) -> None:
    """Record each certificate outcome: passed, reused, failed, timeout, retry-passed or retry-failed.
    Cap node lists to schema bounds while failed_count keeps the true total. Never raise.
    """
    from . import telemetry
    from .telemetry.schema import MAX_LIST, MAX_LIST_ITEM

    def ids(nodes: list[str] | tuple[str, ...]) -> list[str]:
        return [n[:MAX_LIST_ITEM] for n in list(nodes)[:MAX_LIST]]

    figures = as_dict(suite)
    try:
        telemetry.record(
            main_of(root),
            "verify.record",
            outcome=outcome,
            commit=sha,
            data={
                "tree": tree,
                "tier": "full",
                "run_id": run_id,
                "failed_count": len(failed),
                "failed_nodes": ids(failed),
                "retry_nodes": ids(retried),
                "retry_count": len(retried),
                **{k: as_float_or_none(figures.get(k)) for k in ("wall_s", "cpu_children_s")},
                **{k: _count(figures.get(k)) for k in ("tests", "spawns")},
            },
        )
    except Exception:  # noqa: BLE001 — telemetry never changes a record's outcome
        return


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _loadavg() -> list[float] | None:
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except AttributeError, OSError:  # Windows has none
        return None


def summary_counts(log: str) -> list[int]:
    """The tests each pytest summary in `log` counts, in order: passed, failed, errors, skipped,
    xfailed and xpassed. Warnings, deselected and rerun tests are not tests that ran."""
    from . import verify_retry

    totals = verify_retry._TOTALS.findall(log)  # pyright: ignore[reportPrivateUsage]
    return [sum(int(n) for n, _ in verify_retry._RAN.findall(t)) for t in totals]  # pyright: ignore[reportPrivateUsage]


def _figures(run_dir: Path, index: int, seconds: float | None) -> dict[str, Any]:
    """Command `index`'s own suite stats as advisory figures; `stats: missing` when it wrote none,
    never zeros in their place."""
    figures: dict[str, Any] = {"wall_s": None if seconds is None else round(seconds, 3)}
    try:
        stats = as_dict_or_none(
            json.loads((run_dir / verify_run.suite_file(index)).read_text(encoding="utf-8"))
        )
    except OSError, ValueError:
        stats = None
    if stats is None:
        return {**figures, "stats": "missing"}
    totals = as_dict(stats.get("totals"))
    rows = dict_list(stats.get("tests"))

    def top(key: str) -> list[dict[str, Any]]:
        ranked = sorted(rows, key=lambda r: -as_float(r.get(key)))[:TOP_TESTS]
        return [{"test": str(r.get("test") or ""), key: r.get(key)} for r in ranked]

    return {
        **figures,
        "stats": verify_run.suite_file(index),
        "spawns": _count(totals.get("spawns")),
        **{k: as_float_or_none(totals.get(k)) for k in ("test_seconds", "cpu_children_s", "opaque_share")},
        "top_spawns": top("spawns"),
        "top_seconds": top("s"),
    }


def _span_tests(run_dir: Path, index: int) -> tuple[int | None, str]:
    """The tests command `index`'s one pytest summary counts, read only from the part of the log
    that command wrote, or None and why."""
    try:
        begin, end = (int(n) for n in (run_dir / verify_run.suite_file(index, "span")).read_text().split())
        text = (run_dir / "output.log").read_bytes()[begin:end].decode("utf-8", "replace")
    except OSError, ValueError:
        return None, f"command {index} left no readable log span"
    counts = summary_counts(text)
    if len(counts) != 1:
        return None, f"command {index}'s output holds {len(counts)} pytest summaries, not one"
    return counts[0], ""


def suite(
    run_dir: Path,
    evidence: Any,
    names: list[str],
    configured: int,
    loadavg: list[float] | None,
    retry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """What the run's suite cost. `tests` is read from the full pytest command's own span of the
    log and `spawns` from its own stats; the first `configured` of `names` are the verify commands,
    the rest a selection rule's extras. Extras and a serial flake retry are kept apart, advisory
    only. What cannot be read is None, and `missing` says why."""
    from . import verify_retry

    seconds = {int(c.index): float(c.elapsed_seconds) for c in evidence.commands}
    full = verify_retry.pytest_index(names[:configured])
    missing: list[str] = []
    block: dict[str, Any] = {"command": full, "tests": None, "spawns": None}
    if full is None or full not in seconds:
        missing.append("the verify commands hold no single pytest command that ran")
    else:
        figures = _figures(run_dir, full, seconds.get(full))
        block["tests"], why = _span_tests(run_dir, full)
        if why:
            missing.append(why)
        if figures["stats"] == "missing":
            missing.append(f"command {full} wrote no suite stats")
        block |= figures
    block["loadavg"] = loadavg
    block["extras"] = [
        {
            "index": i,
            "command": names[i],
            "tests": _span_tests(run_dir, i)[0],
            **_figures(run_dir, i, seconds.get(i)),
        }
        for i in sorted(seconds)
        if i >= configured
    ]
    if retry is not None:
        block["retry"] = _retry_figures(run_dir.parent / str(retry.get("run_id") or ""))
    block["missing"] = missing
    return block


def _retry_figures(run_dir: Path) -> dict[str, Any]:
    from .dispatch.verification_evidence import Evidence

    try:
        evidence = Evidence.from_dict(json.loads((run_dir / "terminal.json").read_text(encoding="utf-8")))
        rerun = evidence.commands[-1]
    except OSError, ValueError, TypeError, IndexError:
        return {"run_id": run_dir.name, "stats": "missing"}
    return {
        "run_id": run_dir.name,
        "index": rerun.index,
        **_figures(run_dir, rerun.index, rerun.elapsed_seconds),
    }


def _run(root: Path, sha: str, tree: str, extra: tuple[str, ...] = ()) -> dict[str, Any]:
    rp = harness_repo(root)
    env_info = environment(rp)
    policy_digest, _ = policy(rp)
    if rp.verification is None:
        raise AgentError(f"{rp.name} has no dispatch.verify commands; a full pass record needs them")
    from .dispatch.test_select import with_extra
    from .dispatch.verification_evidence import config_digest, infrastructure_cause
    from .repos import VerificationCommand

    run_config = with_extra(rp.verification, extra)
    commands: tuple[VerificationCommand, ...] = run_config.commands
    config = config_digest(run_config)
    run_id = secrets.token_hex(16)
    runs = store(root) / "runs"
    from .dispatch.lane import record_lane, wait_limit

    main = main_of(root)
    with record_lane(
        root, rp, run_id, wait_limit(rp), settled=lambda: verify_run.settle(main, runs / run_id)
    ):
        started, loadavg = _now(), _loadavg()
        outcome = verify_run.run(main, rp, sha, run_id, runs, extra)
    evidence = outcome["evidence"]
    where = runs / run_id
    if outcome["changed"]:
        raise AgentError(f"the run changed the isolated checkout of {sha[:12]}; see {where}")
    try:
        life = json.loads((where / "lifecycle.json").read_text(encoding="utf-8"))
        log = (where / "output.log").read_text(encoding="utf-8", errors="replace")
    except OSError, ValueError:
        raise AgentError(f"the run left no readable evidence; see {where}") from None
    if evidence is None:
        raise AgentError(f"the run of {sha[:12]} left no evidence; see {where}")
    names = [c.command for c in commands]
    why = _failure(evidence, life, log, len(commands), names)
    cause = infrastructure_cause(
        evidence,
        run_config,
        output=log,
        lifecycle_reason=str(life.get("reason") or "") if life.get("command_pid") is None else "",
    )
    if cause:
        timed_out = any(c.outcome == "timeout" for c in evidence.commands)
        event(root, sha, tree, "timeout" if timed_out else "failed", run_id=run_id)
        raise RecordFailed(
            f"{sha[:12]} did not pass: {why or cause}; verification infrastructure: {cause}; see {where}",
            where,
            "timeout" if timed_out else "incomplete",
        )
    retry: dict[str, Any] | None = None
    if why is not None:
        from . import verify_retry

        timed_out = any(c.outcome == "timeout" for c in evidence.commands)
        tried = verify_retry.attempt(root, rp, sha, tree, run_id, outcome, why, names, started)
        retried = str_list(as_dict(tried.retry).get("nodes"))
        if not tried.passed:
            outcome_name = verify_retry.FAILED if tried.retry else ("timeout" if timed_out else "failed")
            event(root, sha, tree, outcome_name, run_id=run_id, failed=tried.failed, retried=retried)
            raise RecordFailed(
                f"{sha[:12]} did not pass: {why}; {tried.note}; see {where}",
                where,
                "timeout" if timed_out else "failed",
            )
        retry = tried.retry
        cost = suite(where, evidence, names, len(rp.verification.commands), loadavg, retry)
        event(
            root,
            sha,
            tree,
            verify_retry.PASSED,
            run_id=run_id,
            failed=tried.failed,
            retried=retried,
            suite=cost,
        )
    else:
        cost = suite(where, evidence, names, len(rp.verification.commands), loadavg)
        event(root, sha, tree, "passed", run_id=run_id, suite=cost)
    result = {
        "schema": SCHEMA,
        "policy": POLICY,
        "tier": "full",
        "sha": sha,
        "tree": tree,
        "provenance": [{"sha": sha, "at": started}],
        "run_id": run_id,
        "evidence": outcome["hashes"],
        "command_count": len(commands),
        "commands": [
            {
                "index": c.index,
                "command": commands[int(c.index)].command,
                "outcome": c.outcome,
                "exit_code": c.exit_code,
                "seconds": round(c.elapsed_seconds, 3),
            }
            for c in evidence.commands
        ],
        "suite": cost,
        "config_digest": config,
        **({"extra": list(extra)} if extra else {}),
        # A run that passed only through its one serial retry of approved timing flakes: the
        # failed run stays failed in its own evidence and record; `check` re-derives both.
        **({"retry": retry} if retry else {}),
        "verifier_inputs": sorted(verifier_inputs(rp)),
        "env_digest": env_info["digest"],
        "policy_digest": policy_digest,
        "environment": env_info["facts"],
        "recorded_only": {
            "codex": _version(["codex", "--version"]),
            "agent_vars": sorted(k for k in os.environ if k.startswith(("AGENT_", "CODEX_"))),
        },
        "started": started,
        "completed": _now(),
    }
    atomic_write(_path(root, tree, env_info["digest"], policy_digest), json.dumps(result, indent=1))
    return result


def show(root: Path, rev: str) -> dict[str, Any]:
    """The applicable record for `rev`, why none applies, and every record kept for its tree."""
    sha, tree = resolve(root, rev)
    found, why = lookup(root, sha)
    kept = sorted(p.name for p in (store(root) / "v1" / tree).glob("*.json"))
    return {"sha": sha, "tree": tree, "applies": found is not None, "why": why, "record": found, "kept": kept}


def by_run(root: Path, run_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """The kept records of these runs, keyed by run id, as stored and unchecked: figures to show,
    never evidence. A run with no readable record is left out."""
    wanted = set(run_ids) - {""}
    found: dict[str, dict[str, Any]] = {}
    if not wanted:
        return found
    for path in (store(root) / "v1").glob("*/*.json"):
        try:
            kept = as_dict(json.loads(path.read_text(encoding="utf-8")))
        except OSError, ValueError:
            continue
        if (run_id := str(kept.get("run_id") or "")) in wanted:
            found[run_id] = kept
    return found


def covers(root: Path, worktree: Path) -> bool:
    """True when `worktree` is a checkout of this harness, the one repo records cover."""
    try:
        return main_of(worktree).resolve() == main_of(root).resolve()
    except AgentError, OSError:
        return False


def reusable(
    root: Path, worktree: Path, launch_base: str | None, selector: Any = None
) -> tuple[str, str] | None:
    """`(head, pointer)` when a clean harness worktree's head tree has an applicable record that
    also ran the extra commands `selector`'s selection requires ."""
    from .dispatch.verification_process import observe_tree

    if not covers(root, worktree):
        return None
    try:
        tree = observe_tree(worktree, launch_base=launch_base)
        if not (tree.tracked_clean and tree.index_clean):
            return None
        found, _ = lookup(root, tree.head)
        if found is not None and selector is not None:
            extra = selector.select(worktree, launch_base, tree.head).extra
            if not set(extra) <= set(str_list(found.get("extra"))):
                return None
    except Exception:  # noqa: BLE001 — anything unknown means run the verifier
        return None
    return (tree.head, pointer(found)) if found is not None else None
