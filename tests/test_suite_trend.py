"""The suite trend per landing in `land status`, and doctor's row on budget slack, from journals
and pass records written to disk."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_cli import land_journal as jn
from agent_cli import ops, suite_budget, verify_record
from agent_cli.doctor import checks_land

BUDGET = "[suite]\ntests_max = {tests}\nspawns_max = {spawns}\nheadroom_pct = 3\n"


@pytest.fixture
def main(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    return tmp_path


def landing(root: Path, ident: str, run_id: str, suite: dict | None, state: str = "done") -> None:
    jn.write(
        root, {"id": ident, "state": state, "branch": f"agent/{ident}", "record": run_id, "ts": jn.now()}
    )
    path = verify_record.store(root) / "v1" / f"tree-{run_id}" / "key.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"run_id": run_id, **({"suite": suite} if suite is not None else {})}))


def budget(root: Path, tests: int, spawns: int) -> None:
    (root / suite_budget.PATH).parent.mkdir(exist_ok=True)
    (root / suite_budget.PATH).write_text(BUDGET.format(tests=tests, spawns=spawns), encoding="utf-8")


def test_land_status_shows_each_landings_figures_and_unknown_without_a_suite_block(main: Path):
    landing(main, "20261020T100000-aaaaaa", "a" * 32, None)
    landing(main, "20261021T100000-bbbbbb", "b" * 32, {"tests": 8705, "spawns": 60666, "wall_s": 283.4})
    op = ops.land(main, status=True)
    new, old = op.data["landings"]
    assert (new["tests"], new["spawns"], new["wall_s"]) == (8705, 60666, 283.4)
    assert (old["tests"], old["spawns"], old["wall_s"]) == (None, None, None)
    lines = op.text.splitlines()
    assert (
        "20261021T100000-bbbbbb done agent/20261021T100000-bbbbbb: 8,705 tests, 60,666 spawns, 283 s" in lines
    )
    assert "20261020T100000-aaaaaa done agent/20261020T100000-aaaaaa: ? tests, ? spawns, ? s" in lines


def test_a_landing_with_a_done_operation_file_is_listed_once(main: Path):
    landing(main, "20261023T001837-396277", "a" * 32, {"tests": 10, "spawns": 20, "wall_s": 1.0})
    jn.op_update(main, "20261023T001837-8ea98c", state="done", journal="20261023T001837-396277")
    landings = ops.land(main, status=True).data["landings"]
    assert [row["id"] for row in landings] == ["20261023T001837-396277"]


def test_doctor_warns_on_slack_above_twice_the_headroom_and_names_the_tighter_value(main: Path):
    landing(main, "20261021T100000-bbbbbb", "b" * 32, {"tests": 1000, "spawns": 10000, "wall_s": 9.0})
    budget(main, tests=1070, spawns=10600)
    [row] = checks_land.budget_slack(main)
    assert row["level"] == "warn"
    assert "tests_max = 1070 sits 70 above the latest 1,000; write tests_max = 1030" in row["detail"]
    assert "spawns_max" not in row["detail"]


def test_doctor_passes_within_twice_the_headroom(main: Path):
    landing(main, "20261021T100000-bbbbbb", "b" * 32, {"tests": 1000, "spawns": 10000})
    budget(main, tests=1060, spawns=10300)
    [row] = checks_land.budget_slack(main)
    assert row["level"] == "ok"


def test_doctor_skips_without_a_budget_a_landing_or_suite_figures(main: Path):
    assert checks_land.budget_slack(main) == []
    budget(main, tests=1060, spawns=10300)
    assert checks_land.budget_slack(main) == []
    landing(main, "20261021T100000-bbbbbb", "b" * 32, None)
    assert checks_land.budget_slack(main) == []
    landing(main, "20261022T100000-cccccc", "c" * 32, {"tests": 1}, state="aborted")
    assert checks_land.budget_slack(main) == []


def test_doctor_warns_rather_than_fails_on_an_unreadable_budget(main: Path):
    landing(main, "20261021T100000-bbbbbb", "b" * 32, {"tests": 1000, "spawns": 10000})
    (main / suite_budget.PATH).parent.mkdir(exist_ok=True)
    (main / suite_budget.PATH).write_text("[suite]\ntests_max = 'x'\n", encoding="utf-8")
    [row] = checks_land.budget_slack(main)
    assert row["level"] == "warn" and "BudgetError" in row["detail"]
