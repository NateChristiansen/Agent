"""`codex_normalize.mcp_result_evidence`: one correlated MCP tool result from `.out` frames, or a reason.

Frame shapes follow the app-server protocol schema the installed client generates
(`codex app-server generate-json-schema`, codex-cli 0.162.1: `ItemCompletedNotification`,
`McpToolCallThreadItem`, `McpToolCallResult`, `TurnCompletedNotification`). They are synthetic:
passing here shows the reader's rules, not that a live Codex worker emits these frames.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from agent_cli.dispatch import codex_normalize
from agent_cli.dispatch.codex_normalize import mcp_result_evidence

THREAD = "019a0000-0000-7000-8000-00000000aaaa"
TURN = "019a0000-0000-7000-8000-00000000bbbb"
NONCE = "n3-0123456789abcdef01234567"


def call(**over: Any) -> dict[str, Any]:
    item = {
        "type": "mcpToolCall",
        "id": "call_read_1",
        "server": "n3grants",
        "tool": "read_nonce",
        "arguments": {},
        "status": "completed",
        "error": None,
        "result": {"content": [{"type": "text", "text": f"n3 fixture nonce: {NONCE}"}]},
        "durationMs": 4,
    }
    item.update(over)
    return item


def completed(item: dict[str, Any], *, thread: str = THREAD, turn: str = TURN) -> dict[str, Any]:
    return {
        "method": "item/completed",
        "params": {"threadId": thread, "turnId": turn, "item": item, "completedAtMs": 1},
    }


def turn_done(status: str = "completed", *, thread: str = THREAD, turn: str = TURN) -> dict[str, Any]:
    return {
        "method": "turn/completed",
        "params": {"threadId": thread, "turn": {"id": turn, "items": [], "status": status}},
    }


def frames(*events: dict[str, Any]) -> list[dict[str, Any]]:
    started = {
        "method": "turn/started",
        "params": {"threadId": THREAD, "turn": {"id": TURN, "items": [], "status": "inProgress"}},
    }
    usage = {"method": "thread/tokenUsage/updated", "params": {"threadId": THREAD, "tokenUsage": {}}}
    return [started, usage, *events]


def evidence(tmp_path: Path, events: list[dict[str, Any]], **kw: Any) -> dict[str, Any]:
    out = tmp_path / "d.out"
    out.write_bytes("".join(json.dumps(e) + "\n" for e in events).encode())
    kw.setdefault("thread_id", THREAD)
    return mcp_result_evidence(out, server="n3grants", tool="read_nonce", needle=NONCE, **kw)


def test_one_completed_call_on_the_recorded_thread_matches(tmp_path: Path):
    got = evidence(tmp_path, frames(completed(call()), turn_done()))
    assert got == {
        "correlated": True,
        "reason": "matched",
        "thread_id": THREAD,
        "item_id": "call_read_1",
        "turn_id": TURN,
    }
    assert NONCE not in json.dumps(got)


def test_a_repeated_identical_completion_counts_once(tmp_path: Path):
    assert evidence(tmp_path, frames(completed(call()), completed(call()), turn_done()))["correlated"]


@pytest.mark.parametrize(
    ("events", "reason"),
    [
        (frames(turn_done()), "missing"),
        (frames(completed(call(server="other")), turn_done()), "missing"),
        (frames(completed(call(tool="write_sentinel")), turn_done()), "missing"),
        (
            frames(
                {
                    "method": "item/started",
                    "params": {
                        "threadId": THREAD,
                        "turnId": TURN,
                        "item": call(status="inProgress"),
                        "startedAtMs": 1,
                    },
                },
                turn_done(),
            ),
            "missing",
        ),
        (
            frames(completed(call(status="failed", error={"message": "denied"}, result=None)), turn_done()),
            "error",
        ),
        (frames(completed(call(error={"message": "boom"})), turn_done()), "malformed"),
        (frames(completed(call(status="inProgress")), turn_done()), "error"),
        (
            frames(completed(call(result={"content": {"type": "text", "text": NONCE}})), turn_done()),
            "malformed",
        ),
        (frames(completed(call(result={"content": ["n3 " + NONCE]})), turn_done()), "malformed"),
        (frames(completed(call(result=None)), turn_done()), "malformed"),
        (
            frames(
                completed(
                    call(
                        result={
                            "content": [{"type": "text", "text": "no value"}],
                            "structuredContent": {"nonce": NONCE},
                        }
                    )
                ),
                turn_done(),
            ),
            "mismatch",
        ),
        (
            frames(completed(call(result={"content": [{"type": "resource", "text": NONCE}]})), turn_done()),
            "mismatch",
        ),
        (frames(completed(call()), turn_done("interrupted")), "incomplete"),
        (frames(completed(call())), "incomplete"),
        (frames(completed(call(), thread="019a-other"), turn_done()), "foreign"),
        (frames(completed(call()), turn_done(thread="019a-other")), "foreign"),
        (
            frames({"method": "item/completed", "params": {"turnId": TURN, "item": call()}}, turn_done()),
            "foreign",
        ),
        (
            frames(
                completed(call()), completed(call(server="n3grants", result={"content": []})), turn_done()
            ),
            "conflicting",
        ),
        (frames(completed(call()), completed(call(id="call_read_2")), turn_done()), "ambiguous"),
        (frames(completed(call(id="call read; rm")), turn_done()), "malformed"),
        (frames(completed(call(), turn=""), turn_done()), "malformed"),
        (frames({"method": "item/completed", "params": "x"}), "malformed"),
    ],
)
def test_wrong_or_incomplete_evidence_stays_uncorrelated(
    tmp_path: Path, events: list[dict[str, Any]], reason: str
):
    got = evidence(tmp_path, events)
    assert got["correlated"] is False
    assert got["item_id"] is None
    assert got["reason"].startswith(reason), got["reason"]
    assert NONCE not in json.dumps(got)


def without(frame: dict[str, Any], *path: str) -> dict[str, Any]:
    """`frame` with the field at `path` (keys under `params`) removed."""
    copy = json.loads(json.dumps(frame))
    node = copy["params"]
    for key in path[:-1]:
        node = node[key]
    del node[path[-1]]
    return copy


def with_value(frame: dict[str, Any], value: Any, *path: str) -> dict[str, Any]:
    copy = json.loads(json.dumps(frame))
    node = copy["params"]
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return copy


def turn_with(**over: Any) -> dict[str, Any]:
    frame = turn_done()
    frame["params"]["turn"].update(over)
    return frame


@pytest.mark.parametrize(
    ("events", "reason"),
    [
        # the reviewer's reproductions: the same turn completed again as failed, or with an error
        (frames(completed(call()), turn_done(), turn_done("failed")), "conflicting"),
        (frames(completed(call()), turn_done("failed"), turn_done()), "conflicting"),
        (frames(completed(call()), turn_with(error={"message": "turn failed"})), "error"),
        # ... and required native fields missing
        (frames(without(completed(call()), "item", "arguments"), turn_done()), "malformed"),
        (frames(without(completed(call()), "completedAtMs"), turn_done()), "malformed"),
        # every other consumed required field missing or of the wrong type
        (frames(with_value(completed(call()), "1", "completedAtMs"), turn_done()), "malformed"),
        (frames(with_value(completed(call()), True, "completedAtMs"), turn_done()), "malformed"),
        (frames(without(completed(call()), "turnId"), turn_done()), "malformed"),
        (frames(without(completed(call()), "item"), turn_done()), "malformed"),
        (frames(without(completed(call()), "item", "server"), turn_done()), "malformed"),
        (frames(without(completed(call()), "item", "tool"), turn_done()), "malformed"),
        (frames(without(completed(call()), "item", "status"), turn_done()), "malformed"),
        (frames(without(completed(call()), "item", "type"), turn_done()), "malformed"),
        (frames(with_value(completed(call()), "done", "item", "status"), turn_done()), "malformed"),
        (frames(with_value(completed(call()), "boom", "item", "error"), turn_done()), "malformed"),
        (frames(with_value(completed(call()), "4", "item", "durationMs"), turn_done()), "malformed"),
        (frames(with_value(completed(call()), {}, "item", "result"), turn_done()), "malformed"),
        (frames(with_value(completed(call()), 7, "item", "server"), turn_done()), "malformed"),
        (frames(completed(call()), without(turn_done(), "turn", "status")), "malformed"),
        (frames(completed(call()), without(turn_done(), "turn", "items")), "malformed"),
        (frames(completed(call()), without(turn_done(), "turn", "id")), "malformed"),
        (frames(completed(call()), turn_with(status="finished")), "malformed"),
        (frames(completed(call()), turn_with(error="boom")), "malformed"),
        (frames(completed(call()), with_value(turn_done(), None, "turn")), "malformed"),
        (
            [
                {
                    "method": "item/started",
                    "params": {"threadId": THREAD, "turnId": TURN, "item": call(status="inProgress")},
                },
                completed(call()),
                turn_done(),
            ],
            "malformed",
        ),
        # contradictions carried in the completed turn's own items
        (
            frames(completed(call()), turn_with(items=[call(status="failed", error={"message": "x"})])),
            "conflicting",
        ),
        (frames(completed(call()), turn_with(items=[call(id="call_read_2")])), "ambiguous"),
        (frames(turn_done(), completed(call())), "conflicting"),
        (frames(completed(call()), turn_with(items=[{"type": "agentMessage"}])), "malformed"),
    ],
)
def test_contradictory_or_off_schema_evidence_stays_uncorrelated(
    tmp_path: Path, events: list[dict[str, Any]], reason: str
):
    got = evidence(tmp_path, events)
    assert got["correlated"] is False and got["item_id"] is None
    assert got["reason"].startswith(reason), got["reason"]
    assert NONCE not in json.dumps(got)


@pytest.mark.parametrize(
    "events",
    [
        frames(completed(call()), turn_done(), turn_done()),
        frames(completed(call()), turn_with(items=[call()])),
        frames(completed(call(durationMs=None, error=None)), turn_with(error=None)),
        frames(completed({k: v for k, v in call().items() if k not in ("error", "durationMs")}), turn_done()),
        frames(completed(call(arguments=None)), turn_done()),
    ],
)
def test_valid_variants_still_correlate(tmp_path: Path, events: list[dict[str, Any]]):
    got = evidence(tmp_path, events)
    assert got["correlated"] is True, got["reason"]


def started(item: dict[str, Any], *, turn: str = TURN) -> dict[str, Any]:
    return {
        "method": "item/started",
        "params": {"threadId": THREAD, "turnId": turn, "item": item, "startedAtMs": 1},
    }


def in_progress(**over: Any) -> dict[str, Any]:
    return call(status="inProgress", result=None, durationMs=None, **over)


OTHER_TURN = "019a0000-0000-7000-8000-00000000cccc"


@pytest.mark.parametrize(
    "events",
    [
        # the reviewer's reproduction: started as the sentinel, completed as read_nonce
        frames(started(in_progress(tool="write_sentinel")), completed(call()), turn_done()),
        frames(started(in_progress(server="other")), completed(call()), turn_done()),
        frames(started(in_progress(type="dynamicToolCall")), completed(call()), turn_done()),
        frames(started(in_progress(), turn=OTHER_TURN), completed(call()), turn_done()),
        frames(
            started(in_progress()),
            started(in_progress(tool="write_sentinel")),
            completed(call()),
            turn_done(),
        ),
        frames(started(in_progress(tool="write_sentinel")), turn_with(items=[call()])),
        frames(completed(call()), started(in_progress()), turn_done()),
    ],
)
def test_one_item_id_with_two_identities_is_conflicting(tmp_path: Path, events: list[dict[str, Any]]):
    got = evidence(tmp_path, events)
    assert got["correlated"] is False and got["item_id"] is None
    assert got["reason"].startswith("conflicting"), got["reason"]


@pytest.mark.parametrize(
    "events",
    [
        frames(started(in_progress()), completed(call()), turn_done()),
        frames(started(in_progress()), started(in_progress()), completed(call()), turn_done()),
        frames(started(in_progress()), completed(call()), turn_with(items=[call()])),
        frames(
            started(in_progress(id="call_sentinel", tool="write_sentinel")),
            started(in_progress()),
            completed(call()),
            turn_done(),
        ),
    ],
)
def test_a_started_call_that_completes_with_the_same_identity_correlates(
    tmp_path: Path, events: list[dict[str, Any]]
):
    got = evidence(tmp_path, events)
    assert got["correlated"] is True, got["reason"]


def test_a_sentinel_call_sharing_the_read_id_is_conflicting(tmp_path: Path):
    events = frames(completed(call()), completed(call(tool="write_sentinel")), turn_done())
    assert evidence(tmp_path, events)["reason"].startswith("conflicting")


def test_unparsable_frame_rejects_the_whole_output(tmp_path: Path):
    out = tmp_path / "d.out"
    out.write_text(
        json.dumps(completed(call())) + "\n{not json\n" + json.dumps(turn_done()) + "\n", encoding="utf-8"
    )
    got = mcp_result_evidence(out, thread_id=THREAD, server="n3grants", tool="read_nonce", needle=NONCE)
    assert got["reason"].startswith("malformed")


@pytest.mark.parametrize("thread", ["", "has space", "x" * 200])
def test_no_valid_recorded_thread_is_unavailable(tmp_path: Path, thread: str):
    got = evidence(tmp_path, frames(completed(call()), turn_done()), thread_id=thread)
    assert got["reason"].startswith("unavailable")
    assert got["thread_id"] is None


def test_missing_output_and_empty_needle_are_unavailable(tmp_path: Path):
    got = mcp_result_evidence(
        tmp_path / "none.out", thread_id=THREAD, server="n3grants", tool="read_nonce", needle=NONCE
    )
    assert got["reason"].startswith("unavailable")
    out = tmp_path / "d.out"
    out.write_text(json.dumps(completed(call())) + "\n", encoding="utf-8")
    empty = mcp_result_evidence(out, thread_id=THREAD, server="n3grants", tool="read_nonce", needle="")
    assert empty["reason"].startswith("unavailable")


def test_oversized_output_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    events = frames(completed(call()), turn_done())
    size = len("".join(json.dumps(e) + "\n" for e in events).encode())
    monkeypatch.setattr(codex_normalize, "MAX_EVIDENCE_BYTES", size - 1)
    assert evidence(tmp_path, events)["reason"].startswith("oversized")
    monkeypatch.setattr(codex_normalize, "MAX_EVIDENCE_BYTES", size)
    assert evidence(tmp_path, events)["correlated"]


def test_non_utf8_output_is_malformed(tmp_path: Path):
    out = tmp_path / "d.out"
    out.write_bytes(json.dumps(completed(call())).encode() + b"\n\xff\xfe\n")
    got = mcp_result_evidence(out, thread_id=THREAD, server="n3grants", tool="read_nonce", needle=NONCE)
    assert got["reason"].startswith("malformed")
