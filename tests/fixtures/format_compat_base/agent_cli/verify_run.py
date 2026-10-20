# ruff: noqa
from __future__ import annotations
import contextlib
import dataclasses
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from .core import AgentError, agent_path
from .dispatch.verification_store import OBSERVED

RUN_ID = "000-verify-01"
_FILTERS = {
    "-k",
    "-m",
    "--ignore",
    "--ignore-glob",
    "--deselect",
    "--lf",
    "--last-failed",
    "--sw",
    "--stepwise",
}
EVIDENCE_FILES = ("terminal.json", "lifecycle.json", "output.log")
PENDING = "pending-cleanup.json"
_EXAMPLES = (("agent.example.yaml", "agent.yaml"), (".mcp.example.json", ".mcp.json"))


class Git:
    def __init__(self, repo: Path):
        self.env = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
        self.args = [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "core.untrackedCache=false",
        ]
        names = self.run(repo, "config", "--name-only", "--get-regexp", "^filter\\.", ok=(0, 1))
        for key in names.split():
            if key.endswith((".clean", ".smudge", ".process")):
                self.args += ["-c", f"{key}="]
            elif key.endswith(".required"):
                self.args += ["-c", f"{key}=false"]

    def run(self, cwd: Path, *args: str, ok: tuple[int, ...] = (0,)) -> str:
        proc = subprocess.run(
            [*self.args, *args], cwd=cwd, env=self.env, capture_output=True, text=True, check=False
        )
        if proc.returncode not in ok:
            raise AgentError(f"git {' '.join(args)} failed: {proc.stderr.strip()[-300:]}")
        return proc.stdout


def filtered(command: str) -> bool:
    try:
        words = shlex.split(command)
    except ValueError:
        return True
    if "pytest" not in [Path(w).name for w in words]:
        return False
    after = words[[Path(w).name for w in words].index("pytest") + 1 :]
    return any(
        (
            w in _FILTERS
            or any((w.startswith(f + "=") for f in _FILTERS if f.startswith("--")))
            or re.fullmatch("-[km].+", w)
            or (not w.startswith("-") and ("/" in w or w.endswith(".py") or "::" in w))
            for w in after
        )
    )


def run(
    main: Path,
    repo: Any,
    sha: str,
    run_id: str,
    runs: Path,
    extra: tuple[str, ...] = (),
    *,
    config: Any = None,
    unset: tuple[str, ...] = (),
) -> dict[str, Any]:
    from .dispatch import verification
    from .dispatch.options import verifier_env
    from .dispatch.test_select import with_extra

    if repo.verification is None:
        raise AgentError(f"{repo.name} has no dispatch.verify commands; a full pass record needs them")
    config = config if config is not None else with_extra(repo.verification, extra)
    if any((filtered(c.command) for c in repo.verification.commands)):
        raise AgentError(
            "a configured pytest command selects a subset of the suite; that is never a full run"
        )
    git = Git(main)
    holder = Path(tempfile.mkdtemp(prefix=f"verify-{sha[:12]}-"))
    checkout = holder / "tree"
    launched: Any = None
    confirmed = False
    try:
        git.run(main, "worktree", "add", "--detach", str(checkout), sha)
        _refuse_filters(git, checkout)
        if git.run(checkout, "status", "--porcelain", "--ignored=no"):
            raise AgentError("the isolated checkout was not clean before the run")
        for example, name in _EXAMPLES:
            if (checkout / example).is_file():
                shutil.copyfile(checkout / example, checkout / name)
        prepared = verification.prepare(
            checkout,
            checkout,
            dispatch_id=RUN_ID,
            attempt=1,
            final_sha=sha,
            config=config,
            environment={k: v for k, v in verifier_env(repo).items() if k not in unset},
            launch_base=sha,
            run_id=run_id,
        )
        temp = holder / "pytest"
        temp.mkdir()
        prepared = dataclasses.replace(
            prepared, environment={**prepared.environment, "PYTEST_DEBUG_TEMPROOT": str(temp)}
        )
        launched = prepared.binding
        (runs / run_id).mkdir(parents=True, exist_ok=True)
        pending = {"checkout": str(checkout), "holder": str(holder), "binding": dataclasses.asdict(launched)}
        (runs / run_id / PENDING).write_text(json.dumps(pending), encoding="utf-8")
        status = verification.wait(verification.launch_prepared(prepared))
        confirmed = status.cleanup_confirmed
        after = git.run(checkout, "status", "--porcelain", "--ignored=no")
        source = agent_path(checkout, "dispatches", f"{RUN_ID}.verification", run_id)
        target = runs / run_id
        target.mkdir(parents=True, exist_ok=True)
        for name in (*EVIDENCE_FILES, OBSERVED):
            if (source / name).is_file():
                shutil.copyfile(source / name, target / name)
                with contextlib.suppress(OSError):
                    shutil.copystat(source / name, target / name)
        evidence = status.evidence
        return {
            "state": evidence.state if evidence is not None else "missing",
            "cleanup_confirmed": status.cleanup_confirmed,
            "changed": after.splitlines(),
            "evidence": evidence,
            "hashes": {
                name: hashlib.sha256((target / name).read_bytes()).hexdigest()
                for name in EVIDENCE_FILES
                if (target / name).is_file()
            },
        }
    finally:
        if launched is None or confirmed:
            _remove(git, main, checkout, holder)
            (runs / run_id / PENDING).unlink(missing_ok=True)


def _remove(git: Git, main: Path, checkout: Path, holder: Path) -> None:
    git.run(main, "worktree", "remove", "--force", str(checkout), ok=(0, 128))
    git.run(main, "worktree", "prune", ok=(0, 128))
    shutil.rmtree(holder, ignore_errors=True)


def unsettled(run_dir: Path) -> bool:
    from .dispatch.verification_evidence import Binding
    from .dispatch.verification_status import load_status

    try:
        pending = json.loads((run_dir / PENDING).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except OSError, ValueError:
        return True
    try:
        return load_status(Path(pending["checkout"]), Binding(**pending["binding"])).requires_lease
    except Exception:
        return True


def settle(main: Path, run_dir: Path) -> bool:
    from .dispatch.verification_evidence import Binding
    from .dispatch.verification_status import load_status

    marker = run_dir / PENDING
    try:
        pending = json.loads(marker.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return True
    except OSError, ValueError:
        return False
    try:
        checkout = Path(pending["checkout"])
        if load_status(checkout, Binding(**pending["binding"])).requires_lease:
            return False
    except Exception:
        return False
    _remove(Git(main), main, checkout, Path(pending["holder"]))
    marker.unlink(missing_ok=True)
    return True


def _refuse_filters(git: Git, checkout: Path) -> None:
    paths = git.run(checkout, "ls-files", "-z")
    if not paths:
        return
    proc = subprocess.run(
        [*git.args, "check-attr", "--stdin", "-z", "filter"],
        cwd=checkout,
        env=git.env,
        input=paths,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode:
        raise AgentError("git check-attr failed; the tree's attributes cannot be read")
    fields = proc.stdout.split("\x00")
    for i in range(0, len(fields) - 2, 3):
        if fields[i + 2] not in ("unspecified", "unset"):
            raise AgentError(f"{fields[i]} has a `filter` attribute; a pass record never runs one")
