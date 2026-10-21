"""The convention gate over a worker's diff."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_cli import repos as repos_mod
from agent_cli.dispatch import conventions as c
from agent_cli.dispatch.evaluate import _apply_conventions
from agent_cli.dispatch.handoffs import _conventions_lines
from agent_cli.dispatch.terminal import _account
from agent_cli.doctor.checks_repos import _diff_rule_checks
from agent_cli.repos_validate import validate_registry_data

TEST_RULE = {
    "paths": "tests/*test_*.py",
    "change": "added",
    "forbid": r"def\s+test_\d+\s*\(",
    "doc": "tests/AGENTS.md",
    "rule": "Test names describe the behavior",
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "tests" / "api").mkdir(parents=True)
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "tests" / "AGENTS.md").write_text(
        "- **Test names describe the behavior**, never `test_1` numbering"
        " (for example `test_rejects_missing_token`).\n"
    )
    (r / "tests" / "api" / "test_old.py").write_text("def test_000():\n    pass\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    _git(r, "branch", "base")
    return r


def _commit(r: Path, rel: str, text: str) -> None:
    p = r / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", f"add {rel}")


def _rp(rules: list[dict[str, str]]) -> repos_mod.Repo:
    return repos_mod.Repo(name="x", short="x", path="../x", remote="r", dispatch={"diff_rules": rules})


def test_parse_diff_numbers_added_lines_and_marks_new_files():
    text = (
        "diff --git a/a.py b/a.py\nnew file mode 100644\n--- /dev/null\n+++ b/a.py\n"
        "@@ -0,0 +1,2 @@\n+line one\n+line two\n"
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -10,0 +11 @@\n+inserted\n"
        "diff --git a/c.py b/c.py\ndeleted file mode 100644\n--- a/c.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n"
    )
    files = {f.path: f for f in c.parse_diff(text)}
    assert files["a.py"].status == "A" and files["a.py"].added == [(1, "line one"), (2, "line two")]
    assert files["b.py"].status == "M" and files["b.py"].added == [(11, "inserted")]
    assert files["c.py"].status == "D" and files["c.py"].added == []
    assert files["c.py"].removed == [(1, "gone")]


def test_parse_diff_reads_header_lookalikes_inside_a_hunk_as_lines():
    text = "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -4 +4 @@\n--- a/x\n+++ b/x\n"
    (f,) = c.parse_diff(text)
    assert f.path == "b.py"
    assert f.removed == [(4, "-- a/x")] and f.added == [(4, "++ b/x")]


GUARD_RULE = {
    "paths": "src/*.py",
    "change": "any",
    "forbid": r"#\s*guard:",
    "doc": "tests/AGENTS.md",
    "rule": "Test names describe the behavior",
}


@pytest.fixture
def guarded(repo: Path) -> Path:
    _commit(repo, "src/keep.py", "a = 1\n# guard: keep\nb = 2\n")
    _commit(repo, "src/gone.py", "x = 1\n# guard: gone\n")
    _git(repo, "branch", "-f", "base")
    (repo / "src/keep.py").write_text("a = 1\nb = 2\n")
    (repo / "src/gone.py").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "drop guards")
    return repo


def test_a_removal_rule_catches_a_deleted_file_and_a_removal_only_hunk(guarded: Path):
    files = c.read_diff(guarded, "base")
    assert {f.path: f.status for f in files} == {"src/gone.py": "D", "src/keep.py": "M"}
    rows = c.diff_rules_violations(files, _rp([{**GUARD_RULE, "lines": "removed"}]).diff_rules)
    assert [(r["path"], r["line"], r["text"], r["lines"]) for r in rows] == [
        ("src/gone.py", 2, "# guard: gone", "removed"),
        ("src/keep.py", 2, "# guard: keep", "removed"),
    ]
    assert c.block_lines({"violations": rows})[0] == (
        "convention: src/gone.py:2 (removed from the base) breaks tests/AGENTS.md: "
        '"Test names describe the behavior" (line: # guard: gone)'
    )
    assert _conventions_lines({"violations": rows})[2] == (
        "- **breaks** `tests/AGENTS.md` at `src/gone.py:2 (removed from the base)`: "
        '"Test names describe the behavior"'
    )


def test_a_removal_rule_honors_its_change_kind(guarded: Path):
    files = c.read_diff(guarded, "base")
    for change, paths in (("deleted", ["src/gone.py"]), ("modified", ["src/keep.py"])):
        rule = {**GUARD_RULE, "lines": "removed", "change": change}
        assert [r["path"] for r in c.diff_rules_violations(files, _rp([rule]).diff_rules)] == paths


def test_a_rule_without_lines_still_checks_added_lines_only(guarded: Path):
    assert c.diff_rules_violations(c.read_diff(guarded, "base"), _rp([GUARD_RULE]).diff_rules) == []
    _commit(guarded, "src/keep.py", "a = 1\nb = 2\n# guard: back\n")
    rows = c.diff_rules_violations(c.read_diff(guarded, "base"), _rp([GUARD_RULE]).diff_rules)
    assert rows == [
        {
            "path": "src/keep.py",
            "line": 3,
            "text": "# guard: back",
            "doc": "tests/AGENTS.md",
            "rule": "Test names describe the behavior",
        }
    ]


def test_a_new_test_file_with_numbered_tests_breaks_the_rule(repo: Path):
    _commit(
        repo,
        "tests/api/test_new.py",
        "import pytest\n\ndef test_000():\n    pass\n",
    )
    rows = c.diff_rules_violations(c.read_diff(repo, "base"), _rp([TEST_RULE]).diff_rules)
    assert [(r["path"], r["line"]) for r in rows] == [("tests/api/test_new.py", 3)]
    assert rows[0]["doc"] == "tests/AGENTS.md"


def test_a_numbered_test_added_to_an_existing_numbered_file_passes(repo: Path):
    old = repo / "tests/api/test_old.py"
    _commit(
        repo,
        "tests/api/test_old.py",
        old.read_text() + "def test_001():\n    pass\n",
    )
    assert c.diff_rules_violations(c.read_diff(repo, "base"), _rp([TEST_RULE]).diff_rules) == []


def test_a_descriptive_new_test_file_passes(repo: Path):
    _commit(
        repo,
        "tests/api/test_new.py",
        "def test_rejects_missing_token():\n    pass\n",
    )
    assert c.diff_rules_violations(c.read_diff(repo, "base"), _rp([TEST_RULE]).diff_rules) == []


def test_three_dot_diff_ignores_lines_merged_in_from_the_base(repo: Path):
    _git(repo, "checkout", "-q", "-b", "work")
    _commit(repo, "tests/api/test_mine.py", "def test_mine_works():\n    pass\n")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "tests/api/test_theirs.py", "def test_000():\n    pass\n")
    _git(repo, "checkout", "-q", "work")
    _git(repo, "merge", "-q", "--no-edit", "main")
    # The worker's base is the ref it branched from, which main has since moved past.
    paths = {f.path for f in c.read_diff(repo, "base")}
    assert "tests/api/test_mine.py" in paths


def test_added_comments_join_consecutive_lines_and_skip_code():
    files = [
        c.FileDiff(
            "a.js",
            "A",
            [
                (1, "/**"),
                (2, " * Parse the token"),
                (3, " */"),
                (4, "let x; // trailing"),
                (6, "// Given a thing"),
            ],
        ),
        c.FileDiff("b.py", "A", [(1, "#!/usr/bin/env python"), (2, "# a note")]),
        c.FileDiff("c.md", "A", [(1, "# Heading")]),
    ]
    got = [(b["path"], b["line"], b["text"]) for b in c.added_comments(files)]
    assert got == [("a.js", 1, "Parse the token"), ("a.js", 6, "Given a thing"), ("b.py", 2, "a note")]


def test_comment_text_keeps_the_name_a_reference_tag_points_at():
    files = [
        c.FileDiff(
            "k.ts", "A", [(3, '// Returns <seealso langword="null"/> when the <b>row</b> is missing.')]
        )
    ]
    assert c.added_comments(files)[0]["text"] == "Returns null when the  row  is missing."


def test_comments_already_on_the_branch_need_no_row_but_rules_hold_the_whole_branch(repo: Path):
    _commit(
        repo,
        "tests/api/test_new.py",
        "# earlier comment\ndef test_000():\n    pass\n",
    )
    start = _git(repo, "rev-parse", "HEAD").strip()
    _commit(repo, "src/b.py", "b = 1\n")
    d = {"worktree": str(repo), "base": "base", "start_sha": start, "thread": []}
    out = {"state": "done", "result": {}, "error": None}
    _apply_conventions(d, out, _rp([TEST_RULE]))
    assert out["conventions"]["comments"] == []
    assert len(out["conventions"]["violations"]) == 1
    assert out["state"] == "blocked"


def test_comments_from_a_merged_in_main_need_no_row(repo: Path):
    # A worker fast-forwarded to a remote tip that held a merge of main, so main's
    # commits sat inside start..HEAD and every comment in them was asked for a row.
    _git(repo, "checkout", "-q", "-b", "work")
    _commit(repo, "src/mine.py", "m = 1\n")
    start = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "src/theirs.py", "# written on main\nt = 1\n")
    _git(repo, "checkout", "-q", "work")
    _git(repo, "merge", "-q", "--no-edit", "main")
    _commit(repo, "src/mine.py", "# written by the worker\nm = 1\n")
    d = {"worktree": str(repo), "base": "base", "start_sha": start, "thread": []}
    out = {"state": "done", "result": {}, "error": None}
    _apply_conventions(d, out, _rp([]))
    assert [(cm["path"], cm["text"]) for cm in out["conventions"]["comments"]] == [
        ("src/mine.py", "written by the worker")
    ]


def test_rules_judge_a_branch_that_merged_newer_main_on_its_own_files(repo: Path):
    # A branch that merged a far newer main: `base...HEAD` would hold every file main changed.
    _git(repo, "checkout", "-q", "-b", "work")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "tests/api/test_main.py", "def test_001():\n    pass\n")
    _git(repo, "update-ref", "refs/remotes/origin/main", "main")
    _git(repo, "checkout", "-q", "work")
    _git(repo, "merge", "-q", "--no-edit", "main")
    _commit(repo, "tests/api/test_mine.py", "def test_002():\n    pass\n")
    rp = repos_mod.Repo(
        name="x",
        short="x",
        path="../x",
        remote="r",
        default_branch="main",
        dispatch={"diff_rules": [TEST_RULE]},
    )
    d = {"worktree": str(repo), "base": "base", "thread": []}
    gated = c.gate(d, rp, {})
    assert gated is not None
    assert [v["path"] for v in gated["violations"]] == ["tests/api/test_mine.py"]
    # With no origin/<default> in the worktree, the dispatch base is the fallback.
    _git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    assert c.pr_base(repo, d, rp) == "base"


def test_apply_conventions_demotes_done_and_names_the_doc(repo: Path):
    _commit(repo, "tests/api/test_new.py", "def test_000():\n    pass\n")
    d = {"worktree": str(repo), "base": "base", "thread": [{"from": "manager", "text": "build it"}]}
    out = {"state": "done", "result": {"next_steps": ["push"]}, "error": None}
    _apply_conventions(d, out, _rp([TEST_RULE]))
    assert out["state"] == "blocked"
    assert out["result"]["next_steps"][0].startswith("convention: tests/api/test_new.py:1")
    assert "tests/AGENTS.md" in out["result"]["next_steps"][0]
    assert out["result"]["next_steps"][-1] == "push"


def test_apply_conventions_records_but_does_not_demote_a_question(repo: Path):
    _commit(repo, "tests/api/test_new.py", "def test_000():\n    pass\n")
    d = {"worktree": str(repo), "base": "base", "thread": []}
    out = {"state": "question", "result": {}, "error": None}
    _apply_conventions(d, out, _rp([TEST_RULE]))
    assert out["state"] == "question"
    assert len(out["conventions"]["violations"]) == 1


def test_apply_conventions_skips_a_missing_worktree(tmp_path: Path):
    out = {"state": "done", "result": {}, "error": None}
    _apply_conventions({"worktree": str(tmp_path / "gone"), "base": "base"}, out, _rp([TEST_RULE]))
    assert out["state"] == "done" and "conventions" not in out


def _record(repo: Path) -> dict:
    start = _git(repo, "rev-parse", "HEAD").strip()
    return {"worktree": str(repo), "base": "base", "start_sha": start, "thread": [], "attempt": 1}


def _attempt(d: dict, comments: list | None = None, conventions: list | None = None) -> dict:
    """One attempt's return through the real gate, accounted onto the record the way `finalize`
    does, then the record moved to the next attempt the way a resume does."""
    out = {
        "state": "done",
        "result": {"comments": comments or [], "conventions": conventions or []},
        "error": None,
    }
    _apply_conventions(d, out, _rp([]))
    _account(d, out)
    d["attempt"] += 1
    return out


def test_an_edited_comment_drops_its_old_reason_and_a_current_row_wins(repo: Path):
    d = _record(repo)
    _commit(repo, "src/a.py", "# why a is one\na = 1\n")
    _attempt(d, [{"path": "src/a.py", "line": 1, "reason": "a's value"}])
    _commit(repo, "src/a.py", "# why a is one, not two\na = 1\n")
    out = _attempt(d)
    assert out["state"] == "done"
    assert [(cm["text"], cm["reason"]) for cm in out["conventions"]["comments"]] == [
        ("why a is one, not two", "")
    ]
    out = _attempt(d, [{"path": "src/a.py", "line": 1, "reason": "rules out two"}])
    assert out["state"] == "done"
    assert [cm["reason"] for cm in out["conventions"]["comments"]] == ["rules out two"]


def test_a_comment_deleted_then_put_back_does_not_get_its_old_reason(repo: Path):
    d = _record(repo)
    _commit(repo, "src/a.py", "# why a is one\na = 1\n")
    _attempt(d, [{"path": "src/a.py", "line": 1, "reason": "a's value"}])
    _commit(repo, "src/a.py", "a = 1\n")
    assert _attempt(d)["state"] == "done"
    _commit(repo, "src/a.py", "# why a is one\na = 1\n")
    out = _attempt(d)
    assert out["state"] == "done"
    assert [(cm["text"], cm["reason"]) for cm in out["conventions"]["comments"]] == [("why a is one", "")]


def test_named_paths_resolve_full_paths_new_files_and_unique_bare_names(repo: Path):
    text = "Mirror `test_old.py`. Create `tests/api/test_new.py`. Ignore e.g. and v1.2 and `missing.py`."
    assert c.named_paths(repo, text) == [
        "tests/api/test_old.py",
        "tests/api/test_new.py",
    ]


def test_governing_docs_walk_up_and_skip_a_claude_md_that_only_imports_agents(repo: Path):
    (repo / "tests" / "CLAUDE.md").write_text("@AGENTS.md\n")
    (repo / "AGENTS.md").write_text("root, loaded already\n")
    assert c.governing_docs(repo, ["tests/api/x.py"]) == ["tests/AGENTS.md"]
    (repo / "tests" / "CLAUDE.md").write_text("own rules\n")
    assert c.governing_docs(repo, ["tests"]) == [
        "tests/AGENTS.md",
        "tests/CLAUDE.md",
    ]


def test_prompt_section_names_the_docs_and_the_rules(repo: Path):
    lines = "\n".join(c.prompt_section(repo, "model it on `test_old.py`", _rp([TEST_RULE]).diff_rules))
    assert "`tests/AGENTS.md`" in lines
    assert "outrank this work order" in lines
    assert "Test names describe the behavior" in lines
    assert "`comments` row" not in lines  # no comment needs a row (2026-10-05)
    assert c.prompt_section(repo, "nothing named here", []) == []


@pytest.mark.parametrize(
    ("patch", "needle"),
    [
        ({"change": "new"}, "change must be one of"),
        ({"forbid": "["}, "not a valid regex"),
        ({"doc": ""}, "doc must be a non-empty string"),
        ({"lines": "both"}, "lines must be one of added, removed"),
        ({"lines": ""}, "lines must be a non-empty string"),
        ({"lines": "removed"}, "change must be one of modified, deleted, any when lines is removed"),
        ({"change": "deleted"}, "change must be one of added, modified, any when lines is added"),
        ({"side": "removed"}, "and optionally lines"),
    ],
)
def test_validate_rejects_a_bad_diff_rule(patch: dict[str, str], needle: str):
    data = {
        "version": 1,
        "repos": [
            {"name": "x", "path": "../x", "remote": "r", "dispatch": {"diff_rules": [{**TEST_RULE, **patch}]}}
        ],
    }
    assert any(needle in p for p in validate_registry_data(data))


def test_doctor_warns_when_the_doc_no_longer_says_the_rule(repo: Path):
    assert _diff_rule_checks(_rp([TEST_RULE]), repo)[0]["level"] == "ok"
    (repo / "tests/AGENTS.md").write_text("Name tests however you like.\n")
    assert _diff_rule_checks(_rp([TEST_RULE]), repo)[0]["level"] == "warn"
    assert _diff_rule_checks(_rp([{**TEST_RULE, "doc": "nope.md"}]), repo)[0]["level"] == "fail"
