"""Prepare the local SSH server idempotently through person-only setup. On Windows manage OpenSSH
Server, sshd service and private-network firewall setup; on macOS report Remote Login requirements.

Read state before each action and re-read afterward. Report ok, confirmed changed, owner-decision
warn or failed. Unknown, malformed or timed-out reads never authorize changes. Never choose trusted
networks, open Public/Domain SSH, widen ACLs, change login policy or silently change shell/account
type. Existing firewall changes require explicit private-only mode and affect only profile/enabled
fields. Route OS calls through Runner for isolated tests.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..records.narrow import as_dict_or_none, as_list_or_none, dict_list
from . import launcher, system
from .errors import PeerNetError

INSTALL_TIMEOUT_S = 15 * 60
SUDO_TIMEOUT_S = 5 * 60
RULE = "OpenSSH-Server-In-TCP"
ORDER = {"ok": 0, "changed": 1, "warn": 2, "failed": 3}


@dataclass(frozen=True)
class Result:
    code: int
    out: str
    err: str


class Runner:
    """Runs one OS command with a deadline. Tests replace it."""

    def run(self, argv: list[str], timeout: float = 60.0) -> Result:
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return Result(124, "", "timeout")
        except OSError as exc:
            return Result(127, "", type(exc).__name__)
        return Result(done.returncode, done.stdout, done.stderr)

    def ps(self, script: str, timeout: float = 60.0) -> Result:
        """PowerShell with every error terminating: a failed cmdlet exits 3 with its message on
        stderr, so a failed read is never mistaken for an empty one."""
        wrapped = (
            "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'; "
            f"try {{ {script} }} catch {{ [Console]::Error.WriteLine($_.Exception.Message); exit 3 }}"
        )
        return self.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", wrapped], timeout)

    def interactive(self, argv: list[str], timeout: float = SUDO_TIMEOUT_S) -> Result:
        """For sudo: the terminal stays attached, so its password prompt and messages reach the
        owner directly. The harness never sees the password."""
        try:
            done = subprocess.run(argv, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            return Result(124, "", "timeout")
        except OSError as exc:
            return Result(127, "", type(exc).__name__)
        return Result(done.returncode, "", "")

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def _step(name: str, status: str, detail: str = "") -> dict[str, str]:
    return {"step": name, "status": status, "detail": detail}


def _why(result: Result) -> str:
    text = (result.err or result.out).strip().replace("\n", " ")
    return ("timed out" if result.code == 124 else f"exit {result.code}") + (
        f": {text[:200]}" if text else ""
    )


def _guard(name: str, step: Callable[[], dict[str, str]]) -> dict[str, str]:
    """A step that raised becomes a failed step; the steps before it keep their reports."""
    try:
        return step()
    except PeerNetError as exc:
        return _step(name, "failed", f"{exc.code}: {exc.detail}")
    except OSError as exc:
        return _step(name, "failed", f"{type(exc).__name__}: {exc}")


def setup_server(runner: Runner | None = None, *, firewall_private_only: bool = False) -> dict[str, Any]:
    runner = runner or Runner()
    if os.name == "nt":
        if not system.is_elevated():
            raise PeerNetError("needs-elevation", "run setup-server from an elevated PowerShell")
        steps = _windows(runner, firewall_private_only)
    else:
        system.refuse_root()  # the harness runs as the owner; only the one sudo step is privileged
        steps = _mac(runner)
    result = max((s["status"] for s in steps), key=ORDER.__getitem__, default="ok")
    return {"steps": steps, "result": result}


# --- Windows ------------------------------------------------------------------------------


def _query(runner: Runner, script: str) -> tuple[str, Any]:
    """`("ok", data)`, `("absent", None)` for a successful empty read, or `("error", why)`."""
    result = runner.ps(script)
    if result.code != 0:
        return "error", _why(result)
    if not result.out.strip():
        return "absent", None
    try:
        return "ok", json.loads(result.out)
    except ValueError:
        return "error", "unreadable output"


def _windows(runner: Runner, firewall_private_only: bool) -> list[dict[str, str]]:
    steps = [_guard("ssh server installed", lambda: _capability(runner))]
    if steps[0]["status"] == "failed":
        return steps
    steps.append(_guard("sshd automatic and running", lambda: _service(runner)))
    steps.append(_guard("host keys", lambda: _host_keys(runner, windows=True)))
    steps.append(_guard("firewall", lambda: _firewall(runner, firewall_private_only)))
    steps.append(_guard("network", lambda: _network(runner)))
    steps.append(_guard("admin keys file", _admin_keys_file))
    steps.append(_guard("ssh shell", lambda: _shell(windows=True)))
    steps.append(_guard("account", lambda: _account(runner)))
    return steps


CAPABILITY = "(Get-WindowsCapability -Online -Name 'OpenSSH.Server*').State | ConvertTo-Json"


def _capability(runner: Runner) -> dict[str, str]:
    name = "ssh server installed"
    state, data = _query(runner, CAPABILITY)
    if state != "ok" or not isinstance(data, str) or data not in {"Installed", "NotPresent", "Staged"}:
        return _step(
            name,
            "failed",
            f"could not read the OpenSSH.Server state ({data if state == 'ok' else data or state})",
        )
    if data == "Installed":
        return _step(name, "ok")
    added = runner.ps(
        "Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 | Out-Null", INSTALL_TIMEOUT_S
    )
    if added.code == 124:
        return _step(
            name, "failed", "install did not finish in 15 min; it may still be running, re-run later"
        )
    if added.code != 0:
        return _step(name, "failed", "install failed (blocked by policy or offline?): " + _why(added))
    state, data = _query(runner, CAPABILITY)
    if (state, data) != ("ok", "Installed"):
        return _step(name, "failed", f"install reported success but the state is {data or state}")
    return _step(name, "changed", "installed OpenSSH.Server")


SERVICE = (
    "Get-Service sshd | Select-Object @{n='Status';e={[string]$_.Status}},"
    "@{n='StartType';e={[string]$_.StartType}} | ConvertTo-Json"
)


STATUSES = {"Running", "Stopped", "StartPending", "StopPending", "Paused", "PausePending", "ContinuePending"}
START_TYPES = {"Automatic", "Manual", "Disabled", "Boot", "System"}


def _service_shape(data: object) -> bool:
    shape = as_dict_or_none(data)
    return (
        shape is not None
        and isinstance(shape.get("Status"), str)
        and shape["Status"] in STATUSES
        and isinstance(shape.get("StartType"), str)
        and shape["StartType"] in START_TYPES
    )


def _service(runner: Runner) -> dict[str, str]:
    name = "sshd automatic and running"
    state, now = _query(runner, SERVICE)
    if state != "ok" or not _service_shape(now):
        return _step(
            name, "failed", f"could not read the sshd service ({now if state == 'ok' else now or state})"
        )
    changes: list[str] = []
    if now.get("StartType") != "Automatic":
        done = runner.ps("Set-Service -Name sshd -StartupType Automatic")
        if done.code != 0:
            return _step(name, "failed", "could not set StartupType Automatic: " + _why(done))
        changes.append("set Automatic")
    if now.get("Status") != "Running":
        done = runner.ps("Start-Service sshd")
        if done.code != 0:
            return _step(
                name, "failed", "could not start sshd (see the OpenSSH/Operational log): " + _why(done)
            )
        changes.append("started")
    if not changes:
        return _step(name, "ok")
    runner.sleep(5)
    state, after = _query(runner, SERVICE)
    if state != "ok" or not _service_shape(after):
        return _step(name, "failed", f"could not re-read sshd after changing it ({after or state})")
    if after.get("Status") != "Running":
        return _step(name, "failed", "sshd stopped again after starting; see the OpenSSH/Operational log")
    if after.get("StartType") != "Automatic":
        return _step(name, "failed", f"StartType is {after.get('StartType')} after setting Automatic")
    return _step(name, "changed", ", ".join(changes))


def _host_keys(runner: Runner, *, windows: bool) -> dict[str, str]:
    name = "host keys"
    fps = system.host_fingerprints()  # raises host-key-unreadable rather than reporting none
    if fps:
        return _step(name, "ok", fps[0])
    if not windows:
        runner.run(["ssh-keyscan", "-T", "5", "localhost"], timeout=15)  # sshd makes its keys
        fps = system.host_fingerprints()
        if fps:
            return _step(name, "changed", fps[0])
    return _step(name, "failed", "no host key yet; the SSH server has not generated one")


RULE_READ = (
    # Enumerate and filter rather than ask by name: absence is then an empty, successful read,
    # and every real error (access denied, provider failure) stays terminating under the
    # wrapper's ErrorActionPreference=Stop. No -ErrorAction override anywhere.
    f"$r = @(Get-NetFirewallRule | Where-Object {{ $_.Name -eq '{RULE}' }}); "
    "if ($r.Count -eq 0) { return }; "
    "ConvertTo-Json -InputObject @($r | ForEach-Object { $p = $_ | Get-NetFirewallPortFilter; "
    "[pscustomobject]@{ Enabled = [string]$_.Enabled; Profile = [string]$_.Profile; "
    "Direction = [string]$_.Direction; Action = [string]$_.Action; Protocol = [string]$p.Protocol; "
    "LocalPort = [string]$p.LocalPort } })"
)
# `-InputObject @(...)` rather than `-AsArray`: Windows PowerShell 5.1, which `powershell` runs,
# has no -AsArray, and piping a one-element array would unwrap it to a bare object.
WANT_SHAPE = {"Direction": "Inbound", "Action": "Allow", "Protocol": "TCP", "LocalPort": "22"}


def _rule(runner: Runner) -> tuple[str, Any]:
    state, data = _query(runner, RULE_READ)
    if state != "ok":
        return state, data
    fields = {"Enabled", "Profile", "Direction", "Action", "Protocol", "LocalPort"}
    rules = as_list_or_none(data)
    rule = as_dict_or_none(rules[0]) if rules is not None and len(rules) == 1 else None
    if rule is None or any(not isinstance(rule.get(f), str) for f in fields):
        return "error", f"expected one {RULE} rule, read {len(rules) if rules is not None else 'none'}"
    return "ok", rule


def _firewall(runner: Runner, private_only: bool) -> dict[str, str]:
    name = "firewall"
    state, rule = _rule(runner)
    if state == "error":
        return _step(name, "failed", f"could not read the {RULE} rule ({rule})")
    if state == "absent":
        made = runner.ps(
            f"New-NetFirewallRule -Name '{RULE}' -DisplayName 'OpenSSH Server (sshd)' -Direction Inbound "
            "-Protocol TCP -LocalPort 22 -Action Allow -Profile Private -Enabled True | Out-Null"
        )
        if made.code != 0:
            return _step(name, "failed", "could not create the rule: " + _why(made))
        state, rule = _rule(runner)
        if state != "ok" or {**WANT_SHAPE, "Profile": "Private", "Enabled": "True"}.items() - rule.items():
            return _step(
                name, "failed", f"the new rule does not read back as inbound TCP 22 Private ({rule})"
            )
        return _step(name, "changed", "inbound TCP 22 allowed on Private networks only")
    shape = {k: rule.get(k) for k in WANT_SHAPE}
    if shape != WANT_SHAPE:
        # Direction, action, protocol and port are the owner's; this command never edits them.
        return _step(name, "warn", f"existing {RULE} rule is {shape}, not inbound TCP 22 allow; left as is")
    if rule.get("Profile") == "Private" and rule.get("Enabled") == "True":
        return _step(name, "ok", "inbound TCP 22 on Private")
    detail = f"existing rule: profile {rule.get('Profile')}, enabled {rule.get('Enabled')}"
    if not private_only:
        return _step(
            name, "warn", detail + "; left as is. Pass --firewall private-only to make it Private only."
        )
    done = runner.ps(f"Set-NetFirewallRule -Name '{RULE}' -Profile Private -Enabled True")
    if done.code != 0:
        return _step(name, "failed", "could not narrow the rule to Private: " + _why(done))
    state, rule = _rule(runner)
    if state != "ok" or (rule.get("Profile"), rule.get("Enabled")) != ("Private", "True"):
        return _step(name, "failed", f"the rule does not read back as Private and enabled ({rule})")
    return _step(name, "changed", detail + "; narrowed to Private, enabled")


NETWORKS = (
    "ConvertTo-Json -InputObject @(Get-NetConnectionProfile | ForEach-Object { [pscustomobject]@{ "
    "Name = [string]$_.Name; Category = [string]$_.NetworkCategory } })"
)


def _network(runner: Runner) -> dict[str, str]:
    name = "network"
    state, data = _query(runner, NETWORKS)
    listed: list[object] | None = [] if state == "absent" else as_list_or_none(data)
    rows = dict_list(listed)
    shaped = (
        listed is not None
        and len(rows) == len(listed)
        and all(
            isinstance(r.get("Name"), str)
            and isinstance(r.get("Category"), str)
            and r["Category"] in {"Public", "Private", "DomainAuthenticated"}
            for r in rows
        )
    )
    if state == "error" or not shaped:
        return _step(
            name,
            "failed",
            f"could not read the network profiles ({data if state == 'ok' else data or state})",
        )
    if not rows:
        return _step(name, "warn", "no connected network")
    off = [f"{r.get('Name')} ({r.get('Category')})" for r in rows if r.get("Category") != "Private"]
    if off:
        return _step(
            name,
            "warn",
            f"{', '.join(off)}: the Private-only SSH rule does not apply there. If you trust a network, "
            "set it to Private in Settings > Network & internet > (your network) > Properties",
        )
    return _step(name, "ok", "every connected network is Private")


def _admin_keys_file() -> dict[str, str]:
    """Create the shared admin keys file with its ACL before it is published; never touch an
    existing one. A file that appears meanwhile (a publish race) is reported, not replaced."""
    from .authorized import refuse_links

    name = "admin keys file"
    path = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "ssh" / "administrators_authorized_keys"
    refuse_links(path)
    want = {system.ADMINS_SID, system.SYSTEM_SID}
    if path.exists():
        have = system.acl_sids(path)
        if have == want:
            return _step(name, "ok")
        return _step(name, "warn", f"ACL grants {sorted(have)}; sshd needs only SYSTEM and Administrators")
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    os.close(fd)
    try:
        system.restrict_admin_file(tmp)
        if system.acl_sids(tmp) != want:
            return _step(name, "failed", "could not set the ACL")
        try:
            os.link(tmp, path)  # never overwrites
        except FileExistsError:
            return _step(name, "warn", "the file appeared while setting up; left as is, re-run to check it")
    finally:
        tmp.unlink(missing_ok=True)
    return _step(name, "changed", "created empty, SYSTEM and Administrators only")


def _shell(windows: bool) -> dict[str, str]:
    try:
        launcher.windows_shell() if windows else launcher.posix_shell()
    except PeerNetError as exc:
        return _step("ssh shell", "warn", f"{exc.code}: pairing needs cmd.exe (Windows) or sh, bash or zsh")
    return _step("ssh shell", "ok")


def _account(runner: Runner) -> dict[str, str]:
    state, source = _query(
        runner, "(Get-LocalUser -Name $env:USERNAME).PrincipalSource.ToString() | ConvertTo-Json"
    )
    if state != "ok":
        return _step("account", "warn", f"could not read the account type ({source or state})")
    if source == "MicrosoftAccount":
        return _step(
            "account",
            "warn",
            "Microsoft account: in testing, password SSH to such an account failed; "
            "if pairing fails at the password, start `pair` from this machine",
        )
    return _step("account", "ok", str(source))


# --- Mac ----------------------------------------------------------------------------------


OVERRIDE_RE = re.compile(r'^\s*"com\.openssh\.sshd" => (enabled|disabled)\s*$')
LAUNCHCTL_NOT_FOUND = 113  # `launchctl print` for a service that is not loaded


def _remote_login(runner: Runner) -> str:
    """`on`, `off` or `unknown`, from launchd's own records, never a port probe: a listener on 22
    alone would not prove Remote Login serves it. Only the recognised format counts; anything
    else is `unknown`, which never leads to enabling.

    1. `launchctl print-disabled system` lists the override that the Remote Login switch writes
       (`"com.openssh.sshd" => enabled|disabled`). Seen on the owner's Mac, 2026-10-03.
    2. With no override (the switch never touched), `launchctl print system/com.openssh.sshd`:
       exit 0 means the job is loaded (on); 113, "could not find service", means off."""
    result = runner.run(["launchctl", "print-disabled", "system"], timeout=15)
    if result.code != 0 or "disabled services = {" not in result.out:
        return "unknown"
    named = [line for line in result.out.splitlines() if "com.openssh.sshd" in line]
    if len(named) > 1:
        return "unknown"
    if named:
        match = OVERRIDE_RE.match(named[0])
        if not match:
            return "unknown"
        return "on" if match.group(1) == "enabled" else "off"
    job = runner.run(["launchctl", "print", "system/com.openssh.sshd"], timeout=15)
    if job.code == 0:
        return "on"
    if job.code == LAUNCHCTL_NOT_FOUND:
        return "off"
    return "unknown"


def _mac(runner: Runner) -> list[dict[str, str]]:
    name = "remote login"
    steps: list[dict[str, str]] = []
    state = _remote_login(runner)
    if state == "unknown":
        return [_step(name, "failed", "could not read the sshd launchd state")]
    if state == "on":
        steps.append(_step(name, "ok"))
    else:
        print("setup-server: turning on Remote Login needs your password for sudo.", flush=True)
        done = runner.interactive(["sudo", "systemsetup", "-f", "-setremotelogin", "on"])
        if done.code != 0 or _remote_login(runner) != "on":
            why = "timed out" if done.code == 124 else f"exit {done.code}"
            hint = (
                "if macOS asked for Full Disk Access, grant it to your terminal and re-run; "
                "or turn it on in System Settings > General > Sharing > Remote Login"
            )
            steps.append(_step(name, "failed", f"{why}; {hint}"))
            return steps
        steps.append(_step(name, "changed", "turned on"))
    steps.append(_guard("host keys", lambda: _host_keys(runner, windows=False)))
    steps.append(_guard("ssh shell", lambda: _shell(windows=False)))
    return steps
