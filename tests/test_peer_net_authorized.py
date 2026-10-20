"""authorized_keys edits touch one line and keep every other byte."""

from __future__ import annotations

import base64
import os
import stat

import pytest

from agent_cli.peer_net import authorized
from agent_cli.peer_net.errors import PeerNetError

NAME = b"ssh-ed25519"
BLOB = len(NAME).to_bytes(4, "big") + NAME + (32).to_bytes(4, "big") + bytes(range(32))
KEY = "ssh-ed25519 " + base64.b64encode(BLOB).decode()
OTHER_BLOB = BLOB[:-1] + b"\xff"
OTHER = "ssh-ed25519 " + base64.b64encode(OTHER_BLOB).decode()
FP = authorized.fingerprint(BLOB)


def test_options_with_quotes_and_escapes_parse():
    line = 'from="192.0.2.1",command="echo \\"hi there\\"" ' + KEY + " owner@laptop"
    entry = authorized.parse_line(line)
    assert entry is not None and entry.blob == BLOB and entry.comment == "owner@laptop"
    assert entry.options.startswith('from="192.0.2.1"')


def test_a_line_without_options_and_junk_lines():
    plain = authorized.parse_line(KEY)
    assert plain is not None and plain.options == ""
    assert authorized.parse_line("# a comment") is None
    assert authorized.parse_line("garbage here") is None


def test_the_public_key_must_be_one_well_formed_ed25519_key():
    assert authorized.ed25519_blob(KEY + " comment") == BLOB
    for bad in ("ssh-rsa " + KEY.split()[1], "ssh-ed25519 notbase64!", KEY + "\n" + KEY, "ssh-ed25519 AAAA"):
        with pytest.raises(PeerNetError):
            authorized.ed25519_blob(bad)


def test_the_same_key_under_other_options_is_still_a_duplicate():
    data = f'no-pty,from="x" {KEY} owner@laptop\n'.encode()
    with pytest.raises(PeerNetError) as caught:
        authorized.refuse_duplicate(data, BLOB, "mac")
    assert caught.value.code == "key-present"


def test_a_marker_for_the_alias_on_another_key_is_a_collision():
    data = f"{OTHER} agent-peer-net:mac:abcdef12\n".encode()
    with pytest.raises(PeerNetError) as caught:
        authorized.refuse_duplicate(data, BLOB, "mac")
    assert caught.value.code == "marker-collision"


def test_appending_keeps_every_byte_including_a_missing_final_newline():
    assert authorized.with_line(b"keep this", "new") == b"keep this\nnew\n"
    assert authorized.with_line(b"a\r\nb\r\n", "new") == b"a\r\nb\r\nnew\r\n"
    assert authorized.with_line(b"", "new") == b"new\n"


def test_removal_takes_only_the_matching_managed_line():
    line = authorized.managed_line('"/py" -I "/s" --from-device mac', BLOB, "mac", "abcdef12" + "0" * 24)
    data = f"first {OTHER}\n{line}\n# last\n".encode()
    new, removed = authorized.without_line(data, "mac", "abcdef12", FP)
    assert removed == 1 and new == f"first {OTHER}\n# last\n".encode()
    with pytest.raises(PeerNetError):
        authorized.without_line(data, "mac", "00000000", FP)  # same alias, other op: a collision


@pytest.mark.parametrize(
    "command",
    [
        '"/usr/bin/py" -I "/srv/agent/bin/peer-net-receive" --from-device mac',
        '"C:\\Users\\n c\\py\\python.exe" -I "C:\\Users\\n c\\Agent\\bin\\peer-net-receive" --from-device mac',
    ],
)
def test_the_command_round_trips_through_the_option_escaping(command):
    line = authorized.managed_line(command, BLOB, "mac", "a" * 32)
    entry = authorized.parse_line(line)
    assert entry is not None and entry.options.startswith('restrict,command="')
    assert authorized.option_command(entry) == command  # what sshd hands the shell
    assert line.endswith("agent-peer-net:mac:aaaaaaaa")


@pytest.mark.parametrize("command", ['a\\"b', "a\nb", "a\rb"])
def test_ambiguous_commands_are_refused(command):
    with pytest.raises(PeerNetError):
        authorized.managed_line(command, BLOB, "mac", "a" * 32)


def test_rewrite_in_place_keeps_the_mode_and_refuses_a_changed_file(tmp_path):
    path = tmp_path / "authorized_keys"
    path.write_bytes(b"one\n")
    os.chmod(path, 0o640)
    authorized.rewrite_in_place(path, authorized.sha256(b"one\n"), b"one\ntwo\n")
    assert path.read_bytes() == b"one\ntwo\n"
    if os.name != "nt":  # Windows has no POSIX mode bits to keep
        assert stat.S_IMODE(path.stat().st_mode) == 0o640
    with pytest.raises(PeerNetError) as caught:
        authorized.rewrite_in_place(path, authorized.sha256(b"one\n"), b"x")
    assert caught.value.code == "changed-underneath" and path.read_bytes() == b"one\ntwo\n"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_symlinked_file_is_refused(tmp_path):
    real = tmp_path / "real"
    real.write_bytes(b"")
    link = tmp_path / "authorized_keys"
    link.symlink_to(real)
    with pytest.raises(PeerNetError) as caught:
        authorized.refuse_links(link)
    assert caught.value.code == "unsafe-path"
