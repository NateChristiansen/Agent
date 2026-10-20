"""The dispatch rubric: the table in harness/dispatch-rubric.md and the rows a launch reads
(039; cut to a table in phase 6b).

The doc is hand-maintained again, so this is the comparison that keeps it honest: a hand-edited
rubric once described a `--provider` flag and an `ultra` effort that existed nowhere in the code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli.core import AgentError
from agent_cli.dispatch import rubric

DOC = Path(__file__).resolve().parents[1] / "harness" / "dispatch-rubric.md"


def _doc_rows() -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for line in DOC.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 7 and cells[0] in rubric.ROWS:
            rows[cells[0]] = cells
    return rows


def test_the_document_table_matches_the_rows():
    """Row names, models, effort policy and budget band agree with what a launch sends."""
    doc = _doc_rows()
    assert list(doc) == list(rubric.ROWS), "the doc lists the rows in the code's order"
    for name, row in rubric.ROWS.items():
        _, shape, claude, codex, policy, band, why = doc[name]
        assert shape and why, name
        assert (claude, codex) == (row.models[rubric.CLAUDE], row.models[rubric.CODEX]), name
        want_policy = ", ".join(f"{p}: {e} (owner)" for p, e in row.efforts.items()) or "vendor default"
        assert policy == want_policy, name
        want_band = "the owner sets it" if row.budget is None else f"${row.budget[0]:g}-{row.budget[1]:g}"
        assert band == want_band, name


def test_row_names_are_stable():
    """Dispatch records and the subagent guard name these."""
    assert list(rubric.ROWS) == ["probe", "review", "mechanical", "build", "design", "max"]


def test_only_owner_codex_rows_pin_effort():
    for row in rubric.ROWS.values():
        assert row.efforts == ({rubric.CODEX: "medium"} if row.name in ("review", "build") else {})


def test_codex_models_are_priced():
    from agent_cli.prices import PRICE_TABLE

    for row in rubric.ROWS.values():
        assert row.model(rubric.CODEX) in PRICE_TABLE, row.name


def test_sonnet_5_5_cost_is_estimated():
    from agent_cli.prices import price_claude_usage

    assert price_claude_usage("claude-sonnet-5-5", {"output_tokens": 1_000_000}) == pytest.approx(10.0)


def test_picking_an_unknown_row_lists_the_real_ones_instead_of_defaulting():
    assert rubric.pick(rubric.CLAUDE, "build").model() == "opus"
    assert rubric.pick(rubric.CODEX, "build").model(rubric.CODEX) == "gpt-6.1-sol"
    with pytest.raises(AgentError) as err:
        rubric.pick(rubric.CLAUDE, "enormous")
    message = str(err.value)
    assert "enormous" in message
    for name in ("probe", "review", "mechanical", "build", "design"):
        assert name in message


def test_max_is_owner_only_and_off_every_ladder():
    """No automatic escalation reaches the owner-only model."""
    assert rubric.pick(rubric.CLAUDE, "max").owner_only is True
    with pytest.raises(AgentError, match="owner's to choose"):
        rubric.select_row(rubric.CLAUDE, "max")
    with pytest.raises(AgentError, match="provider must be"):
        rubric.select_row("gemini", "build")
    assert "fable" not in rubric.MODEL_LADDERS[rubric.CLAUDE]
    assert rubric.MODEL_LADDERS[rubric.CODEX] == ("gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra")


def test_codex_escalation_keeps_effort_at_the_top_rung():
    from agent_cli.dispatch.options import next_rubric_tier

    assert next_rubric_tier("gpt-6-sol", None, rubric.CODEX) == ("gpt-6-astra", None)
    assert next_rubric_tier("gpt-6.1-sol", "medium", rubric.CODEX) == ("gpt-6-astra", "medium")
    assert next_rubric_tier("gpt-6-luna", None, rubric.CODEX) == ("gpt-6.1-sol", None)
    assert next_rubric_tier("gpt-6-astra", "low", rubric.CODEX) == ("gpt-6-astra", "low")
    assert next_rubric_tier("gpt-6-astra", None, rubric.CODEX) == ("gpt-6-astra", None)
