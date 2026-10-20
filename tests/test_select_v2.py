"""Selection policy 2 against real git histories (ADR-0045):
the launch stamp, records not on policy 2, weakened tables, empty diffs, whole-suite selections
and every refusal."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from agent_cli import verify_record
from agent_cli.core import AgentError
from agent_cli.dispatch import test_select
from land_helpers import git as _git

V1 = """
[[full]]
reason = "core"
paths = ["src/agent_cli/core.py"]

[[bundle]]
name = "spend"
paths = ["src/agent_cli/dispatch/spend.py"]
tests = ["tests/test_dispatch_spend.py"]
"""

V2 = """
version = 2

[[fallback]]
name = "fast"
tests = ["tests/test_fast.py"]

[[broad]]
reason = "core"
paths = ["src/agent_cli/core.py"]
tests = ["tests/test_core.py"]

[[bundle]]
name = "spend"
paths = ["src/agent_cli/dispatch/spend.py"]
tests = ["tests/test_dispatch_spend.py"]
"""

TESTS = ("test_dispatch_spend.py", "test_fast.py", "test_core.py", "test_ledger.py", "test_other.py")


def _repo(tmp_path: Path, table: str | None) -> Path:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "src" / "agent_cli" / "dispatch").mkdir(parents=True)
    if table is not None:
        (root / "tests" / "selection.toml").write_text(table, encoding="utf-8")
    for name in TESTS:
        (root / "tests" / name).write_text("def test_x():\n    pass\n", encoding="utf-8")
    (root / "src" / "agent_cli" / "core.py").write_text("x = 1\n", encoding="utf-8")
    (root / "src" / "agent_cli" / "dispatch" / "spend.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _commit(repo: Path, files: dict[str, str], message: str = "change", *flags: str) -> str:
    for rel, text in files.items():
        (repo / rel).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", *flags, "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def v2(tmp_path: Path) -> Path:
    return _repo(tmp_path, V2)


@pytest.fixture
def v1(tmp_path: Path) -> Path:
    return _repo(tmp_path, V1)


def test_a_launch_is_stamped_by_its_base_s_table(tmp_path: Path, v2: Path):
    assert test_select.launch_policy(v2, _git(v2, "rev-parse", "HEAD")) == 2
    bare = _repo(tmp_path / "none", None)
    assert test_select.launch_policy(bare, _git(bare, "rev-parse", "HEAD")) == 0


def test_a_new_launch_on_a_v1_base_is_refused(v1: Path):
    base = _git(v1, "rev-parse", "HEAD")
    _commit(v1, {"tests/selection.toml": V2}, "migrate")
    with pytest.raises(AgentError, match="predates selection v2"):
        test_select.launch_policy(v1, base)
    with pytest.raises(AgentError, match="predates selection v2"):
        test_select.stamp(v1, base, None)


def test_a_base_without_the_table_the_repo_keeps_is_refused(tmp_path: Path):
    repo = _repo(tmp_path, None)
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, {"tests/selection.toml": V2}, "add the table")
    with pytest.raises(AgentError, match="no readable"):
        test_select.launch_policy(repo, base)


def test_an_escalation_keeps_the_contract_it_escalates(v1: Path):
    base = _git(v1, "rev-parse", "HEAD")
    assert test_select.stamp(v1, base, {"id": "legacy"}) == 1  # a record from before the stamp
    assert test_select.stamp(v1, base, {"selection_policy": 2}) == 2


@pytest.mark.parametrize(
    ("record", "reason"), [({}, "pre-v2 dispatch"), ({"selection_policy": 0}, "no-table")]
)
def test_a_dispatch_not_on_policy_2_runs_its_commands_as_configured(
    v2: Path, record: dict[str, Any], reason: str
):
    base = _git(v2, "rev-parse", "HEAD")
    head = _commit(v2, {"src/agent_cli/core.py": "x = 2\n"})
    selection = test_select.select_for(v2, {"start_sha": base, **record}, head)
    assert (selection.scope, selection.reason, selection.tests) == ("full", reason, ())
    assert test_select.config_for(_config(), selection) == _config()
    # a pre-stamp selection digests without its policy, as rows published before the field did
    legacy_row = test_select.Selection("full", "core", ()).record(1)
    assert legacy_row["policy"] == 1
    assert test_select.Selection("full", "core", (), policy=2).digest() != legacy_row["digest"]


def test_a_multiline_import_of_a_changed_module_is_selected(v2: Path):
    """Codex's phase-2 review: a parenthesized import spanning lines escaped the line grep."""
    _commit(
        v2,
        {
            "tests/test_multiline.py": "from agent_cli.dispatch import (\n    other,\n    spend,\n)\n",
            "tests/test_not_spend.py": "from agent_cli.dispatch import (\n    spending,\n)\n",
        },
    )
    base = _git(v2, "rev-parse", "HEAD")
    head = _commit(v2, {"src/agent_cli/dispatch/spend.py": "x = 2\n"})
    selection = test_select.select(v2, base, head, 2)
    assert "tests/test_multiline.py" in selection.tests
    assert "tests/test_not_spend.py" not in selection.tests


def test_a_v2_attempt_on_a_v1_base_is_refused(v1: Path):
    base = _git(v1, "rev-parse", "HEAD")
    head = _commit(v1, {"src/agent_cli/dispatch/spend.py": "x = 2\n"})
    selection = test_select.select(v1, base, head, 2)
    assert selection.scope == "refused" and selection.reason == "base predates selection v2"


def test_a_core_change_is_scoped_at_dispatch(v2: Path):
    base = _git(v2, "rev-parse", "HEAD")
    head = _commit(v2, {"src/agent_cli/core.py": "x = 2\n"})
    selection = test_select.select(v2, base, head, 2)
    assert selection.scope == "scoped" and set(selection.tests) == {"tests/test_core.py"}


def test_a_weakened_candidate_table_is_ignored_for_every_path(v2: Path):
    base = _git(v2, "rev-parse", "HEAD")
    weakened = V2.replace('tests = ["tests/test_dispatch_spend.py"]', 'tests = ["tests/test_other.py"]')
    head = _commit(v2, {"tests/selection.toml": weakened, "src/agent_cli/dispatch/spend.py": "x = 2\n"})
    selection = test_select.select(v2, base, head, 2)
    assert "tests/test_dispatch_spend.py" in selection.tests and "tests/test_other.py" not in selection.tests


def test_an_extending_candidate_maps_the_paths_it_adds(v2: Path):
    base = _git(v2, "rev-parse", "HEAD")
    extended = (
        V2
        + '\n[[bundle]]\nname = "ledger"\npaths = ["src/agent_cli/ledger.py"]\ntests = ["tests/test_ledger.py"]\n'
    )
    head = _commit(v2, {"tests/selection.toml": extended, "src/agent_cli/ledger.py": "x = 1\n"})
    selection = test_select.select(v2, base, head, 2)
    assert "tests/test_ledger.py" in selection.tests and "unmapped" not in selection.reason


@pytest.mark.parametrize("shape", ["empty-commit", "change-then-revert"])
def test_an_empty_diff_with_head_moved_runs_the_fallback(v2: Path, shape: str):
    base = _git(v2, "rev-parse", "HEAD")
    if shape == "empty-commit":
        head = _commit(v2, {}, "nothing", "--allow-empty")
    else:
        _commit(v2, {"src/agent_cli/dispatch/spend.py": "x = 2\n"})
        head = _commit(v2, {"src/agent_cli/dispatch/spend.py": "x = 1\n"}, "revert")
    assert head != base
    selection = test_select.select(v2, base, head, 2)
    assert (selection.scope, selection.reason, selection.tests) == (
        "scoped",
        "empty-diff",
        ("tests/test_fast.py",),
    )
    assert selection.record(1)["unmapped_paths"] == []


def test_mapped_and_unmapped_paths_keep_only_the_unmapped_subset(v2: Path):
    base = _git(v2, "rev-parse", "HEAD")
    head = _commit(
        v2,
        {"src/agent_cli/dispatch/spend.py": "x = 2\n", "src/agent_cli/ledger.py": "x = 1\n"},
    )
    selection = test_select.select(v2, base, head, 2)
    row = selection.record(1)
    assert row["paths"] == ("src/agent_cli/dispatch/spend.py", "src/agent_cli/ledger.py")
    assert row["unmapped_paths"] == ["src/agent_cli/ledger.py"]
    assert selection.reason == "unmapped"


def test_deleted_unmapped_path_stays_in_reason_not_subset(v2: Path):
    _commit(v2, {"src/agent_cli/ledger.py": "x = 1\n"}, "add path")
    base = _git(v2, "rev-parse", "HEAD")
    (v2 / "src/agent_cli/ledger.py").unlink()
    _git(v2, "add", "-A")
    _git(v2, "commit", "-q", "-m", "delete path")
    selection = test_select.select(v2, base, _git(v2, "rev-parse", "HEAD"), 2)
    row = selection.record(1)
    assert selection.reason == "deleted"
    assert row["paths"] == ("src/agent_cli/ledger.py",)
    assert row["unmapped_paths"] == []


@pytest.mark.parametrize("shape", ["empty-commit", "change-then-revert"])
def test_an_empty_diff_reuses_a_certificate_for_the_tree(
    v2: Path, shape: str, monkeypatch: pytest.MonkeyPatch
):
    base = _git(v2, "rev-parse", "HEAD")
    if shape == "empty-commit":
        head = _commit(v2, {}, "nothing", "--allow-empty")
    else:
        _commit(v2, {"src/agent_cli/dispatch/spend.py": "x = 2\n"})
        head = _commit(v2, {"src/agent_cli/dispatch/spend.py": "x = 1\n"}, "revert")
    found: dict[str, Any] = {"run_id": "a" * 32, "tree": _git(v2, "rev-parse", "HEAD^{tree}"), "extra": []}

    def covers(_root: Path, _worktree: Path) -> bool:
        return True

    def lookup(_root: Path, _rev: str) -> tuple[dict[str, Any], str]:
        return found, "ok"

    monkeypatch.setattr(verify_record, "covers", covers)
    monkeypatch.setattr(verify_record, "lookup", lookup)
    selector = test_select.selector_for({"selection_policy": 2})
    reused = verify_record.reusable(v2, v2, base, selector)
    assert reused is not None and reused[0] == head


def test_a_selection_of_every_test_is_refused(v2: Path):
    base = _git(v2, "rev-parse", "HEAD")
    every = "from agent_cli.dispatch import spend\n\ndef test_x():\n    pass\n"
    _commit(v2, {f"tests/{name}": every for name in TESTS})
    head = _commit(v2, {"src/agent_cli/dispatch/spend.py": "x = 2\n"})
    selection = test_select.select(v2, _git(v2, "rev-parse", "HEAD~1"), head, 2)
    assert selection.scope == "refused" and "landing certificate" in selection.reason
    assert test_select.select(v2, base, head, 2).scope == "refused"  # the test edits select them too


def test_every_refusal_selects_nothing(v2: Path):
    head = _git(v2, "rev-parse", "HEAD")
    for selection in (
        test_select.select(v2, None, head, 2),
        test_select.select(v2, "0" * 40, head, 2),
    ):
        assert selection.scope == "refused" and selection.tests == () and selection.extra == ()
        with pytest.raises(ValueError):
            test_select.config_for(_config(), selection)


def test_an_unreadable_candidate_table_is_refused(v2: Path):
    base = _git(v2, "rev-parse", "HEAD")
    head = _commit(v2, {"tests/selection.toml": "version = [unclosed\n"})
    assert test_select.select(v2, base, head, 2).scope == "refused"


def test_an_empty_fallback_with_nothing_selected_is_refused(tmp_path: Path):
    repo = _repo(tmp_path, V2.replace('tests = ["tests/test_fast.py"]', "tests = []"))
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, {}, "nothing", "--allow-empty")
    selection = test_select.select(repo, base, head, 2)
    assert selection.scope == "refused" and "fallback" in selection.reason


def test_legacy_selection_without_context_still_accepts(v2: Path):
    base = _git(v2, "rev-parse", "HEAD")
    head = _commit(v2, {"src/agent_cli/dispatch/spend.py": "x = 2\n"})
    selection = test_select.select(v2, base, head, 2)
    row = selection.record(1)
    row.pop("unmapped_paths")
    dispatch = {"attempt": 1, "start_sha": base, "selection_policy": 2, "verification_scopes": [row]}
    assert (
        test_select.accepted(v2, dispatch, head, _config())
        .commands[0]
        .command.endswith("tests/test_dispatch_spend.py")
    )


def _config() -> Any:
    from agent_cli.repos import VerificationCommand, VerificationConfig

    return VerificationConfig((VerificationCommand("uv run pytest", 900),), 1200)


def test_the_ledger_counts_refusals_and_fallback_runs():
    rows = [
        test_select.Selection("scoped", "selected", ("a.py",), ("tests/test_a.py",), policy=2).record(1),
        test_select.Selection("scoped", "unmapped", ("b.py",), ("tests/test_fast.py",), policy=2).record(1),
        test_select.refused("base predates selection v2").record(1),
    ]
    text = test_select.rates([{"attempt": 1, "verification_scopes": rows}])
    assert "3 attempts, 2 scoped, 0 full" in text and "1 refused" in text
    assert "1 scoped ran the fallback" in text


# Codex's phase 3-5 review: each probe below changed a selection it must not have.


def test_an_extension_never_narrows_an_existing_path(v2: Path):
    _commit(v2, {"tests/test_other.py": "from agent_cli.dispatch import spend\n\ndef test_x():\n    pass\n"})
    base = _git(v2, "rev-parse", "HEAD")
    broader = (
        V2
        + '\n[[broad]]\nreason = "spend"\npaths = ["src/agent_cli/dispatch/spend.py"]\ntests = ["tests/test_dispatch_spend.py"]\n'
    )
    head = _commit(v2, {"tests/selection.toml": broader, "src/agent_cli/dispatch/spend.py": "x = 2\n"})
    selection = test_select.select(v2, base, head, 2)
    assert {"tests/test_dispatch_spend.py", "tests/test_other.py"} <= set(selection.tests)


def test_dropping_a_bundle_s_extra_is_a_weakening(tmp_path: Path):
    with_extra = V2.replace(
        'tests = ["tests/test_dispatch_spend.py"]',
        'tests = ["tests/test_dispatch_spend.py"]\nextra = ["false"]',
    )
    repo = _repo(tmp_path, with_extra)
    base = _git(repo, "rev-parse", "HEAD")
    head = _commit(repo, {"tests/selection.toml": V2, "src/agent_cli/dispatch/spend.py": "x = 2\n"})
    assert test_select.select(repo, base, head, 2).extra == ("false",)


@pytest.mark.parametrize(
    "broken",
    [
        V2.replace('paths = ["src/agent_cli/core.py"]', "paths = 7"),
        V2.replace('paths = ["src/agent_cli/core.py"]', "paths = [7]"),
        V2.replace('tests = ["tests/test_core.py"]', "tests = 7"),
        V2.replace('reason = "core"', 'reason = ""'),
        V2.replace("[[fallback]]", "[[fullback]]"),
    ],
)
def test_a_malformed_base_table_is_refused_at_launch_and_selection(tmp_path: Path, broken: str):
    repo = _repo(tmp_path, broken)
    base = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(AgentError, match="malformed"):
        test_select.launch_policy(repo, base)
    head = _commit(repo, {"src/agent_cli/dispatch/spend.py": "x = 2\n"})
    selection = test_select.select(repo, base, head, 2)
    assert selection.scope == "refused" and "malformed" in selection.reason


def test_a_malformed_candidate_is_never_an_extension():
    broken = tomllib.loads(V2.replace('tests = ["tests/test_fast.py"]', "tests = 7"))
    assert not test_select.extends(tomllib.loads(V2), broken)
