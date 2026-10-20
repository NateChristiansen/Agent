"""A stale verification names what changed, and a config change keeps the result but does not
count it (C11). A dispatch passed every command, then agent.yaml's timeouts changed
under it, and it was reported only as "stale dispatch, attempt, commit, config or verification
run"."""

from __future__ import annotations

from dataclasses import replace

from agent_cli.dispatch.verification_evidence import Binding, stale

OLD = "a" * 64
NEW = "b" * 64


def _binding(**over: object) -> Binding:
    base = Binding(dispatch_id="001-mac-20", attempt=1, final_sha="c" * 40, config_digest=OLD, run_id="r1")
    return replace(base, **over)  # type: ignore[arg-type]


def test_matching_evidence_is_not_stale():
    assert stale(_binding(), _binding(), OLD) is None


def test_a_config_change_after_the_run_is_named_and_not_counted():
    why = stale(_binding(), _binding(), NEW)
    assert why is not None
    assert "config changed after this run" in why and "aaaaaaaaaaaa" in why and "bbbbbbbbbbbb" in why
    assert "does not apply" in why


def test_another_attempt_or_commit_is_named():
    why = stale(_binding(), _binding(attempt=2, final_sha="d" * 40), OLD)
    assert why is not None and "attempt 1 differs from 2" in why and "final_sha" in why
