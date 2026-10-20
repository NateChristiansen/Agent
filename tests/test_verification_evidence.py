import json
from dataclasses import FrozenInstanceError, replace

import pytest

from agent_cli.dispatch.verification_evidence import (
    Binding,
    CommandOutcome,
    Evidence,
    TreeState,
    assess,
    config_digest,
)
from agent_cli.repos import VerificationCommand, VerificationConfig, verification_config

SHA = "a" * 40
OTHER_SHA = "b" * 40
CONFIG = VerificationConfig((VerificationCommand("pytest", 10.0), VerificationCommand("ruff", 5.0)), 12.0)
BINDING = Binding("001-01", 1, SHA, config_digest(CONFIG), "verification-run-1")
CLEAN = TreeState(SHA, True, True)


def complete():
    return Evidence(
        1,
        BINDING,
        "complete",
        CLEAN,
        CLEAN,
        (CommandOutcome(0, "exited", 0, 1.0), CommandOutcome(1, "exited", 0, 1.0)),
        3.0,
    )


def assessment(raw, **kwargs):
    args = {"expected": BINDING, "config": CONFIG, "current_tree": CLEAN} | kwargs
    return assess(raw, **args)


def test_complete_evidence_round_trips_and_is_immutable():
    evidence = complete()
    raw = json.loads(json.dumps(evidence.to_dict(), allow_nan=False))
    assert Evidence.from_dict(raw) == evidence
    assert assessment(raw).verified
    assert assessment(evidence).status == "verified"
    raw["binding"]["attempt"] = 2
    assert evidence.binding.attempt == 1
    with pytest.raises(FrozenInstanceError):
        setattr(evidence, "state", "running")  # noqa: B010 — test runtime immutability
    with pytest.raises(FrozenInstanceError):
        setattr(evidence.commands[0], "exit_code", 1)  # noqa: B010 — test runtime immutability


def test_digest_covers_command_order_text_and_both_bounds():
    config = verification_config(
        {
            "verify": [
                {"command": "pytest", "timeout_seconds": 10},
                {"command": "ruff", "timeout_seconds": 5},
            ],
            "verify_timeout_seconds": 12,
        }
    )
    assert config_digest(config) == config_digest(CONFIG), "integer/float spelling is not a config change"
    variants = [
        replace(CONFIG, commands=tuple(reversed(CONFIG.commands))),
        replace(CONFIG, commands=(VerificationCommand("pytest -q", 10), CONFIG.commands[1])),
        replace(CONFIG, commands=(VerificationCommand("pytest", 9), CONFIG.commands[1])),
        replace(CONFIG, timeout_seconds=11),
    ]
    assert all(config_digest(config) != config_digest(CONFIG) for config in variants)


@pytest.mark.parametrize(
    "field,value",
    [
        ("dispatch_id", "002-01"),
        ("attempt", 2),
        ("final_sha", OTHER_SHA),
        ("config_digest", "c" * 64),
        ("run_id", "verification-run-2"),
    ],
)
def test_stale_binding_cannot_be_replayed(field, value):
    evidence = replace(complete(), binding=replace(BINDING, **{field: value}))
    assert assessment(evidence).status == "unverified"


def test_current_config_is_checked_independently_of_stored_digest():
    changed = replace(CONFIG, timeout_seconds=11.0)
    assert assessment(complete(), config=changed).status == "unverified"


@pytest.mark.parametrize("raw", [None, {}, {"commands": [{"cmd": "pytest", "exit": 0}]}, "passed", [], True])
def test_missing_and_worker_reported_evidence_is_unverified(raw):
    assert assessment(raw).status == "unverified"


@pytest.mark.parametrize("version", [True, 2, "1"])
def test_unknown_or_malformed_schema_is_unverified(version):
    raw = complete().to_dict() | {"schema_version": version}
    assert assessment(raw).status == "unverified"


@pytest.mark.parametrize("state", ["running", "cancelled", "interrupted"])
def test_partial_lifecycle_cannot_prove_completion(state):
    assert assessment(replace(complete(), state=state)).status == "incomplete"


@pytest.mark.parametrize(
    "commands",
    [
        (),
        (CommandOutcome(0, "exited", 0, 1),),
        (CommandOutcome(1, "exited", 0, 1), CommandOutcome(0, "exited", 0, 1)),
        (CommandOutcome(0, "exited", 0, 1), CommandOutcome(0, "exited", 0, 1)),
        (*complete().commands, CommandOutcome(2, "exited", 0, 1)),
    ],
)
def test_partial_duplicate_extra_and_reordered_results_are_incomplete(commands):
    assert assessment(replace(complete(), commands=commands)).status == "incomplete"


@pytest.mark.parametrize("field", ["before", "after", "current_tree"])
@pytest.mark.parametrize(
    "tree", [None, TreeState(OTHER_SHA, True, True), TreeState(SHA, False, True), TreeState(SHA, True, False)]
)
def test_pre_post_and_final_tree_are_required_and_bound_to_clean_commit(field, tree):
    evidence = complete() if field == "current_tree" else replace(complete(), **{field: tree})
    args = {"current_tree": tree} if field == "current_tree" else {}
    result = assessment(evidence, **args)
    assert result.status == ("incomplete" if tree is None else "unverified")


@pytest.mark.parametrize(
    "outcome,exit_code", [("exited", 1), ("timeout", None), ("cancelled", None), ("error", None)]
)
def test_failed_command_cannot_verify(outcome, exit_code):
    commands = (CommandOutcome(0, outcome, exit_code, 1), complete().commands[1])
    result = assessment(replace(complete(), commands=commands))
    # Audit 10 plan.md:45 (§A1): a command time bound is infrastructure, not a failed check.
    assert result.status == ("incomplete" if outcome == "timeout" else "failed")
    assert ("time bound" if outcome == "timeout" else "command 0") in result.reasons[0]


def test_per_command_total_and_impossible_elapsed_times_reject():
    commands = (CommandOutcome(0, "exited", 0, 10.5), complete().commands[1])
    # Audit 10 plan.md:45-47 (§A1) retires the pass gate for bounds, keeping invalid evidence failed.
    assert assessment(replace(complete(), commands=commands, elapsed_seconds=12)).infrastructure
    assert assessment(replace(complete(), elapsed_seconds=12.1)).infrastructure
    assert assessment(replace(complete(), elapsed_seconds=1)).status == "failed"


@pytest.mark.parametrize("elapsed", [True, -1, float("nan")])
def test_invalid_times_cannot_enter_evidence(elapsed):
    raw = complete().to_dict() | {"elapsed_seconds": elapsed}
    assert assessment(raw).status == "unverified"
    raw = complete().to_dict()
    raw["commands"][0]["elapsed_seconds"] = elapsed
    assert assessment(raw).status == "unverified"


@pytest.mark.parametrize(
    "change",
    [
        {"extra": "unknown field"},
        {"state": "passed"},
        {"commands": {}},
        {"binding": {}},
        {"before": {"head": SHA, "tracked_clean": 1, "index_clean": True}},
    ],
)
def test_malformed_snapshots_fail_closed(change):
    assert assessment(complete().to_dict() | change).status == "unverified"


def test_boolean_integer_and_partial_sha_traps_reject():
    for field, value in (("attempt", True), ("final_sha", "abcdef")):
        raw = complete().to_dict()
        raw["binding"][field] = value
        assert assessment(raw).status == "unverified"
    for field, value in (("index", False), ("exit_code", False)):
        raw = complete().to_dict()
        raw["commands"][0][field] = value
        assert assessment(raw).status == "unverified"


def test_explicit_exemption_requires_current_authorization_and_never_verifies():
    evidence = Evidence(1, BINDING, "exempt", None, None, (), 0, "read-only review --no-checks")
    assert assessment(evidence).status == "unverified"
    assert assessment(evidence, authorized_exemption="another reason").status == "unverified"
    result = assessment(evidence, authorized_exemption=evidence.exemption)
    assert result.status == "exempt" and not result.verified
    assert assessment(complete(), authorized_exemption=evidence.exemption).status == "unverified"
    with pytest.raises(ValueError, match="must not pretend"):
        replace(evidence, commands=complete().commands)


def test_legacy_absent_config_never_turns_into_success():
    binding = replace(BINDING, config_digest=config_digest(None))
    evidence = replace(complete(), binding=binding)
    assert assessment(evidence, expected=binding, config=None).status == "unverified"
    exempt = Evidence(1, binding, "exempt", None, None, (), 0, "explicit read-only review")
    result = assessment(exempt, expected=binding, config=None, authorized_exemption=exempt.exemption)
    assert result.status == "exempt" and not result.verified


def test_full_sha256_git_objects_are_supported():
    sha = "a" * 64
    binding = replace(BINDING, final_sha=sha)
    tree = TreeState(sha, True, True)
    evidence = replace(complete(), binding=binding, before=tree, after=tree)
    assert assessment(evidence, expected=binding, current_tree=tree).verified
