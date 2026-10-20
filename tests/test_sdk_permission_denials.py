"""Claude's own `permission_denials` reach the record, `.out`, `dispatch show` and the handoff only
as `state.shown_denials` keeps them: a shown tool name, a Bash command with endpoints and credentials
withheld, no other argument.

The SDK hands the result envelope over raw. A server named by its endpoint puts that endpoint in
every tool name, and a denied command carries its arguments, credentials included. Both used to be
stored and printed verbatim; records written that way still exist and render through the same
helper."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock

from agent_cli import dispatch, features, ops
from agent_cli.dispatch import handoffs, normalize, state, terminal
from agent_cli.ops import dispatch as ops_dispatch
from dispatch_helpers import _new_fast, droot, envelope, one  # noqa: F401

SECRET = "secret.example"
TOKEN = "zq7token"
ENDPOINT = f"https://{SECRET}/{TOKEN}"
LEAKS = (SECRET, TOKEN, "https", "Bearer", "Web Fetch")
CURL = 'curl -H "[redacted] [redacted-endpoint] -o out'
PUSH = "API_TOKEN=[redacted] git push origin main"

# each raw SDK entry, the entry every store keeps, and the line every view prints
CASES: list[tuple[object, object, str]] = [
    (
        {
            "tool_name": "Bash",
            "tool_use_id": "toolu_01safe",
            "tool_input": {"command": f'curl -H "Authorization: Bearer {TOKEN}" {ENDPOINT} -o out'},
        },
        {"tool_name": "Bash", "tool_use_id": "toolu_01safe", "command": CURL, "input_omitted": True},
        CURL,
    ),
    (
        {"tool_name": "Bash", "tool_input": {"command": f"API_TOKEN={TOKEN} git push origin main"}},
        {"tool_name": "Bash", "command": PUSH, "input_omitted": True},
        PUSH,
    ),
    (
        {"tool_name": f"mcp__{ENDPOINT}__read", "tool_use_id": ENDPOINT, "tool_input": {"command": ENDPOINT}},
        {"tool_name": "[redacted-mcp-tool]", "input_omitted": True},
        "[redacted-mcp-tool]",
    ),
    (
        {"tool_name": "mcp__helper__run", "tool_input": {"command": f"curl {ENDPOINT}"}},
        {"tool_name": "mcp__helper__run", "input_omitted": True},
        "mcp__helper__run",
    ),
    (
        {"tool_name": "WebFetch", "tool_input": {"url": ENDPOINT}},
        {"tool_name": "WebFetch", "input_omitted": True},
        "WebFetch",
    ),
    (
        {"tool_name": f"Web Fetch({ENDPOINT})", "tool_input": {}},
        {"tool_name": "[redacted-tool]", "input_omitted": True},
        "[redacted-tool]",
    ),
    (
        {"tool_name": None, "tool_input": {"command": ENDPOINT}},
        {"tool_name": "", "input_omitted": True},
        "(unnamed tool)",
    ),
    (
        {"tool_name": 7, "tool_input": ENDPOINT, "extra": ENDPOINT},
        {"tool_name": "7", "input_omitted": True},
        "7",
    ),
    (f"curl {ENDPOINT}?token={TOKEN}", "curl [redacted-endpoint]", "curl [redacted-endpoint]"),
    (
        "item/commandExecution/requestApproval",
        "item/commandExecution/requestApproval",
        "item/commandExecution/requestApproval",
    ),
    (
        {"tool_name": "Bash", "tool_input": {"command": "git status --short"}},
        {"tool_name": "Bash", "command": "git status --short", "input_omitted": True},
        "git status --short",
    ),
]
RAW = [raw for raw, _, _ in CASES]
STORED = [stored for _, stored, _ in CASES]
SHOWN = [shown for _, _, shown in CASES]
# a `last_result` or `pending_outcome` that is not an envelope: no view carries it
MALFORMED: list[object] = [
    f'curl -H "Authorization: Bearer {TOKEN}" {ENDPOINT}',
    RAW,
    [{"permission_denials": RAW, "url": {"nested": ENDPOINT}}],
    7,
    "garbled",
]


def _no_leak(text: str) -> None:
    for leak in LEAKS:
        assert leak not in text, leak


def _result(denials: object) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        permission_denials=denials,  # pyright: ignore[reportArgumentType]
    )


def test_the_envelope_keeps_only_shown_denials_and_leaves_the_sdk_message_alone():
    msg = _result(copy.deepcopy(RAW))
    env = normalize.result_envelope(msg)
    assert env["permission_denials"] == STORED
    assert normalize.out_record(msg)["permission_denials"] == STORED
    assert msg.permission_denials == RAW
    _no_leak(json.dumps(env))


def test_shown_denials_are_stable_and_survive_malformed_envelopes():
    assert state.shown_denials(STORED) == STORED
    assert state.shown_denials(state.shown_denials(RAW)) == STORED
    assert state.shown_denials(None) == [] and state.shown_denials([]) == []
    assert state.shown_denials({"tool_name": f"mcp__{ENDPOINT}__x"}) == [
        {"tool_name": "[redacted-mcp-tool]", "input_omitted": True}
    ]
    assert state.shown_denials(f"mcp__{ENDPOINT}__x") == ["mcp__[redacted-endpoint]"]
    assert state.shown_denials([None, [ENDPOINT], ""]) == ["None", "['[redacted-endpoint]", ""]
    assert state.permission_denials({"permission_denials": [""]}) == ["(unnamed tool)"]


def test_a_live_dispatch_stores_and_shows_only_shown_denials(droot: Path, fake_claude, run):
    q = envelope("question", summary="denied")
    q["permission_denials"] = RAW
    fake_claude.scenario([one(q), one(envelope())])
    d = _new_fast(droot)
    assert d["state"] == "blocked" and f"{len(RAW)} denied command(s)" in d["error"]

    record = dispatch.file(droot, d["id"], ".json").read_text(encoding="utf-8")
    stored = json.loads(record)
    assert stored["permission_denials"] == STORED
    assert stored["last_result"]["permission_denials"] == STORED
    out = dispatch.file(droot, d["id"], ".out").read_text(encoding="utf-8")
    [result] = [json.loads(line) for line in out.splitlines() if json.loads(line)["type"] == "result"]
    assert result["permission_denials"] == STORED
    handoff = (droot / d["handoff"]).read_text(encoding="utf-8")
    assert f"permission denials: {len(RAW)}\n\n" + "\n".join(f"- `{s}`" for s in SHOWN) in handoff

    shown = run(["--json", "dispatch", "show", d["id"]])
    assert shown.json["permission_denials"] == STORED
    assert shown.json["last_result"]["permission_denials"] == STORED
    text = run(["dispatch", "show", d["id"]]).out
    assert f"  permission denials: {len(RAW)}\n" + "\n".join(f"    {s}" for s in SHOWN) in text
    for written in (record, out, handoff, shown.out, text):
        _no_leak(written)


def _historical(root: Path, fake_claude) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    """A finished dispatch whose record holds claude's raw denials, as one written before them."""
    fake_claude.scenario([one(envelope())])
    d = _new_fast(root)
    path = dispatch.file(root, d["id"], ".json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["permission_denials"] = RAW
    raw["last_result"] = {**raw["last_result"], "permission_denials": RAW}
    raw["pending_outcome"] = {"state": "blocked", "permission_denials": RAW}
    path.write_text(json.dumps(raw), encoding="utf-8")
    return d, path, raw


def test_a_historical_raw_record_renders_shown_and_actions_return_it_shown(droot: Path, fake_claude, run):
    d, path, raw = _historical(droot, fake_claude)
    shown = run(["--json", "dispatch", "show", d["id"]])
    for data in (shown.json, shown.json["last_result"], shown.json["pending_outcome"]):
        assert data["permission_denials"] == STORED
    text = run(["dispatch", "show", d["id"]]).out
    assert "\n".join(f"    {s}" for s in SHOWN) in text
    page = handoffs.render_handoff(droot, raw, features.resolve(droot, "1"))
    assert "\n".join(f"- `{s}`" for s in SHOWN) in page
    for written in (shown.out, text, page):
        _no_leak(written)
    assert json.loads(path.read_text(encoding="utf-8"))["permission_denials"] == RAW, "show only reads"

    # a `last_result` or `pending_outcome` that is not an envelope is omitted from show
    for bad in MALFORMED:
        broken = {**raw, "last_result": copy.deepcopy(bad), "pending_outcome": copy.deepcopy(bad)}
        path.write_text(json.dumps(broken), encoding="utf-8")
        shown = run(["--json", "dispatch", "show", d["id"]])
        assert shown.code == 0, (bad, shown.err)
        assert shown.json["last_result"] is None and shown.json["pending_outcome"] is None
        assert shown.json["permission_denials"] == STORED
        text = run(["dispatch", "show", d["id"]])
        assert text.code == 0, (bad, text.err)
        for written in (shown.out, text.out):
            _no_leak(written)
        kept = json.loads(path.read_text(encoding="utf-8"))
        assert (kept["last_result"], kept["pending_outcome"], kept["permission_denials"]) == (bad, bad, RAW)
    path.write_text(json.dumps(raw), encoding="utf-8")

    collected = run(["--json", "dispatch", "collect", d["id"]])
    assert [r["id"] for r in collected.json] == [d["id"]] and "permission_denials" not in collected.json[0]
    acked = run(["--json", "dispatch", "ack", d["id"]])
    for data in (acked.json, acked.json["last_result"], acked.json["pending_outcome"]):
        assert data["permission_denials"] == STORED
    for returned in (collected, acked):
        _no_leak(returned.out)


RECORD = {
    **{"id": "001-mac-01", "short": "alpha", "state": "done", "branch": "b", "budget_usd": 1.0, "attempt": 1},
    **{"abandoned_from": "done", "pending": False, "permission_denials": RAW},
    **{"last_result": {"permission_denials": RAW}, "pending_outcome": {"permission_denials": RAW}},
}
OPS = {
    "cancel": {},
    "abandon": {"note": "n"},
    "ack": {},
    "say": {"message": "m"},
    "new": {"repo": "a", "prompt": "p"},
}


@pytest.mark.parametrize("action", OPS)
def test_every_op_returning_a_record_shows_its_denials(tmp_path: Path, monkeypatch, action):
    monkeypatch.setattr(dispatch, action, lambda _root, **_kw: copy.deepcopy(RECORD))
    target = {} if action == "new" else {"dispatch_id": RECORD["id"]}
    op = getattr(ops_dispatch, f"dispatch_{action}")(tmp_path, **target, **OPS[action])
    for data in (op.data, op.data["last_result"], op.data["pending_outcome"]):
        assert data["permission_denials"] == STORED, action
    _no_leak(json.dumps(op.data))


def test_resume_returns_its_record_shown(tmp_path: Path, monkeypatch):
    raw = copy.deepcopy(RECORD)
    monkeypatch.setattr(dispatch, "resume", lambda _root, **_kw: raw)
    op = ops.dispatch_resume(tmp_path, dispatch_id=RECORD["id"], answer="a")
    shown = {"permission_denials": STORED}
    assert op.data == {**RECORD, **shown, "last_result": shown, "pending_outcome": shown}
    assert raw == RECORD, "the record resume returned is not changed"
    monkeypatch.setattr(dispatch, "resume", lambda _root, **_kw: copy.deepcopy(op.data))
    assert ops.dispatch_resume(tmp_path, dispatch_id=RECORD["id"], answer="a").data == op.data
    _no_leak(json.dumps(op.data) + op.text)


@pytest.mark.parametrize("bad", MALFORMED, ids=range(len(MALFORMED)))
def test_resume_omits_a_malformed_result_or_outcome(tmp_path: Path, monkeypatch, bad):
    raw = {**copy.deepcopy(RECORD), "last_result": copy.deepcopy(bad), "pending_outcome": copy.deepcopy(bad)}
    before = copy.deepcopy(raw)
    monkeypatch.setattr(dispatch, "resume", lambda _root, **_kw: raw)
    op = ops.dispatch_resume(tmp_path, dispatch_id=RECORD["id"], answer="a")
    omitted = {"last_result": None, "pending_outcome": None}
    assert op.data == {**RECORD, "permission_denials": STORED, **omitted}
    assert raw == before, "the record resume returned is not changed"
    assert ops_dispatch.shown_record(op.data) == op.data
    _no_leak(json.dumps(op.data) + op.text)


def test_resuming_a_historical_raw_record_prints_it_shown(droot: Path, fake_claude, run):
    fake_claude.scenario([one(envelope("question", open_questions=["go?"])), one(envelope())])
    d = _new_fast(droot)
    path = dispatch.file(droot, d["id"], ".json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["permission_denials"] = RAW
    raw["last_result"] = {**raw["last_result"], "permission_denials": RAW}
    path.write_text(json.dumps(raw), encoding="utf-8")
    resumed = run(["--json", "dispatch", "resume", d["id"], "--answer", "go", "--watch"])
    assert resumed.code == 0, resumed.err
    assert resumed.json["state"] == "done"
    assert resumed.json["permission_denials"] == STORED
    assert resumed.json["last_result"]["permission_denials"] == []
    _no_leak(resumed.out)


def test_out_records_name_tools_as_shown():
    tools = [f"mcp__{ENDPOINT}__read", "mcp__helper__read", "Bash"]
    blocks = [
        ToolUseBlock(id=f"t{i}", name=name, input={"command": ENDPOINT}) for i, name in enumerate(tools)
    ]
    record = normalize.out_record(AssistantMessage(content=[*blocks], model="m"))
    assert record["tools"] == ["[redacted-mcp-tool]", "mcp__helper__read", "Bash"]


def test_a_denied_row_from_the_return_is_listed_shown():
    rows = [
        {"cmd": f"curl -H 'Authorization: Bearer {TOKEN}' {ENDPOINT}", "exit": "DENIED"},
        {"cmd": "gh pr create", "exit": 1, "status": "DENIED"},
        {"cmd": f"curl {ENDPOINT}", "exit": 0},
    ]
    shown = state.permission_denials({"result": {"commands": rows}})
    assert shown == ["curl -H '[redacted] [redacted-endpoint]", "gh pr create"]


def test_finalizing_a_raw_outcome_stores_it_shown():
    current: dict[str, Any] = {"attempt": 2, "permission_denials": RAW[:2]}
    terminal._account(current, {"permission_denials": RAW[2:]})  # pyright: ignore[reportPrivateUsage]
    assert current["permission_denials"] == STORED
    _no_leak(json.dumps(current))
