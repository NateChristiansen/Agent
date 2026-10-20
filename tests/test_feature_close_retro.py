"""Closing does not need a retro; the result and event report whether one was there.
Audit 10 plan.md:118 (K6) retires the session-start reminder only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli import features, ops


@pytest.fixture
def feature(tmp_root: Path, notes_remote: Path, run) -> features.Feature:
    run(["feature", "new", "probe", "--title", "Probe"])
    return features.resolve(tmp_root, "1")


@pytest.mark.parametrize("status", ["done", "abandoned"])
def test_a_feature_without_a_retro_closes_and_reports_it(tmp_root: Path, feature, status: str):
    op = ops.feature_close(tmp_root, feature="1", status=status)
    closed = features.resolve(tmp_root, "1")
    assert closed.status == status
    assert not (closed.dir / "retro.md").exists()
    assert op.data["next"] == "/journal today" and "/journal today" in op.text
    assert op.data["retro"] == "missing" and "Missing retro: 001" in op.text


def test_ten_open_lessons_refuse_the_close_and_nine_allow_it(tmp_root: Path, feature):
    from agent_cli import lessons
    from agent_cli.core import AgentError

    for n in range(10):
        lessons.add(tmp_root, title=f"finding {n}", source="none", evidence="symptom", home="x")
    log = features.resolve(tmp_root, "1").dir / "log.md"
    before = log.read_bytes()
    with pytest.raises(AgentError, match="lesson queue has 10 open entries"):
        ops.feature_close(tmp_root, feature="1", note="closing note")
    assert log.read_bytes() == before, "a refused close logs no closing note"
    with pytest.raises(AgentError, match="lesson queue has 10 open entries"):
        ops.feature_set(tmp_root, feature="1", status="done")
    assert features.resolve(tmp_root, "1").status != "done"
    lessons.resolve(tmp_root, entry_id=lessons.open_entries(tmp_root)[0].id, disposition="dropped", note="x")
    ops.feature_close(tmp_root, feature="1")
    assert features.resolve(tmp_root, "1").status == "done"


def test_a_feature_with_a_retro_closes_without_a_missing_warning(tmp_root: Path, feature, run):
    (feature.dir / "retro.md").write_text("# Retro\n", encoding="utf-8")
    closed = run(["--json", "feature", "close", "1"])
    assert closed.code == 0 and closed.json["retro"] == "present"
    assert features.resolve(tmp_root, "1").status == "done"
    assert (feature.dir / "retro.md").read_text(encoding="utf-8") == "# Retro\n"
    assert "Missing retro" not in closed.out
