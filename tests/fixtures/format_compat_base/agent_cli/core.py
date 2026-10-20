# ruff: noqa
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
    token = _STRICT_READS.set(True)
    try:
        yield
    finally:
        _STRICT_READS.reset(token)


def reads_strict() -> bool:
    return _STRICT_READS.get()


def json_files(base: Path) -> list[Path]:
    if reads_strict():
        return sorted((p for p in base.iterdir() if p.suffix == ".json"))
    return sorted(base.glob("*.json"))


ROOT_MARKER = "bin/agent"
AGENT_SUBTREE_DIRS = (
    "dispatches",
    "journal",
    "leases",
    "peers",
    "peer-net",
    "plan-reviews",
    "raw",
    "sessions",
    "work-order-previews",
)
AGENT_SUBTREE_FILES = ("status.json", "live.json", "default-feature")
AGENT_SUBTREES = (*AGENT_SUBTREE_DIRS, *AGENT_SUBTREE_FILES)
AGENT_DIR = ".agent"
AGENT_UNREGISTERED = frozenset(
    {
        "scratch",
        "browser",
        "dispatch-board",
        "dispatch-notices",
        "install.json",
        "keep.json",
        "land",
        "live-failed",
        "locks",
        "mcp",
        "mcp-servers",
        "notes-linked.json",
        "notes-root",
        "render-failure.json",
        "tabs",
        "tabs-off",
        "telemetry",
        "terminal-python",
        "usage",
        "verify",
    }
)
AGENT_NAMES = frozenset(AGENT_SUBTREES) | AGENT_UNREGISTERED
_DRIVE = re.compile("^[A-Za-z]:")


def agent_parts(subtree: str, *parts: str) -> tuple[str, ...]:
    if subtree not in AGENT_NAMES:
        raise ValueError(
            f"`{AGENT_DIR}/{subtree}` is not registered: add it to core.AGENT_SUBTREE_DIRS/FILES (protected) or core.AGENT_UNREGISTERED (with the reason)"
        )
    for part in parts:
        if not part or part in (".", "..") or any((c in part for c in "/\\\x00")) or _DRIVE.match(part):
            raise ValueError(f"{part!r} is not one path component inside `{AGENT_DIR}/{subtree}`")
    return (AGENT_DIR, subtree, *parts)


def agent_path[P: PurePath](root: P, subtree: str, *parts: str) -> P:
    return root.joinpath(*agent_parts(subtree, *parts))


class _AgentRoot(Path):
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
    return _AgentRoot(root, AGENT_DIR)


def agent_dir_name() -> str:
    return AGENT_DIR


def agent_display(subtree: str, *parts: str) -> str:
    return "/".join(agent_parts(subtree, *parts))


GENERATED_VIEWS = {
    "status": Path("STATUS.md"),
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
    return (*(f"Edit(/.agent/{name}/**)" for name in dirs), *(f"Edit(/.agent/{name})" for name in files))


def agent_path_pattern(
    dirs: tuple[str, ...] = AGENT_SUBTREE_DIRS, files: tuple[str, ...] = AGENT_SUBTREE_FILES
) -> str:
    named = "|".join((re.escape(name) for name in dirs))
    owned_files = "|".join((re.escape(name) for name in files))
    return f"\\.agent(?:/(?:{named})(?:/[^\\s]*)?|/(?:{owned_files})|/)?"


EXIT_OK = 0
EXIT_NOT_FOUND = 1
EXIT_USAGE = 2
EXIT_EXTERNAL = 3


class AgentError(Exception):
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
        f"not inside an agent harness (no {ROOT_MARKER} found walking up from {here}); set AGENT_ROOT or pass --root"
    )


def is_alive(pid: int | None) -> bool:
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
    return dt.strftime("%Y-%m-%d %H:%M")


REPLACE_RETRY_SECONDS = 2.0


def replace(src: str | Path, dst: str | Path) -> None:
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
    if path.is_symlink():
        path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    others = _hard_link_names(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    sides: list[Path] = []
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as fh:
            fh.write(text)
        os.chmod(tmp, 420)
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
            f"{path} has {count - 1} other hard link(s) and only {len(others)} are the notes copy or another clone's; writing it would sever the rest"
        )
    return others


def append_line(path: Path, text: str, mode: int = 420) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, mode)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


def write_if_changed(path: Path, text: str) -> bool:
    try:
        if path.exists() and path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    atomic_write(path, text)
    return True


def coerce_date(value: Any) -> Any:
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
    as_json: bool = False

    def emit(self, data: Any, text: str | None = None) -> None:
        if self.as_json:
            sys.stdout.write(json.dumps(data, indent=2, default=json_default) + "\n")
        elif text is not None:
            if text and (not text.endswith("\n")):
                text += "\n"
            sys.stdout.write(text)
        elif isinstance(data, str):
            sys.stdout.write(data + ("\n" if not data.endswith("\n") else ""))
        else:
            sys.stdout.write(json.dumps(data, indent=2, default=json_default) + "\n")

    def done(self, op: Any) -> int:
        self.emit(op.data, op.text)
        return op.code


def json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def table(rows: list[list[str]], headers: list[str]) -> str:
    if not rows:
        return "(none)"
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join(("{:<" + str(w) + "}" for w in widths))
    lines = [fmt.format(*headers), fmt.format(*["-" * w for w in widths])]
    lines += [fmt.format(*row) for row in rows]
    return "\n".join(lines)
