"""Reading and changing sshd's authorized_keys file for enrollment.

The file holds the owner's other keys, so every change here touches exactly one line, keeps
every other byte, and rewrites in place so its ACL and mode survive. The parser understands
OpenSSH's options prefix, and keys are compared by their decoded blob: a copy of the same key
under other options or comments is still the same key.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import PeerNetError

KEY_TYPES = {
    "ssh-ed25519",
    "ssh-rsa",
    "ssh-dss",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com",
    "sk-ecdsa-sha2-nistp256@openssh.com",
}
MARKER_RE = re.compile(r"agent-peer-net:([a-z][a-z0-9-]{0,15}):([0-9a-f]{8})")


@dataclass(frozen=True)
class Entry:
    options: str
    key_type: str
    blob: bytes
    comment: str


def _is_type(token: str) -> bool:
    return token in KEY_TYPES or token.endswith("-cert-v01@openssh.com")


def _split_options(line: str) -> tuple[str, str]:
    """The options prefix and the rest. The prefix ends at the first whitespace outside quotes;
    inside quotes `\\"` is an escaped quote."""
    quoted = False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and quoted and i + 1 < len(line):
            i += 2
            continue
        if ch == '"':
            quoted = not quoted
        elif ch in " \t" and not quoted:
            return line[:i], line[i:].lstrip()
        i += 1
    raise ValueError("options without a key")


def parse_line(line: str) -> Entry | None:
    """One authorized_keys line, or None for a blank line, a comment or one that cannot parse
    (sshd ignores such a line too, and it is kept untouched)."""
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    try:
        first = text.split(None, 1)[0]
        options, rest = ("", text) if _is_type(first) else _split_options(text)
        parts = rest.split(None, 2)
        if len(parts) < 2 or not _is_type(parts[0]):
            return None
        blob = base64.b64decode(parts[1], validate=True)
    except ValueError, binascii.Error:
        return None
    return Entry(options, parts[0], blob, parts[2] if len(parts) > 2 else "")


def ed25519_blob(public_line: str) -> bytes:
    """The blob of a single `ssh-ed25519 <base64> [comment]` public key, checked for shape."""
    parts = public_line.strip().split()
    if len(parts) not in {2, 3} or parts[0] != "ssh-ed25519" or "\n" in public_line.strip():
        raise PeerNetError("bad-key", "key")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except ValueError, binascii.Error:
        raise PeerNetError("bad-key", "key") from None
    name = b"ssh-ed25519"
    expected = len(name).to_bytes(4, "big") + name + (32).to_bytes(4, "big")
    if len(blob) != len(expected) + 32 or not blob.startswith(expected):
        raise PeerNetError("bad-key", "key")
    return blob


def fingerprint(blob: bytes) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def managed_line(command: str, blob: bytes, alias: str, op_id: str) -> str:
    """`restrict,command="..."` plus the key and the marker revoke matches on.

    sshd unescapes only `\\"` inside an option value; every other backslash, Windows path
    separators included, is literal. So the command's own quotes are written `\\"`, and a
    command that already holds `\\"` or a newline (ambiguous once escaped) is refused.
    """
    if '\\"' in command or "\n" in command or "\r" in command:
        raise PeerNetError("unsafe-path", "launcher")
    encoded = base64.b64encode(blob).decode()
    escaped = command.replace('"', '\\"')
    return f'restrict,command="{escaped}" ssh-ed25519 {encoded} agent-peer-net:{alias}:{op_id[:8]}'


def option_command(entry: Entry) -> str | None:
    """The `command="..."` value as sshd reads it, unescaped; for round-trip tests and doctor."""
    match = re.search(r'(?:^|,)command="((?:[^"\\]|\\.)*)"', entry.options)
    return match.group(1).replace('\\"', '"') if match else None


def entries(data: bytes) -> list[tuple[int, Entry]]:
    out: list[tuple[int, Entry]] = []
    for i, line in enumerate(data.decode("utf-8", errors="replace").splitlines()):
        entry = parse_line(line)
        if entry is not None:
            out.append((i, entry))
    return out


def refuse_duplicate(data: bytes, blob: bytes, alias: str) -> None:
    for _, entry in entries(data):
        if entry.blob == blob:
            raise PeerNetError("key-present", "key")
        marker = MARKER_RE.search(entry.comment)
        if marker and marker.group(1) == alias:
            raise PeerNetError("marker-collision", "alias")


def with_line(data: bytes, line: str) -> bytes:
    """`data` with `line` appended; every existing byte kept, a missing final newline added."""
    sep = b"" if not data or data.endswith(b"\n") else b"\r\n" if b"\r\n" in data else b"\n"
    newline = b"\r\n" if b"\r\n" in data else b"\n"
    return data + sep + line.encode("utf-8") + newline


def without_line(data: bytes, alias: str, op8: str, key_fp: str) -> tuple[bytes, int]:
    """`data` without the managed line matching marker, op id and key fingerprint; the count
    removed. A marker for this alias on another key or op is a collision, refused."""
    kept: list[bytes] = []
    removed = 0
    for raw in data.splitlines(keepends=True):
        entry = parse_line(raw.decode("utf-8", errors="replace"))
        marker = MARKER_RE.search(entry.comment) if entry else None
        if entry and marker and marker.group(1) == alias:
            if fingerprint(entry.blob) != key_fp or marker.group(2) != op8:
                raise PeerNetError("marker-collision", "alias")
            removed += 1
            continue
        kept.append(raw)
    return b"".join(kept), removed


def refuse_links(path: Path) -> None:
    """No symlink or reparse point at the file or its directory."""
    for candidate in (path, path.parent):
        try:
            st = os.lstat(candidate)
        except FileNotFoundError:
            continue
        reparse = getattr(st, "st_file_attributes", 0) & getattr(os, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if os.path.islink(candidate) or (os.name == "nt" and reparse):
            raise PeerNetError("unsafe-path", "authorized_keys")


def rewrite_in_place(path: Path, expected_sha: str, new: bytes) -> None:
    """Overwrite `path` with `new` without replacing the file, so ACL and mode survive. Refuses
    if the file changed since it was read (another editor)."""
    refuse_links(path)
    with path.open("r+b") as fh:
        current = fh.read()
        if hashlib.sha256(current).hexdigest() != expected_sha:
            raise PeerNetError("changed-underneath", "authorized_keys")
        fh.seek(0)
        fh.write(new)
        fh.truncate()
        fh.flush()
        os.fsync(fh.fileno())


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
