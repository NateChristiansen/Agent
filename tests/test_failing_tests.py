"""A failed verification names the failing tests from its run log (C3)."""

from __future__ import annotations

from pathlib import Path

from agent_cli.dispatch.terminal import FAILURE_LINES, failing_tests

LOG = """\
...F.
=========================== short test summary info ============================
FAILED tests/test_procs.py::test_snapshot_imports_at_call_time - ImportError
ERROR tests/test_win_driver.py::test_select - fixture missing
2 failed, 4380 passed in 600.0s
"""


def _run(root: Path, text: str) -> dict[str, object]:
    area = root / ".agent/dispatches/001-mac-03.verification/run1"
    area.mkdir(parents=True)
    (area / "output.log").write_text(text, encoding="utf-8")
    return {"run_area": ".agent/dispatches/001-mac-03.verification/run1"}


def test_the_failing_tests_are_listed(tmp_path: Path):
    lines = failing_tests(tmp_path, _run(tmp_path, LOG), "failed")
    assert lines == [
        "FAILED tests/test_procs.py::test_snapshot_imports_at_call_time - ImportError",
        "ERROR tests/test_win_driver.py::test_select - fixture missing",
    ]


def test_only_a_failed_run_gets_them(tmp_path: Path):
    assert failing_tests(tmp_path, _run(tmp_path, LOG), "incomplete") == []


def test_a_long_list_is_cut_short(tmp_path: Path):
    many = "".join(f"FAILED tests/test_x.py::test_{i}\n" for i in range(FAILURE_LINES + 3))
    lines = failing_tests(tmp_path, _run(tmp_path, many), "failed")
    assert len(lines) == FAILURE_LINES + 1 and lines[-1] == "... 3 more"
