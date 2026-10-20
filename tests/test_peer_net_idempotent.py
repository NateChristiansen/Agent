"""Enrollment commands are idempotent and bound to one account.

Running a command again with the same
inputs leaves the same durable state, writes no second backup, and never downgrades a grant.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_cli.peer_net import enroll, intents, ssh, store, system
from agent_cli.peer_net.errors import PeerNetError
from test_peer_net_enroll import (  # noqa: F401 -- shared fixtures
    MAC_PUB,
    SCANNED_FP,
    _keys,
    _ready,
    _status_ok,
    home,
    remote,
)


def _backups(root: Path) -> list[Path]:
    directory = root / ".agent" / "peer-net" / "backups"
    return sorted(directory.glob("*.bak")) if directory.exists() else []


def _state(root: Path) -> dict:
    out = enroll.show(root)
    out.pop("open_intents", None)
    return out


def test_init_twice_reprints_and_writes_nothing(tmp_root, home):
    first = enroll.init(tmp_root, "windows")
    key_bytes = (home / ".ssh" / "agent_peer_windows").read_bytes()
    again = enroll.init(tmp_root, "windows")
    assert again["already"] is True and again["public_key"] == first["public_key"]
    assert (home / ".ssh" / "agent_peer_windows").read_bytes() == key_bytes
    with pytest.raises(PeerNetError):
        enroll.init(tmp_root, "laptop")  # a different device still refuses


def test_authorize_twice_is_a_noop_with_no_second_backup(tmp_root, home):
    _ready(tmp_root)
    enroll.authorize(tmp_root, "mac", MAC_PUB)
    data, backups, state = _keys(home).read_bytes(), _backups(tmp_root), _state(tmp_root)
    again = enroll.authorize(tmp_root, "mac", MAC_PUB)
    assert again["already"] is True
    assert _keys(home).read_bytes() == data and _backups(tmp_root) == backups and _state(tmp_root) == state


def test_authorize_reinstalls_a_missing_line_without_downgrading_the_grant(tmp_root, home, monkeypatch):
    _ready(tmp_root)
    enroll.authorize(tmp_root, "mac", MAC_PUB)
    _keys(home).write_bytes(b"ssh-rsa AAAAB3Nza owner@laptop\n")  # someone removed our line
    states: list[str] = []
    real = enroll.mutate.rewrite

    def watch(*a, **k):
        conn = store.connect(tmp_root)
        states.append(conn.execute("SELECT state FROM inbound WHERE alias='mac'").fetchone()[0])
        conn.close()
        return real(*a, **k)

    monkeypatch.setattr(enroll.mutate, "rewrite", watch)
    out = enroll.authorize(tmp_root, "mac", MAC_PUB)
    assert "already" not in out and states == ["active"]  # active throughout the reinstall
    assert b"owner@laptop" in _keys(home).read_bytes() and b"agent-peer-net:mac:" in _keys(home).read_bytes()
    assert _status_ok(tmp_root)
    enroll.revoke(tmp_root, "mac")  # the reinstalled line is the one revoke removes
    assert b"agent-peer-net:mac:" not in _keys(home).read_bytes()


def test_add_twice_reprobes_and_the_outcome_sets_verified(tmp_root, home, remote):
    answer, calls = remote
    _ready(tmp_root)
    enroll.add(tmp_root, "mac", "user", "192.0.2.10", 22, SCANNED_FP)
    pin = ssh.pin_path(tmp_root, "mac").read_bytes()
    answer["ok"] = False  # the re-probe fails this time
    again = enroll.add(tmp_root, "mac", "user", "192.0.2.10", 22, SCANNED_FP)
    assert again["already"] is True and again["verified"] is False
    assert enroll.show(tmp_root)["outbound"][0]["verified"] is False  # cleared, not kept
    assert ssh.pin_path(tmp_root, "mac").read_bytes() == pin
    with pytest.raises(PeerNetError):
        enroll.add(tmp_root, "mac", "user", "192.0.2.11", 22, SCANNED_FP)  # a different host


def test_on_and_off_twice_are_noops(tmp_root, home):
    _ready(tmp_root)
    state = _state(tmp_root)
    enroll.set_enabled(tmp_root, True)
    assert _state(tmp_root) == state
    enroll.set_enabled(tmp_root, False)
    enroll.set_enabled(tmp_root, False)
    assert enroll.show(tmp_root)["enabled"] is False


def test_another_account_is_refused(tmp_root, home, monkeypatch):
    _ready(tmp_root)
    monkeypatch.setattr(system, "account_id", lambda: "uid:99999")
    for call in (
        lambda: enroll.authorize(tmp_root, "mac", MAC_PUB),
        lambda: enroll.set_enabled(tmp_root, False),
    ):
        with pytest.raises(PeerNetError) as caught:
            call()
        assert caught.value.code == "wrong-account"


@pytest.mark.skipif(os.name == "nt", reason="POSIX euid")
def test_root_is_refused(tmp_root, home, monkeypatch):
    monkeypatch.setattr(system.os, "geteuid", lambda: 0)
    with pytest.raises(PeerNetError) as caught:
        enroll.init(tmp_root, "windows")
    assert caught.value.code == "root" and not store.exists(tmp_root)


def _forget_account(root: Path) -> None:
    conn = store.connect(root)
    with store.transaction(conn):
        conn.execute("DELETE FROM enrollment WHERE k='account'")  # a store from before identity
    conn.close()


def _account(root: Path):
    conn = store.connect(root)
    row = conn.execute("SELECT v FROM enrollment WHERE k='account'").fetchone()
    adoptions = conn.execute("SELECT payload FROM intents WHERE op='adopt-account'").fetchall()
    conn.close()
    return row, [json.loads(a[0]) for a in adoptions]


def test_an_old_store_adopts_once_and_keeps_grants_routes_and_off(tmp_root, home, remote):
    _ready(tmp_root)
    enroll.authorize(tmp_root, "mac", MAC_PUB)
    enroll.add(tmp_root, "mac", "user", "192.0.2.10", 22, SCANNED_FP)
    enroll.set_enabled(tmp_root, False)
    _forget_account(tmp_root)
    before = _state(tmp_root)
    assert before["inbound"] and before["outbound"] and before["enabled"] is False
    enroll.show(tmp_root)  # read-only: never adopts
    assert _account(tmp_root) == (None, [])
    enroll.set_enabled(tmp_root, False)  # the first writing command adopts, journaled once
    row, adoptions = _account(tmp_root)
    assert row == (system.account_id(),) and len(adoptions) == 1
    assert adoptions[0] == {"account": system.account_id(), "open_intents": []}
    assert _state(tmp_root) == before  # grants, routes and the off state are preserved


def test_an_old_store_with_an_open_intent_adopts_only_through_denial(tmp_root, home, monkeypatch):
    _ready(tmp_root)
    real = enroll.authorized.rewrite_in_place

    def die(*a, **k):
        real(*a, **k)
        raise OSError("killed after the write")

    monkeypatch.setattr(enroll.authorized, "rewrite_in_place", die)
    with pytest.raises(OSError):
        enroll.authorize(tmp_root, "mac", MAC_PUB)
    monkeypatch.undo()
    _forget_account(tmp_root)
    with pytest.raises(PeerNetError) as caught:
        enroll.set_enabled(tmp_root, True)  # a granting command cannot re-own an open change
    assert caught.value.code == "intent-open" and _account(tmp_root) == (None, [])
    enroll.set_enabled(tmp_root, False)  # denial may, and records which intents were open
    row, adoptions = _account(tmp_root)
    assert row == (system.account_id(),) and len(adoptions[0]["open_intents"]) == 1


def test_adoption_refuses_an_unsafe_store(tmp_root, home):
    _ready(tmp_root)
    conn = store.connect(tmp_root)
    with store.transaction(conn):
        conn.execute("DELETE FROM enrollment WHERE k='account'")
    conn.close()
    if os.name != "nt":
        os.chmod(tmp_root / ".agent" / "peer-net", 0o755)
        with pytest.raises(PeerNetError) as caught:
            enroll.set_enabled(tmp_root, False)
        assert caught.value.code == "unsafe-permissions"
        conn = store.connect(tmp_root)
        assert conn.execute("SELECT v FROM enrollment WHERE k='account'").fetchone() is None
        conn.close()


def test_intents_helper_begin_in_needs_the_callers_transaction(tmp_root):
    conn = store.connect(tmp_root)
    with store.transaction(conn):
        op = intents.begin_in(conn, "x", "-", {})
    assert intents.get(conn, op) is not None
    conn.close()
