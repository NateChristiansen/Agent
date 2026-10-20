"""No comment needs a justification row (owner decision, 2026-10-05); the return gate refuses
comment and docstring noise the same way the commit check does."""

from __future__ import annotations

from pathlib import Path

from agent_cli.dispatch import conventions as c
from agent_cli.dispatch.evaluate import _apply_conventions
from test_conventions import _commit, _rp, repo  # noqa: F401 -- repo is a pytest fixture


def test_a_comments_row_only_annotates_and_must_overlap_the_block():
    comments = [
        {"path": "a.py", "line": 5, "end": 7, "text": "Return the error as a value, not an exception."},
        {
            "path": "b.py",
            "line": 3,
            "end": 3,
            "text": "Retries twice because the upstream drops the first call.",
        },
    ]
    rows = [
        {"path": "b.py", "line": 3, "reason": "the retry count is not obvious from the call"},
        {"path": "a.py", "line": 9, "reason": "outside the block"},
        {"path": "a.py", "line": 6, "reason": "  "},
    ]
    c.annotate_reasons(comments, rows)
    assert [cm["reason"] for cm in comments] == ["", "the retry count is not obvious from the call"]


def test_a_row_with_an_end_covers_a_run_of_blocks():
    docs = [{"path": "g.py", "line": n, "end": n + 2, "text": f"Return row {n}."} for n in (5, 11, 16, 121)]
    rows = [{"path": "g.py", "line": 5, "end": 118, "reason": "each accessor needs a one-line docstring"}]
    c.annotate_reasons(docs, rows)
    assert [bool(d["reason"]) for d in docs] == [True, True, True, False]


def test_a_comment_without_a_row_no_longer_blocks(repo: Path):
    """A work order asked for a comment edit, the worker made it and sent no row,
    and the return came back blocked for paperwork. No comment needs a row (2026-10-05)."""
    _commit(repo, "src/a.py", "# Explains the thing\na = 1\n# Also this\nb = 2\n")
    d = {"worktree": str(repo), "base": "base", "thread": []}
    out = {
        "state": "done",
        "result": {"comments": [{"path": "src/a.py", "line": 1, "reason": "why a is 1"}]},
        "error": None,
    }
    _apply_conventions(d, out, _rp([]))
    assert out["state"] == "done"
    assert [cm["reason"] for cm in out["conventions"]["comments"]] == ["why a is 1", ""]
    assert out["conventions"]["noise"] == []


def test_the_return_gate_blocks_comment_noise_too(repo: Path):
    _commit(repo, "src/a.py", "# Added for feature 028 so the gate sees it\na = 1\n")
    for name in ("b", "c", "d"):
        _commit(repo, f"src/{name}.py", "# Shared boilerplate line\nx = 1\n")
    d = {"worktree": str(repo), "base": "base", "thread": []}
    out = {"state": "done", "result": {}, "error": None}
    _apply_conventions(d, out, _rp([]))
    assert out["state"] == "blocked"
    whys = sorted(n["why"] for n in out["conventions"]["noise"])
    assert whys[0] == 'cites "feature 028"' and whys.count("same text in 3 files") == 3
    assert out["result"]["next_steps"][0].startswith("comment noise: ")


def test_a_docstring_that_cites_an_id_is_noise(repo: Path):
    _commit(repo, "src/m.py", 'def f():\n    """Fixes the cap (lesson L-20261005-13)."""\n    return 1\n')
    d = {"worktree": str(repo), "base": "base", "thread": []}
    out = {"state": "done", "result": {}, "error": None}
    _apply_conventions(d, out, _rp([]))
    assert out["state"] == "blocked"
    assert [n["path"] for n in out["conventions"]["noise"]] == ["src/m.py"]
