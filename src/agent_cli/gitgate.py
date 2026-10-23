"""The git boundaries of this repo: a pre-commit gate on staged content, and a pre-push guard
(see ADR-0026). Each registered product clone gets a pre-push hook too, the code-review gate on
pushes (`push_review`, ADR-0063).

Both came out of one afternoon. Two commits landed lines longer than ruff's limit. Every check
involved already existed. They just ran after the commit: format in CI. Both committers ran
focused tests. Nothing sat at the commit itself.

The pre-commit gate therefore reads the STAGED blob of every Python file, never the working
tree. It formats and lints that blob in memory. It never writes the working tree or the index,
because this checkout is shared by several managers and a hook that reformatted or re-staged
files would carry another session's unstaged hunks into the commit.

The pre-push guard refuses every push from the owner's source checkout, marked by the local
config `agent.exportOnly=true`. Its `origin` is the public repo and its history is the
unsquashed private one, so an ordinary `git push` there would publish everything the export
exists to hold back. The only public route is `agent export`, which builds and verifies
a squash in a separate directory and stops before pushing. A public clone has no such history
and no such config, so its pre-push hook does nothing.

Local hooks can be bypassed (`--no-verify`, `-c core.hooksPath`, plumbing). This closes the
ordinary path; CI stays the authoritative backstop for format and size.
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

from .core import AgentError

# `tests/test_file_sizes.py` imports these so the two cannot drift.
# Codex stops loading instruction files once their combined size reaches
# `project_doc_max_bytes`, 32 KiB by default, silently dropping the tail. The gate sits at 95%
# of that cliff (see ADR-0024).
PROJECT_DOC_MAX_BYTES = 32 * 1024
INSTRUCTION_BUDGET = int(PROJECT_DOC_MAX_BYTES * 0.95)
INSTRUCTION_NAMES = ("AGENTS.md", "AGENTS.override.md")
CHECKED_TOPS = ("src", "tests")  # what CI formats and lints

ALLOW_PUSH_ENV = "AGENT_ALLOW_DIRECT_PUSH"
EXPORT_ONLY_KEY = "agent.exportOnly"
PUSH_REFUSAL = (
    "push refused: this checkout's history is private and its origin is the public repo.\n"
    "Publish with `bin/agent export <dir> --remote <new-repo-url> -m <message>`, review the result, then push from that directory.\n"
    f"To push from here anyway, set {ALLOW_PUSH_ENV}=1 for that one command."
)


def _git(root: Path, *args: str, safe_directory: bool = False) -> bytes:
    options = ["-c", f"safe.directory={root.resolve().as_posix()}"] if safe_directory else []
    proc = subprocess.run(["git", *options, "-C", str(root), *args], capture_output=True, check=False)
    if proc.returncode:
        raise AgentError(f"git {args[0]} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def _ruff() -> str:
    beside = Path(sys.executable).with_name("ruff.exe" if os.name == "nt" else "ruff")
    found = str(beside) if beside.exists() else shutil.which("ruff")
    if not found:
        raise AgentError("ruff not found; run `uv sync` in this checkout")
    return found


def _staged(root: Path) -> list[str]:
    out = _git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    return [p for p in out.decode().split("\0") if p]


def _checked_python(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return path.endswith(".py") and bool(parts) and parts[0] in CHECKED_TOPS


def _run_ruff(root: Path, args: list[str], blob: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([_ruff(), *args], cwd=root, input=blob, capture_output=True, check=False)


def check_python(root: Path, path: str, blob: bytes) -> list[str]:
    """Problems with one staged Python blob: formatting and lint. The 500-line cap is gone: files were
    split only to fit it, which grew re-export `__init__`s and forced detours on
    ordinary fixes."""
    problems: list[str] = []
    stdin = ["--stdin-filename", path, "-"]
    formatted = _run_ruff(root, ["format", *stdin], blob)
    if formatted.returncode != 0:
        detail = formatted.stderr.decode(errors="replace").strip().splitlines()
        return [f"{path}: ruff format failed: {detail[-1] if detail else 'no output'}"]
    if formatted.stdout != blob:
        problems.append(f"{path}: not formatted (run `uv run ruff format {path}`, then stage it)")
    lint = _run_ruff(root, ["check", "--quiet", *stdin], blob)
    if lint.returncode != 0:
        first = lint.stdout.decode(errors="replace").strip().splitlines()
        problems.append(f"{path}: ruff check: {first[0] if first else 'failed'}")
    return problems


def _instruction_bytes(root: Path) -> int:
    """Combined size of every instruction file as staged, which is what the commit will hold."""
    listed = _git(root, "ls-files", "-z").decode().split("\0")
    total = 0
    for path in listed:
        parts = PurePosixPath(path).parts
        if parts and parts[-1] in INSTRUCTION_NAMES and "vendor" not in parts:
            total += int(_git(root, "cat-file", "-s", f":{path}").strip() or 0)
    return total


def pre_commit(root: Path) -> list[str]:
    """Every reason the staged content should not be committed. Empty means commit."""
    staged = _staged(root)
    problems: list[str] = []
    for path in staged:
        if _checked_python(path):
            problems += check_python(root, path, _git(root, "show", f":{path}"))
    if any(PurePosixPath(p).name in INSTRUCTION_NAMES for p in staged):
        total = _instruction_bytes(root)
        if total > INSTRUCTION_BUDGET:
            problems.append(
                f"instruction files total {total:,} bytes, budget {INSTRUCTION_BUDGET:,}: Codex "
                "silently drops what falls past its limit (ADR-0024)"
            )
    return problems


def export_only_setting(root: Path, *, safe_directory: bool = False) -> bool | None:
    """`agent.exportOnly` as set, or None when unset. It is local git config, set once on the
    owner's source checkout, because the tracked hook is the same in every clone: a clone of
    the public repo holds only the squash and pushes to its own fork freely."""
    proc = subprocess.run(
        [
            "git",
            *(["-c", f"safe.directory={root.resolve().as_posix()}"] if safe_directory else []),
            "-C",
            str(root),
            "config",
            "--bool",
            EXPORT_ONLY_KEY,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    value = proc.stdout.strip()
    return None if not value else value == "true"


def export_only(root: Path) -> bool:
    return export_only_setting(root) is True


def settle_export_only(root: Path) -> tuple[str, str]:
    """Settle `agent.exportOnly` so a clone fails closed, and never change a value already set.

    A tracked `.harness-export` marker (`export.EXPORT_MARKER`) means a clone of the public
    export: set false. `agent export` writes the marker into every squash and the source tree
    never carries it. With no marker at HEAD and harness role `source`, the checkout may hold
    private history: set true, so a push is refused and the export route is the only way out.
    Only a readable consumer role is left unset; its pushes are refused elsewhere. A missing or
    unparseable `agent.local.yaml` reads as `source`, so it fails closed too.
    Returns `(state, detail)`: `already` when set, `done` when this set it, `skipped` otherwise,
    with what the owner should set."""
    from .export import EXPORT_MARKER
    from .harness_role import SOURCE, role

    current = export_only_setting(root)
    if current is not None:
        return "already", f"{EXPORT_ONLY_KEY}={'true' if current else 'false'}"
    try:
        tracked = _git(root, "ls-tree", "--name-only", "HEAD", EXPORT_MARKER).decode().strip()
    except AgentError:
        return "skipped", "not a git checkout with a HEAD"
    if tracked != EXPORT_MARKER:
        try:
            is_source = role(root) == SOURCE
        except Exception:  # noqa: BLE001 — no readable role leaves the setting unset
            is_source = False
        if is_source:
            _git(root, "config", EXPORT_ONLY_KEY, "true")
            return "done", (
                f"{EXPORT_ONLY_KEY}=true: direct pushes are refused here; publish with `agent export`"
            )
        return "skipped", (
            f"not a public export; set it yourself: `git config {EXPORT_ONLY_KEY} true` on a checkout "
            "with private history, false otherwise"
        )
    _git(root, "config", EXPORT_ONLY_KEY, "false")
    # Not "(a clone of the public export)": next to exportOnly=false it read as a contradiction.
    # The setting means this checkout pushes directly.
    return "done", f"{EXPORT_ONLY_KEY}=false: this clone of the public repo pushes directly, no export step"


def pre_push(root: Path, env: dict[str, str] | None = None) -> list[str]:
    """Refuse the push from an export-only checkout unless allowed for this one command."""
    if not export_only(root):
        return []
    return [] if (env if env is not None else os.environ).get(ALLOW_PUSH_ENV) == "1" else [PUSH_REFUSAL]


# ------------------------------------------------------------------ install

MARKER = "# agent-githook:v1"
HOOKS = ("pre-commit", "pre-push")
# The retired standalone push hook marked its second line with one of these. It runs a subcommand
# that no longer exists, so every push from that clone fails until it is replaced. The name is
# split so the retired check's name stays out of the tree.
_RETIRED = "agent-push-" + "scr" + "ub"
LEGACY_PUSH_MARKERS = (f"# {_RETIRED}:v1 ", f"# {_RETIRED}:v2 ")


def legacy_push_hook(text: str) -> bool:
    """The retired push hook, recognised by its marker line alone."""
    lines = text.replace("\r\n", "\n").split("\n")
    return len(lines) > 1 and lines[1].startswith(LEGACY_PUSH_MARKERS)


def script(name: str) -> str:
    """The hook body. The hooks dir is shared by every worktree, but `.venv` is not, so it runs
    the worktree's own `agent` when it has one and the main checkout's otherwise, always with
    the worktree as `--root`. It fails closed when neither exists rather than skipping.

    Each venv is tried as POSIX (`.venv/bin/agent`) and Windows (`.venv/Scripts/python.exe`):
    Git for Windows runs hooks in its own sh, and a hook that knew only the POSIX path blocked
    every commit and push on the owner's first Windows clone.
    Windows runs `python.exe -I -m agent_cli`, not the `agent.exe` shim: a pre-push full pass
    held the shim for minutes, and `uv sync` cannot replace it while any process runs it.
    An installed hook of the earlier body reads as stale and setup rewrites it."""
    args = ' "$@"' if name == "pre-push" else ""
    return (
        f"#!/bin/sh\n{MARKER} {name}\n"
        "root=$(git rev-parse --show-toplevel) || exit 2\n"
        "common=$(git rev-parse --path-format=absolute --git-common-dir) || exit 2\n"
        'main=$(dirname "$common")\n'
        'for agent in "$root/.venv/bin/agent" "$root/.venv/Scripts/python.exe" '
        '"$main/.venv/bin/agent" "$main/.venv/Scripts/python.exe"; do\n'
        '  [ -x "$agent" ] && break\n'
        "done\n"
        'if [ ! -x "$agent" ]; then\n'
        f"  printf '%s\\n' '{name} gate unavailable: run uv sync in this checkout' >&2\n"
        "  exit 2\n"
        "fi\n"
        "module=\n"
        'case "$agent" in *.exe) module="-I -m agent_cli" ;; esac\n'
        f'exec "$agent" $module --root "$root" git {name}{args}\n'
    )


def hooks_dir(root: Path, *, safe_directory: bool = False) -> Path:
    """Where git reads hooks for this checkout, `core.hooksPath` included. Hooks live in the
    common git dir, so a manager worktree (see ADR-0023) uses the same ones as the main
    checkout."""
    rel = _git(root, "rev-parse", "--git-path", "hooks", safe_directory=safe_directory).decode().strip()
    return (root / rel).resolve()


def inspect(root: Path, *, safe_directory: bool = False) -> dict[str, str]:
    """`installed`, `stale` (ours, out of date), `missing`, `legacy` (the retired push hook,
    replaced on install), or `foreign` (someone else's)."""
    out: dict[str, str] = {}
    for name in HOOKS:
        path = hooks_dir(root, safe_directory=safe_directory) / name
        if not path.exists():
            out[name] = "missing"
            continue
        # newline="" keeps a CR, so a hook an older build wrote with CRLF reads as ours but
        # stale and the next install rewrites it with LF (fresh Windows clone).
        text = path.read_text(encoding="utf-8", errors="replace", newline="")
        if name == "pre-push" and not path.is_symlink() and legacy_push_hook(text):
            out[name] = "legacy"
        elif not text.replace("\r\n", "\n").startswith(f"#!/bin/sh\n{MARKER}"):
            out[name] = "foreign"
        else:
            # git skips a hook without the executable bit, silently.
            active = text == script(name) and os.access(path, os.X_OK)
            out[name] = "installed" if active else "stale"
    return out


def install(root: Path) -> dict[str, str]:
    """Write both hooks. Ours are replaced when stale, and so is the retired push hook; a hook
    someone else wrote is left alone and reported, because it may be doing work this repo does not
    know about."""
    directory = hooks_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    result: dict[str, str] = {}
    for name, state in inspect(root).items():
        if state == "foreign":
            result[name] = "foreign hook present, left in place"
            continue
        if state == "installed":
            result[name] = "already installed"
            continue
        path = directory / name
        if state == "legacy":
            path.unlink()
        # LF on every OS. write_text's default newline translation wrote CRLF on Windows: doctor
        # then took the gate's own pre-push hook for a foreign one, and sh may choke on `do\r`.
        path.write_text(script(name), encoding="utf-8", newline="\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        result[name] = {"missing": "installed", "legacy": "replaced the retired push hook"}.get(
            state, "updated"
        )
    return result


# ------------------------------------------------------------------ product clones

REVIEW_HOOK = "pre-push review"


def review_script(harness: Path) -> str:
    """The pre-push hook of a registered product clone: the harness's code-review gate on the
    exact refs git pushes (`push_review`), run by the harness's own venv against its records.
    The harness's main checkout is written in, because nothing in the clone leads back to it: a
    manager worktree that ran setup goes away, and the main checkout holds the venv `script` falls
    back to and the records. Its venv is tried as POSIX and Windows, as `script` does. It fails
    closed when neither exists. A hook naming another path reads as stale, so setup rewrites it."""
    home = shlex.quote(_main_checkout(harness).as_posix())
    return (
        f"#!/bin/sh\n{MARKER} {REVIEW_HOOK}\n"
        f"harness={home}\n"
        "root=$(git rev-parse --show-toplevel) || exit 2\n"
        'for agent in "$harness/.venv/bin/agent" "$harness/.venv/Scripts/python.exe"; do\n'
        '  [ -x "$agent" ] && break\n'
        "done\n"
        'if [ ! -x "$agent" ]; then\n'
        "  printf '%s\\n' \"pre-push code-review gate unavailable: run uv sync in $harness\" >&2\n"
        "  exit 2\n"
        "fi\n"
        "module=\n"
        'case "$agent" in *.exe) module="-I -m agent_cli" ;; esac\n'
        'exec "$agent" $module --root "$harness" git push-review "$root" "$@"\n'
    )


def _main_checkout(harness: Path) -> Path:
    """`worktree.main_checkout`, as `verify_record.main_of` finds it; `harness` itself when it is
    not a git checkout, whatever repository holds it."""
    from .worktree import main_checkout

    if not (harness / ".git").exists():
        return harness.resolve()
    try:
        return main_checkout(harness).resolve()
    except Exception:  # noqa: BLE001 — a harness git cannot place is its own main checkout
        return harness.resolve()


def _clone_dirs(checkout: Path) -> tuple[Path, Path | None]:
    """The clone's git common dir and hooks dir, both absolute. The hooks dir is None when it is
    not one place: git reads a relative `core.hooksPath` from each worktree's own top, so a linked
    worker would not run a hook found under this checkout."""
    out = _git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir", "--git-path", "hooks")
    common, hooks = out.decode().strip().splitlines()
    configured = subprocess.run(
        ["git", "-C", str(checkout), "config", "--get", "core.hooksPath"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if configured and not Path(os.path.expanduser(configured)).is_absolute():
        return Path(common).resolve(), None
    return Path(common).resolve(), Path(hooks).resolve()


def common_dir(checkout: Path) -> Path:
    """The checkout's git common dir: one repository's, shared by its linked worktrees."""
    return _clone_dirs(checkout)[0]


def inspect_review(checkout: Path, harness: Path) -> str:
    """The product clone's pre-push hook: `installed`, `stale` (ours, out of date), `missing`, or
    `foreign` (someone else's, a harness's full pre-push, or a `core.hooksPath` outside the git
    dir or relative, which the repo manages itself)."""
    return _review_state(*_clone_dirs(checkout), harness)


def _review_state(common: Path, hooks: Path | None, harness: Path) -> str:
    if hooks is None or not hooks.is_relative_to(common):
        return "foreign"
    path = hooks / "pre-push"
    if not path.exists():
        return "missing"
    text = path.read_text(encoding="utf-8", errors="replace", newline="")
    # Only the review hook is this install's: another harness clone's full pre-push carries the
    # same marker and its own export-only and pass-record gates.
    if text.replace("\r\n", "\n").split("\n")[:2] != ["#!/bin/sh", f"{MARKER} {REVIEW_HOOK}"]:
        return "foreign"
    return "installed" if text == review_script(harness) and os.access(path, os.X_OK) else "stale"


def install_review(root: Path) -> dict[str, str]:
    """Write the pre-push code-review hook into every registered product clone; a worker's
    worktree shares it through its clone's git dir. The harness's own checkout has `install`'s
    hook. A hook someone else wrote is left in place and reported. Keyed by repo short."""
    from . import repos as repos_mod

    own = common_dir(root) if (root / ".git").exists() else None
    result: dict[str, str] = {}
    for repo in repos_mod.load_registry(root).repos:
        checkout = repo.abs_path(root)
        if not (checkout / ".git").exists():
            result[repo.short] = "not cloned here"
            continue
        try:
            common, hooks = _clone_dirs(checkout)
            if common == own:
                continue
            result[repo.short] = _install_review(common, hooks, root)
        except (AgentError, OSError, ValueError) as exc:
            result[repo.short] = f"failed: {exc}"
    return result


def _install_review(common: Path, hooks: Path | None, harness: Path) -> str:
    state = _review_state(common, hooks, harness)
    if state == "foreign" or hooks is None:
        return "foreign hook present, left in place"
    if state == "installed":
        return "already installed"
    hooks.mkdir(parents=True, exist_ok=True)
    path = hooks / "pre-push"
    path.write_text(review_script(harness), encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return "installed" if state == "missing" else "updated"


def describe(data: dict[str, Any]) -> str:
    return "\n".join(f"{name}: {state}" for name, state in data.items())
