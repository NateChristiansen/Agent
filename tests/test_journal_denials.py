"""`journal collect` copies each finished dispatch's `last_result` into the bundle it prints and the
`.collect.json` it writes. A record stored before claude's denials were kept shown still holds them
raw: a tool name with the server's endpoint in it, a Bash command with its bearer header. The bundle
carries them only as `state.shown_denials` keeps them; the record on disk is left as it is."""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path
from typing import Any

from agent_cli import dispatch, journal
from test_sdk_permission_denials import ENDPOINT, LEAKS, MALFORMED, RAW, STORED, TOKEN

DAY = "2026-09-03"
COST = {"subtype": "success", "total_cost_usd": 1.23, "num_turns": 5, "session_id": "s"}
MCP_X = {"tool_name": "[redacted-mcp-tool]", "input_omitted": True}

# each record's `last_result` as stored, then as the bundle carries it
RESULTS: dict[str, tuple[object, object]] = {
    "001-01": ({**COST, "permission_denials": RAW}, {**COST, "permission_denials": STORED}),
    "001-02": ({**COST, "permission_denials": STORED}, {**COST, "permission_denials": STORED}),
    "001-03": ({"permission_denials": {"tool_name": f"mcp__{ENDPOINT}__x"}}, {"permission_denials": [MCP_X]}),
    "001-04": (
        {"permission_denials": f"curl -H 'Authorization: Bearer {TOKEN}' {ENDPOINT}"},
        {"permission_denials": ["curl -H '[redacted] [redacted-endpoint]"]},
    ),
    "001-05": ({"permission_denials": None}, {"permission_denials": []}),
    "001-06": ({**COST, "permission_denials": []}, {**COST, "permission_denials": []}),
    "001-07": (COST, COST),
    "001-08": (None, None),
    **{f"001-{9 + i:02d}": (bad, None) for i, bad in enumerate(MALFORMED)},
}


def _record(dispatch_id: str, last_result: object) -> dict[str, Any]:
    return {
        **{"id": dispatch_id, "feature": 1, "short": "alpha", "state": "done", "attempt": 1},
        **{"cost_usd": 1.23, "model": None, "effort": None, "finished": f"{DAY}T10:00:00-07:00"},
        **{"result": {"summary": "did the thing. more detail"}, "error": None, "handoff": None},
        **{"last_result": copy.deepcopy(last_result), "permission_denials": RAW},
    }


def _row(dispatch_id: str, last_result: object) -> dict[str, Any]:
    return {
        **{"id": dispatch_id, "kind": "work", "feature": 1, "short": "alpha", "state": "done"},
        **{"cost_usd": 1.23, "last_result": last_result, "attempt": 1, "model": None, "effort": None},
        **{"summary": "did the thing.", "handoff": None},
    }


ROWS = [_row(i, shown) for i, (_, shown) in RESULTS.items()]


def _seed(root: Path) -> dict[str, str]:
    ddir = root / ".agent" / "dispatches"
    ddir.mkdir(parents=True)
    stored: dict[str, str] = {}
    for dispatch_id, (raw, _) in RESULTS.items():
        text = json.dumps(_record(dispatch_id, raw))
        (ddir / f"{dispatch_id}.json").write_text(text, encoding="utf-8")
        stored[dispatch_id] = text
    return stored


def _no_leak(text: str) -> None:
    for leak in LEAKS:
        assert leak not in text, leak


def test_collect_prints_and_writes_historical_denials_shown(tmp_root: Path, run):
    stored = _seed(tmp_root)
    printed = run(["--json", "journal", "collect", DAY])
    assert printed.code == 0, printed.err
    assert printed.json["dispatches"] == ROWS
    written = run(["--json", "journal", "collect", DAY, "--write"])
    assert written.code == 0, written.err
    path = tmp_root / ".agent" / "journal" / f"{DAY}.collect.json"
    assert written.json["written"] == path.relative_to(tmp_root).as_posix()
    persisted = path.read_text(encoding="utf-8")
    assert json.loads(persisted)["dispatches"] == ROWS == written.json["dispatches"]
    for text in (printed.out, written.out, persisted):
        _no_leak(text)
    for dispatch_id, text in stored.items():
        assert dispatch.file(tmp_root, dispatch_id, ".json").read_text(encoding="utf-8") == text


def test_collect_is_stable_and_leaves_the_loaded_records_alone(tmp_root: Path, monkeypatch):
    records = [_record(i, raw) for i, (raw, _) in RESULTS.items()]
    before = copy.deepcopy(records)
    monkeypatch.setattr(dispatch, "load_all", lambda _root: records)
    first = journal.collect(tmp_root, date.fromisoformat(DAY))["dispatches"]
    assert first == ROWS
    assert records == before
    again = [_record(row["id"], row["last_result"]) for row in first]
    monkeypatch.setattr(dispatch, "load_all", lambda _root: again)
    assert journal.collect(tmp_root, date.fromisoformat(DAY))["dispatches"] == ROWS
