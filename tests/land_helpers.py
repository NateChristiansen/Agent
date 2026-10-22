"""A harness checkout to land into, and a way to run `agent land` in a child process that can
crash or pause at a named step (land.py's test seam)."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

import suite_cost
from agent_cli import land_journal as jn
from agent_cli import verify_record

PASS = "echo 3 passed in 0.01s"
REGISTRY = """version: 1
repos:
  - name: Harness
    short: harness
    path: .
    remote: git@github.com:example/harness.git
    default_branch: main
    harness: "none"
    dispatch:
      verify:
        - command: "{cmd}"
          timeout_seconds: 20
      verify_timeout_seconds: 30
    notes: ""
"""


def git(root: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr}")
    return proc.stdout.strip()


@pytest.fixture(scope="session")
def _land_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Main with three files, and `feature` one commit ahead: it edits `a.txt`, adds `new.txt` and
    deletes `gone.txt`. Built once per worker and copied per test (verify speed, phase 2)."""
    root = tmp_path_factory.mktemp("land-template") / "harness"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "agent").write_bytes(b"")
    (root / ".gitignore").write_bytes(b"/agent.yaml\n.agent/\n")
    for name, text in (("a.txt", "a1\n"), ("b.txt", "b1\n"), ("gone.txt", "bye\n")):
        (root / name).write_bytes(text.encode())
    (root / "harness" / "lessons").mkdir(parents=True)
    (root / "harness" / "lessons" / "entry.md").write_bytes(b"# queue\n")
    (root / "agent.yaml").write_bytes(REGISTRY.format(cmd=PASS).encode())
    git(root, "init", "-q", "-b", "main", "--template=")
    # Git for Windows sets core.autocrlf=true system-wide, which land refuses on these paths.
    git(root, "config", "core.autocrlf", "false")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "base")
    git(root, "switch", "-q", "-c", "feature")
    (root / "a.txt").write_bytes(b"a2\n")
    (root / "new.txt").write_bytes(b"new\n")
    (root / "gone.txt").unlink()
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "feature work")
    git(root, "switch", "-q", "main")
    # Where `recorded` keeps records. Under xdist every worker's base temp sits in one session
    # directory, so workers share them.
    base = tmp_path_factory.getbasetemp()
    _KEEP[:] = [(base.parent if os.environ.get("PYTEST_XDIST_WORKER") else base) / "land-records"]
    return root


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _land_template: Path) -> Path:
    """A copy of `_land_template`: main, and `feature` one commit ahead."""
    root = tmp_path / "harness"
    shutil.copytree(_land_template, root, symlinks=True)
    monkeypatch.setenv("AGENT_ROOT", str(root))
    return root


#: Where records already run in this session are kept, by (tree, environment digest, policy
#: digest): the session's directory that outlives each test's own, shared by xdist workers.
_KEEP: list[Path] = []


def recorded(root: Path, rev: str = "feature") -> None:
    """A pass record for `rev` up front, so a landing reuses it. Records are keyed by tree,
    environment and policy, so the first run per key in a session is real and later checkouts of
    the same tree get a copy of that record and its run, which must still apply. Which test pays
    for the real run depends on scheduling, so the spawn ceiling leaves both paths out."""
    with suite_cost.uncounted():
        _recorded(root, rev)


def _recorded(root: Path, rev: str) -> None:
    rp = verify_record.harness_repo(root)
    _, tree = verify_record.resolve(root, rev)
    key = (tree, verify_record.environment(rp)["digest"], verify_record.policy(rp)[0])
    store = verify_record.store(root)
    saved = _KEEP[0] / hashlib.sha256("\0".join(key).encode()).hexdigest()[:24] if _KEEP else None
    if saved is not None and saved.is_dir():
        shutil.copytree(saved, store, symlinks=True, dirs_exist_ok=True)
        found, why = verify_record.lookup(root, rev)
        assert found is not None, why
        return
    made = verify_record.record(root, rev)
    if saved is not None:
        staging = Path(tempfile.mkdtemp(dir=saved.parent.parent))
        for part in (Path("v1") / tree, Path("runs") / made["run_id"]):
            shutil.copytree(store / part, staging / part, symlinks=True)
        saved.parent.mkdir(parents=True, exist_ok=True)
        try:
            staging.rename(saved)  # another worker that got there first wins
        except OSError:
            shutil.rmtree(staging, ignore_errors=True)


@pytest.fixture
def certified(monkeypatch: pytest.MonkeyPatch) -> None:
    """In-process landings take any candidate as certified, for tests of what land does to main and
    the checkout rather than of the verifier. Recovery still looks records up for real."""
    from agent_cli import land

    monkeypatch.setattr(
        land, "_certificate", lambda root, cand, **_: {"run_id": "certified-in-test", "reused": True}
    )


def child(root: Path, seam: str = "", *args: str) -> subprocess.Popen[str]:
    """`agent land feature` in a child process; `seam` is `<point>:exit` or `<point>:pause`."""
    code = (
        "import sys; from pathlib import Path; from agent_cli import land\n"
        "try:\n"
        f"    land.land(Path({str(root)!r}), 'feature', strategy={(args[0] if args else 'ff')!r})\n"
        "except Exception as exc:\n"
        "    print(exc, file=sys.stderr); sys.exit(3)\n"
    )
    env = {**os.environ, "AGENT_LAND_SEAM": seam} if seam else dict(os.environ)
    return subprocess.Popen(
        [sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )


def paused(root: Path, proc: subprocess.Popen[str], timeout: float = 30) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        flags = list(jn.land_dir(root).glob("*.paused"))
        if flags:
            return flags[0].stem
        if proc.poll() is not None:
            raise AssertionError(f"land exited before pausing: {proc.communicate()}")
        time.sleep(0.02)
    raise AssertionError("land never reached its pause point")


def resume(root: Path, ident: str) -> None:
    (jn.land_dir(root) / f"{ident}.resume").write_text("")


def only_journal(root: Path) -> dict[str, Any]:
    [found] = jn.every(root)
    return found


def fingerprint(root: Path) -> str:
    """Every file in the checkout and its .git, by path and bytes (land's own journal dir aside)."""
    h = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file() or p.is_symlink()):
        rel = path.relative_to(root).as_posix()
        if rel.startswith((".agent/", ".git/logs/")) or rel.endswith(".pyc"):
            continue
        h.update(rel.encode())
        h.update(os.readlink(path).encode() if path.is_symlink() else path.read_bytes())
    return h.hexdigest()
