"""Session token accounting.

`spend_usd` is dispatched worker spend. On a harness day the work is in the manager's own
sessions and their subagents, which nothing measured: a day read a few cents of worker spend
while the sessions ran far more. `usage/` collects the counts
(tests/test_usage_*.py); these cover how the note reads them.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from agent_cli.journal import tokens as tokens_mod
from usage_helpers import publish

DAY = date(2026, 9, 3)  # the fixed clock's today()


def test_summary_is_biggest_first_and_compact(tmp_root: Path):
    by_model = {
        "opus-5": dict.fromkeys(("requests", *tokens_mod.FIELDS), 0) | {"output_tokens": 2_000_000},
        "sonnet-5": dict.fromkeys(("requests", *tokens_mod.FIELDS), 0) | {"output_tokens": 5_000_000},
    }
    assert tokens_mod.summary(by_model) == "sonnet-5 5.0M, opus-5 2.0M"


def test_the_note_carries_the_day_s_tokens(tmp_root: Path):
    from agent_cli import journal as journal_mod

    publish(
        tmp_root,
        DAY,
        record={
            "total_tokens": 1_500_000,
            "requests": 1,
            "tokens_manager": 1_500_000,
            "tokens_subagent": 0,
            "tokens_worker": 0,
            "tokens_other": 0,
            "sources": {
                "claude": {"status": "complete", "files": 1, "requests": 1},
                "codex": {"status": "complete", "files": 0, "requests": 0},
                "costs": {"status": "complete"},
            },
            "models": {
                "claude-opus-5": {
                    "requests": 1,
                    "input": 0,
                    "output": 1_500_000,
                    "cache_write": 0,
                    "cache_read": 0,
                }
            },
        },
    )
    path = journal_mod.write_generated(tmp_root, DAY, "- did the thing\n")
    text = path.read_text()
    assert "session_tokens: 1500000" in text
    assert "session_models: opus-5 1.5M" in text
    assert "tokens_complete: true" in text
