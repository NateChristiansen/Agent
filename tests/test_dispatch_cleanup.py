"""Cleanup failures cannot be accepted as successful work."""

from __future__ import annotations

import pytest

from agent_cli import dispatch
from dispatch_helpers import _new, droot, envelope, one, worker_return  # noqa: F401 — pytest fixture


@pytest.mark.parametrize("reason", ["completed", "budget_exhausted", "max_turns"])
@pytest.mark.parametrize("cancelled", [False, True])
def test_harness_disconnect_failure_blocks_done(droot, fake_claude, monkeypatch, reason, cancelled):
    result = envelope(cost=0.42)
    result["terminal_reason"] = reason
    fake_claude.scenario([one(result)])

    def factory(options):
        transport = fake_claude.factory(options)
        close = transport.close

        async def failed_close():
            await close()
            if cancelled:
                record = dispatch.load(droot, "001-mac-01")
                record["state"] = "cancelled"
                dispatch.state.save(droot, record)
            raise RuntimeError("synthetic denied cleanup with a live descendant")

        transport.close = failed_close
        return transport

    monkeypatch.setattr(dispatch.supervisor, "transport_factory", factory)
    record = _new(droot, no_checks=True)
    assert record["state"] == ("cancelled" if cancelled else "failed")
    assert record["stop_reason"] == "cleanup_failed"
    assert record["exit_code"] == 1
    assert record["last_result"]["total_cost_usd"] == 0.42
    if not cancelled:
        assert record["cost_usd"] == 0.42
    # The worker's own status survives, stored once with the envelope pointing at it.
    assert worker_return(record)["status"] == "done"
    assert "structured_output" not in record["last_result"]
    if not cancelled:
        assert "cleanup failed" in record["error"]
    assert len(fake_claude.calls()) == 1
