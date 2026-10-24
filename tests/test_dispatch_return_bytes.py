"""Serialized UTF-8 bytes of the worker return contract and of a whole dispatch record.

`tests/fixtures/dispatch_return_bytes.json` holds the method and the numbers measured with this
same `measure()` on the earlier code (the return carried branch, commits, files_touched and
commands, and the record kept the return in `result`, in `last_result.structured_output` and as
the JSON text in `last_result.result`) and at the final code. The test measures the same
representative payload through the current code and checks it against the recorded "after".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_cli import dispatch, features
from agent_cli.dispatch import codex_normalize
from dispatch_helpers import _new_fast, droot, one  # noqa: F401 -- droot is a pytest fixture

FIXTURE = Path(__file__).parent / "fixtures" / "dispatch_return_bytes.json"

SHARED = {
    "nonce": "{{NONCE}}",
    "status": "done",
    "summary": "Added the readme line and a test for it; lint, format and the focused tests pass.",
    "open_questions": [],
    "next_steps": ["push and open the PR"],
    "notes": "Kept the change to README.md and one test; nothing else needed touching.",
}
LEGACY = {
    "branch": "feature/alpha-work-mac-01",
    "commits": [{"sha": "0123456789abcdef0123456789abcdef01234567", "subject": "ID-42 add readme line"}],
    "files_touched": ["README.md", "tests/test_readme.py"],
    "commands": [
        {"cmd": "uv run pytest tests/test_readme.py", "exit": 0},
        {"cmd": "uv run ruff check README.md tests/test_readme.py", "exit": 0},
        {"cmd": "uv run ruff format tests/test_readme.py", "exit": 0},
    ],
}

# Host-dependent values of the saved record, held at fixed values by key wherever they occur.
HELD = {
    "cli_path": "<cli_path>",
    "supervisor_pid": 12345,
    "notify": ["chat", "macos"],
    "worktree_s": 0.5,
    "duration_s": None,
    "env": ["<env names>"],
    "results": {"chat": "pending", "macos": "skipped"},
}


def _size(value: Any, **kw: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, **kw).encode("utf-8"))


def payload() -> dict[str, Any]:
    """The same worker content under whichever contract is current: the legacy fields are
    included only while the schema still requires them."""
    required = set(dispatch.RETURN_SCHEMA["required"])
    return {**SHARED, **{k: v for k, v in LEGACY.items() if k in required}}


def measure(root: Path, fake_claude: Any) -> dict[str, int]:
    ret = payload()
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 12,
        "duration_api_ms": 9,
        "session_id": "{{SESSION}}",
        "total_cost_usd": 0.42,
        "num_turns": 5,
        "result": json.dumps(ret),
        "terminal_reason": "completed",
        "structured_output": ret,
    }
    fake_claude.scenario([one(result, commit="ID-42 add readme line")])
    d = _new_fast(root)
    assert d["state"] == "done", d.get("error")
    saved = json.loads(dispatch.file(root, d["id"], ".json").read_text(encoding="utf-8"))
    for verdict in ("agree", "revise"):
        review = {**ret, "verdict": verdict, "summary": "Read all callers.\n\nOptional: none."}
        rendered = dispatch.render_handoff(root, {**d, "result": review}, features.resolve(root, "1"))
        assert json.dumps(review, indent=2, ensure_ascii=False) in rendered
    return {
        "schema_bytes": _size(dispatch.RETURN_SCHEMA),
        "codex_schema_bytes": _size(codex_normalize.codex_output_schema(dispatch.RETURN_SCHEMA)),
        "return_bytes": _size(ret),
        "record_bytes": _size(_host_neutral(saved, root, str(d["options"]["cli_path"])), indent=2),
    }


def _host_neutral(value: Any, root: Path, cli_path: str) -> Any:
    """The saved record with what depends on where it ran held constant: the harness root
    (in every path, as `<root>`, whether or not it is spelled through a symlink), the fake CLI's
    path, the supervisor pid, the notify channels and their delivery text, the setup timing, the
    names of the environment variables the host passed, and the verification duration. Every
    other field, including everything the change controls, is measured as saved."""
    if isinstance(value, dict):
        return {k: HELD[k] if k in HELD else _host_neutral(v, root, cli_path) for k, v in value.items()}
    if isinstance(value, list):
        return [_host_neutral(v, root, cli_path) for v in value]
    if isinstance(value, str):
        if value == cli_path:
            return "<cli_path>"
        for spelled in {str(root), str(root.resolve()), root.as_posix(), root.resolve().as_posix()}:
            value = value.replace(spelled, "<root>")
        if "<root>" in value:
            value = value.replace("\\", "/")
    return value


def test_return_and_record_bytes_match_the_recorded_after(droot: Path, fake_claude):
    recorded = json.loads(FIXTURE.read_text(encoding="utf-8"))
    measured = measure(droot, fake_claude)
    for key in ("schema_bytes", "codex_schema_bytes", "return_bytes", "record_bytes"):
        assert measured[key] == recorded["after"][key], (key, measured)
        assert measured[key] < recorded["before"][key], key
