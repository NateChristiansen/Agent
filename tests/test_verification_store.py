"""Strict durable metadata types: bool/int equality must not authorize a run."""

from __future__ import annotations

import sys
import threading
from dataclasses import asdict

import pytest

from agent_cli.dispatch.verification_evidence import Binding
from agent_cli.dispatch.verification_store import Store


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path, Binding("001-01", 1, "a" * 40, "b" * 64, "c" * 32), create=True)
    try:
        yield value
    finally:
        value.close()


def lifecycle():
    return {
        "phase": "finished",
        "creator_pid": 17,
        "guardian_pid": 16,
        "command_pid": 18,
        "watchdog_pid": 19,
        "command_index": 0,
        "cleanup_confirmed": True,
        "finished": True,
        "reason": "commands_complete",
        "started": 1.25,
    }


def mutate(raw, key, value):
    if key.startswith("binding."):
        raw["binding"][key.split(".")[1]] = value
    else:
        raw[key] = value


@pytest.mark.parametrize(
    "key,value",
    [
        ("schema_version", True),
        ("schema_version", 1.0),
        ("binding.attempt", True),
        ("binding.attempt", 1.0),
        ("binding.unrecognized", "extra"),
        ("creator_pid", None),
        ("creator_pid", True),
        ("guardian_pid", 16.0),
        ("command_pid", False),
        ("watchdog_pid", 19.0),
        ("command_index", False),
        ("cleanup_confirmed", 1),
        ("finished", 1),
        ("phase", []),
        ("reason", []),
        ("started", True),
        ("unrecognized", "extra"),
        ("error", None),
        ("error", "AgentError: raw text"),
        ("error", {"step": "observe_tree", "type": "AgentError"}),
        ("error", {"step": "elsewhere", "type": "AgentError", "message": None}),
        ("error", {"step": "observe_tree", "type": "not a class", "message": None}),
        ("error", {"step": "observe_tree", "type": "AgentError", "message": 7}),
        ("error", {"step": "observe_tree", "type": "AgentError", "message": "x" * 501}),
        ("error", {"step": "observe_tree", "type": "AgentError", "message": None, "detail": "x"}),
    ],
)
def test_lifecycle_rejects_malformed_equal_values(store, key, value):
    store.lifecycle(lifecycle())
    raw = store.read("lifecycle.json")
    mutate(raw, key, value)
    store.write("lifecycle.json", raw)
    with pytest.raises(ValueError):
        store.load_lifecycle()


@pytest.mark.parametrize(
    "key,value",
    [
        ("schema_version", True),
        ("schema_version", 1.0),
        ("binding.attempt", True),
        ("binding.attempt", 1.0),
        ("binding.unrecognized", "extra"),
        ("command_index", False),
        ("command_index", 0.0),
        ("watchdog_pid", 19.0),
        ("command_pgid", 18.0),
        ("cleanup_confirmed", 1),
        ("reason", []),
        ("unrecognized", "extra"),
    ],
)
def test_cleanup_receipt_rejects_malformed_equal_values(store, key, value):
    raw = {
        "schema_version": 1,
        "binding": asdict(store.binding),
        "command_index": 0,
        "watchdog_pid": 19,
        "command_pgid": 18,
        "cleanup_confirmed": True,
        "reason": "released",
    }
    mutate(raw, key, value)
    store.write("cleanup-00000.json", raw)
    with pytest.raises(ValueError):
        store.cleanup_record(lifecycle())


def test_lifecycle_writer_cannot_override_reserved_binding(store):
    with pytest.raises(ValueError, match="reserved"):
        store.lifecycle({**lifecycle(), "binding": asdict(store.binding)})
    with pytest.raises(ValueError, match="reserved"):
        store.lifecycle({**lifecycle(), "schema_version": 99})


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows refuses to replace an open file")
def test_a_lifecycle_write_waits_out_a_reader_holding_the_file(store):
    store.lifecycle(lifecycle())
    opened, done = threading.Event(), threading.Event()

    def reader():
        with open(store.path / "lifecycle.json", "rb"):
            opened.set()
            done.wait(0.2)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        assert opened.wait(5)
        store.lifecycle({**lifecycle(), "reason": "runner_error"})
    finally:
        done.set()
        thread.join()
    assert store.load_lifecycle()["reason"] == "runner_error"


def test_lifecycle_error_is_optional(store):
    store.write("lifecycle.json", {"schema_version": 1, "binding": asdict(store.binding), **lifecycle()})
    assert "error" not in store.load_lifecycle()
    failed = {
        **lifecycle(),
        "reason": "runner_error",
        "error": {
            "step": "observe_tree",
            "type": "AgentError",
            "message": "verification requires an existing commit",
        },
    }
    store.lifecycle(failed)
    assert store.load_lifecycle()["error"] == failed["error"]
    store.lifecycle({**failed, "error": {"step": "evidence", "type": "OSError", "message": None}})
    assert store.load_lifecycle()["error"]["message"] is None


def test_typed_metadata_round_trip(store):
    store.lifecycle(lifecycle())
    assert store.load_lifecycle()["command_index"] == 0
    raw = {
        "schema_version": 1,
        "binding": asdict(store.binding),
        "command_index": 0,
        "watchdog_pid": 19,
        "command_pgid": 18,
        "cleanup_confirmed": True,
        "reason": "released",
    }
    store.write("cleanup-00000.json", raw, immutable=True)
    assert store.cleanup_record(lifecycle()) == raw
