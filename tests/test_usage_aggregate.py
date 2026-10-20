"""Several machines' usage records summed into one journal day."""

from __future__ import annotations

from datetime import date

from agent_cli.usage import aggregate
from usage_helpers import UPDATED, sample_checkpoint, sample_record

DAY = date(2026, 10, 9)


def test_two_machines_are_summed():
    windows = sample_record("windows", dispatch_usd=8.59, manager_usd=1.0)
    out = aggregate(DAY, ["mac", "windows"], {"mac": sample_record(), "windows": windows}, {})
    assert out["usage_machines"] == {
        "mac": {"status": "complete", "updated": UPDATED},
        "windows": {"status": "complete", "updated": UPDATED},
    }
    assert out["tokens_complete"] is True
    assert out["session_tokens"] == 3000
    assert out["requests"] == 6
    assert out["tokens_manager"] == 1400
    assert out["models"]["gpt-6-sol"]["cache_read"] == 800
    assert out["spend_usd"] == 11.84
    assert out["manager_usd"] == 5.5
    assert out["dispatches"] == 4
    assert out["spend_complete"] is True


def test_missing_machine_makes_the_total_a_lower_bound():
    out = aggregate(DAY, ["mac", "windows"], {"mac": sample_record()}, {})
    assert out["usage_machines"]["windows"] == {"status": "missing"}
    assert out["tokens_complete"] is False
    assert out["session_tokens"] == 1500


def test_inactive_machine_with_a_covering_checkpoint_is_complete():
    checkpoint = sample_checkpoint("windows", covered_through="2026-10-10T00:00:00-06:00")
    out = aggregate(DAY, ["mac", "windows"], {"mac": sample_record()}, {"windows": checkpoint})
    assert out["usage_machines"]["windows"] == {"status": "inactive", "updated": UPDATED}
    assert out["tokens_complete"] is True
    assert out["session_tokens"] == 1500


def test_checkpoint_short_of_the_day_end_is_missing():
    checkpoint = sample_checkpoint("windows", covered_through="2026-10-09T23:00:00-06:00")
    out = aggregate(DAY, ["mac", "windows"], {"mac": sample_record()}, {"windows": checkpoint})
    assert out["usage_machines"]["windows"] == {"status": "missing"}
    assert out["tokens_complete"] is False


def test_partial_machine_is_summed_but_not_complete():
    partial = sample_record(
        "windows", status="partial", requests=2, total_tokens=1000, tokens_other=0, tokens_worker=200
    )
    del partial["models"]["gpt-6-sol"]
    partial["sources"]["codex"] = {"status": "unavailable", "reason": "rollouts unreadable"}
    out = aggregate(DAY, ["mac", "windows"], {"mac": sample_record(), "windows": partial}, {})
    assert out["usage_machines"]["windows"] == {"status": "partial", "updated": UPDATED}
    assert out["tokens_complete"] is False
    assert out["session_tokens"] == 2500


def test_invalid_record_counts_as_missing():
    bad = sample_record("windows", tokens_other=999)
    out = aggregate(DAY, ["mac", "windows"], {"mac": sample_record(), "windows": bad}, {})
    assert out["usage_machines"]["windows"]["status"] == "missing"
    assert "tokens_* sum" in out["usage_machines"]["windows"]["reason"]
    assert out["session_tokens"] == 1500


def test_unexpected_machine_with_a_record_is_included():
    out = aggregate(DAY, ["mac"], {"mac": sample_record(), "laptop": sample_record("laptop")}, {})
    assert sorted(out["usage_machines"]) == ["laptop", "mac"]
    assert out["session_tokens"] == 3000


def test_costs_unavailable_on_one_machine_leaves_spend_incomplete():
    windows = sample_record("windows", status="partial")
    windows["sources"]["costs"] = {"status": "unavailable", "reason": "unreadable"}
    for key in (
        "manager_usd",
        "manager_complete",
        "dispatches",
        "dispatch_usd",
        "probe_usd",
        "spend_complete",
    ):
        del windows[key]
    out = aggregate(DAY, ["mac", "windows"], {"mac": sample_record(), "windows": windows}, {})
    assert out["spend_usd"] == 3.25
    assert out["spend_complete"] is False
    assert out["manager_complete"] is False
