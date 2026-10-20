"""Canonical lease paths and guard identity cannot diverge or discard legacy holders."""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_cli import leases
from agent_cli.core import AgentError
from agent_cli.dispatch.guards import area_guard
from agent_cli.lease_keys import area_key
from dispatch_helpers import _new, droot, envelope, one  # noqa: F401 — droot is a fixture


@pytest.mark.parametrize(
    "area",
    [
        "repo:a:b",
        "repo_a:b",
        "repo:../x",
        "repo:/x",
        "repo:a/b",
        "repo:a\\b",
        "repo:.",
        "repo:..",
        "repo:á",
        "repo:ａ",
        "repo:a\0",
        " repo:a",
    ],
)
def test_noncanonical_area_refuses_before_any_mutation(tmp_path, area):
    with pytest.raises(AgentError, match="noncanonical lease area"):
        leases.take(tmp_path, area, session_id="fixture", pid=os.getpid())
    assert not (tmp_path / ".agent").exists()
    with pytest.raises(AgentError, match="noncanonical lease area"):
        with area_guard(tmp_path, area):
            pytest.fail("invalid area acquired a guard")
    assert not (tmp_path / ".agent").exists()


def test_canonical_paths_are_compatible_and_share_the_storage_key(tmp_path):
    for area, key in (
        ("repo:a_b", "repo_a_b"),
        ("vault:harness", "vault_harness"),
        ("feature:027", "feature_027"),
        ("dispatch:001-01", "dispatch_001-01"),
    ):
        assert area_key(area) == key
        assert leases.area_file(tmp_path, area).name == key + ".json"
        lease = leases.take(tmp_path, area, session_id="fixture", pid=os.getpid())["lease"]
        saved = leases.get(tmp_path, area)
        assert saved is not None and saved["session_id"] == lease["session_id"]


def test_colon_aliases_cannot_both_publish_under_controlled_contention(tmp_path):
    barrier = threading.Barrier(2)

    def take(area):
        barrier.wait(5)
        try:
            leases.take(tmp_path, area, session_id=area, pid=os.getpid())
            return "taken"
        except AgentError:
            return "refused"

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(take, area) for area in ("repo:a_b", "repo:a:b")]
        assert [future.result(timeout=10) for future in futures] == ["taken", "refused"]
    saved = leases.get(tmp_path, "repo:a_b")
    assert saved is not None and saved["area"] == "repo:a_b"
    assert len(list((tmp_path / ".agent/leases").glob("*.json"))) == 1


@pytest.mark.parametrize(
    "legacy_area,filename",
    [
        ("repo:a:b", "repo_a_b.json"),
        ("repo:ａ_b", "repo_ａ_b.json"),
    ],
)
def test_legacy_alias_is_preserved_and_cannot_be_silently_reclaimed(tmp_path, legacy_area, filename):
    directory = tmp_path / ".agent/leases"
    directory.mkdir(parents=True)
    path = directory / filename
    payload = {"area": legacy_area, "session_id": "old-holder", "pid": os.getpid()}
    original = json.dumps(payload).encode()
    path.write_bytes(original)
    for action in (
        lambda: leases.get(tmp_path, "repo:a_b"),
        lambda: leases.take(tmp_path, "repo:a_b", session_id="new-holder", pid=os.getpid()),
        lambda: leases.wait(tmp_path, "repo:a_b", session_id="new-holder", pid=os.getpid(), timeout=0),
        lambda: leases.release(tmp_path, "repo:a_b", "old-holder"),
    ):
        with pytest.raises(AgentError, match="malformed|alias|noncanonical"):
            action()
    assert path.read_bytes() == original
    assert len(list(directory.iterdir())) == 2  # untouched legacy record plus guard directory
    # Legacy lower-case extension records remain inspectable; no automatic mutation is needed.
    if filename.endswith(".json"):
        assert leases.list_all(tmp_path)[0]["session_id"] == "old-holder"


def test_existing_canonical_holder_is_retained(tmp_path):
    directory = tmp_path / ".agent/leases"
    directory.mkdir(parents=True)
    path = directory / "repo_alpha.json"
    original = json.dumps({"area": "repo:alpha", "session_id": "old-holder", "pid": os.getpid()}).encode()
    path.write_bytes(original)
    with pytest.raises(AgentError, match="held by old-holder"):
        leases.take(tmp_path, "repo:alpha", session_id="new-holder", pid=os.getpid())
    assert path.read_bytes() == original
    assert leases.release(tmp_path, "repo:alpha", "old-holder")


def test_case_aliases_share_guard_and_preserve_legacy_filename(tmp_path):
    directory = tmp_path / ".agent/leases"
    directory.mkdir(parents=True)
    path = directory / "repo_Mixed.json"
    path.write_text(json.dumps({"area": "repo:Mixed", "session_id": "holder", "pid": os.getpid()}))
    assert area_key("repo:Mixed") == area_key("REPO:mixed") == "repo_mixed"
    assert leases.area_file(tmp_path, "repo:mixed") == path
    with area_guard(tmp_path, "repo:Mixed"):
        with area_guard(tmp_path, "repo:mixed", timeout=0.1):
            pass  # same-thread reentry uses exactly the same lock identity
        with ThreadPoolExecutor(1) as pool:

            def peer():
                with pytest.raises(AgentError, match="guard is busy"):
                    with area_guard(tmp_path, "REPO:mixed", timeout=0.1):
                        pytest.fail("case alias acquired another guard")

            pool.submit(peer).result(timeout=5)
    touched = leases.take(tmp_path, "repo:mixed", session_id="holder", pid=os.getpid())
    assert touched["lease"]["area"] == "repo:Mixed"
    assert leases.request(tmp_path, "repo:mixed", session_id="waiter", pid=os.getpid())
    saved = leases.get(tmp_path, "REPO:mixed")
    assert saved is not None and saved["area"] == "repo:Mixed" and len(saved["requests"]) == 1
    assert [entry.name for entry in directory.glob("*.json")] == ["repo_Mixed.json"]
    assert leases.release(tmp_path, "repo:mixed", "holder")
    assert not path.exists()


def test_concurrent_case_aliases_cannot_both_take(tmp_path):
    barrier = threading.Barrier(2)

    def take(area):
        barrier.wait(5)
        try:
            leases.take(tmp_path, area, session_id=area, pid=os.getpid())
            return "taken"
        except AgentError:
            return "refused"

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(take, area) for area in ("repo:Mixed", "repo:mixed")]
        assert sorted(future.result(timeout=10) for future in futures) == ["refused", "taken"]
    assert len(list((tmp_path / ".agent/leases").glob("*.json"))) == 1


def test_duplicate_legacy_alias_inventory_fails_closed(tmp_path, monkeypatch):
    directory = tmp_path / ".agent/leases"
    directory.mkdir(parents=True)
    path = directory / "repo_mixed.json"
    original = json.dumps({"area": "repo:mixed", "session_id": "holder", "pid": os.getpid()})
    path.write_text(original)
    original_iter = Path.iterdir

    def inventory(self):
        if self == directory:
            # Model the two entries possible on a case-sensitive host; this fixture is also
            # deterministic on case-insensitive macOS where creating both is impossible.
            return iter([path, directory / "repo_Mixed.json"])
        return original_iter(self)

    monkeypatch.setattr(Path, "iterdir", inventory)
    with pytest.raises(AgentError, match="ambiguous"):
        leases.take(tmp_path, "repo:mixed", session_id="peer", pid=os.getpid())
    assert path.read_text() == original


def test_mixed_case_dispatch_uses_legacy_lease_without_registry_migration(droot, fake_claude, monkeypatch):
    import yaml

    from agent_cli import dispatch

    registry_path = droot / "agent.yaml"
    data = yaml.safe_load(registry_path.read_text())
    alpha = next(repo for repo in data["repos"] if repo["short"] == "alpha")
    alpha["short"] = "MixedCase"
    registry_path.write_text(yaml.safe_dump(data))  # synthetic fixture only
    before = registry_path.read_bytes()
    directory = droot / ".agent/leases"
    directory.mkdir(parents=True, exist_ok=True)
    legacy = directory / "repo_MixedCase.json"
    legacy.write_text(
        json.dumps({"area": "repo:MixedCase", "session_id": "dispatch:001-mac-01", "pid": os.getpid()})
    )
    run_worker = dispatch.supervisor.run_worker
    seen = []

    def inspect(*args, **kwargs):
        assert leases.area_file(droot, "repo:mixedcase") == legacy
        saved = leases.get(droot, "repo:mixedcase")
        assert saved is not None and saved["area"] == "repo:MixedCase"
        seen.append(True)
        return run_worker(*args, **kwargs)

    monkeypatch.setattr(dispatch.supervisor, "run_worker", inspect)
    fake_claude.scenario([one(envelope(), commit="add the worker line")])  # a change, so the verifier runs
    result = _new(droot, repo=alpha["name"])
    assert seen and result["state"] == "done"
    assert result["verification_result"]["status"] == "verified"
    assert not legacy.exists() and registry_path.read_bytes() == before


def test_worktree_area_takes_exactly_two_segments(tmp_path):
    assert area_key("worktree:agent/024-locks-mac-01") == "worktree_agent~024-locks-mac-01"
    lease = leases.take(tmp_path, "worktree:agent/024-locks-mac-01", session_id="fixture", pid=os.getpid())
    assert lease["lease"]["area"] == "worktree:agent/024-locks-mac-01"
    for bad in (
        "worktree:agent/a/b",
        "worktree:/agent/a",
        "worktree:agent/a/",
        "worktree:agent//a",
        "worktree:agent",
        "worktree:agent/.a",
        "worktree:agent/..",
        "repo:agent/a",
    ):
        with pytest.raises(AgentError, match="noncanonical lease area"):
            area_key(bad)


def test_worktree_area_keys_stay_injective():
    areas = ("worktree:a/b", "worktree:a/b.c", "worktree:a.b/c", "worktree:ab/c", "vault:a/b", "repo:a")
    assert len({area_key(area) for area in areas}) == len(areas)


def test_distinct_worktree_areas_do_not_conflict(tmp_path):
    assert leases.conflicts("worktree:a/b", "worktree:A/b")
    assert not leases.conflicts("worktree:a/b", "worktree:a/b-2")
    assert not leases.conflicts("worktree:a/b", "worktree:a/c")
    assert not leases.conflicts("worktree:a/b", "repo:a")
    leases.take(tmp_path, "worktree:a/b", session_id="one", pid=os.getpid())
    leases.take(tmp_path, "worktree:a/c", session_id="two", pid=os.getpid())
    with pytest.raises(AgentError, match="is held by"):
        leases.take(tmp_path, "worktree:a/b", session_id="two", pid=os.getpid())
