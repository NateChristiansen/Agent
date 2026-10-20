"""Preview and replace scoped feature documents without rewriting CLI-owned plan bytes.

Directory descriptors anchor access after root validation. The directory flock serializes
this command across harness clones sharing notes; other writers do not take that lock.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import stat
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import features, frontmatter, notes
from .core import AgentError

try:
    import fcntl
except ImportError:  # No unlocked fallback on platforms without directory locks.
    fcntl = None

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\.md\Z")
RESERVED = {"log.md", "status.md", "dashboard.md", "index.md"}
PROTECTED = ("Tasks", "Handoffs")
HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")


def _text(data: bytes) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AgentError("document must be UTF-8") from exc
    if "\0" in text:
        raise AgentError("document must not contain NUL bytes")
    return text


def _frontmatter(data: bytes) -> tuple[bytes, str]:
    lines = data.splitlines(keepends=True)
    if not lines or lines[0].rstrip(b"\r\n") != b"---":
        raise AgentError("plan.md must start with frontmatter")
    for index, line in enumerate(lines[1:], 1):
        if line.rstrip(b"\r\n") == b"---":
            return b"".join(lines[: index + 1]), _text(b"".join(lines[index + 1 :]))
    raise AgentError("plan.md has unterminated frontmatter")


def _sections(body: str, *, rendered: bool) -> dict[str, str]:
    """Protect both markdown headings and the existing task writer's raw `## ` scan.

    Reject ambiguous protected headings, even in examples. Fenced/commented copies must
    not stand in for a real section. This is deliberately narrower than arbitrary Markdown.
    """
    lines = body.splitlines(keepends=True)
    boundaries: list[tuple[int, str]] = []
    fence = ""
    comment = False
    for index, line in enumerate(lines):
        bare = line.rstrip("\r\n")
        heading = HEADING_RE.fullmatch(bare)
        if heading and any(heading[2].casefold().startswith(p.casefold()) for p in PROTECTED):
            if bare not in {f"## {p}" for p in PROTECTED}:
                raise AgentError("plan.md has an ambiguous Tasks or Handoffs heading")
        if rendered:
            was_comment = comment
            if "<!--" in bare:
                comment = True
            if "-->" in bare:
                comment = False
            if was_comment or "<!--" in bare:
                continue
            marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", bare)
            if marker:
                if not fence:
                    fence = marker[1]
                elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                    fence = ""
                continue
            if fence:
                continue
            if heading and len(heading[1]) <= 2:
                boundaries.append((index, heading[2]))
        elif line.startswith("## "):
            boundaries.append((index, bare[3:]))
    result: dict[str, str] = {}
    for pos, (start, name) in enumerate(boundaries):
        if name not in PROTECTED:
            continue
        if name in result:
            raise AgentError(f"plan.md has duplicate {name} sections")
        end = boundaries[pos + 1][0] if pos + 1 < len(boundaries) else len(lines)
        result[name] = "".join(lines[start:end])
    if set(result) != set(PROTECTED):
        raise AgentError("plan.md must have one visible canonical Tasks and Handoffs section")
    return result


def _protect_plan(before: bytes, after: bytes) -> None:
    old_meta, old_body = _frontmatter(before)
    new_meta, new_body = _frontmatter(after)
    if old_meta != new_meta:
        raise AgentError("plan.md frontmatter is CLI-owned here; preserve it byte-for-byte")
    for rendered in (False, True):
        if _sections(old_body, rendered=rendered) != _sections(new_body, rendered=rendered):
            raise AgentError("plan.md Tasks and Handoffs are CLI-owned; preserve them byte-for-byte")


def _target(root: Path, target: str) -> tuple[Path, str, str]:
    parts = target.split("/")
    if (
        len(parts) != 3
        or parts[0] != "features"
        or not features.DIR_RE.fullmatch(parts[1])
        or not NAME_RE.fullmatch(parts[2])
    ):
        raise AgentError("target must be features/<existing NNN-slug>/<top-level name>.md")
    feature, name = parts[1:]
    if name.casefold() in RESERVED or (name.casefold() == "plan.md" and name != "plan.md"):
        raise AgentError(f"{name} is reserved or CLI-owned")
    base = root.resolve() / "features"
    if base.is_symlink():
        registered = notes.read_notes_root(root)
        if registered is None:
            raise AgentError("features symlink requires the registered notes root")
        expected = registered.resolve() / "features"
        # The one setup-managed link is allowed. Its target cannot itself redirect elsewhere.
        if expected.is_symlink() or base.resolve() != expected:
            raise AgentError("features symlink does not point to the registered feature root")
        base = expected
    return base, feature, name


@contextmanager
def _directory(base: Path, feature: str, *, apply: bool) -> Generator[int]:
    if fcntl is None or not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise AgentError("document editing requires POSIX directory locks and no-follow directory access")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    base_fd = os.open(base, flags)
    try:
        fd = os.open(feature, flags, dir_fd=base_fd)
        try:
            if apply:
                fcntl.flock(fd, fcntl.LOCK_EX)
            yield fd
        finally:
            os.close(fd)  # Releases the lock, including on failure.
    finally:
        os.close(base_fd)


def _read(fd: int, name: str) -> tuple[bytes, os.stat_result] | None:
    try:
        source = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        return None
    with os.fdopen(source, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise AgentError(f"{name} must be a regular file with one link")
        return stream.read(), info


def _validate_feature(fd: int, feature: str) -> None:
    plan = _read(fd, "plan.md")
    if plan is None:
        raise AgentError("target must belong to an existing feature with plan.md")
    meta, _ = frontmatter.split(_text(plan[0]).replace("\r\n", "\n"))
    number, slug = feature.split("-", 1)
    if (
        meta.get("id") != int(number)
        or meta.get("slug") != slug
        or meta.get("status") not in features.STATUSES
    ):
        raise AgentError("plan.md identity/status does not match an existing feature")


def _write(fd: int, name: str, replacement: bytes, original: tuple[bytes, os.stat_result] | None) -> None:
    temporary = f".document-{uuid.uuid4().hex}.tmp"
    handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(replacement)
            stream.flush()
            os.fchmod(stream.fileno(), stat.S_IMODE(original[1].st_mode) if original else 0o644)
            os.fsync(stream.fileno())
        current = _read(fd, name)
        if _version(current) != _version(original):
            raise AgentError("target changed during preparation; preview again")
        if original is None:
            # link is atomic and refuses any target that appeared since the absent check.
            os.link(temporary, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        else:
            os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass


def _version(value: tuple[bytes, os.stat_result] | None) -> tuple[object, ...] | None:
    if value is None:
        return None
    data, info = value
    return data, info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_mode


# Shared by the CLI-owned Handoffs migration. Keep the same no-follow access, directory
# locking and compare-before-replace behavior as ordinary document editing.
plan_frontmatter = _frontmatter
plan_sections = _sections
feature_directory = _directory
validate_feature = _validate_feature
read_target = _read
replace_target = _write


def edit(
    root: Path,
    *,
    target: str,
    source: Path,
    apply: bool = False,
    expect_sha256: str | None = None,
    create: bool = False,
    expect_absent: bool = False,
) -> dict[str, Any]:
    """Return a unified diff and whole-file digest; mutate only with explicit apply."""
    if create != expect_absent:
        raise AgentError("creation requires both --create and --expect-absent")
    if create and expect_sha256 is not None:
        raise AgentError("--create cannot use --expect-sha256")
    if apply and not create and not expect_sha256:
        raise AgentError("existing-file apply requires --expect-sha256 from a fresh preview")
    if expect_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", expect_sha256):
        raise AgentError("--expect-sha256 must be a lowercase SHA-256 digest")
    try:
        replacement = source.read_bytes()
        new_text = _text(replacement)
        base, feature, name = _target(root, target)
        with _directory(base, feature, apply=apply) as fd:
            _validate_feature(fd, feature)
            original = _read(fd, name)
            if create and original is not None:
                raise AgentError("--expect-absent failed: target already exists")
            if not create and original is None:
                raise AgentError("target does not exist; use --create --expect-absent for a new document")
            before = original[0] if original else b""
            digest = hashlib.sha256(before).hexdigest() if original else None
            if expect_sha256 is not None and expect_sha256 != digest:
                raise AgentError("stale SHA-256: target changed; preview again")
            if name == "plan.md":
                _protect_plan(before, replacement)
            diff_lines = difflib.unified_diff(
                _text(before).splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=target if original else "/dev/null",
                tofile=target,
            )
            diff = "".join(
                line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                for line in diff_lines
            )
            changed = original is None or before != replacement
            if apply and changed:
                _write(fd, name, replacement, original)
            return {
                "target": target,
                "resolved_target": str(base / feature / name),
                "applied": apply,
                "changed": changed,
                "create": create,
                "before_sha256": digest,
                "after_sha256": hashlib.sha256(replacement).hexdigest(),
                "diff": diff,
            }
    except OSError as exc:
        raise AgentError(f"document access refused: {exc}") from exc
