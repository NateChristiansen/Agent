"""Scoped attempt checks under selection v2 (dispatch/test_select.py, ADR-0045): the table maps
every source module and helper, history's failed runs each select their failing tests, and no
change makes a dispatch run the whole suite."""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import pytest

from agent_cli.dispatch import test_select
from agent_cli.repos import VerificationCommand, VerificationConfig
from land_helpers import git as _git

ROOT = Path(__file__).resolve().parents[1]
TABLE = tomllib.loads((ROOT / "tests" / "selection.toml").read_text(encoding="utf-8"))
HEAD_TESTS = sorted(f"tests/{p.name}" for p in (ROOT / "tests").glob("test_*.py"))
Lookup = Callable[[str], list[str]]


def _none(_path: str) -> list[str]:
    return []


def _choose(
    *paths: str,
    status: str = "M",
    tests: list[str] | None = None,
    mentions: Lookup = _none,
    importers: Lookup = _none,
) -> test_select.Selection:
    available = HEAD_TESTS + [t for t in tests or [] if t not in HEAD_TESTS]
    seen = test_select.Observed(TABLE, [(status, p) for p in paths], available, None, mentions, importers)
    return test_select.choose(seen, TABLE)


def test_the_table_is_version_2_with_a_nonempty_fallback():
    assert test_select.version_of(TABLE) == 2 and "full" not in TABLE
    assert [t for r in TABLE["fallback"] for t in r["tests"]]


def test_every_source_module_and_test_helper_maps_to_a_rule():
    rules = [*TABLE["broad"], *TABLE["bundle"]]
    unmapped: list[str] = []
    for path in sorted(ROOT.glob("src/agent_cli/**/*.py")) + sorted(ROOT.glob("tests/*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith("tests/test_"):
            continue
        if not any(PurePosixPath(rel).full_match(p) for r in rules for p in r["paths"]):
            unmapped.append(rel)
    assert unmapped == [], "add each to a bundle or a broad rule in tests/selection.toml"


def test_every_rule_names_tests_that_exist():
    for kind in ("broad", "bundle", "fallback"):
        for rule in TABLE[kind]:
            for pattern in rule["tests"]:
                assert any(PurePosixPath(t).full_match(pattern) for t in HEAD_TESTS), (kind, rule, pattern)


# The nine failed verification runs the audit replayed (2026-10): what each changed, and the test
# files that failed. Each must select its failing tests (or name them in an extra command).
REPLAYS: list[tuple[str, list[str], list[str]]] = [
    (
        "CLI/ops comparison differed in record_revision",
        [
            "src/agent_cli/dispatch/closing.py",
            "src/agent_cli/dispatch/terminal.py",
            "src/agent_cli/mcp/coverage.py",
            "src/agent_cli/ops/__init__.py",
            "tests/test_dispatch_notice_producers.py",
        ],
        ["tests/test_ops_behaviour.py"],
    ),
    (
        "preview mutation and verification preflight",
        [
            "src/agent_cli/dispatch/allocation.py",
            "src/agent_cli/dispatch/lifecycle.py",
            "src/agent_cli/leases.py",
            "src/agent_cli/worktree.py",
            "tests/test_dispatch_alloc.py",
        ],
        ["tests/test_dispatch_mcp_launch.py", "tests/test_dispatch_verification_preflight.py"],
    ),
    (
        "root pollution from the verify lane",
        ["src/agent_cli/dispatch/lane.py", "tests/test_dispatch_run_temproot.py"],
        [],
    ),
    (
        "process inventory untrusted under churn",
        ["src/agent_cli/dispatch/lane.py", "tests/test_dispatch_run_temproot.py"],
        ["tests/test_dispatch_inventory_real.py"],
    ),
    (
        "manager evals could not create features without linked notes",
        [
            "src/agent_cli/features.py",
            "src/agent_cli/notes_claims.py",
            "tests/conftest.py",
            "tests/harness_root.py",
        ],
        [],  # its failing test (test_evals.py) was deleted with the evals; the change stays scoped
    ),
    (
        "root pollution beside a phase-graph change",
        [
            "harness/README.md",
            "harness/phase-recovery.md",
            "src/agent_cli/templates.py",
            "tests/test_phase_graph.py",
        ],
        ["tests/test_setup.py"],
    ),
    (
        "undocumented dispatch keys and a resume lease mutation",
        [
            "src/agent_cli/dispatch/holdings.py",
            "src/agent_cli/dispatch/lifecycle.py",
            "tests/test_dispatch_holdings.py",
        ],
        # its doc-key test (test_agent_yaml_doc.py) was deleted with the docs meta-tests
        ["tests/test_dispatch_verification_preflight.py"],
    ),
]


@pytest.mark.parametrize("why,changed,failing", REPLAYS, ids=[r[0] for r in REPLAYS])
def test_each_historical_failure_selects_its_failing_tests(why: str, changed: list[str], failing: list[str]):
    mentions, importers = test_select._lookups(ROOT, "HEAD")  # pyright: ignore[reportPrivateUsage]
    selection = _choose(*changed, tests=changed, mentions=mentions, importers=importers)
    assert selection.scope == "scoped", (why, selection.reason)
    named = " ".join(selection.extra)
    assert all(f in selection.tests or PurePosixPath(f).name in named for f in failing), (why, selection)


def test_ordinary_dispatch_and_notes_changes_are_scoped():
    dispatch = _choose("src/agent_cli/dispatch/spend.py")
    assert dispatch.scope == "scoped"
    assert {"tests/test_dispatch_spend.py", "tests/test_ops_behaviour.py"} <= set(dispatch.tests)
    notes = _choose("src/agent_cli/notes_claims.py")
    assert notes.scope == "scoped" and "tests/test_notes_claims.py" in notes.tests


@pytest.mark.parametrize(
    "path,named",
    [
        ("tests/conftest.py", "tests/test_seeded_isolation.py"),
        ("src/agent_cli/core.py", "tests/test_config_file.py"),
        ("src/agent_cli/ops/__init__.py", "tests/test_ops_cases.py"),
        ("pyproject.toml", "tests/test_doctor.py"),
        ("uv.lock", "tests/test_doctor.py"),
        ("bin/agent", "tests/test_dispatch_launch_flags.py"),
        (".github/workflows/ci.yml", "tests/test_ci_workflow.py"),
        ("src/agent_cli/hooks/guards.py", "tests/test_permissions.py"),
        (".claude/settings.json", "tests/test_permissions.py"),
        ("src/agent_cli/dispatch/verification_process.py", "tests/test_dispatch_verify_lane.py"),
        ("src/agent_cli/mcp/specs_dispatch.py", "tests/test_mcp_wording.py"),
    ],
)
def test_a_broad_path_is_scoped_to_its_rule_s_tests(path: str, named: str):
    """What used to run the whole suite at dispatch now runs the broad rule's tests; the landing
    certificate runs everything once, for the exact candidate (ADR-0045)."""

    def everything(changed: str) -> list[str]:
        return list(HEAD_TESTS) if changed == path else []  # as if every test imported it

    selection = _choose("src/agent_cli/dispatch/spend.py", path, importers=everything)
    assert selection.scope == "scoped", selection.reason
    assert named in selection.tests and len(selection.tests) < len(HEAD_TESTS) // 2


@pytest.mark.parametrize(
    "path",
    [
        "src/agent_cli/permissions.py",
        "src/agent_cli/dispatch/codex_permissions.py",
        "src/agent_cli/dispatch/settings.py",
    ],
)
def test_the_mcp_grant_contract_selects_both_providers(path: str):
    selection = _choose(path)
    assert selection.scope == "scoped", selection.reason
    assert {
        "tests/test_doctor_repo_mcp.py",
        "tests/test_permissions.py",
        "tests/test_doctor_malformed_records.py",
        "tests/test_forge.py",
    } <= set(selection.tests), selection


@pytest.mark.parametrize("path", ["src/agent_cli/brand_new_module.py", "Makefile"])
def test_an_unmapped_path_adds_the_fallback(path: str):
    selection = _choose(path)
    fallback = {t for r in TABLE["fallback"] for t in r["tests"]}
    assert selection.scope == "scoped" and "unmapped" in selection.reason
    assert fallback <= set(selection.tests)


def test_a_changed_helper_selects_the_tests_that_import_it():
    asked: list[str] = []

    def importers(path: str) -> list[str]:
        asked.append(path)
        return ["tests/test_dispatch_spend.py"]

    selection = _choose("tests/land_helpers.py", importers=importers)
    assert asked == ["tests/land_helpers.py"] and "tests/test_dispatch_spend.py" in selection.tests


def test_runtime_changes_add_the_inventory_stress_pass():
    selection = _choose("src/agent_cli/procs.py")
    assert selection.extra and "test_dispatch_inventory_real.py" in selection.extra[0]


def test_a_lazily_imported_module_selects_its_bundle_and_its_own_tests():
    """`resume_mode` is imported inside a supervisor function, never at module scope."""
    selection = _choose("src/agent_cli/dispatch/resume_mode.py")
    assert selection.scope == "scoped"
    assert {"tests/test_dispatch_resume_mode.py", "tests/test_dispatch_resume.py"} <= set(selection.tests)


def test_a_deleted_test_and_an_empty_diff_run_the_fallback():
    fallback = {t for r in TABLE["fallback"] for t in r["tests"]}
    deleted = _choose("tests/test_dispatch_spend.py", status="D")
    assert deleted.scope == "scoped" and "deleted" in deleted.reason and fallback <= set(deleted.tests)
    empty = _choose()
    assert empty.reason == "empty-diff" and set(empty.tests) == fallback


def test_a_doc_change_selects_the_tests_that_mention_it():
    seen: list[str] = []

    def mentions(path: str) -> list[str]:
        seen.append(path)
        return ["tests/test_leases.py"]

    selection = _choose("harness/safety.md", mentions=mentions)
    assert seen == ["harness/safety.md"]
    assert {"tests/test_leases.py", "tests/test_file_sizes.py"} <= set(selection.tests)


def test_a_selection_of_the_whole_suite_is_refused():
    def everything(_path: str) -> list[str]:
        return list(HEAD_TESTS)

    selection = _choose("src/agent_cli/dispatch/spend.py", importers=everything)
    assert selection.scope == "refused" and "landing certificate" in selection.reason
    assert selection.tests == ()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "src" / "agent_cli" / "dispatch").mkdir(parents=True)
    (root / "tests" / "selection.toml").write_text((ROOT / "tests" / "selection.toml").read_text())
    for name in ("test_dispatch_spend.py", "test_maintenance.py", "test_notes.py"):
        (root / "tests" / name).write_text("def test_x():\n    pass\n")
    (root / "src" / "agent_cli" / "dispatch" / "spend.py").write_text("x = 1\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    return root


def test_select_reads_the_table_from_the_base_not_the_candidate(repo: Path):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "src" / "agent_cli" / "dispatch" / "spend.py").write_text("x = 2\n")
    (repo / "tests" / "selection.toml").write_text('[[full]]\nreason = "nothing"\npaths = []\n')
    _git(repo, "commit", "-qam", "change spend and gut the table")
    selection = test_select.select(repo, base, _git(repo, "rev-parse", "HEAD"), 2)
    assert selection.scope == "scoped" and "tests/test_dispatch_spend.py" in selection.tests


def test_select_scopes_a_plain_change_and_refuses_without_a_base_or_table(repo: Path):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "src" / "agent_cli" / "dispatch" / "spend.py").write_text("x = 2\n")
    _git(repo, "commit", "-qam", "change spend")
    head = _git(repo, "rev-parse", "HEAD")
    selection = test_select.select(repo, base, head, 2)
    assert selection.scope == "scoped" and selection.policy == 2
    assert set(selection.tests) == {"tests/test_dispatch_spend.py", "tests/test_maintenance.py"}
    assert test_select.select(repo, None, head, 2).scope == "refused"
    unreadable = test_select.select(repo, "0" * 40, head, 2)
    assert unreadable.scope == "refused" and "could not be read" in unreadable.reason


def test_only_the_pytest_command_is_narrowed():
    config = VerificationConfig(
        (VerificationCommand("uv run ruff check src tests", 60), VerificationCommand("uv run pytest", 900)),
        1200,
    )
    scoped = test_select.Selection(
        "scoped", "selected", ("src/x.py",), ("tests/test_a.py", "tests/test_b c.py")
    )
    narrowed = test_select.config_for(config, scoped)
    assert narrowed.commands[0] == config.commands[0]
    assert narrowed.commands[1].command == "uv run pytest tests/test_a.py 'tests/test_b c.py'"
    full = test_select.Selection("full", "core", ("src/agent_cli/core.py",))
    assert test_select.config_for(config, full) == config
    stressed = test_select.Selection(
        "full", "verification-runtime", ("src/agent_cli/procs.py",), (), ("uv run pytest x",)
    )
    assert [c.command for c in test_select.config_for(config, stressed).commands][-1] == "uv run pytest x"


def test_published_policy_one_evidence_has_an_explicit_transition(tmp_path: Path):
    """Codex review, phase 5: policy 1 was deleted, and acceptance re-derived its row as a v2
    `full / pre-v2 dispatch` and raised on the digest. A published full row stands as the
    configured commands; a published scoped row cannot be checked and must be verified again."""
    from agent_cli.repos import VerificationConfig

    config = VerificationConfig((), 60)
    full = test_select.Selection("full", "core", ("src/agent_cli/core.py",), (), policy=1)
    d: dict[str, object] = {"attempt": 1, "start_sha": "base", "selection_policy": 1}
    test_select.publish(d, full, head_sha="head", observed_at=None)
    assert test_select.accepted(tmp_path, d, "head", config) is config
    scoped = test_select.Selection("scoped", "selected", ("src/a.py",), ("tests/test_a.py",), policy=1)
    d2: dict[str, object] = {"attempt": 1, "start_sha": "base", "selection_policy": 1}
    test_select.publish(d2, scoped, head_sha="head", observed_at=None)
    with pytest.raises(ValueError, match="cannot be re-derived"):
        test_select.accepted(tmp_path, d2, "head", config)


def test_context_does_not_change_the_acceptance_digest():
    selection = test_select.Selection(
        "scoped",
        "unmapped",
        ("src/a.py", "src/b.py"),
        ("tests/test_a.py",),
        policy=2,
        unmapped_paths=("src/b.py",),
    )
    assert (
        selection.digest()
        == test_select.Selection("scoped", "unmapped", selection.paths, selection.tests, policy=2).digest()
    )
    assert selection.record(1)["digest"] == selection.digest()
    dispatch = {"attempt": 1, "start_sha": "base"}
    test_select.publish(dispatch, selection, head_sha="head", observed_at="2026-10-03T00:00:00+00:00")
    row = dispatch["verification_scopes"][0]
    assert (row["base_sha"], row["head_sha"], row["observed_at"]) == (
        "base",
        "head",
        "2026-10-03T00:00:00+00:00",
    )
    assert row["digest"] == selection.digest()


def test_same_digest_republish_preserves_first_head_and_time():
    selection = test_select.Selection("scoped", "selected", ("src/a.py",), ("tests/test_a.py",))
    dispatch = {"attempt": 1, "start_sha": "base"}
    test_select.publish(dispatch, selection, head_sha="first", observed_at="2026-10-01T00:00:00+00:00")
    first = dispatch["verification_scopes"][0].copy()
    test_select.publish(dispatch, selection, head_sha="second", observed_at="2026-10-02T00:00:00+00:00")
    assert dispatch["verification_scopes"] == [first]
