"""The bootstrap session: password-only by default, one remote command built only from a
checked repo path, a bounded ssh process, and the Windows accept.cmd checked for drift."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_cli.peer_net import bootstrap, enroll, launcher, remote, ssh, store, system
from agent_cli.peer_net.errors import PeerNetError

REPO = Path(__file__).resolve().parent.parent


# --- the remote command -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "repo",
    [
        "/srv/a'b",
        '/srv/a"b',
        "/srv/100%",
        "/srv/a&b",
        "/srv/$(id)",
        "/srv/`id`",
        "/srv/a;b",
        "relative/path",
        "/srv/a\nb",
        "/srv/../etc",
        "C:\\Agent\\..\\x",
        "C:\\Agent/mixed",
        "C:relative",
        "\\\\server\\share",
        "",
    ],
)
def test_a_hostile_or_relative_repo_is_refused_before_any_command_is_built(repo):
    with pytest.raises(PeerNetError) as caught:
        remote.command(repo)
    assert caught.value.code == "unsafe-path"


def test_the_windows_command_names_only_the_generated_file():
    assert remote.command("C:\\Users\\user\\Agent (main)") == (
        '"C:\\Users\\user\\Agent (main)\\.agent\\peer-net\\accept.cmd"'
    )


def _fake_repo(tmp_path: Path, exit_code: int = 0) -> Path:
    repo = tmp_path / "my repo (x)"
    (repo / "bin").mkdir(parents=True)
    agent = repo / "bin" / "agent"
    agent.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\"\nexit {exit_code}\n")
    agent.chmod(0o755)
    return repo


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell quoting")
@pytest.mark.parametrize("shell", ["sh", "bash", "zsh"])
def test_the_posix_command_round_trips_through_a_real_shell(tmp_path, shell):
    if shutil.which(shell) is None:
        pytest.skip(f"{shell} not installed")
    repo = _fake_repo(tmp_path)
    done = subprocess.run(
        [shell, "-c", remote.command(str(repo))], capture_output=True, text=True, timeout=10
    )
    assert done.returncode == 0 and done.stdout == "peer-net\naccept\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell quoting")
def test_accepts_exit_status_reaches_ssh_through_the_shell(tmp_path):
    repo = _fake_repo(tmp_path, exit_code=7)
    done = subprocess.run(["sh", "-c", remote.command(str(repo))], capture_output=True, timeout=10)
    assert done.returncode == 7


@pytest.mark.skipif(os.name != "nt", reason="real cmd.exe")
def test_accept_cmd_runs_through_real_cmd_and_passes_a_nonzero_exit(tmp_path):
    repo = tmp_path / "Agent (main) x"
    target = repo / ".agent" / "peer-net"
    target.mkdir(parents=True)
    (target / "accept.cmd").write_bytes(b"@exit /b 5\r\n")
    done = subprocess.run(f'cmd.exe /d /s /c "{remote.command(str(repo))}"', timeout=10)
    assert done.returncode == 5


# --- accept.cmd: generated, hashed, checked for drift ------------------------------------------


@pytest.fixture
def b_store(tmp_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows reads home from here, not HOME
    monkeypatch.setattr(system, "host_fingerprints", lambda: ["SHA256:hostkey"])
    monkeypatch.setattr(system, "authorized_file", lambda: (home / ".ssh" / "authorized_keys", False))
    monkeypatch.setattr(
        launcher,
        "resolve",
        lambda root: launcher.Launch("/usr/bin/python3", "/srv/agent/bin/peer-net-receive", "posix"),
    )
    enroll.init(tmp_root, "win")
    conn = store.connect(tmp_root)
    yield conn
    conn.close()


PY = "C:\\Python313\\python.exe"


def test_accept_cmd_content_is_fixed_and_derived_only_from_checked_inputs():
    assert remote.accept_cmd_bytes(PY, "C:\\Agent") == (
        b'@"C:\\Python313\\python.exe" -I "C:\\Agent\\bin\\peer-net-entry" peer-net accept\r\n'
        b"@exit /b %ERRORLEVEL%\r\n"
    )
    with pytest.raises(PeerNetError):
        remote.accept_cmd_bytes('C:\\Py"thon\\python.exe', "C:\\Agent")
    with pytest.raises(PeerNetError):
        remote.accept_cmd_bytes(PY, "C:\\Agent & calc")


def test_init_writes_accept_cmd_once_and_the_check_passes(tmp_root, b_store):
    assert remote.write_accept_cmd(tmp_root, b_store, PY) is True
    assert remote.write_accept_cmd(tmp_root, b_store, PY) is False  # idempotent: no second write
    remote.check_accept_cmd(tmp_root, PY)


def test_a_drifted_file_an_upgraded_interpreter_or_a_missing_hash_stops_the_entry(tmp_root, b_store):
    remote.write_accept_cmd(tmp_root, b_store, PY)
    path = tmp_root / remote.ACCEPT_CMD_REL
    with pytest.raises(PeerNetError) as caught:
        remote.check_accept_cmd(tmp_root, "C:\\Python314\\python.exe")  # interpreter upgraded
    assert caught.value.code == "accept-drift"
    path.write_bytes(path.read_bytes() + b"@calc\r\n")
    with pytest.raises(PeerNetError):
        remote.check_accept_cmd(tmp_root, PY)
    assert remote.write_accept_cmd(tmp_root, b_store, PY) is True  # init repairs it
    remote.check_accept_cmd(tmp_root, PY)
    with store.transaction(b_store):  # a crash between the replace and the hash update
        b_store.execute("DELETE FROM enrollment WHERE k=?", (remote.HASH_KEY,))
    with pytest.raises(PeerNetError):
        remote.check_accept_cmd(tmp_root, PY)
    assert remote.write_accept_cmd(tmp_root, b_store, PY) is True
    remote.check_accept_cmd(tmp_root, PY)


@pytest.mark.skipif(sys.platform == "win32", reason="a symlink needs a privilege the Windows test user lacks")
def test_a_linked_accept_cmd_is_refused_by_init_and_by_the_entry(tmp_root, b_store, tmp_path):
    remote.write_accept_cmd(tmp_root, b_store, PY)
    path = tmp_root / remote.ACCEPT_CMD_REL
    foreign = tmp_path / "foreign.cmd"
    foreign.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(foreign)
    with pytest.raises(PeerNetError):
        remote.check_accept_cmd(tmp_root, PY)
    with pytest.raises(PeerNetError):
        remote.write_accept_cmd(tmp_root, b_store, PY)
    assert foreign.read_bytes() == remote.accept_cmd_bytes(PY, str(tmp_root.resolve()))


def test_the_entry_dispatches_only_peer_net_accept():
    entry = REPO / "bin" / "peer-net-entry"
    for argv in [[], ["peer-net", "receive", "--from-device", "x"], ["peer-net", "accept", "x"], ["show"]]:
        done = subprocess.run([sys.executable, "-I", str(entry), *argv], capture_output=True, timeout=30)
        assert done.returncode == 2, argv


@pytest.mark.skipif(os.name == "nt", reason="the Windows entry checks accept.cmd first")
def test_the_entry_runs_accept_which_reads_only_its_stdin():
    entry = REPO / "bin" / "peer-net-entry"
    done = subprocess.run(
        [sys.executable, "-I", str(entry), "peer-net", "accept"],
        input=b"",
        capture_output=True,
        timeout=60,
        env={**os.environ, "SSH_CONNECTION": "192.0.2.5 1 192.0.2.9 22"},
    )
    assert done.returncode == 1  # the session ended before hello: refused, nothing written
    assert b"pair-closed" in done.stderr


# --- the bootstrap argv -----------------------------------------------------------------------


def _argv(tmp_root: Path, **over) -> list[str]:
    return bootstrap.argv(
        tmp_root,
        alias="mac",
        user="user",
        host="192.0.2.9",
        port=22,
        remote_command=remote.command("/home/user/workspace/Agent"),
        **over,
    )


def test_the_bootstrap_is_password_only_by_default(tmp_root):
    command = _argv(tmp_root)
    assert "-i" not in command
    for option in [
        "PubkeyAuthentication=no",
        "IdentityAgent=none",
        "IdentitiesOnly=yes",
        "PreferredAuthentications=keyboard-interactive,password",
        "StrictHostKeyChecking=yes",
        "ForwardAgent=no",
        "ClearAllForwardings=yes",
        "RequestTTY=no",
    ]:
        assert command[command.index(option) - 1] == "-o"
    assert command[-3:] == ["--", "192.0.2.9", "/home/user/workspace/Agent/bin/agent peer-net accept"]


def test_a_login_key_turns_on_only_that_key(tmp_root, tmp_path):
    key = tmp_path / "id_owner"
    command = _argv(tmp_root, login_key=key)
    assert command[command.index("-i") + 1] == str(key)
    assert "PubkeyAuthentication=yes" in command and "IdentitiesOnly=yes" in command
    assert "PasswordAuthentication=yes" not in command


@pytest.mark.skipif(shutil.which("ssh") is None, reason="needs OpenSSH")
def test_real_ssh_parses_the_bootstrap_argv(tmp_root):
    command = _argv(tmp_root)
    done = subprocess.run([ssh.binary("ssh"), "-G", *command[1:]], capture_output=True, text=True, timeout=10)
    assert done.returncode == 0, done.stderr
    config = set(done.stdout.lower().splitlines())
    for line in [
        "pubkeyauthentication false",  # how `ssh -G` prints a parsed `no`
        "identityagent none",
        "identitiesonly yes",
        "forwardagent no",
        "clearallforwardings yes",
        "requesttty false",
        "stricthostkeychecking true",
        "hostkeyalias agent-peer-mac",
        "port 22",
        "user user",
    ]:
        assert line in config, line


def _keygen(path: Path, passphrase: str = "", kind: str = "ed25519", *extra: str) -> str:
    subprocess.run(
        ["ssh-keygen", "-q", "-t", kind, "-N", passphrase, "-C", "", *extra, "-f", str(path)],
        check=True,
        timeout=60,
    )
    return path.with_name(path.name + ".pub").read_text().strip()


def _fp(pub_line: str) -> str:
    import base64

    from agent_cli.peer_net import authorized

    return authorized.fingerprint(base64.b64decode(pub_line.split()[1]))


needs_keygen = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs OpenSSH")


@needs_keygen
def test_an_encrypted_owner_key_is_fingerprinted_without_its_passphrase(tmp_root, b_store, tmp_path):
    owner = tmp_path / "id_owner"
    pub = _keygen(owner, "a secret passphrase")
    assert bootstrap.login_key_fingerprint(owner) == _fp(pub)
    owner.with_name("id_owner.pub").unlink()  # no sidecar: the key file alone is enough
    assert bootstrap.login_key_fingerprint(owner) == _fp(pub)
    bootstrap.refuse_peer_key(tmp_root, b_store, owner)


@needs_keygen
def test_an_enrolled_key_under_another_name_with_an_unrelated_sidecar_is_refused(tmp_root, b_store, tmp_path):
    # Reproduced: the old check trusted the .pub beside the key.
    enrolled = Path(os.environ["HOME"]) / ".ssh" / "agent_peer_win"
    login = tmp_path / "login"
    login.write_bytes(enrolled.read_bytes())
    unrelated = tmp_path / "unrelated"
    login.with_name("login.pub").write_text(_keygen(unrelated) + "\n")
    with pytest.raises(PeerNetError) as caught:
        bootstrap.refuse_peer_key(tmp_root, b_store, login)
    assert caught.value.code == "login-key"
    login.with_name("login.pub").unlink()
    with pytest.raises(PeerNetError):  # without a sidecar, the key itself is the enrolled one
        bootstrap.refuse_peer_key(tmp_root, b_store, login)


@needs_keygen
def test_a_sidecar_that_disagrees_with_the_owner_key_is_refused(tmp_path):
    owner, other = tmp_path / "id_owner", tmp_path / "other"
    _keygen(owner)
    owner.with_name("id_owner.pub").write_text(_keygen(other) + "\n")
    with pytest.raises(PeerNetError) as caught:
        bootstrap.login_key_fingerprint(owner)
    assert "does not match" in caught.value.detail


def _openssh(body: bytes) -> str:
    import base64

    text = base64.b64encode(body).decode()
    return "-----BEGIN OPENSSH PRIVATE KEY-----\n" + text + "\n-----END OPENSSH PRIVATE KEY-----\n"


def _s(data: bytes) -> bytes:
    return len(data).to_bytes(4, "big") + data


@pytest.mark.parametrize(
    "content",
    [
        "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----\n",  # legacy PEM
        "not a key at all\n",
        _openssh(b"wrong-magic-v1\x00" + _s(b"none") * 3),
        _openssh(bootstrap.OPENSSH_MAGIC + _s(b"none") * 3 + (2).to_bytes(4, "big") + _s(b"a") + _s(b"b")),
        _openssh(bootstrap.OPENSSH_MAGIC + _s(b"none") + b"\x00\x00\xff\xff"),  # a length past the end
        _openssh(bootstrap.OPENSSH_MAGIC),  # truncated
        "-----BEGIN OPENSSH PRIVATE KEY-----\n!!!\n-----END OPENSSH PRIVATE KEY-----\n",
    ],
    ids=["pem", "text", "magic", "two keys", "overlong length", "truncated", "bad base64"],
)
def test_a_malformed_or_unsupported_key_file_is_refused(tmp_path, content):
    path = tmp_path / "key"
    path.write_text(content)
    with pytest.raises(PeerNetError) as caught:
        bootstrap.login_key_fingerprint(path)
    assert caught.value.code == "login-key"


def test_an_oversized_key_file_is_refused_unread(tmp_path):
    path = tmp_path / "key"
    path.write_bytes(b"x" * (bootstrap.MAX_KEY_FILE + 1))
    with pytest.raises(PeerNetError):
        bootstrap.login_key_fingerprint(path)


# --- the CLI path ---------------------------------------------------------------------------


def test_a_fresh_pair_initializes_privately_before_the_bootstrap_writes_anything(tmp_path, monkeypatch):
    import argparse

    from agent_cli.cli import peer_net as cli

    root = tmp_path / "fresh"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows reads home from here, not HOME
    monkeypatch.setattr(system, "host_fingerprints", lambda: ["SHA256:hostkey"])
    monkeypatch.setattr(system, "host_key_line", lambda: "ssh-ed25519 " + "A" * 68)

    def unreachable(host, port):
        raise PeerNetError("unreachable", "host")

    monkeypatch.setattr(ssh, "scan", unreachable)  # stops right after init and the bootstrap argv
    args = argparse.Namespace(
        alias="mac",
        target="user@192.0.2.9",
        repo="/home/user/workspace/Agent",
        port=22,
        device="win",
        enable=False,
        restart=False,
        login_key=None,
        self_user="user",
        self_port=22,
    )
    with pytest.raises(PeerNetError) as caught:
        cli.pair(root, args)
    assert caught.value.code == "unreachable"  # it got past initialization
    peer_net_dir = root / ".agent" / "peer-net"
    if os.name == "nt":
        assert system.is_private(peer_net_dir)
    else:
        assert peer_net_dir.stat().st_mode & 0o077 == 0
    store.check_private(root)
    assert enroll.show(root)["device"] == "win"
    assert (peer_net_dir / "ssh_config").read_bytes() == b""


@pytest.mark.parametrize(
    "bad",
    [{"target": "no-at-sign"}, {"repo": "relative"}, {"alias": "win"}, {"port": 0}, {"self_user": "a b"}],
)
def test_bad_pair_arguments_are_refused_before_anything_is_written(tmp_path, monkeypatch, bad):
    import argparse

    from agent_cli.cli import peer_net as cli

    root = tmp_path / "fresh"
    root.mkdir()
    args = argparse.Namespace(
        **{
            "alias": "mac",
            "target": "user@192.0.2.9",
            "repo": "/home/user/workspace/Agent",
            "port": 22,
            "device": "win",
            "enable": False,
            "restart": False,
            "login_key": None,
            "self_user": "user",
            "self_port": 22,
            **bad,
        }
    )
    with pytest.raises(PeerNetError):
        cli.pair(root, args)
    assert not (root / ".agent").exists()


@pytest.mark.parametrize(
    ("mine", "theirs", "code"),
    [(True, True, 0), (True, False, 3), (False, True, 3), (False, False, 3)],
)
def test_pair_exits_3_unless_both_directions_verified(mine, theirs, code):
    from agent_cli.cli.peer_net import pair_report

    text, exit_code = pair_report({"verified": mine, "their_probe": {"verified": theirs}})
    assert exit_code == code
    assert ("route back" in text) is (not theirs)


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs OpenSSH")
def test_a_peer_or_enrolled_key_cannot_be_the_login_key(tmp_root, b_store, tmp_path):
    ssh_dir = Path(os.environ["HOME"]) / ".ssh"
    with pytest.raises(PeerNetError) as caught:
        bootstrap.refuse_peer_key(tmp_root, b_store, ssh_dir / "agent_peer_win")  # this device's key
    assert caught.value.code == "login-key"
    granted = tmp_path / "granted"
    pub = _keygen(granted)
    from agent_cli.peer_net import authorized

    with store.transaction(b_store):
        b_store.execute(
            "INSERT INTO inbound (alias, key_fp, state, revoked, op_id) VALUES ('mac', ?, 'active', 0, 'x')",
            (authorized.fingerprint(authorized.ed25519_blob(pub)),),
        )
    with pytest.raises(PeerNetError):
        bootstrap.refuse_peer_key(tmp_root, b_store, granted)  # a key some device is granted with
    owner = tmp_path / "id_owner"
    _keygen(owner)
    bootstrap.refuse_peer_key(tmp_root, b_store, owner)  # the owner's own key is fine


# --- the ssh process is bounded ---------------------------------------------------------------


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_the_session_speaks_lines_over_the_process():
    echo = "import sys\nfor line in sys.stdin:\n    sys.stdout.write(line); sys.stdout.flush()"
    session = bootstrap.SshSession(_python(echo), deadline_s=20)
    message = {"v": 1, "phase": "ack", "pair_id": "a" * 32}
    session.send(message, 5)
    assert session.recv(5) == message
    session.close()
    assert session.returncode == 0  # it saw EOF and exited


def test_close_kills_a_process_that_ignores_eof(monkeypatch):
    monkeypatch.setattr(bootstrap, "EXIT_GRACE_S", 0.2)
    session = bootstrap.SshSession(_python("import time; time.sleep(60)"), deadline_s=60)
    started = time.monotonic()
    session.close()
    assert session.proc.poll() is not None and time.monotonic() - started < 5


def test_the_watchdog_ends_a_blocked_read_at_the_deadline():
    session = bootstrap.SshSession(_python("import time; time.sleep(60)"), deadline_s=0.3)
    started = time.monotonic()
    with pytest.raises(PeerNetError) as caught:
        session.recv(10)  # blocked: the process neither writes nor exits on its own
    assert caught.value.code == "pair-closed" and time.monotonic() - started < 5
    session.close()
    assert session.proc.poll() is not None
