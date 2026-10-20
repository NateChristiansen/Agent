"""OS facts enrollment depends on: sshd's host keys, which authorized_keys file sshd reads for
this account, and file permissions.

Windows follows Microsoft's OpenSSH key-management rules: a member of Administrators
(S-1-5-32-544) is read from `%ProgramData%\\ssh\\administrators_authorized_keys`, whose ACL must
grant only Administrators and SYSTEM (S-1-5-18). Membership decides the file, not elevation;
writing that file needs elevation. SIDs, never localized group names.
"""

from __future__ import annotations

import contextvars
import os
import re
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from .authorized import fingerprint
from .errors import PeerNetError

ADMINS_SID = "S-1-5-32-544"
SYSTEM_SID = "S-1-5-18"
HIGH_INTEGRITY_SID = "S-1-16-12288"


def _ssh_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "ssh"
    return Path("/etc/ssh")


def host_key_line() -> str:
    """This machine's sshd ed25519 host public key as `ssh-ed25519 <base64>`, for pairing to
    send to the other side over the owner-authenticated session."""
    pub = _ssh_dir() / "ssh_host_ed25519_key.pub"
    try:
        parts = pub.read_text(encoding="utf-8").split()
    except PermissionError:
        raise PeerNetError("host-key-unreadable", "run pair from an elevated shell") from None
    except OSError:
        raise PeerNetError("no-host-keys", "start the SSH server, then pair again") from None
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise PeerNetError("no-host-keys", "no ed25519 host key")
    return f"{parts[0]} {parts[1]}"


def host_fingerprints() -> list[str]:
    """SHA256 fingerprints of this machine's sshd ed25519 host key, or [] when sshd has none
    yet (Remote Login never enabled, or sshd never started)."""
    import base64

    out: list[str] = []
    pub = _ssh_dir() / "ssh_host_ed25519_key.pub"
    try:
        parts = pub.read_text(encoding="utf-8").split()
    except PermissionError:
        # A present OpenSSH key can be unreadable without administrator rights. Report access
        # failure rather than a missing key.
        raise PeerNetError("host-key-unreadable", "run init from an elevated shell") from None
    except OSError:
        return []
    if len(parts) >= 2 and parts[0] == "ssh-ed25519":
        try:
            out.append(fingerprint(base64.b64decode(parts[1], validate=True)))
        except ValueError:
            return []
    return out


def _whoami(*args: str) -> str:
    try:
        return subprocess.run(
            ["whoami", *args], capture_output=True, text=True, check=True, timeout=_budget(10.0)
        ).stdout
    except OSError, subprocess.SubprocessError:
        raise PeerNetError("system", "whoami") from None


def is_admin_member() -> bool:
    return os.name == "nt" and ADMINS_SID in _whoami("/groups")


def is_elevated() -> bool:
    return os.name == "nt" and HIGH_INTEGRITY_SID in _whoami("/groups")


def account_id() -> str:
    """The account running this command, as enrollment records it: the user SID on Windows, the
    uid on POSIX. Group membership and elevation are deliberately not part of it."""
    if os.name == "nt":
        return "sid:" + user_sid()
    return f"uid:{os.getuid()}"


def refuse_root() -> None:
    """Enrollment state, keys and the store belong to the owner's account, never root's: a
    store made under sudo would lock the owner out of their own pairing."""
    if os.name != "nt" and os.geteuid() == 0:
        raise PeerNetError("root", "run as your own user, not with sudo")


def user_sid() -> str:
    line = _whoami("/user", "/fo", "csv", "/nh").strip().splitlines()[-1]
    return line.split(",")[-1].strip().strip('"')


def authorized_file() -> tuple[Path, bool]:
    """The file sshd reads for this account, and whether it is the shared admin file."""
    if is_admin_member():
        return _ssh_dir() / "administrators_authorized_keys", True
    return Path.home() / ".ssh" / "authorized_keys", False


PS_TIMEOUT_S = 30.0
PS_ATTEMPTS = 2  # Retry a timed-out PowerShell query once because startup under load can exceed
# the first deadline.
MIN_TRY_S = 1.0  # a retry is not started with less budget than this
SID_RE = re.compile(r"S-1-[0-9-]{1,180}")
NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")
_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar("peer_net_deadline", default=None)


@contextmanager
def deadline(end_monotonic: float) -> Generator[None]:
    """Bound every OS check made inside (PowerShell, whoami, icacls) by one caller deadline,
    such as a cross-device send's budget, instead of each tool's own timeout."""
    token = _DEADLINE.set(end_monotonic)
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def _budget(default: float) -> float:
    """The timeout for the next tool call: its default, capped by the active deadline."""
    end = _DEADLINE.get()
    if end is None:
        return default
    remaining = end - time.monotonic()
    if remaining <= 0:
        raise PeerNetError("timeout", "permission check")
    return min(default, remaining)


def _fail(step: str, path: Path, reason: str) -> PeerNetError:
    """`system` with the step, the file's name and a fixed reason code (`timeout`,
    `start-failed`, `exit-<n>`, `bad-answer`): a bare "acl" hid why it failed (task 17). Never
    the tool's own error text, which can carry full local paths, and within the 64 characters a
    refusal detail may have when it crosses to another device."""
    name = NAME_SAFE_RE.sub("_", path.name)[:24] or "file"
    return PeerNetError("system", f"{step} {name}: {reason}"[:64])


def _powershell(script: str, path: Path, step: str) -> str:
    """Run one PowerShell script about `path` (passed through the environment, never the
    command text). A timeout is retried once while the budget allows; any other failure is
    reported at once."""
    for attempt in range(PS_ATTEMPTS):
        timeout = _budget(PS_TIMEOUT_S)
        try:
            done = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, "PEERNET_PATH": str(path)},
            )
        except subprocess.TimeoutExpired:
            end = _DEADLINE.get()
            if attempt + 1 < PS_ATTEMPTS and (end is None or end - time.monotonic() >= MIN_TRY_S):
                continue
            raise _fail(step, path, "timeout") from None
        except OSError:
            raise _fail(step, path, "start-failed") from None
        if done.returncode != 0:
            raise _fail(step, path, f"exit-{done.returncode}")
        return done.stdout
    raise _fail(step, path, "timeout")  # unreachable: the last attempt always returns or raises


# Every fact is collected before anything is printed, and any error stops the script with a
# nonzero exit (PowerShell's default is to continue): a partial answer is never read as a
# whole one. END marks a complete reply.
ACL_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    "$sid = [System.Security.Principal.SecurityIdentifier]; "
    "$acl = Get-Acl -LiteralPath $env:PEERNET_PATH; "
    "$owner = $acl.GetOwner($sid).Value; "
    "$grants = @($acl.Access | ForEach-Object { $_.IdentityReference.Translate($sid).Value }); "
    "'OWNER ' + $owner; foreach ($g in $grants) { 'SID ' + $g }; 'END'"
)


def acl_facts(path: Path) -> tuple[str, set[str]]:
    """(owner SID, granted SIDs) for `path`, from one PowerShell call whose whole reply is
    validated: exactly one owner, SID-shaped values only, and the END marker last."""
    lines = [line.strip() for line in _powershell(ACL_SCRIPT, path, "acl").splitlines() if line.strip()]
    if not lines or lines[-1] != "END":
        raise _fail("acl", path, "bad-answer")
    owners = [line[6:] for line in lines[:-1] if line.startswith("OWNER ")]
    sids = [line[4:] for line in lines[:-1] if line.startswith("SID ")]
    if len(owners) != 1 or len(owners) + len(sids) != len(lines) - 1:
        raise _fail("acl", path, "bad-answer")
    if not all(SID_RE.fullmatch(value) for value in [*owners, *sids]):
        raise _fail("acl", path, "bad-answer")
    return owners[0], set(sids)


def acl_sids(path: Path) -> set[str]:
    """The SIDs granted on `path`."""
    return acl_facts(path)[1]


def restrict_admin_file(path: Path) -> None:
    _icacls(path, f"*{ADMINS_SID}:F", f"*{SYSTEM_SID}:F")


def restrict_to_user(path: Path) -> None:
    """User-only access: 0600 for a file and 0700 for a directory on POSIX; an inheritance-free
    ACL granting only this user's SID on Windows."""
    if os.name == "nt":
        # A directory's grant is inheritable, so files created in it later start user-only.
        _icacls(path, f"*{user_sid()}:(OI)(CI)F" if path.is_dir() else f"*{user_sid()}:F")
    else:
        os.chmod(path, 0o700 if path.is_dir() else 0o600)


OWNER_RIGHTS_SID = "S-1-3-4"


def is_private(path: Path) -> bool:
    """No access for group or others on POSIX. On Windows only this user, SYSTEM,
    Administrators and OWNER RIGHTS are granted, and the owner is one of the first three.

    OWNER RIGHTS (S-1-3-4) is there because Python's own `os.mkdir(mode=0o700)` grants it on
    Windows (3.13+), alongside the user, SYSTEM and Administrators; it gives the object's owner
    its rights, so it is private exactly when the owner is. An elevated shell makes
    Administrators the owner (seen)."""
    if os.name == "nt":
        trusted = {user_sid(), SYSTEM_SID, ADMINS_SID}
        owner, sids = acl_facts(path)  # one PowerShell call, not two
        return sids <= trusted | {OWNER_RIGHTS_SID} and owner in trusted
    return os.stat(path).st_mode & 0o077 == 0


def _icacls(path: Path, *grants: str) -> None:
    command = ["icacls", str(path), "/inheritance:r"]
    for grant in grants:
        command += ["/grant:r", grant]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=_budget(PS_TIMEOUT_S))
    except subprocess.TimeoutExpired:
        raise _fail("icacls", path, "timeout") from None
    except OSError:
        raise _fail("icacls", path, "start-failed") from None
    if done.returncode != 0:
        raise _fail("icacls", path, f"exit-{done.returncode}")
