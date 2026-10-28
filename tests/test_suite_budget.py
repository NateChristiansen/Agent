"""The suite budget (suite_budget.py): land refuses an over-budget record, a record that could not
measure its suite, and any loosening of main's tests/budget.toml, unless the owner's raise matches
a new [[raise]] entry."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli import land, ops, suite_budget, verify_record
from agent_cli.cli import parse
from agent_cli.core import AgentError
from agent_cli.mcp.specs_harness import harness as harness_tool
from agent_cli.mcp.tools import Ctx
from land_helpers import git, harness  # noqa: F401

BASE = """\
[suite]
tests_max = 100
spawns_max = 1000
headroom_pct = 10

[[raise]]
date = 2026-10-01
from = 90
to = 100
reason = "seed"
approved_by = "owner"

[slow]
"tests/test_a.py::test_slow" = 50

[sleep]
"tests/test_b.py" = 2

[weak]
"tests/test_c.py::test_weak" = "grandfathered"

[[skip]]
node = "tests/test_d.py::test_skip"
reason = "flaky on ci"
expires = 2026-12-01
"""
RAISE = """
[[raise]]
date = 2026-10-22
from = 100
to = 101
reason = "probe growth"
approved_by = "owner"
"""
SLOW = '"tests/test_a.py::test_slow" = 50'
SLEEP = '"tests/test_b.py" = 2'
WEAK = '"tests/test_c.py::test_weak" = "grandfathered"'
SKIP = """[[skip]]
node = "tests/test_d.py::test_skip"
reason = "flaky on ci"
expires = 2026-12-01
"""

LOOSENINGS = {
    "tests_max": ("tests_max = 100", "tests_max = 101"),
    "spawns_max": ("spawns_max = 1000", "spawns_max = 1001"),
    "headroom_pct": ("headroom_pct = 10", "headroom_pct = 15"),
    "slow-new": (SLOW, SLOW + '\n"tests/test_e.py::test_new" = 5'),
    "slow-higher": (SLOW, SLOW.replace("50", "60")),
    "sleep-new": (SLEEP, SLEEP + '\n"tests/test_f.py" = 1'),
    "sleep-higher": (SLEEP, SLEEP.replace("2", "3")),
    "weak-new": (WEAK, WEAK + '\n"tests/test_g.py::test_mock" = "mock only"'),
    "skip-new": (
        SKIP,
        SKIP + '\n[[skip]]\nnode = "tests/test_h.py::t"\nreason = "r"\nexpires = 2026-11-01\n',
    ),
    "skip-later": ("expires = 2026-12-01", "expires = 2027-01-01"),
}
TIGHTENINGS = {
    "lower-limits": ("tests_max = 100\nspawns_max = 1000", "tests_max = 90\nspawns_max = 900"),
    "slow-removed": (SLOW, ""),
    "slow-lower": (SLOW, SLOW.replace("50", "40")),
    "sleep-lower": (SLEEP, SLEEP.replace("2", "1")),
    "weak-removed": (WEAK, ""),
    "skip-removed": (SKIP, ""),
    "skip-earlier": ("expires = 2026-12-01", "expires = 2026-11-01"),
}


def _record(tests: int | None = 100, spawns: int | None = 1000, missing: tuple[str, ...] = ()) -> dict:
    return {
        "run_id": "r",
        "reused": True,
        "suite": {"tests": tests, "spawns": spawns, "missing": list(missing)},
    }


def _edit(old: str, new: str) -> str:
    assert old in BASE
    return BASE.replace(old, new, 1)


def test_a_record_within_budget_passes_and_one_over_names_its_figure_limit_and_overage():
    assert suite_budget.check(_record(), BASE, BASE) == []
    assert suite_budget.check(_record(tests=101, spawns=1250), BASE, BASE) == [
        "suite.tests 101 exceeds tests_max 100 by 1",
        "suite.spawns 1,250 exceeds spawns_max 1,000 by 250",
    ]


@pytest.mark.parametrize("kind", LOOSENINGS)
def test_each_loosening_refuses_without_a_raise_and_passes_with_a_matching_one(kind: str):
    loose = _edit(*LOOSENINGS[kind])
    [problem] = suite_budget.check(_record(), BASE, loose)
    assert problem.startswith(("[suite]", "[slow]", "[sleep]", "[weak]", "[[skip]]"))
    assert suite_budget.check(_record(), BASE, loose + RAISE, "probe growth") == []


def test_a_raise_must_match_a_new_raise_entry_by_reason():
    loose = _edit(*LOOSENINGS["tests_max"])
    for given, text in (("other reason", loose + RAISE), ("seed", loose), ("probe growth", loose)):
        problems = suite_budget.check(_record(), BASE, text, given)
        assert problems[-1] == f"budget_raise {given!r} matches no [[raise]] entry the candidate adds"
    assert suite_budget.check(_record(), BASE, BASE, "seed")  # an unneeded raise still has to match


def test_a_matching_raise_bounds_records_even_when_the_candidate_raises_limits():
    assert suite_budget.check(_record(tests=101), BASE, BASE + RAISE)
    assert suite_budget.check(_record(tests=101), BASE, BASE + RAISE, "probe growth") == []
    assert suite_budget.check(_record(tests=150), BASE, BASE + RAISE, "probe growth") == [
        "suite.tests 150 exceeds [[raise]] 'probe growth' to 101 by 49"
    ]
    candidate = _edit("tests_max = 100", "tests_max = 200") + RAISE
    assert suite_budget.check(_record(tests=101), BASE, candidate, "probe growth") == []
    assert suite_budget.check(_record(tests=102), BASE, candidate, "probe growth") == [
        "suite.tests 102 exceeds [[raise]] 'probe growth' to 101 by 1"
    ]


def test_matching_raises_keep_records_within_their_spawn_ceilings():
    main = _edit("spawns_max = 1000", "spawns_max = 62490")
    raised = RAISE.replace("from = 100", "from = 62490").replace("to = 101", "to = 65000")
    cases = [(main, main + raised, "probe growth", 100)]
    candidate = (Path(__file__).parent / "budget.toml").read_text()
    prefix, *entries = candidate.split("\n[[raise]]\n")
    budget = suite_budget.parse(candidate)
    assert budget.limits["tests_max"] == 8970
    assert budget.limits["spawns_max"] == 62490
    for index in (0, 1):
        entry = budget.raises[index]
        assert entry["from"] == 62490 and entry["to"] == 65000, entry
        candidate = prefix + "".join("\n[[raise]]\n" + text for text in entries[index:])
        main = prefix + "".join("\n[[raise]]\n" + text for text in entries[index + 1 :])
        cases.append((main, candidate, str(entry["reason"]), 8970))
    for main, candidate, reason, tests in cases:
        for spawns, admitted in ((64139, True), (65000, True), (65001, False)):
            scenario = (reason, spawns)
            record = _record(tests=tests, spawns=spawns)
            assert suite_budget.check(record, main, candidate), scenario
            assert suite_budget.check(record, main, candidate, "other reason"), scenario
            assert suite_budget.check(record, candidate, candidate, reason), scenario
            if reason == str(budget.raises[0]["reason"]):
                assert suite_budget.check(record, main, candidate, str(budget.raises[1]["reason"])), scenario
            assert (not suite_budget.check(record, main, candidate, reason)) == admitted, scenario


def test_matching_raise_entries_bound_each_metric():
    raised = RAISE + RAISE.replace("from = 100", "from = 1000").replace("to = 101", "to = 1001")
    assert suite_budget.check(_record(tests=101, spawns=1001), BASE, BASE + raised, "probe growth") == []
    assert len(suite_budget.check(_record(tests=102, spawns=1002), BASE, BASE + raised, "probe growth")) == 2


def test_a_numeric_raise_for_a_suite_metric_needs_a_finite_ceiling():
    for ceiling in ('"lots"', "nan", "inf", "true"):
        raised = RAISE.replace("to = 101", f"to = {ceiling}")
        problems = suite_budget.check(_record(tests=101), BASE, BASE + raised, "probe growth")
        assert len(problems) == 1, (ceiling, problems)
        assert "to must be a finite number" in problems[0], (ceiling, problems)


def test_other_raise_shapes_keep_the_existing_exception_behavior():
    for start in ('"old"', "90", "true"):
        raised = RAISE.replace("from = 100", f"from = {start}")
        assert suite_budget.check(_record(tests=150), BASE, BASE + raised, "probe growth") == [], start


@pytest.mark.parametrize("kind", TIGHTENINGS)
def test_tightening_passes(kind: str):
    assert suite_budget.check(_record(tests=90, spawns=900), BASE, _edit(*TIGHTENINGS[kind])) == []


def test_without_a_budget_on_main_nothing_is_enforced_and_adding_one_is_no_loosening():
    assert suite_budget.check(_record(tests=10**6, spawns=None), None, BASE) == []
    assert suite_budget.check(_record(tests=10**6), None, None) == []


def test_deleting_main_s_budget_refuses_and_keeps_main_s_limits():
    assert suite_budget.check(_record(), BASE, None) == [f"the candidate deletes {suite_budget.PATH}"]
    assert (
        suite_budget.check(_record(tests=101), BASE, None)[1] == "suite.tests 101 exceeds tests_max 100 by 1"
    )
    assert suite_budget.check(_record(), BASE, None, "probe growth")[-1].startswith("budget_raise")


def test_a_record_that_could_not_measure_its_suite_refuses_unless_raised():
    record = _record(tests=None, missing=("command 0's output holds 2 pytest summaries, not one",))
    assert suite_budget.check(record, BASE, BASE) == [
        "the record has no suite.tests (command 0's output holds 2 pytest summaries, not one)"
    ]
    assert suite_budget.check({"run_id": "old"}, BASE, BASE) == [
        "the record has no suite.tests (the record has no suite figures)",
        "the record has no suite.spawns (the record has no suite figures)",
    ]
    assert suite_budget.check(record, BASE, BASE + RAISE, "probe growth") == []


UNREADABLE = {
    "syntax": ("[suite]", "[suite"),
    "text": ("tests_max = 100", 'tests_max = "lots"'),
    "nan": ("tests_max = 100", "tests_max = nan"),
    "inf": ("spawns_max = 1000", "spawns_max = inf"),
    "slow-nan": (SLOW, SLOW.replace("50", "nan")),
    "sleep-inf": (SLEEP, SLEEP.replace("2", "inf")),
}


@pytest.mark.parametrize("kind", UNREADABLE)
def test_an_unreadable_or_non_finite_budget_refuses_on_either_side(kind: str):
    bad = _edit(*UNREADABLE[kind])
    huge = _record(tests=10**6, spawns=10**6)
    [problem] = suite_budget.check(huge, BASE, bad + RAISE, "probe growth")
    assert problem.startswith(f"the candidate's {suite_budget.PATH} cannot be read")
    assert suite_budget.check(huge, bad, BASE)[0].startswith(f"main's {suite_budget.PATH} cannot be read")


def test_a_raise_entry_alone_is_not_approval_and_no_record_compares_only_the_files():
    loose = _edit(*LOOSENINGS["tests_max"])
    assert suite_budget.check(_record(), BASE, loose + RAISE)
    assert suite_budget.check(None, BASE, BASE) == []
    assert suite_budget.check(None, BASE, loose)


def _commit_budget(root: Path, text: str, branch: str | None = None) -> None:
    if branch:
        git(root, "switch", "-qc", branch)
    (root / "tests").mkdir(exist_ok=True)
    (root / suite_budget.PATH).write_text(text)
    git(root, "add", suite_budget.PATH)
    git(root, "commit", "-qm", f"budget on {branch or 'main'}")
    git(root, "switch", "-q", "main")


def test_landing_refuses_an_over_budget_record_and_lands_one_within(harness: Path, monkeypatch):  # noqa: F811
    _commit_budget(harness, BASE)
    old = git(harness, "rev-parse", "main")
    figures = {"tests": 101}
    monkeypatch.setattr(verify_record, "record", lambda root, rev, **kw: _record(tests=figures["tests"]))
    with pytest.raises(AgentError, match="suite.tests 101 exceeds tests_max 100 by 1"):
        land.land(harness, "feature", strategy="squash")
    assert git(harness, "rev-parse", "main") == old
    figures["tests"] = 100
    assert land.land(harness, "feature", strategy="squash")["state"] == "done"


def test_landing_a_loosened_budget_needs_the_owner_s_matching_raise(harness: Path, monkeypatch):  # noqa: F811
    _commit_budget(harness, BASE)
    _commit_budget(harness, _edit(*LOOSENINGS["tests_max"]) + RAISE, branch="grow")
    old = git(harness, "rev-parse", "main")
    monkeypatch.setattr(verify_record, "record", lambda root, rev, **kw: _record(tests=101))
    with pytest.raises(AgentError, match=r"tests_max raised from 100 to 101.*budget_raise"):
        ops.land(harness, branch="grow")
    with pytest.raises(AgentError, match="matches no"):
        ops.land(harness, branch="grow", budget_raise="seed")
    assert git(harness, "rev-parse", "main") == old
    op = ops.land(harness, branch="grow", budget_raise="probe growth")
    assert op.data["state"] == "done" and op.data["budget_raise"] == "probe growth"
    assert git(harness, "rev-parse", "main") != old


def test_the_mcp_land_action_hands_budget_raise_to_the_detached_cli(harness: Path, monkeypatch):  # noqa: F811
    import subprocess

    seen: list[str] = []
    popen = subprocess.Popen

    class Child:
        pid = 12345

    def spawn(argv, **kwargs):
        if "--op" not in argv:
            return popen(argv, **kwargs)
        seen.extend(argv)
        return Child()

    monkeypatch.setattr(subprocess, "Popen", spawn)
    harness_tool(Ctx(harness, "probe"), action="land", ref="feature", budget_raise="-a dash first")
    assert parse(seen[seen.index("--root") :]).budget_raise == "-a dash first"
