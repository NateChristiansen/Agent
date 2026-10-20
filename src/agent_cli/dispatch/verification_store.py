"""Verifier snapshots with guarded storage; protection is not same-UID authentication.

The caller owns the harness root. POSIX uses descriptor-relative paths. Windows checks reparse
points and file identity before opening. Writes are atomic and file-fsynced; evidence/cleanup
snapshots are create-once. A same-user process able to replace these files can forge them; G09
must supply trusted bindings and retain its mutation lease.
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import stat
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..core import agent_parts
from ..records.narrow import as_dict_or_none
from .verification_evidence import Binding, Evidence, binding_of

try:
    import fcntl
except ImportError:  # Windows uses win_lock instead
    fcntl = None  # type: ignore[assignment]

LIMIT = 2 * 1024 * 1024
#: UTC start and end of a run, beside its evidence (`Store.observed`).
OBSERVED = "observed.json"
REPLACE_ATTEMPTS = 50
REPLACE_PAUSE = 0.01


class Store:
    def __init__(self, root: Path, binding: Binding, *, create: bool = False):
        if re.fullmatch(r"[0-9a-f]{32}", binding.run_id) is None:
            raise ValueError("verifier run_id must be a generated 32-character hexadecimal identifier")
        self.binding = binding
        self.root = root.resolve(strict=True)
        self.parts = agent_parts("dispatches", binding.dispatch_id + ".verification", binding.run_id)
        self.path = self.root.joinpath(*self.parts)
        if os.name == "nt":
            from ..win_lock import directory_path

            directory_path(root, self.parts, create=create)
            self.fd = -1
            return
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in self.parts:
                if create:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=fd)
                        os.fsync(fd)
                    except FileExistsError:
                        pass
                following = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = following
            self.fd = fd
        except BaseException:
            os.close(fd)
            raise

    @contextmanager
    def lock(self):
        if os.name == "nt":
            from ..win_lock import exclusive, open_lock_file

            fd = open_lock_file(self.root, (*self.parts, "guard.lock"))
            try:
                with exclusive(fd, timeout=2):
                    yield
            finally:
                os.close(fd)
            return
        if fcntl is None:
            raise OSError("verifier publication requires supported file locking")
        fd = os.open(
            "guard.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=self.fd
        )
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("verifier guard must be a regular file")
            deadline = time.monotonic() + 2
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("verifier publication/recovery guard is busy") from None
                    time.sleep(0.01)
            yield
        finally:
            os.close(fd)

    def close(self) -> None:
        if self.fd != -1:
            os.close(self.fd)

    @staticmethod
    def _name(name: str) -> None:
        if re.fullmatch(r"[a-z0-9][a-z0-9.-]*", name) is None:
            raise ValueError("invalid verifier snapshot name")

    def read(self, name: str) -> dict[str, Any] | None:
        self._name(name)
        try:
            if os.name == "nt":
                from ..win_lock import directory_path, open_regular_file

                directory = directory_path(self.root, self.parts, create=False)
                fd = open_regular_file(directory / name)
            else:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError("verifier snapshots must be regular files")
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError("verifier snapshot exceeds size bound")
        value = as_dict_or_none(json.loads(raw))
        if value is None:
            raise ValueError("verifier snapshot must be an object")
        return value

    def modified(self, name: str) -> float | None:
        """A snapshot's mtime; None when it cannot be read. Windows reads it from a handle
        opened the way `read` opens one, so a reparse point or a swapped file gives None."""
        self._name(name)
        if os.name == "nt":
            from ..core import AgentError
            from ..win_lock import directory_path, open_regular_file

            try:
                fd = open_regular_file(directory_path(self.root, self.parts, create=False) / name)
            except OSError, AgentError:
                return None
            try:
                return os.fstat(fd).st_mtime
            finally:
                os.close(fd)
        try:
            return os.stat(name, dir_fd=self.fd, follow_symlinks=False).st_mtime
        except OSError:
            return None

    def write(self, name: str, value: dict[str, Any], *, immutable: bool = False) -> None:
        self._name(name)
        raw = (json.dumps(value, sort_keys=True, allow_nan=False) + "\n").encode()
        if len(raw) > LIMIT:
            raise ValueError("verifier snapshot exceeds size bound")
        if os.name == "nt":
            self._write_windows(name, raw, immutable=immutable)
            return
        temporary = "tmp-" + secrets.token_hex(16)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if immutable:
                os.link(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd, follow_symlinks=False)
            else:
                # Refuse an existing symlink even though replace would replace the link itself.
                try:
                    check = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
                except FileNotFoundError:
                    pass
                else:
                    regular = stat.S_ISREG(os.fstat(check).st_mode)
                    os.close(check)
                    if not regular:
                        raise ValueError("verifier snapshots must be regular files")
                os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def _write_windows(self, name: str, raw: bytes, *, immutable: bool) -> None:
        from ..win_lock import directory_path, is_reparse

        directory = directory_path(self.root, self.parts, create=False)
        destination = directory / name
        temporary = directory / ("tmp-" + secrets.token_hex(16))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if immutable:
                os.link(temporary, destination)
            else:
                try:
                    info = destination.lstat()
                except FileNotFoundError:
                    pass
                else:
                    if not stat.S_ISREG(info.st_mode) or is_reparse(info):
                        raise ValueError("verifier snapshots must be regular files")
                # A reader holding the destination open makes the replace fail with access denied.
                for attempt in range(REPLACE_ATTEMPTS):
                    try:
                        os.replace(temporary, destination)
                        break
                    except PermissionError:
                        if attempt == REPLACE_ATTEMPTS - 1:
                            raise
                        time.sleep(REPLACE_PAUSE)
        finally:
            temporary.unlink(missing_ok=True)

    def evidence(self, evidence: Evidence) -> None:
        if evidence.binding != self.binding:
            raise ValueError("evidence binding mismatch")
        name = (
            "terminal.json"
            if evidence.state != "running"
            else "initial.json"
            if evidence.before is None
            else f"evidence-{len(evidence.commands):05d}.json"
        )
        self.write(name, evidence.to_dict(), immutable=True)
        if evidence.before is None or evidence.state != "running":
            self.observed(ended=evidence.state != "running")

    def observed(self, *, ended: bool) -> None:
        """The run's UTC start and end, in a sidecar beside the evidence. `Evidence`
        parses an exact field set and its digests bind the snapshot, so the times cannot go in
        `terminal.json`; a report placed a run by a copied file's mtime instead. Best effort: a
        missing or unreadable sidecar leaves readers on their earlier behaviour."""
        from ..core import iso, now

        try:
            current = self.read(OBSERVED) or {}
            stamp = iso(now())
            value = {
                "schema_version": 1,
                "run_id": self.binding.run_id,
                "started_utc": current.get("started_utc") or stamp,
                "ended_utc": stamp if ended else current.get("ended_utc"),
            }
            self.write(OBSERVED, value)
        except OSError, ValueError:
            pass

    def lifecycle(self, value: dict[str, Any]) -> None:
        if {"schema_version", "binding"} & set(value):
            raise ValueError("reserved verifier lifecycle fields")
        raw = {"schema_version": 1, "binding": asdict(self.binding), **value}
        self._validate_lifecycle(raw)
        self.write("lifecycle.json", raw)

    def _matches_binding(self, raw: Any) -> bool:
        try:
            return binding_of(raw) == self.binding
        except TypeError, ValueError:
            return False

    def cleanup_record(self, life: dict[str, Any]) -> dict[str, Any] | None:
        if life["command_index"] is None:
            return None
        raw = self.read(f"cleanup-{life['command_index']:05d}.json")
        fields = {
            "schema_version",
            "binding",
            "command_index",
            "watchdog_pid",
            "command_pgid",
            "cleanup_confirmed",
            "reason",
        }
        numbers = ("schema_version", "command_index", "watchdog_pid", "command_pgid")
        if raw is not None and (
            set(raw) != fields
            or any(type(raw[key]) is not int for key in numbers)
            or raw["schema_version"] != 1
            or not self._matches_binding(raw["binding"])
            or raw["command_index"] != life["command_index"]
            or raw["watchdog_pid"] != life["watchdog_pid"]
            or raw["command_pgid"] != life["command_pid"]
            or type(raw["cleanup_confirmed"]) is not bool
            or not isinstance(raw["reason"], str)
            or raw["reason"] not in {"released", "timeout"}
        ):
            raise ValueError("invalid or stale verifier cleanup record")
        return raw

    def load_lifecycle(self) -> dict[str, Any]:
        return self._validate_lifecycle(self.read("lifecycle.json"))

    def _validate_lifecycle(self, raw: dict[str, Any] | None) -> dict[str, Any]:
        fields = {
            "schema_version",
            "binding",
            "phase",
            "guardian_pid",
            "creator_pid",
            "command_pid",
            "watchdog_pid",
            "command_index",
            "cleanup_confirmed",
            "finished",
            "reason",
            "started",
        }
        if (
            raw is None
            or set(raw) != fields
            or type(raw["schema_version"]) is not int
            or raw["schema_version"] != 1
            or not self._matches_binding(raw["binding"])
        ):
            raise ValueError("invalid or stale verifier lifecycle")
        if not isinstance(raw["phase"], str) or raw["phase"] not in {
            "created",
            "observing",
            "preparing",
            "running",
            "cleanup",
            "finished",
        }:
            raise ValueError("invalid verifier lifecycle phase")
        for key in ("creator_pid", "guardian_pid", "command_pid", "watchdog_pid"):
            if raw[key] is not None and (type(raw[key]) is not int or raw[key] < 1):
                raise ValueError("invalid verifier process identity")
        if raw["creator_pid"] is None:
            raise ValueError("verifier creator identity is required")
        if type(raw["finished"]) is not bool or type(raw["cleanup_confirmed"]) is not bool:
            raise ValueError("invalid verifier completion flags")
        index = raw["command_index"]
        if index is not None and (type(index) is not int or index < 0):
            raise ValueError("invalid verifier command index")
        if (
            isinstance(raw["started"], bool)
            or not isinstance(raw["started"], (int, float))
            or not math.isfinite(raw["started"])
        ):
            raise ValueError("invalid verifier start time")
        if not isinstance(raw["reason"], str) or raw["reason"] not in {
            "pending",
            "observing",
            "initial_tree_changed",
            "parent_interrupted",
            "cancelled",
            "command_failed",
            "command_timeout",
            "command_error",
            "commands_complete",
            "runner_error",
            "unsupported_tree",
            "cleanup_unconfirmed",
            "guardian_interrupted",
            "final_tree_changed",
        }:
            raise ValueError("invalid verifier lifecycle reason")
        if raw["finished"] != (raw["phase"] == "finished"):
            raise ValueError("inconsistent verifier lifecycle completion")
        return raw
