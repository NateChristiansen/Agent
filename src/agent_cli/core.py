"""Shared plumbing: root discovery, clock, atomic writes, output helpers, errors."""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Generator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePath
from typing import Any

_STRICT_READS: contextvars.ContextVar[bool] = contextvars.ContextVar("strict_reads", default=False)


@contextlib.contextmanager
def strict_reads() -> Generator[None]:
    """Within this block the record readers that skip a malformed file (`sessions.all_records`,
    `dispatch.load_all`) raise instead. For a reader that must know its view is complete: the
    tab fallback's derivation, which paints the `?` form rather than a partial view. Ordinary
    snapshot writes stay lenient, so one corrupt unrelated
    record never stops the snapshot."""
    token = _STRICT_READS.set(True)
    try:
        yield
    finally:
        _STRICT_READS.reset(token)


def reads_strict() -> bool:
    return _STRICT_READS.get()


def json_files(base: Path) -> list[Path]:
    """The `*.json` files directly in `base`, sorted. `Path.glob` hides an error listing the
    folder (no permission, say) and yields nothing; under `strict_reads` the folder is listed
    with `iterdir`, which raises, so a strict reader never takes an unlisted folder for an empty
    one."""
    if reads_strict():
        return sorted(p for p in base.iterdir() if p.suffix == ".json")
    return sorted(base.glob("*.json"))


# What makes a directory the harness root. `bin/agent` and not `agent.yaml` since ADR-0012:
# the registry is the owner's, lives in the notes repo, and is a symlink (or absent entirely)
# in a fresh clone. `bin/agent` is the harness's own and is always there.
ROOT_MARKER = "bin/agent"

# The CLI-owned subtrees of `.agent/`, directories first, then the single files. One registry:
# `hooks.PROTECTED_PATH_RE` (Bash writes), `hooks.NATIVE_GENERATED_DENY` (the `Edit(...)` rules
# Claude Code consults for Edit/Write/MultiEdit/NotebookEdit) and `doctor`'s check that
# `.claude/settings.json` still carries them are all derived from this, instead of three hand-
# maintained spellings that had to agree. `.agent/scratch/` is deliberately absent:
# it is the one corner of `.agent/` agents write to (plan drafts, work orders, journal drafts). Adding a name
# here protects it in all three places at once.
AGENT_SUBTREE_DIRS = (
    "dispatches",
    "journal",
    "leases",
    # Peer inboxes. `peers.send` appends rather than rewriting because two
    # senders race; an agent editing an inbox by hand would drop whichever message it had not
    # read. Missing here until 033, so the one store three managers coordinate through was the
    # only part of `.agent/` an agent could edit freely.
    "peers",
    # The cross-device receiver's SQLite store. A hand edit could forge an
    # acceptance or erase the dedupe record a retry depends on.
    "peer-net",
    # Plan-review snapshots and records. `plan-gate` lets a plan reach the owner on
    # these; a hand-written record must not be able to stand in for a review.
    "plan-reviews",
    "raw",
    "sessions",
    # Approval bindings belong outside dispatch records, but keep their file protection.
    "work-order-previews",
)
AGENT_SUBTREE_FILES = ("status.json", "live.json", "default-feature")
AGENT_SUBTREES = (*AGENT_SUBTREE_DIRS, *AGENT_SUBTREE_FILES)

#: The state folder's name, spelled here only. `tests/test_prose_rules.py` refuses the literal,
#: and this name, anywhere else in `src/agent_cli/`; other modules build paths with `agent_path`,
#: `agent_parts` or `agent_root`, and use `agent_dir_name()` for ignore lines, regexes and
#: messages.
AGENT_DIR = ".agent"
#: Register every runtime entry as protected or deliberately unprotected. Unprotected entries use
#: ordinary client prompts; scratch is writable draft space.
AGENT_UNREGISTERED = frozenset({
    "scratch",
    "browser", "dispatch-board", "dispatch-notices", "install.json", "keep.json", "land",
    "live-failed", "locks", "mcp", "mcp-servers", "notes-linked.json", "notes-root",
    "render-failure.json", "tabs", "tabs-off", "telemetry", "terminal-python", "usage", "verify",
})  # fmt: skip
AGENT_NAMES = frozenset(AGENT_SUBTREES) | AGENT_UNREGISTERED


_DRIVE = re.compile(r"^[A-Za-z]:")


def agent_parts(subtree: str, *parts: str) -> tuple[str, ...]:
    """`(".agent", subtree, *parts)`, for code that opens a path one component at a time.

    Raises `ValueError` for a subtree in neither `AGENT_SUBTREES` nor `AGENT_UNREGISTERED` (a new
    `.agent/` entry must be registered, or listed as deliberately unregistered, before any code
    can build a path into it), and for any component that could leave the subtree it names:
    empty, `.` or `..`, a path separator, a drive, or a NUL. A deeper path is several parts."""
    if subtree not in AGENT_NAMES:
        raise ValueError(
            f"`{AGENT_DIR}/{subtree}` is not registered: add it to core.AGENT_SUBTREE_DIRS/FILES "
            "(protected) or core.AGENT_UNREGISTERED (with the reason)"
        )
    for part in parts:
        if not part or part in (".", "..") or any(c in part for c in "/\\\0") or _DRIVE.match(part):
            raise ValueError(f"{part!r} is not one path component inside `{AGENT_DIR}/{subtree}`")
    return (AGENT_DIR, subtree, *parts)


def agent_path[P: PurePath](root: P, subtree: str, *parts: str) -> P:
    """`root/.agent/<subtree>/<parts>`, the one way `src/` builds a path into `.agent/`. Raises
    `ValueError` for an unregistered subtree or a component that leaves it (`agent_parts`).
    `root` may be `Path()` for a path relative to the vault root."""
    return root.joinpath(*agent_parts(subtree, *parts))


class _AgentRoot(Path):
    """`root/.agent` itself. Appending to it (`/`, `joinpath`) goes through `agent_path`, so
    `agent_root(root) / "x"` raises for an unregistered `x` exactly as `agent_path(root, "x")`
    does. Every other derived path (`parent`, `resolve()`, a listing) is a plain `Path`."""

    def with_segments(self, *pathsegments: str | os.PathLike[str]) -> Path:
        return Path(*pathsegments)

    def __truediv__(self, key: str | os.PathLike[str]) -> Path:
        return self.joinpath(key)

    def joinpath(self, *pathsegments: str | os.PathLike[str]) -> Path:
        parts = [p for segment in pathsegments for p in PurePath(segment).parts]
        if not parts:
            return Path(self)
        return agent_path(Path(self).parent, parts[0], *parts[1:])


def agent_root(root: Path) -> Path:
    """`root/.agent` itself, for code that reads, copies or links the whole folder. What is
    appended to it is checked as `agent_path` checks it (`_AgentRoot`). Kept over a private
    helper because three modules need the folder itself (export, worktree linking, the hook
    probe), and a checked `/` is simpler than three narrower functions."""
    return _AgentRoot(root, AGENT_DIR)


def agent_dir_name() -> str:
    """The folder's bare name, for ignore lines, regexes and messages. Never a path: build one
    with `agent_path` (the ban in `tests/test_prose_rules.py` refuses this in a path expression)."""
    return AGENT_DIR


def agent_display(subtree: str, *parts: str) -> str:
    """`.agent/<subtree>/<parts>` as text, for a message a person or model reads. Checked as
    `agent_path` checks a path."""
    return "/".join(agent_parts(subtree, *parts))


# Paths shared by the view writers and cleanup. Only these outputs are regenerable;
# the protected state registry above also includes durable records.
GENERATED_VIEWS = {
    "status": Path("STATUS.md"),  # retired output; keep cleanup and protection for old clones
    "adrs": Path("adrs/README.md"),
    "dashboard": Path("Dashboard.md"),
    "index": Path("Index.md"),
    **{name: Path("bases") / f"{name}.base" for name in ("features", "journal", "handoffs", "docs")},
    "adrs_base": Path("bases/adrs.base"),
}
REGENERABLE_STATE = frozenset(
    {
        agent_path(Path(), "status.json"),
        agent_path(Path(), "live.json"),
        agent_path(Path(), "tabs", "metrics.jsonl"),
        agent_path(Path(), "tabs", "plugin-metrics.jsonl"),
    }
)


def agent_deny_patterns(
    dirs: tuple[str, ...] = AGENT_SUBTREE_DIRS, files: tuple[str, ...] = AGENT_SUBTREE_FILES
) -> tuple[str, ...]:
    """The `Edit(...)` deny rules for `AGENT_SUBTREES`: a `/**` glob per directory, the bare
    path per file. Claude Code consults an `Edit(pattern)` rule for Write, MultiEdit and
    NotebookEdit too, so these cover every native write into `.agent/`."""
    return (*(f"Edit(/.agent/{name}/**)" for name in dirs), *(f"Edit(/.agent/{name})" for name in files))


def agent_path_pattern(
    dirs: tuple[str, ...] = AGENT_SUBTREE_DIRS, files: tuple[str, ...] = AGENT_SUBTREE_FILES
) -> str:
    """The `.agent` branch of `hooks.PROTECTED_PATH_RE`, from the same registry: `.agent`
    itself, `.agent/`, each owned file, and anything under an owned directory. Nothing else.
    `.agent/scratch/...` is agents' own, and any subtree not named in the registry falls
    through to Claude Code's ordinary permission prompt."""
    named = "|".join(re.escape(name) for name in dirs)
    owned_files = "|".join(re.escape(name) for name in files)
    return rf"\.agent(?:/(?:{named})(?:/[^\s]*)?|/(?:{owned_files})|/)?"


EXIT_OK = 0
EXIT_NOT_FOUND = 1
EXIT_USAGE = 2
EXIT_EXTERNAL = 3


class AgentError(Exception):
    """A user-facing error with an exit code. The CLI prints it and exits."""

    def __init__(self, message: str, code: int = EXIT_USAGE):
        super().__init__(message)
        self.message = message
        self.code = code


class NotFound(AgentError):
    def __init__(self, message: str):
        super().__init__(message, EXIT_NOT_FOUND)


class ExternalFailure(AgentError):
    def __init__(self, message: str):
        super().__init__(message, EXIT_EXTERNAL)


def find_root(explicit: str | None = None) -> Path:
    """Locate the harness root.

    Order: explicit --root, $AGENT_ROOT, then walk up from cwd to the first
    directory containing `bin/agent`.
    """
    if explicit:
        root = Path(explicit).expanduser().resolve()
        if not (root / ROOT_MARKER).exists():
            raise AgentError(f"--root {root} has no {ROOT_MARKER}")
        return root
    env = os.environ.get("AGENT_ROOT")
    if env:
        root = Path(env).expanduser().resolve()
        if not (root / ROOT_MARKER).exists():
            raise AgentError(f"AGENT_ROOT={root} has no {ROOT_MARKER}")
        return root
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / ROOT_MARKER).exists():
            return candidate
    raise AgentError(
        f"not inside an agent harness (no {ROOT_MARKER} found walking up from {here}); "
        "set AGENT_ROOT or pass --root"
    )


def is_alive(pid: int | None) -> bool:
    """Whether a process with this pid exists (`os.kill(pid, 0)`), the one liveness check
    leases, session records, and dispatch supervisor/worker pids all read through. False for
    `None`, `0`, or a pid that's gone (`ProcessLookupError`). True for a pid we cannot signal
    (`PermissionError`: it exists, just isn't ours).

    Windows asks the kernel instead (`winproc.pid_alive`): there signal 0 is `CTRL_C_EVENT`,
    so `os.kill(pid, 0)` interrupts a console process group rather than testing a pid."""
    if not pid:
        return False
    if sys.platform == "win32":
        from .winproc import pid_alive

        try:
            return pid_alive(int(pid))
        except TypeError, ValueError, OverflowError:
            return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except TypeError, ValueError:
        return False
    return True


def now() -> datetime:
    """Local-time clock. AGENT_NOW (ISO-8601) overrides it for tests."""
    override = os.environ.get("AGENT_NOW")
    if override:
        parsed = datetime.fromisoformat(override)
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed
    return datetime.now().astimezone()


def today() -> date:
    return now().date()


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def stamp(dt: datetime) -> str:
    """The header timestamp used in log.md: YYYY-MM-DD HH:MM."""
    return dt.strftime("%Y-%m-%d %H:%M")


#: How long `replace` keeps retrying a Windows rename that another process's open handle blocks.
REPLACE_RETRY_SECONDS = 2.0


def replace(src: str | Path, dst: str | Path) -> None:
    """`os.replace`, retried on Windows while the destination is open elsewhere.

    Windows refuses to rename over a file another process holds open without delete sharing
    (`PermissionError`, WinError 5), and a reader such as a manager's `dispatch show` holds the
    record for milliseconds. A supervisor whose final record write hits that refusal ends without
    its terminal state. POSIX renames never fail this way, so there it is one plain `os.replace`.
    A refusal that outlasts `REPLACE_RETRY_SECONDS` still raises."""
    if sys.platform != "win32":
        os.replace(src, dst)
        return
    deadline = time.monotonic() + REPLACE_RETRY_SECONDS
    delay = 0.01
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            left = deadline - time.monotonic()
            if left <= 0:
                raise
            time.sleep(min(delay, left))
            delay = min(delay * 2, 0.2)


def atomic_write(path: Path, text: str, *, newline: str | None = None) -> None:
    """Write text to path via a temp file + rename in the same directory.

    A symlink is written through: the rename lands on the target, so a generated file that
    `agent setup --notes <path>` linked into the vault stays a link (see ADR-0012).

    A hard link (the plain-Windows fallback, `links.make`) is kept too: the new file is linked
    under each other name and renamed over it first: the notes copy, then the other clones'
    copies, then `path`. Each name still flips by one rename to a fully written file, so no
    reader sees a torn one; a crash between the renames leaves whole copies that `notes.status`
    reports diverged. A file with a hard link this harness did not make raises instead of
    severing it."""
    if path.is_symlink():
        path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    others = _hard_link_names(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    sides: list[Path] = []
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)  # mkstemp defaults to 0600. These are ordinary vault files
        for other in others:
            side = other.with_name(f".{other.name}.{os.urandom(6).hex()}")
            os.link(tmp, side)
            sides.append(side)
            replace(side, other)
        replace(tmp, path)
    except BaseException:
        for leftover in (tmp, *sides):
            try:
                os.unlink(leftover)
            except FileNotFoundError:
                pass
        raise


def _hard_link_names(path: Path) -> list[Path]:
    """The other names of a hard-linked `path`, which `atomic_write` must move along with it."""
    try:
        count = os.stat(path).st_nlink
    except FileNotFoundError:
        return []
    if count < 2:
        return []
    from .notes import linked_copies

    others = linked_copies(path)
    if len(others) != count - 1:
        raise AgentError(
            f"{path} has {count - 1} other hard link(s) and only {len(others)} are the notes copy "
            "or another clone's; writing it would sever the rest"
        )
    return others


def append_line(path: Path, text: str, mode: int = 0o644) -> None:
    """Append text with O_APPEND so concurrent hooks never interleave partial writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, mode)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


def write_if_changed(path: Path, text: str) -> bool:
    """atomic_write only when the content differs, so a render that produces the same output
    never churns a generated file in git. Returns True if written."""
    try:
        if path.exists() and path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    atomic_write(path, text)
    return True


def coerce_date(value: Any) -> Any:
    """Frontmatter dates arrive as date objects from YAML, but a hand edit or a plugin can
    leave a string. Accept ISO strings; leave anything else for the validator to report."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return value
    return value


@dataclass
class Output:
    """Routes results to stdout as JSON or human text depending on --json."""

    as_json: bool = False

    def emit(self, data: Any, text: str | None = None) -> None:
        if self.as_json:
            sys.stdout.write(json.dumps(data, indent=2, default=json_default) + "\n")
        elif text is not None:
            if text and not text.endswith("\n"):
                text += "\n"
            sys.stdout.write(text)
        elif isinstance(data, str):
            sys.stdout.write(data + ("\n" if not data.endswith("\n") else ""))
        else:
            sys.stdout.write(json.dumps(data, indent=2, default=json_default) + "\n")

    def done(self, op: Any) -> int:
        """Emit an ops.Op (data, text, code) and hand back its exit code."""
        self.emit(op.data, op.text)
        return op.code


def json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def table(rows: list[list[str]], headers: list[str]) -> str:
    """Render a plain aligned text table (no markdown) for human output."""
    if not rows:
        return "(none)"
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join("{:<" + str(w) + "}" for w in widths)
    lines = [fmt.format(*headers), fmt.format(*["-" * w for w in widths])]
    lines += [fmt.format(*row) for row in rows]
    return "\n".join(lines)
