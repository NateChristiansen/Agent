"""Wall-clock-sensitive test files, marked and grouped (verify speed, phase 4).

These files start real processes and assert they finish, or are killed, inside a deadline:
sleep-and-kill process trees, verifier runners with second-scale budgets, notice and lane polls,
peer-net send bounds. Under heavy parallelism their deadlines, not their logic, decide the result.
They get the `timing` marker and an `xdist_group`, so with `--dist loadgroup` each group runs on
one worker at a time while every other test spreads across the rest. Grouping bounds how many
deadline tests run at once; it reserves no CPU, so the worker count in pyproject.toml is chosen
by repeated measurement, not by this list alone.

`conftest.py` imports `pytest_collection_modifyitems` from here, which is how pytest finds it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

GROUPS = 3  # timing groups, so at most this many workers run deadline tests at once

TIMING_FILES = frozenset(
    {
        "test_verification_runner.py",
        "test_verification_status.py",
        "test_verification_store.py",
        "test_verification_gitlinks.py",
        "test_telemetry_health.py",
        "test_telemetry_routing.py",
        "test_telemetry_sink.py",
        "test_procs.py",
        "test_dispatch_inventory_real.py",
        "test_procs_snapshot_churn.py",
        "test_peer_net_sender_bounds.py",
        "test_peer_net_acl_errors.py",
        "test_peer_net_crash.py",
        "test_peer_net_bootstrap.py",
        "test_peer_net_sender.py",
        "test_peer_net_pair_cli.py",
        "test_peer_net_pairing.py",
        "test_peer_net_channel.py",
        "test_peer_net_remote_list.py",
        "test_peer_net_sender_races.py",
        "test_dispatch_verify_lane.py",
        "test_dispatch_notices.py",
        "test_dispatch_notice_delivery.py",
        "test_dispatch_notice_hook_failures.py",
        "test_dispatch_recovery_verification.py",
        "test_dispatch_alloc_live.py",
        "test_dispatch_guards.py",
        "test_leases.py",
        "test_lease_keys.py",
        "test_land_queue.py",
        "test_claude_environment_boundary.py",
        "test_driver_questions.py",
        "test_codex_reset_race.py",
        "test_isolated_transport.py",
        # Codex's phase 3-5 review: real polling deadlines or elapsed-time assertions.
        "test_dispatch_verification.py",
        "test_dispatch_launch.py",
        "test_dispatch_resume.py",
        "test_dispatch_guard_spans.py",
        "test_win_transport.py",
        "test_win_tree.py",
        "test_win_driver.py",
        "test_win_lock.py",
    }
)


@pytest.hookimpl(tryfirst=True)  # before xdist's loadgroup reads the markers (Codex's review)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    ordered = sorted(TIMING_FILES)
    for item in items:
        name = Path(str(item.fspath)).name
        if name in TIMING_FILES:
            item.add_marker(pytest.mark.timing)
            item.add_marker(pytest.mark.xdist_group(name=f"timing-{ordered.index(name) % GROUPS}"))
