"""The isolated harness root every test runs against: the fixed clock, the tiny registry, and
the `tmp_root` fixture that hands out a copy of it.

Split out of `conftest.py`: the session-template rewrite pushed that file past
the 500-line limit `test_file_sizes.py` enforces. `conftest.py` imports the fixtures from here,
which is what registers them for the whole suite.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

FIXED_NOW = "2026-09-03T14:05:00-07:00"
MACHINE = "mac"

REGISTRY = """version: 1
repos:
  - name: alpha-api
    short: alpha
    path: repos/alpha-api
    remote: {alpha_remote}
    default_branch: main
    worktrees_dir: repos/_worktrees/alpha
    harness: "none"
    dispatch: {{model: null, permission_mode: acceptEdits, max_budget_usd: 1, extra_args: []}}
    notes: ""
  - name: beta-web
    short: beta
    path: repos/beta-web
    remote: git@github.com:example/beta-web.git
    default_branch: main
    worktrees_dir: null
    harness: "none"
    dispatch: {{}}
    notes: ""
"""


def git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout


def _build_root(base: Path) -> None:
    """Write the harness root and its two git repos under `base`.

    Called once per session by `_root_template`; `tmp_root` copies the result. Every path it
    writes is under `base`, so the copy only has to repoint the two files that record an
    absolute path (see `_REPOINTED`).
    """
    root = base / "harness"
    root.mkdir()
    # `bin/agent` is what makes a directory the harness root (core.ROOT_MARKER, ADR-0012).
    # An empty file is enough: nothing in the suite executes the shim through find_root.
    (root / "bin").mkdir()
    (root / "bin" / "agent").write_text("")
    # AGENTS.md is the one always-loaded file since ADR-0024 deleted CLAUDE.md, and doctor
    # fails without it. Every clone has one, so the fixture root does too.
    (root / "AGENTS.md").write_text("# how this repo works\n", encoding="utf-8")
    bare = base / "alpha-remote.git"
    bare.mkdir()
    git(["init", "--bare", "-q", "--initial-branch=main", "--template=", str(bare)], base)
    _no_auto_maintenance(bare / "config")
    (root / "agent.yaml").write_text(REGISTRY.format(alpha_remote=str(bare)), encoding="utf-8")
    # This machine's dispatch label (ADR-0036): `dispatch new` refuses without one, so every
    # fixture machine is `mac` and its new ids read `NNN-mac-SS`.
    (root / "agent.local.yaml").write_text(f"machine: {MACHINE}\n", encoding="utf-8")
    # Doctor opens the configured Codex MCP server, so this fixture needs a runnable entry
    # point from the selected environment. --root still directs every call into this fixture.
    if os.name == "nt":
        _windows_fixture_venv(root / ".venv")
    else:
        _copy_posix_agent(root)
    # the Dashboard's view scripts are tracked harness source every clone carries; doctor fails
    # without them
    views = Path(__file__).resolve().parents[1] / "harness" / "obsidian" / "views"
    shutil.copytree(views, root / "harness" / "obsidian" / "views")
    alpha = root / "repos" / "alpha-api"
    alpha.mkdir(parents=True)
    git(["init", "-q", "--initial-branch=main", "--template="], alpha)
    _no_auto_maintenance(alpha / ".git" / "config")
    git(["config", "user.email", "owner@example.com"], alpha)
    git(["config", "user.name", "Owner"], alpha)
    (alpha / "README.md").write_text("alpha\n", encoding="utf-8")
    git(["add", "."], alpha)
    env = {**os.environ, "GIT_AUTHOR_DATE": FIXED_NOW, "GIT_COMMITTER_DATE": FIXED_NOW}
    subprocess.run(["git", "commit", "-q", "-m", "T-1 initial"], cwd=alpha, env=env, check=True)
    git(["remote", "add", "origin", str(bare)], alpha)
    # receive-pack would start a detached auto-maintenance in the bare repo after the push. Its
    # objects/maintenance.lock then came and went while a test copied the template, and
    # copytree failed on the vanished file. The -c values reach
    # receive-pack through GIT_CONFIG_PARAMETERS, so no extra subprocess is needed.
    no_maintenance = ["-c", "receive.autogc=false", "-c", "maintenance.auto=false", "-c", "gc.auto=0"]
    git([*no_maintenance, "push", "-q", "origin", "main"], alpha)


def _copy_posix_agent(root: Path) -> None:
    """`.venv/bin/agent`, whose shebang names this checkout's venv python."""
    fixture_agent = root / ".venv" / "bin" / "agent"
    fixture_agent.parent.mkdir(parents=True)
    shutil.copy2(Path(__file__).resolve().parents[1] / ".venv" / "bin" / "agent", fixture_agent)


def _windows_fixture_venv(venv: Path) -> None:
    """`.venv\\Scripts\\python.exe`, the Windows launcher since then: the venv
    redirector, its `pyvenv.cfg`, and a `.pth` that adds this checkout's site-packages, so
    `python.exe -I -m agent_cli` imports the harness under test."""
    real = Path(__file__).resolve().parents[1] / ".venv"
    (venv / "Scripts").mkdir(parents=True)
    shutil.copy2(real / "Scripts" / "python.exe", venv / "Scripts" / "python.exe")
    shutil.copy2(real / "pyvenv.cfg", venv / "pyvenv.cfg")
    site = venv / "Lib" / "site-packages"
    site.mkdir(parents=True)
    target = str(real / "Lib" / "site-packages")
    (site / "_harness_under_test.pth").write_text(
        f"import site; site.addsitedir({target!r})\n", encoding="utf-8"
    )


def _no_auto_maintenance(config: Path) -> None:
    """Stop a fixture repo from ever starting detached auto-maintenance, whatever runs in it.

    The per-push `-c` flags above were not enough: A CI run failed the same way,
    on the same vanished `alpha-remote.git/objects/maintenance.lock`. Set in the repo's own
    config right after `git init`, before any command can start maintenance. Appended as text,
    so it costs no extra `git` subprocess."""
    with config.open("a", encoding="utf-8") as fh:
        fh.write("[maintenance]\n\tauto = false\n[gc]\n\tauto = 0\n[receive]\n\tautogc = false\n")


# The only two files `_build_root` leaves holding an absolute path into the template: the
# registry's `remote:` and alpha's `origin`. A copy rewrites both to its own location, so no
# two tests share a remote — `repos sync --pull` fetches and fast-forwards against it.
_REPOINTED = ("harness/agent.yaml", "harness/repos/alpha-api/.git/config")


def _forms(path: str) -> list[str]:
    """How a path appears in text: as is, JSON- or git-config-escaped (backslashes doubled, as a
    Windows path is), and with forward slashes ."""
    return [path, json.dumps(path)[1:-1], path.replace("\\", "/")]


def _pairs(old: Path, new: Path) -> dict[str, str]:
    """Each written form of `old` (and of its resolved path), mapped to the same form of `new`."""
    return {
        o: n
        for raw in {str(old), str(old.resolve())}
        for o, n in zip(_forms(raw), _forms(str(new)), strict=True)
    }


def swap(text: str, old: Path, new: Path) -> str:
    """`text` with every written form of `old` replaced by `new`'s, longest first."""
    pairs = _pairs(old, new)
    for o in sorted(pairs, key=len, reverse=True):
        text = text.replace(o, pairs[o])
    return text


def repoint_files(base: Path, template: Path, relatives: tuple[str, ...]) -> None:
    """Rewrite each copied file under `base` from `template`'s path to `base`'s.

    A plain replace missed Windows git configs, which store `C:\\\\Users\\\\...`: the copied
    clone kept fetching the template's remote while the registry named its own. A copy that still names its template fails here instead of sharing a remote."""
    olds = _pairs(template, base)
    for relative in relatives:
        path = base / relative
        text = swap(path.read_text(encoding="utf-8"), template, base)
        if left := [o for o in olds if o in text]:
            raise AssertionError(f"{path} still names the template after repointing: {left}")
        rewrite(path, text)


def rewrite(path: Path, text: str) -> None:
    """Replace `path`'s content. Git for Windows hides a worktree's `.git` file, and Windows
    refuses to open a hidden file for writing, so the old file goes first."""
    path.unlink()
    path.write_text(text, encoding="utf-8")


def link_notes(root: Path, notes: Path | None = None) -> Path:
    """Move `root`'s plain private paths into a notes repo (default: the sibling setup uses) and
    link them, the way setup leaves a real clone.
    Returns the notes repo."""
    from agent_cli import links as links_mod
    from agent_cli import notes as notes_mod

    notes = notes or root.parent / notes_mod.DEFAULT_NOTES_DIRNAME
    notes.mkdir(exist_ok=True)
    for entry in notes_mod.PRIVATE_SET:
        path = root / entry.path
        if path.exists() and not links_mod.is_link(path):
            (notes / entry.path).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(notes / entry.path))
    notes_mod.link(root, notes)
    notes_mod.write_notes_root(root, notes)
    return notes


@pytest.fixture(scope="session")
def _root_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The harness root, built once for the whole session.

    Building it per test cost eight `git` subprocesses (init --bare, init, two configs, add,
    commit, remote add, push) on every one of the ~800 tests that take `tmp_root`, and
    `test_ops_cases` paid it twice per test for its twin roots. A session template plus a
    copytree is the same tree for a fraction of the cost. Nothing writes to the template:
    tests get a copy.
    """
    base = tmp_path_factory.mktemp("root-template")
    _build_root(base)
    return base


@pytest.fixture
def tmp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _root_template: Path) -> Path:
    """A harness root with agent.yaml, one real git repo (alpha) and one missing (beta)."""
    from agent_cli import codex_mcp

    shutil.copytree(_root_template, tmp_path, symlinks=True, dirs_exist_ok=True)
    repoint_files(tmp_path, _root_template, _REPOINTED)
    root = tmp_path / "harness"
    monkeypatch.setenv("AGENT_ROOT", str(root))
    monkeypatch.setenv("AGENT_NOW", FIXED_NOW)
    monkeypatch.setenv("AGENT_NOTIFY", "off")
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_PID", raising=False)  # set when pytest runs under Claude Code
    # Each doctor test validates static registration. The real stdio round trip is covered in
    # test_mcp_stdio; do not start a new server for every fixture doctor call.
    monkeypatch.setattr(codex_mcp, "probe", lambda root, entry: (True, "fixture MCP probe"))
    monkeypatch.chdir(root)
    return root


NOTES_REMOTE = "notes-remote.git"


def _build_notes(base: Path) -> None:
    """A bare notes remote and a clone of it at setup's default place beside the harness, with
    one pushed commit and `main` tracking `origin/main`."""
    from agent_cli import notes as notes_mod

    remote = base / NOTES_REMOTE
    git(["init", "--bare", "-q", "--initial-branch=main", "--template=", str(remote)], base)
    _no_auto_maintenance(remote / "config")
    clone = base / notes_mod.DEFAULT_NOTES_DIRNAME
    git(["init", "-q", "--initial-branch=main", "--template=", str(clone)], base)
    _no_auto_maintenance(clone / ".git" / "config")
    git(["config", "user.email", "owner@example.com"], clone)
    git(["config", "user.name", "Owner"], clone)
    git(["remote", "add", "origin", str(remote)], clone)
    (clone / "README.md").write_text("notes\n", encoding="utf-8")
    git(["add", "README.md"], clone)
    env = {**os.environ, "GIT_AUTHOR_DATE": FIXED_NOW, "GIT_COMMITTER_DATE": FIXED_NOW}
    subprocess.run(["git", "commit", "-q", "-m", "notes"], cwd=clone, env=env, check=True)
    git(["push", "-q", "-u", "origin", "main"], clone)


@pytest.fixture(scope="session")
def _notes_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.mktemp("notes-template")
    _build_notes(base)
    return base


@pytest.fixture
def notes_remote(tmp_root: Path, _notes_template: Path) -> Path:
    """`tmp_root` with a notes repo whose remote is reachable, registered as its notes root.
    `feature new` claims its id on that remote (ADR-0040) and refuses without one,
    so every test that creates a feature through it uses this. Nothing is linked: the private
    paths stay plain, as the tests were written for. A test that wants the linked layout calls
    `link_notes(tmp_root)`, which links into this same repo. Returns the notes clone."""
    from agent_cli import notes as notes_mod

    clone = copy_notes(_notes_template, tmp_root.parent)
    notes_mod.write_notes_root(tmp_root, clone)
    return clone


def copy_notes(template: Path, base: Path) -> Path:
    """A copy of the notes template's remote and clone under `base`, the clone's `origin`
    repointed at the copied remote. Returns the clone."""
    from agent_cli import notes as notes_mod

    for name in (NOTES_REMOTE, notes_mod.DEFAULT_NOTES_DIRNAME):
        shutil.copytree(template / name, base / name, symlinks=True)
    repoint_files(base, template, (f"{notes_mod.DEFAULT_NOTES_DIRNAME}/.git/config",))
    return base / notes_mod.DEFAULT_NOTES_DIRNAME


@pytest.fixture
def plugin_builds(tmp_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Setup's plugin download, faked: every pinned build lands with its manifest at the pinned
    version, and on Windows the terminal venv's python exists. Downloads are off under the test
    clock, and doctor fails a missing plugin, so a test that expects a passing
    doctor after setup needs this."""
    from agent_cli import installs, obsidian

    def fake_install(root: Path, ids: list[str] | None = None) -> list[dict]:
        rows = []
        for pid in ids if ids is not None else obsidian.PLUGIN_SOURCES:
            _, version = obsidian.PLUGIN_SOURCES[pid]
            build = obsidian.obsidian_dir(root) / "plugins" / pid
            build.mkdir(parents=True, exist_ok=True)
            (build / "main.js").write_text("module.exports = {};\n", encoding="utf-8")
            (build / "manifest.json").write_text(
                f'{{"id": "{pid}", "version": "{version}"}}', encoding="utf-8"
            )
            rows.append({"plugin": pid, "state": "installed", "detail": version})
        return rows

    monkeypatch.setattr(obsidian, "install_plugins", fake_install)
    python = installs.terminal_python_path(tmp_root, "Windows")
    if sys.platform == "win32":
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"")
    return tmp_root
