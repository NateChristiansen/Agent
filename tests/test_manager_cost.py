"""Manager Stop estimates and cumulative daily accounting."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agent_cli import codex_rollouts, core, hooks, prices, sessions

pytestmark = pytest.mark.usefixtures("notes_remote")


def _write(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def _claude(request: str, output: int = 100, model: str = "claude-opus-5-5") -> dict:
    return {
        "type": "assistant",
        "requestId": request,
        "message": {"model": model, "usage": {"input_tokens": 1000, "output_tokens": output}},
    }


def _rollout(
    path: Path, ident: str, parent: str = "", guardian: bool = False, model: str = "gpt-6-sol"
) -> Path:
    return _write(
        path,
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": ident,
                    "source": {"subagent": {"thread_spawn": {"parent_thread_id": parent}}},
                    "thread_source": "guardian_review" if guardian else "cli",
                },
            },
            {
                "type": "turn_context",
                "payload": {
                    "turn_id": ident,
                    "model": "codex-auto-review" if guardian else model,
                },
            },
            {
                "type": "token_usage_record",
                "payload": {
                    "session_id": "manager",
                    "response_id": ident,
                    "turn_id": ident,
                    "usage": {"input_tokens": 1000, "output_tokens": 100},
                    "thread_token_usage": {"input_tokens": 1000, "output_tokens": 100},
                },
            },
        ],
    )


def _stop(root: Path, path: Path, provider: str = "claude", **extra) -> dict:
    _, code = hooks.run(
        root,
        "codex-activity" if provider == "codex" else "activity",
        json.dumps(
            {"session_id": "manager", "hook_event_name": "Stop", "transcript_path": str(path), **extra}
        ),
    )
    assert code == 0
    return sessions.get(root, "manager") or {}


def test_claude_stop_prices_subagents_and_daily_deltas(tmp_root: Path, monkeypatch):
    path = _write(tmp_root / "session.jsonl", [_claude("shared")])
    child = _write(tmp_root / "session/subagents/child.jsonl", [_claude("shared", 300), _claude("child")])
    monkeypatch.setenv("AGENT_NOW", "2026-10-23T12:00:00-06:00")
    record = _stop(tmp_root, path)
    assert record["cost_usd"] == 0.016
    assert record["cost_source"] == "price-estimate"
    assert record["cost_priced_at"] == prices.PRICED_AT
    assert record["cost_by_day"] == {"2026-10-23": 0.016}
    monkeypatch.setenv("AGENT_NOW", "2026-10-24T12:00:00-06:00")
    _write(child, [_claude("shared", 300), _claude("child"), _claude("new")])
    assert _stop(tmp_root, path)["cost_by_day"] == {
        "2026-10-23": 0.016,
        "2026-10-24": 0.006,
    }


@pytest.mark.parametrize("source", ["provider-result", "price-estimate", "unknown"])
def test_estimate_transition_and_failure_clear_prior_days(tmp_root: Path, source: str):
    day = core.today().isoformat()
    sessions.update_record(
        tmp_root,
        "manager",
        lambda r: {
            **r,
            "cost_usd": 5.0,
            "cost_source": source,
            "cost_by_day": {"2026-10-01": 4.0, day: 1.0},
        },
    )
    path = _write(tmp_root / "session.jsonl", [_claude("request")])
    record = _stop(tmp_root, path)
    assert record["cost_usd"] == 0.006
    assert record["cost_source"] == "price-estimate"
    assert record["cost_by_day"] == {"2026-10-01": 4.0, day: 1.0}
    path.unlink()
    record = _stop(tmp_root, path)
    assert record["cost_source"] == "unknown"
    assert record["cost_usd"] is None
    assert record["cost_by_day"] == {"2026-10-01": None, day: None}


@pytest.mark.parametrize(
    "provider, failure",
    [
        ("claude", "missing"),
        ("claude", "malformed"),
        ("claude", "unknown"),
        ("claude", "unreadable"),
        ("codex", "missing"),
        ("codex", "malformed"),
        ("codex", "unicode"),
    ],
)
def test_bad_session_files_are_unknown_and_hooks_exit_zero(tmp_root: Path, provider: str, failure: str):
    path = tmp_root / "session.jsonl"
    if failure == "malformed":
        path.write_text('{"partial":\n')
    elif failure == "unknown":
        _write(path, [_claude("unknown", model="future-model")])
    elif failure == "unreadable":
        path.mkdir()
    elif failure == "unicode":
        path.write_bytes(b"\xff")
    sessions.record_cost(tmp_root, "manager", 3.0, "2026-10-01")
    record = _stop(tmp_root, path, provider)
    assert record["cost_source"] == "unknown"
    assert record["cost_by_day"]["2026-10-01"] is None


@pytest.mark.parametrize(
    "event, extra, dispatch",
    [
        ("SessionStart", {}, False),
        ("PreToolUse", {"tool_name": "AskUserQuestion"}, False),
        ("Stop", {"agent_id": "child"}, False),
        ("Stop", {"agent_type": "reviewer"}, False),
        ("Stop", {}, True),
        ("Stop", {"session_id": ""}, False),
    ],
)
def test_non_manager_stops_and_other_events_do_not_price(
    tmp_root: Path, monkeypatch, event: str, extra: dict, dispatch: bool
):
    def forbidden(*args, **kwargs):
        pytest.fail("event must not price")

    monkeypatch.setattr(prices, "price_claude_session", forbidden)
    monkeypatch.setattr(prices, "price_rollout", forbidden)
    sessions.set_record(tmp_root, "manager", dispatch_id="121-01" if dispatch else None)
    payload = {"session_id": "manager", "hook_event_name": event, **extra}
    for hook in ("activity", "codex-activity"):
        _, code = hooks.run(tmp_root, hook, json.dumps(payload))
        assert code == 0
    assert not (sessions.get(tmp_root, "manager") or {}).get("cost_usd")


@pytest.mark.parametrize(
    "case, expected",
    [("root-parent", 0.009), ("nested", 0.033), ("nested-spawn", 0.033), ("empty", 0.006)],
)
def test_codex_prices_children_across_dates_and_deduplicates(
    tmp_root: Path, monkeypatch, case: str, expected: float
):
    native = tmp_root / "native"
    path = _rollout(native / "sessions/2026/10/23/rollout-root.jsonl", "manager")
    nested = case.startswith("nested")
    child = _rollout(
        native / "sessions/2026/10/24/rollout-child.jsonl",
        "child",
        "manager",
        model="gpt-6-astra" if nested else "gpt-6-sol",
    )
    guardian = _rollout(
        native / "archived_sessions/rollout-guardian.jsonl",
        "guardian",
        "child" if nested else "manager",
        True,
    )
    rows = [json.loads(line) for line in guardian.read_text().splitlines()]
    if case == "nested":
        rows[0]["payload"]["parent_thread_id"] = "child"
        rows[0]["payload"]["source"] = "cli"
    elif case == "empty":
        rows = [
            rows[0],
            {"type": "response_item", "payload": {"type": "message"}},
            {"type": "event_msg", "payload": {"type": "task_started"}},
        ]
    _write(guardian, rows)
    _rollout(native / "sessions/2026/10/24/rollout-unrelated.jsonl", "unrelated")

    def forbidden(*args, **kwargs):
        pytest.fail("Codex re-entry must not price Claude")

    monkeypatch.setattr(prices, "price_claude_session", forbidden)
    record = _stop(tmp_root, path, "codex")
    assert record["cost_usd"] == expected
    assert record["cost_source"] == "price-estimate"
    assert record["cost_priced_at"] == prices.PRICED_AT
    # A copied response in another child file is still counted once.
    duplicate = native / "sessions/2026/10/24/rollout-copy.jsonl"
    duplicate.write_bytes(child.read_bytes())
    assert _stop(tmp_root, path, "codex")["cost_usd"] == expected
    # An unchanged directory listing needs no metadata reads.
    original = Path.open
    opened: list[Path] = []

    def track(p, *args, **kwargs):
        opened.append(p)
        return original(p, *args, **kwargs)

    monkeypatch.setattr(Path, "open", track)
    assert len(codex_rollouts.session_rollouts(tmp_root, path, "manager")) == 3
    assert not [p for p in opened if p.suffix == ".jsonl"]
    _rollout(
        native / "sessions/2026/10/25/rollout-grandchild.jsonl", "grandchild", "child", model="gpt-6-astra"
    )
    assert _stop(tmp_root, path, "codex")["cost_usd"] == round(expected + 0.015, 4)
    assert prices.price_rollout(path, children=[native / "missing.jsonl"]) is None
    if case == "empty":
        assert prices.price_rollout(guardian) is None
    else:
        rows[-1]["payload"]["thread_token_usage"]["input_tokens"] = 2000
        _write(guardian, rows)
        assert prices.price_rollout(path, children=[guardian, child]) is None
        rows[-1]["payload"]["thread_token_usage"]["input_tokens"] = 1000
        rows[1]["payload"]["model"] = "unknown-model"
        _write(guardian, rows)
        assert prices.price_rollout(path, children=[guardian, child]) is None


def test_discovery_deadline_keeps_progress_for_next_stop(tmp_root: Path, monkeypatch):
    path = _rollout(tmp_root / "native/sessions/2026/10/24/rollout-root.jsonl", "manager")
    monkeypatch.setattr(codex_rollouts, "DISCOVERY_SECONDS", -1)
    assert _stop(tmp_root, path, "codex")["cost_source"] == "unknown"
    monkeypatch.setattr(codex_rollouts, "DISCOVERY_SECONDS", 10)
    assert _stop(tmp_root, path, "codex")["cost_usd"] == 0.003


def test_large_rollout_tree_reads_headers_only_once(tmp_root: Path, monkeypatch):
    folder = tmp_root / "native/sessions/2026/10/24"
    path = _rollout(folder / "rollout-root.jsonl", "manager")
    for n in range(5000):
        _write(
            folder / f"rollout-{n}.jsonl",
            [
                {"type": "session_meta", "payload": {"id": f"unrelated-{n}"}},
            ],
        )
    original = Path.open
    headers: list[Path] = []

    def track(p, *args, **kwargs):
        if p.suffix == ".jsonl":
            headers.append(p)
        return original(p, *args, **kwargs)

    monkeypatch.setattr(Path, "open", track)
    started = time.perf_counter()
    for _ in range(20):
        try:
            assert codex_rollouts.session_rollouts(tmp_root, path, "manager") == []
            break
        except TimeoutError:
            pass
    else:
        pytest.fail("discovery failed to converge")
    cold = time.perf_counter() - started
    assert len(headers) == 5001
    headers.clear()
    started = time.perf_counter()
    assert codex_rollouts.session_rollouts(tmp_root, path, "manager") == []
    warm = time.perf_counter() - started
    assert headers == []
    print(f"5001 rollouts: cold={cold:.4f}s, warm={warm:.4f}s; warm header reads=0")


def test_an_unchanged_total_writes_nothing(tmp_root: Path):
    """Repeated totals must not rewrite a session record when spend has not changed."""
    from agent_cli import core, sessions

    day = core.today().isoformat()
    sessions.record_cost(tmp_root, "sess-d", 2.0, day)
    before = (tmp_root / ".agent" / "sessions" / "sess-d.json").stat().st_mtime_ns
    assert sessions.record_cost(tmp_root, "sess-d", 2.004, day) is None
    assert (tmp_root / ".agent" / "sessions" / "sess-d.json").stat().st_mtime_ns == before
    assert sessions.cost_summary_for_day(tmp_root, day)[0] == 2.0


def test_a_cost_that_restarts_adds_nothing(tmp_root: Path):
    """A resumed session can hand over a total lower than the one already stored. The day's
    figure must not go backwards, and a negative delta is not spend."""
    from agent_cli import core, sessions

    day = core.today().isoformat()
    sessions.record_cost(tmp_root, "sess-e", 5.0, day)
    assert sessions.record_cost(tmp_root, "sess-e", 1.0, day) is None
    assert sessions.cost_summary_for_day(tmp_root, day)[0] == 5.0
