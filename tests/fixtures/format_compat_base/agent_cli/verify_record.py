# ruff: noqa
from __future__ import annotations
import hashlib
import json
import os
import platform
import re
import secrets
import subprocess
import time
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from . import config_file, verify_run
from .core import AgentError, agent_path, atomic_write
from .records.narrow import as_dict, as_dict_or_none, as_float, as_int, str_list

SCHEMA = 1
POLICY = 4
STORE = agent_path(Path(), "verify")
RECORD_WAIT_S = 7200.0
KEY_POLL_S = 2.0
DIGEST_ENV = ("LANG", "LC_ALL", "LC_CTYPE", "TZ")
PLUMBING = frozenset({"PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TERM"})
SESSION_ONLY = re.compile(
    "^(?:CLAUDE|AGENT_SESSION|AGENT_TELEMETRY|AGENT_NOW$|AGENT_ROOT$|AGENT_DISPATCH_ID$|AGENT_VERIFY_RUN_ID$|CODEX_THREAD|CODEX_SESSION)"
)
_SUMMARY = re.compile("^=*\\s*(\\d+ passed\\b.*?)=*$", re.M)
_BAD = re.compile("\\b[1-9]\\d* (?:failed|errors?)\\b")
_RUN_ID = re.compile("[0-9a-f]{32}")


def main_of(root: Path) -> Path:
    from .worktree import main_checkout

    return main_checkout(root)


def store(root: Path) -> Path:
    return main_of(root) / STORE


def resolve(root: Path, rev: str) -> tuple[str, str]:
    out: list[str] = []
    for spec in (f"{rev}^{{commit}}", f"{rev}^{{tree}}"):
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", spec],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if rev.startswith("-") or proc.returncode or (not proc.stdout.strip()):
            raise AgentError(f"{rev} is not a commit")
        out.append(proc.stdout.strip())
    return (out[0], out[1])


def _version(argv: list[str], env: Mapping[str, str] | None = None, missing: int | None = None) -> str:
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
    def __init__(self, message: str, run_dir: Path, category: str = "failed") -> None:
        super().__init__(message)
        self.run_dir = run_dir
        self.category = category


class NotListed(AgentError):
    pass


def harness_repo(root: Path) -> Any:
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
    from .dispatch.options import verifier_env

    declared = {str(k) for k in as_dict(rp.dispatch.get("env"))}
    return {
        name: hashlib.sha256(value.encode()).hexdigest()[:16]
        for name, value in sorted(verifier_env(rp).items())
        if name in declared or (name not in PLUMBING and (not SESSION_ONLY.match(name)))
    }


def policy(rp: Any) -> tuple[str, str]:
    from .dispatch.verification_evidence import config_digest

    if rp.verification is None:
        raise AgentError(f"{rp.name} has no dispatch.verify commands; a full pass record needs them")
    config = config_digest(rp.verification)
    return (_digest([POLICY, config, verifier_inputs(rp)]), config)


def _path(root: Path, tree: str, env_digest: str, policy_digest: str) -> Path:
    return store(root) / "v1" / tree / f"{_digest([env_digest, policy_digest])[:24]}.json"


def pointer(record: dict[str, Any]) -> str:
    return f"full pass record {record['run_id']} (tree {record['tree'][:12]})"


def check(
    record: Any, root: Path, *, tree: str, env_digest: str, policy_digest: str, config: str
) -> str | None:
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
    from . import verify_retry

    return verify_retry.check(root, record, evidence, life, log)


def _failure(
    evidence: Any, life: dict[str, Any], log: str, count: int, names: list[str] | None = None
) -> str | None:
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
    rp = harness_repo(root)
    sha, tree = resolve(root, rev)
    env_digest = env_digest or str(environment(rp)["digest"])
    policy_digest, config = policy(rp)
    path = _path(root, tree, env_digest, policy_digest)
    again = f"; run `agent verify record {sha[:12]}`"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return (None, f"no full pass record for {sha[:12]} (tree {tree[:12]}) under these tools{again}")
    except OSError, ValueError:
        return (None, f"the pass record at {path} is unreadable{again}")
    extra = str_list(as_dict(record).get("extra"))
    if extra:
        from .dispatch.test_select import with_extra
        from .dispatch.verification_evidence import config_digest

        config = config_digest(with_extra(rp.verification, extra))
    why = check(record, root, tree=tree, env_digest=env_digest, policy_digest=policy_digest, config=config)
    return (record, "ok") if why is None else (None, f"{sha[:12]}: {why}{again}")


@contextmanager
def _key_lock(root: Path, name: str, wait_s: float):
    if os.name == "nt":
        from contextlib import ExitStack
        from .win_lock import exclusive, open_lock_file

        store(root).mkdir(parents=True, exist_ok=True)
        fd = open_lock_file(store(root), ("locks", f"{name}.lock"))
        try:
            with ExitStack() as held:
                try:
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
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 384)
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
                event(root, sha, tree, "reused", run_id=str(found.get("run_id") or ""))
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
    tier: str = "full",
    run_id: str = "",
    failed: list[str] | tuple[str, ...] = (),
    retried: list[str] | tuple[str, ...] = (),
) -> None:
    from . import telemetry
    from .telemetry.schema import MAX_LIST, MAX_LIST_ITEM

    def ids(nodes: list[str] | tuple[str, ...]) -> list[str]:
        return [n[:MAX_LIST_ITEM] for n in list(nodes)[:MAX_LIST]]

    try:
        telemetry.record(
            main_of(root),
            "verify.record",
            outcome=outcome,
            commit=sha,
            data={
                "tree": tree,
                "tier": tier,
                "run_id": run_id,
                "failed_count": len(failed),
                "failed_nodes": ids(failed),
                "retry_nodes": ids(retried),
                "retry_count": len(retried),
            },
        )
    except Exception:
        return


def _run(root: Path, sha: str, tree: str, extra: tuple[str, ...] = ()) -> dict[str, Any]:
    rp = harness_repo(root)
    env_info = environment(rp)
    policy_digest, _ = policy(rp)
    if rp.verification is None:
        raise AgentError(f"{rp.name} has no dispatch.verify commands; a full pass record needs them")
    from .dispatch.test_select import with_extra
    from .dispatch.verification_evidence import config_digest
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
        started = _now()
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
    retry: dict[str, Any] | None = None
    if why is not None:
        from . import verify_retry

        timed_out = any((c.outcome == "timeout" for c in evidence.commands))
        tried = verify_retry.attempt(root, rp, sha, tree, run_id, outcome, why, names, started)
        retried = str_list(as_dict(tried.retry).get("nodes"))
        if not tried.passed:
            outcome_name = verify_retry.FAILED if tried.retry else "timeout" if timed_out else "failed"
            event(root, sha, tree, outcome_name, run_id=run_id, failed=tried.failed, retried=retried)
            raise RecordFailed(
                f"{sha[:12]} did not pass: {why}; {tried.note}; see {where}",
                where,
                "timeout" if timed_out else "failed",
            )
        retry = tried.retry
        event(root, sha, tree, verify_retry.PASSED, run_id=run_id, failed=tried.failed, retried=retried)
    else:
        event(root, sha, tree, "passed", run_id=run_id)
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
        "config_digest": config,
        **({"extra": list(extra)} if extra else {}),
        **({"retry": retry} if retry else {}),
        "verifier_inputs": sorted(verifier_inputs(rp)),
        "env_digest": env_info["digest"],
        "policy_digest": policy_digest,
        "environment": env_info["facts"],
        "recorded_only": {
            "codex": _version(["codex", "--version"]),
            "agent_vars": sorted((k for k in os.environ if k.startswith(("AGENT_", "CODEX_")))),
        },
        "started": started,
        "completed": _now(),
    }
    atomic_write(_path(root, tree, env_info["digest"], policy_digest), json.dumps(result, indent=1))
    return result


def show(root: Path, rev: str) -> dict[str, Any]:
    sha, tree = resolve(root, rev)
    found, why = lookup(root, sha)
    kept = sorted((p.name for p in (store(root) / "v1" / tree).glob("*.json")))
    from . import land_tier

    small = land_tier.read(root, sha)
    return {
        "sha": sha,
        "tree": tree,
        "applies": found is not None,
        "why": why,
        "record": found,
        "kept": kept,
        **({"small": small} if small else {}),
    }


def covers(root: Path, worktree: Path) -> bool:
    try:
        return main_of(worktree).resolve() == main_of(root).resolve()
    except AgentError, OSError:
        return False


def reusable(
    root: Path, worktree: Path, launch_base: str | None, selector: Any = None
) -> tuple[str, str] | None:
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
    except Exception:
        return None
    return (tree.head, pointer(found)) if found is not None else None
