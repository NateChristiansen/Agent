"""The full run behind a pass record: every `dispatch.verify` command, in an isolated detached
checkout of one commit, under the existing verifier runner (guardian, watchdog, cancellation and
the fail-closed process inventory).

The checkout is made the way `dispatch reverify` makes one: a `mkdtemp` holder, `git worktree add
--detach`, hooks and fsmonitor off, removed afterwards. Every git call runs without system or
global config and with each configured filter driver blanked, and a tree whose attributes name a
`filter` is refused, so no configurable program runs during certification. The checkout gets the
tracked example config the way CI does, and is its own `AGENT_ROOT`: the owner's registry and
local state are never inputs to a record. `git status` before and after must both be empty. The
runner's evidence directory is copied into the record store before the checkout goes away, with
each command's own suite stats (`suite_file`), which the runner names per command.
"""

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

#: The pseudo-dispatch every record run is bound to; the runner's evidence names one.
RUN_ID = "000-verify-01"
#: What a configured pytest command may not carry and still be a full run.
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
#: Run evidence the record hashes; each must exist in the copied run directory.
EVIDENCE_FILES = ("terminal.json", "lifecycle.json", "output.log")
PENDING = "pending-cleanup.json"
_EXAMPLES = (("agent.example.yaml", "agent.yaml"), (".mcp.example.json", ".mcp.json"))


class Git:
    """Hardened git for a record run: no system or global config, no hooks, no fsmonitor, every
    configured filter driver blanked."""

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
        names = self.run(repo, "config", "--name-only", "--get-regexp", r"^filter\.", ok=(0, 1))
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
    """True when a pytest command selects a subset: `-k`, `-m`, `--ignore`, `--deselect`, a
    last-failed or stepwise run, or a test path argument."""
    try:
        words = shlex.split(command)
    except ValueError:
        return True
    if "pytest" not in [Path(w).name for w in words]:
        return False
    after = words[[Path(w).name for w in words].index("pytest") + 1 :]
    return any(
        w in _FILTERS
        or any(w.startswith(f + "=") for f in _FILTERS if f.startswith("--"))
        or re.fullmatch(r"-[km].+", w)
        or (not w.startswith("-") and ("/" in w or w.endswith(".py") or "::" in w))
        for w in after
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
    """Run `repo`'s verify commands, then any `extra` ones a selection rule requires, against `sha`
    and copy the evidence to `runs/<run_id>/`. Returns the outcome; raises when the run cannot be
    made. A run whose cleanup is not confirmed keeps its checkout and leaves `PENDING` naming it,
    for `settle` to finish once the verifier's processes are gone.
    `unset`: environment names the commands run without (a flake retry clears `PYTEST_ADDOPTS`)."""
    from .dispatch import verification
    from .dispatch.lane import temp_parent
    from .dispatch.options import verifier_env
    from .dispatch.test_select import with_extra

    if repo.verification is None:
        raise AgentError(f"{repo.name} has no dispatch.verify commands; a full pass record needs them")
    # `config`: a flake retry's serial command (`verify_retry`), never a full run.
    config = config if config is not None else with_extra(repo.verification, extra)
    # The configured commands must be the whole suite; a selection rule's extras are deliberately
    # narrow reruns on top of it (the phase-5 dogfood found extras refused here).
    if any(filtered(c.command) for c in repo.verification.commands):
        raise AgentError(
            "a configured pytest command selects a subset of the suite; that is never a full run"
        )
    git = Git(main)
    holder = Path(tempfile.mkdtemp(prefix=f"verify-{sha[:12]}-", dir=temp_parent(main)))
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
        temp, stats = holder / "pytest", holder / "suite"
        temp.mkdir()
        stats.mkdir()
        prepared = dataclasses.replace(
            prepared,
            environment={
                **prepared.environment,
                "PYTEST_DEBUG_TEMPROOT": str(temp),
                verification.SUITE_STATS_DIR: str(stats),
            },
        )
        launched = prepared.binding
        # Written before launch, so a crash at any later point leaves the run recoverable and its
        # lane held (the marker was written only in `finally`).
        (runs / run_id).mkdir(parents=True, exist_ok=True)
        pending = {"checkout": str(checkout), "holder": str(holder), "binding": dataclasses.asdict(launched)}
        (runs / run_id / PENDING).write_text(json.dumps(pending), encoding="utf-8")
        status = verification.wait(verification.launch_prepared(prepared))
        confirmed = status.cleanup_confirmed
        after = git.run(checkout, "status", "--porcelain", "--ignored=no")
        source = agent_path(checkout, "dispatches", f"{RUN_ID}.verification", run_id)
        target = runs / run_id
        target.mkdir(parents=True, exist_ok=True)
        for name in (*EVIDENCE_FILES, OBSERVED):  # the sidecar travels along, unhashed
            if (source / name).is_file():
                shutil.copyfile(source / name, target / name)
                with contextlib.suppress(OSError):  # the mtime is best effort; the bytes are the evidence
                    shutil.copystat(source / name, target / name)
        suite: list[str] = []
        for index in range(len(config.commands)):
            for kind in ("json", "span"):
                if (stats / f"{index}.{kind}").is_file():
                    shutil.copyfile(stats / f"{index}.{kind}", target / suite_file(index, kind))
                    suite.append(suite_file(index, kind))
        evidence = status.evidence
        return {
            "state": evidence.state if evidence is not None else "missing",
            "cleanup_confirmed": status.cleanup_confirmed,
            "changed": after.splitlines(),
            "evidence": evidence,
            "hashes": {
                name: hashlib.sha256((target / name).read_bytes()).hexdigest()
                for name in (*EVIDENCE_FILES, *suite)
                if (target / name).is_file()
            },
        }
    finally:
        if launched is None or confirmed:
            _remove(git, main, checkout, holder)
            (runs / run_id / PENDING).unlink(missing_ok=True)


def suite_file(index: int, kind: str = "json") -> str:
    """Command `index`'s copied stats (`json`) or output log byte range (`span`)."""
    return f"suite-{index}.{kind}"


def _remove(git: Git, main: Path, checkout: Path, holder: Path) -> None:
    git.run(main, "worktree", "remove", "--force", str(checkout), ok=(0, 128))
    git.run(main, "worktree", "prune", ok=(0, 128))
    shutil.rmtree(holder, ignore_errors=True)


def unsettled(run_dir: Path) -> bool:
    """Whether a run may still have processes: its marker is there and its verifier has not
    confirmed cleanup. Reads only; `settle` does the removal."""
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
    except Exception:  # noqa: BLE001 — unreadable status: cleanup stays unconfirmed
        return True


def settle(main: Path, run_dir: Path) -> bool:
    """Whether a run's processes are gone, removing a checkout it kept for unconfirmed cleanup
    once its verifier confirms it. A record lane is released only when this is true."""
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
    except Exception:  # noqa: BLE001 — unreadable status: cleanup stays unconfirmed
        return False
    _remove(Git(main), main, checkout, Path(pending["holder"]))
    marker.unlink(missing_ok=True)
    return True


def _refuse_filters(git: Git, checkout: Path) -> None:
    """A tree whose attributes name a filter would run a configured program; refuse it."""
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
    fields = proc.stdout.split("\0")
    for i in range(0, len(fields) - 2, 3):
        if fields[i + 2] not in ("unspecified", "unset"):
            raise AgentError(f"{fields[i]} has a `filter` attribute; a pass record never runs one")
