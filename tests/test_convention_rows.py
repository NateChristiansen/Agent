"""The docs that govern a change: every AGENTS.md or CLAUDE.md above a path the dispatch's own
commits touched is listed for the handoff and the reviewer. No `conventions` row is required, and
a row a return does carry is reported, never blocking (owner decision, 2026-10-05)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import symlinks
from agent_cli import repos as repos_mod
from agent_cli.dispatch.evaluate import _apply_conventions
from agent_cli.dispatch.handoffs import _conventions_lines

UNIT_DOC = "tests/widgets/AGENTS.md"
UNIT_RULE = "Each widget test **renders once, then asserts**."
UNIT_TEST = "tests/widgets/test_dial.py"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _write(r: Path, rel: str, text: str | None) -> None:
    p = r / rel
    if text is None:
        p.unlink()
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _commit(r: Path, files: dict[str, str | None]) -> str:
    for rel, text in files.items():
        _write(r, rel, text)
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "change")
    return _git(r, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    # A root doc, and a test project with its own widget-test rule.
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    _commit(
        r,
        {
            "AGENTS.md": "Keep tests close to the code.\n",
            UNIT_DOC: f"# Widget tests\n\n{UNIT_RULE} Keep fixtures small.\n",
            "tests/widgets/test_knob.py": "int e;\nint f;\n",
            "src/widget.py": "int s;\n",
        },
    )
    _git(r, "branch", "base")
    return r


def _run(repo: Path, rows: list[dict[str, str]] | None = None, start: str | None = None) -> dict:
    d = {"worktree": str(repo), "base": "base", "thread": []}
    if start:
        d["start_sha"] = start
    result = {"next_steps": ["push"]} if rows is None else {"conventions": rows, "next_steps": ["push"]}
    out = {"state": "done", "result": result, "error": None}
    rp = repos_mod.Repo(name="x", short="x", path="../x", remote="r", dispatch={})
    _apply_conventions(d, out, rp)
    return out


def test_a_governed_change_lists_its_doc_and_needs_no_row(repo: Path):
    _commit(repo, {UNIT_TEST: "class T {}\n"})
    out = _run(repo)
    assert out["state"] == "done" and out["result"]["next_steps"] == ["push"]
    assert out["conventions"]["governing_docs"] == [UNIT_DOC]  # the root doc is never listed


def test_a_quote_is_reported_and_a_wrong_one_does_not_block(repo: Path):
    _commit(repo, {UNIT_TEST: "class T {}\n"})
    # Whitespace is ignored: the quote may wrap differently from the doc.
    good = _run(repo, [{"doc": UNIT_DOC, "quote": "Each widget test **renders once,\n then asserts**."}])
    assert good["conventions"]["answered"] == [
        {"doc": UNIT_DOC, "quote": "Each widget test **renders once,\n then asserts**."}
    ]
    bad = _run(
        repo, [{"doc": UNIT_DOC, "quote": "Unit tests are welcome."}, {"doc": "Nope/AGENTS.md", "quote": "x"}]
    )
    assert [b["reason"] for b in bad["conventions"]["bad_quotes"]] == [
        "quote not found in the doc",
        "doc not found",
    ]
    assert bad["state"] == "done"


def test_a_change_outside_any_nested_doc_lists_none(repo: Path):
    _commit(repo, {"src/widget.py": "int s2;\n"})
    out = _run(repo)
    assert out["state"] == "done" and out["conventions"]["governing_docs"] == []


def test_a_delete_only_commit_and_a_removal_only_edit_each_list_the_doc(repo: Path):
    start = _git(repo, "rev-parse", "HEAD")
    _commit(repo, {"tests/widgets/test_knob.py": "int e;\n"})
    assert _run(repo, start=start)["conventions"]["governing_docs"] == [UNIT_DOC]
    _git(repo, "reset", "-q", "--hard", "base")
    _commit(repo, {"tests/widgets/test_knob.py": None})
    assert _run(repo, start=start)["conventions"]["governing_docs"] == [UNIT_DOC]


def test_commits_before_start_do_not_count(repo: Path):
    _commit(repo, {UNIT_TEST: "class T {}\n"})
    start = _git(repo, "rev-parse", "HEAD")
    _commit(repo, {"src/widget.py": "int s2;\n"})
    assert _run(repo, start=start)["conventions"]["governing_docs"] == []


def test_a_merged_in_main_does_not_count(repo: Path):
    _git(repo, "checkout", "-q", "-b", "work")
    start = _commit(repo, {"src/widget.py": "int s2;\n"})
    _git(repo, "checkout", "-q", "main")
    _commit(repo, {UNIT_TEST: "class FromMain {}\n"})
    _git(repo, "checkout", "-q", "work")
    _git(repo, "merge", "-q", "--no-edit", "main")
    _commit(repo, {"src/widget.py": "int s3;\n"})
    assert UNIT_TEST in _git(repo, "diff", "--name-only", "base...HEAD")
    assert _run(repo, start=start)["conventions"]["governing_docs"] == []


def test_a_file_changed_then_reverted_lists_nothing(repo: Path):
    start = _git(repo, "rev-parse", "HEAD")
    _commit(repo, {UNIT_TEST: "class T {}\n"})
    _commit(repo, {UNIT_TEST: None})
    _commit(repo, {"src/widget.py": "int s2;\n"})
    assert _run(repo, start=start)["conventions"]["governing_docs"] == []


def test_a_review_with_no_commits_lists_nothing(repo: Path):
    start = _git(repo, "rev-parse", "HEAD")
    out = _run(repo, start=start)
    assert out["state"] == "done" and out["conventions"]["governing_docs"] == []


def test_the_handoff_lists_the_governing_docs_and_any_quotes(repo: Path):
    _commit(repo, {UNIT_TEST: "class T {}\n"})
    text = "\n".join(_conventions_lines(_run(repo)["conventions"]))
    assert "no findings" in text and f"- `{UNIT_DOC}`" in text
    quoted = "\n".join(_conventions_lines(_run(repo, [{"doc": UNIT_DOC, "quote": UNIT_RULE}])["conventions"]))
    assert f'- `{UNIT_DOC}`: "{UNIT_RULE}"' in quoted
    old = "\n".join(_conventions_lines({"missing_docs": [UNIT_DOC]}))
    assert f"**doc not answered for** `{UNIT_DOC}`" in old  # records from before still render


def test_a_doc_outside_the_worktree_is_never_read(repo: Path, tmp_path: Path):
    # The doc path comes from the worker, so it must not steer the manager's reads out of the
    # worktree: not by `..`, not by an absolute path, not through a symlink.
    symlinks.require()
    secret = tmp_path / "AGENTS.md"
    secret.write_text("the manager's secret\n")
    (repo / "tests/widgets" / "Linked").mkdir()
    (repo / "tests/widgets" / "Linked" / "AGENTS.md").symlink_to(secret)
    _commit(repo, {UNIT_TEST: "class T {}\n"})
    rows = [
        {"doc": "../AGENTS.md", "quote": "the manager's secret"},
        {"doc": str(secret), "quote": "the manager's secret"},
        {"doc": "tests/widgets/Linked/AGENTS.md", "quote": "the manager's secret"},
    ]
    loop = repo / "Loop"
    loop.mkdir()
    (loop / "AGENTS.md").symlink_to(loop / "AGENTS.md")
    rows.append({"doc": "Loop/AGENTS.md", "quote": "x"})
    out = _run(repo, rows)
    reasons = [b["reason"] for b in out["conventions"]["bad_quotes"]]
    assert reasons[:3] == ["doc outside the worktree"] * 3
    assert len(reasons) == 4  # the symlink loop is refused, not raised
    assert out["conventions"]["answered"] == []


def test_a_quote_from_a_skill_file_inside_the_worktree_counts(repo: Path):
    # Any file inside the worktree may be quoted.
    skill = ".claude/skills/tests/SKILL.md"
    rule = "Name every test after the behavior it shows."
    _commit(repo, {skill: f"# Tests\n\n{rule}\n", UNIT_TEST: "class T {}\n"})
    out = _run(repo, [{"doc": UNIT_DOC, "quote": UNIT_RULE}, {"doc": skill, "quote": rule}])
    assert out["conventions"]["bad_quotes"] == []
    assert {"doc": skill, "quote": rule} in out["conventions"]["answered"]
