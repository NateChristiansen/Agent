"""The per-prompt list of waiting returns is the fallback for a lost notice (Codex review of audit
5 phase 6): a notice consumed by a hook that died before rendering it must not silence the list.
A hook records consumption in the recipient's notice store; the source record keeps "none"."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

from agent_cli.dispatch import notice_delivery
from agent_cli.dispatch.notices import GENERATION_FIELD, OUTBOX_FIELD

notify = importlib.import_module("agent_cli.dispatch.notify")  # the package re-exports a `notify` function

MGR = "mgr-session"


def _dispatch(delivery: str = "inbox-accepted") -> dict[str, Any]:
    return {
        "id": "001-mac-01",
        "short": "alpha",
        "state": "done",
        "worktree": "/tmp/wt",
        "chat_pending": True,
        "cost_usd": 0.5,
        "result": {"summary": "finished"},
        GENERATION_FIELD: 1,
        OUTBOX_FIELD: {
            "n1": {
                "generation": 1,
                "delivery": delivery,
                "consumption": "none",  # the source never learns of a hook's consumption
                "envelope": {"recipient": MGR},
            }
        },
    }


@pytest.fixture
def visible(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    monkeypatch.setattr(notify, "_visible_dispatches", lambda root, sid: records)
    return records


def test_a_queued_notice_keeps_the_list_quiet(visible: list[dict[str, Any]], tmp_root: Path):
    visible.append(_dispatch())
    assert notify.inbox_block(tmp_root, session_id=MGR) == ""


def test_a_notice_shown_this_turn_is_not_listed_again(visible: list[dict[str, Any]], tmp_root: Path):
    visible.append(_dispatch())
    notice_delivery.tombstone_all(tmp_root, MGR, ["n1"], "hook-consumed")
    assert notify.inbox_block(tmp_root, session_id=MGR, shown={"001-mac-01"}) == ""


def test_a_consumed_but_unrendered_notice_is_listed(visible: list[dict[str, Any]], tmp_root: Path):
    """The hook saved the consumption in the recipient store, then died before printing: the
    next prompt lists the return, and keeps listing it until the manager handles it."""
    visible.append(_dispatch())
    notice_delivery.tombstone_all(tmp_root, MGR, ["n1"], "hook-consumed")
    for _ in range(2):
        text = notify.inbox_block(tmp_root, session_id=MGR)
        assert "001-mac-01" in text and "waiting on you" in text


def test_a_running_workers_question_is_delivered_by_the_fallback(visible, tmp_root):
    visible.append({**_dispatch("fallback"), "state": "running", "open_question": {"text": "Which option?"}})
    text = notify.inbox_block(tmp_root, session_id=MGR)
    assert "001-mac-01" in text and "waiting on you" in text


def test_a_read_verdict_is_not_delivered_by_the_fallback(visible, tmp_root):
    from agent_cli.dispatch import verdicts

    record = _dispatch("fallback")
    verdicts.mark(record, MGR, "consume")
    visible.append(record)
    assert notify.inbox_block(tmp_root, session_id=MGR) == ""


@pytest.mark.parametrize(
    "fields",
    [
        {"feature": 44},
        {"state": "cancelled"},
        {"kind": "probe"},
        {"state": "cancelled", "verification_cleanup_pending": True},
    ],
)
def test_closed_cancelled_and_probe_returns_never_recover_as_asks(
    visible,
    tmp_root,
    monkeypatch,
    fields,
):
    from agent_cli.dispatch import state

    monkeypatch.setattr(state, "closed_feature_ids", lambda root: {44})
    visible.append({**_dispatch("fallback"), **fields})
    assert notify.inbox_block(tmp_root, session_id=MGR) == ""
