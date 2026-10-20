"""The receiver wakes a Codex recipient with the launch `peer-net init` recorded, never with
sshd's minimal PATH (a wake failed because
/opt/homebrew/bin is not on that PATH)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_cli.ops import peers as ops_peers
from agent_cli.peer_net import enroll, receiver, store, wake_launch
from peer_net_helpers import enable

posix = pytest.mark.skipif(os.name == "nt", reason="POSIX launch forms")


def _exe(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(0o755)
    return path


@pytest.fixture
def node_install(tmp_path: Path) -> dict[str, Path]:
    """A Homebrew-like install: `codex` is a Node script, and `node` records its argv."""
    bin_dir = tmp_path / "homebrew" / "bin"
    bin_dir.mkdir(parents=True)
    out = tmp_path / "woken"
    node = _exe(bin_dir / "node", f'#!/bin/sh\necho "PATH=$PATH" > "{out}"\necho "$@" >> "{out}"\nexit 0\n')
    codex = _exe(bin_dir / "codex", "#!/usr/bin/env node\nconsole.log('codex')\n")
    return {"bin": bin_dir, "node": node, "codex": codex, "out": out}


@posix
def test_a_node_script_codex_is_pinned_to_its_runtime(monkeypatch, node_install):
    monkeypatch.setenv("PATH", str(node_install["bin"]))
    launch = wake_launch.resolve()
    assert launch["argv"] == [os.path.realpath(node_install["node"]), os.path.realpath(node_install["codex"])]
    assert launch["path_dirs"] == [os.path.realpath(node_install["bin"])]


@posix
def test_a_native_codex_is_its_own_argv(monkeypatch, tmp_path):
    native = _exe(tmp_path / "codex", "#!/bin/sh\nexit 0\n")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert wake_launch.resolve()["argv"] == [os.path.realpath(native)]


def test_no_codex_records_nothing_and_init_still_succeeds(tmp_root, monkeypatch, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    enable(tmp_root)
    conn = store.connect(tmp_root)
    try:
        wake_launch.record(conn, {"argv": ["/x"], "path_dirs": []})  # a stale record is removed
        assert enroll._codex_wake(conn).startswith("unavailable:")
        assert conn.execute("SELECT 1 FROM enrollment WHERE k=?", (wake_launch.KEY,)).fetchone() is None
    finally:
        conn.close()


@posix
def test_the_receiver_wakes_codex_under_sshds_minimal_path(tmp_root, monkeypatch, node_install):
    monkeypatch.setenv("PATH", str(node_install["bin"]))
    enable(tmp_root)
    conn = store.connect(tmp_root)
    try:
        wake_launch.record(conn, wake_launch.resolve())
        monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")  # what sshd gives the receiver
        before = dict(os.environ)
        launch = receiver._codex_launch(conn)
    finally:
        conn.close()
    target = {"session_id": "cx-1", "provider": "codex", "activity": {"state": "idle"}}
    accepted, text = ops_peers.wake_peer(target, "mgr-cx", launch)
    assert accepted and "codex queue accepted" in text
    woken = node_install["out"].read_text().splitlines()
    assert woken[0].startswith("PATH=" + os.path.realpath(node_install["bin"]))
    assert woken[1].split()[:4] == [os.path.realpath(node_install["codex"]), "queue", "--thread", "cx-1"]
    assert dict(os.environ) == before  # set for the child only, never this process


def test_without_a_recorded_launch_the_wake_fails_closed(tmp_root, monkeypatch):
    enable(tmp_root)
    conn = store.connect(tmp_root)
    try:
        launch = receiver._codex_launch(conn)
    finally:
        conn.close()
    assert launch is ops_peers.REMOTE_WAKE

    def must_not_run(*a, **k):
        raise AssertionError("the receiver fell back to its own PATH")

    monkeypatch.setattr(ops_peers, "_run", must_not_run)
    accepted, text = ops_peers.wake_peer({"session_id": "cx-1", "provider": "codex"}, "mgr-cx", launch)
    assert not accepted and "next turn" in text


@posix
def test_a_drifted_launch_is_refused_at_use(tmp_root, monkeypatch, node_install, tmp_path):
    monkeypatch.setenv("PATH", str(node_install["bin"]))
    enable(tmp_root)
    conn = store.connect(tmp_root)
    try:
        wake_launch.record(conn, wake_launch.resolve())
        assert wake_launch.load(conn) is not None
        other = _exe(tmp_path / "other.js", "#!/usr/bin/env node\n")
        node_install["codex"].unlink()
        node_install["codex"].symlink_to(other)  # the recorded script now resolves elsewhere
        assert wake_launch.load(conn) is None
        node_install["codex"].unlink()  # and removed entirely
        assert wake_launch.load(conn) is None
    finally:
        conn.close()


@pytest.mark.parametrize(
    "stored",
    [
        "not json",
        json.dumps({"argv": "x", "path_dirs": []}),
        json.dumps({"argv": [], "path_dirs": []}),
        json.dumps({"argv": [1], "path_dirs": []}),
        json.dumps({"path_dirs": []}),
    ],
)
def test_a_malformed_record_is_no_launch(tmp_root, stored):
    enable(tmp_root)
    conn = store.connect(tmp_root)
    try:
        with store.transaction(conn):
            conn.execute("INSERT OR REPLACE INTO enrollment (k, v) VALUES (?, ?)", (wake_launch.KEY, stored))
        assert wake_launch.load(conn) is None
    finally:
        conn.close()


@posix
def test_only_the_exact_supported_shapes_are_a_launch(node_install, tmp_path):
    node, codex = os.path.realpath(node_install["node"]), os.path.realpath(node_install["codex"])
    good = {"argv": [node, codex], "path_dirs": [os.path.dirname(node)]}
    assert wake_launch.valid(good)
    shim = _exe(tmp_path / "codex.cmd", "@echo off\n")
    plain = _exe(tmp_path / "tool", "#!/bin/sh\n")
    bad = [
        {**good, "path_dirs": [""]},  # would put the working directory on PATH
        {**good, "path_dirs": ["relative/bin"]},
        {**good, "path_dirs": [os.path.dirname(node), "/somewhere/else"]},  # not argv's parents
        {**good, "extra": 1},
        {"argv": [node, codex, codex], "path_dirs": good["path_dirs"]},
        {"argv": [codex, node], "path_dirs": good["path_dirs"]},  # a runtime that is not node
        {
            "argv": [node, os.path.realpath(plain)],
            "path_dirs": sorted({os.path.dirname(node), str(tmp_path)}),
        },
        {"argv": [os.path.realpath(shim)], "path_dirs": [str(tmp_path)]},  # a shell shim
        {"argv": [node + "\x00"], "path_dirs": [os.path.dirname(node)]},
        {"argv": ["relative/codex"], "path_dirs": ["relative"]},
        {"argv": ["/" + "x" * 5000], "path_dirs": ["/"]},
        {"argv": [], "path_dirs": []},
    ]
    for config in bad:
        assert not wake_launch.valid(config), config


def test_deeply_nested_json_is_no_launch(tmp_root):
    enable(tmp_root)
    conn = store.connect(tmp_root)
    try:
        with store.transaction(conn):
            conn.execute(
                "INSERT OR REPLACE INTO enrollment (k, v) VALUES (?, ?)",
                (wake_launch.KEY, "[" * 5000 + "]" * 5000),
            )
        assert wake_launch.load(conn) is None
    finally:
        conn.close()


def test_a_bad_codex_record_never_touches_a_claude_wake(tmp_root, monkeypatch):
    from agent_cli.peer_net import receive
    from peer_net_helpers import NOW, envelope, raw, recipient

    enable(tmp_root)
    recipient(tmp_root, sid="cl-recipient", name="mgr-recipient", provider="claude")

    def boom(conn):
        raise AssertionError("loaded the Codex launch for a Claude wake")

    monkeypatch.setattr(wake_launch, "load", boom)
    seen = []

    def fake_wake(target, name, codex_launch=None):
        seen.append(codex_launch)
        return True, "rang"

    monkeypatch.setattr(ops_peers, "wake_peer", fake_wake)
    monkeypatch.setattr("agent_cli.ops.peers.is_alive", lambda pid: True)
    answer = receive(tmp_root, "mac", raw(envelope()), clock=lambda: NOW)
    assert answer["ok"] is True and answer["wake"] == "accepted" and seen == [None]


def test_a_local_wake_still_uses_the_senders_own_codex(monkeypatch):
    seen = []
    monkeypatch.setattr("agent_cli.codex_executable.command", lambda *args: ["/local/codex", *args])
    monkeypatch.setattr(ops_peers, "_run", lambda argv, env=None: seen.append((argv, env)) or True)
    accepted, _ = ops_peers.wake_peer({"session_id": "cx-1", "provider": "codex"}, "mgr-cx")
    assert accepted and seen[0][0][:2] == ["/local/codex", "queue"] and seen[0][1] is None
