"""`verify-pipe`: a check must own the exit code it is judged by.

Split from `test_hooks_guards.py`, which hit the 500-line limit. Its own concern: the other
guards decide whether an action is allowed, this one decides whether the *answer* to an
action will be read correctly.
"""

from __future__ import annotations

from pathlib import Path

from hooks_test_helpers import _decision, _reason, bash


def test_verify_pipe_denies_a_check_whose_exit_code_goes_to_the_pipe(tmp_root: Path, run):
    """A pipeline exits with its LAST segment's status, so `pytest | tail` reports tail's
    success and a failing suite reads as a passing one.

    This is and it recurred three times in one session on 2026-09-23 — twice after being
    noticed and resolved against, and once more in the very call after the fix was written. A
    mistake that survives its own lesson needs a mechanism, which is this.
    """
    for cmd in (
        "uv run pytest -q 2>&1 | tail -6",
        "uv run pytest | head -20",
        "pytest tests/test_leases.py | grep FAILED",
        "uv run ruff check src tests | tail -3",
        "uv run pyright | tail -5",
        "bin/agent eval run | tail -2",
    ):
        r = run(["hook", "verify-pipe"], stdin=bash(cmd))
        assert _decision(r) == "deny", cmd
        assert "LAST command's status" in _reason(r)


def test_verify_pipe_allows_everything_whose_verdict_is_not_at_stake(tmp_root: Path, run):
    """Narrow on purpose. A guard that fired on every pipe would be turned off, and most pipes
    are about output, not a verdict."""
    for cmd in (
        "uv run pytest -q",  # the correct form: unpiped, so the code is the suite's
        "git status --short | head -5",
        "ls src | wc -l",
        "grep -rn pytest src | head",  # names a check but does not run one
        "cat notes.md | tail -3",
        "bin/agent peer list | grep codex",  # not a verification command
        # `doctor` answers with rows, not a verdict, so filtering them is the normal way to read
        # them and grepping for FAIL is the correct read. A guard that fires on the legitimate
        # case gets switched off, and then it guards nothing.
        "bin/agent doctor | grep -i fail",
    ):
        r = run(["hook", "verify-pipe"], stdin=bash(cmd))
        assert r.out.strip() == "", cmd

    # the check in the LAST segment owns the exit code, so it is fine
    r = run(["hook", "verify-pipe"], stdin=bash("cat payload.json | uv run pytest -q"))
    assert r.out.strip() == ""
