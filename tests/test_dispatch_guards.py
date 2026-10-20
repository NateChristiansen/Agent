"""Record CAS and independent process locking protect verifier/cancellation state."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_cli.core import AgentError
from agent_cli.dispatch.guards import area_guard
from agent_cli.dispatch.state import load, load_all, save


def record():
    return {"id": "001-01", "repo": "Alpha", "short": "alpha", "state": "running"}


def test_stale_incidental_copy_cannot_erase_cancel_or_binding(tmp_path):
    original = record()
    save(tmp_path, original)
    stale = load(tmp_path, original["id"])
    current = load(tmp_path, original["id"])
    current.update(state="cancelled", verification={"trusted": "binding"})
    save(tmp_path, current)
    stale.update(pr="fixture PR", chat_pending=False)
    with pytest.raises(AgentError, match="stale"):
        save(tmp_path, stale)
    saved = load(tmp_path, original["id"])
    assert saved["state"] == "cancelled" and saved["verification"] == {"trusted": "binding"}
    assert "pr" not in saved


def test_legacy_load_normalizes_revision_and_creation_refuses_collision(tmp_path):
    path = tmp_path / ".agent/dispatches/001-01.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record()))
    for loaded in (load(tmp_path, "001-01"), load_all(tmp_path)[0]):
        assert loaded["record_revision"] == 0
    with pytest.raises(AgentError, match="creation collision"):
        save(tmp_path, record())
    loaded = load(tmp_path, "001-01")
    loaded["state"] = "cancelled"
    save(tmp_path, loaded)
    assert loaded["record_revision"] == 1


@pytest.mark.parametrize("revision", [True, False, -1, 1.0, "0", None])
def test_invalid_revision_cannot_write(tmp_path, revision):
    d = record()
    save(tmp_path, d)
    d["record_revision"] = revision
    with pytest.raises(AgentError, match="revision"):
        save(tmp_path, d)


def test_repo_identity_cannot_change_guard(tmp_path):
    d = record()
    save(tmp_path, d)
    d["short"] = "different"
    with pytest.raises(AgentError, match="identity"):
        save(tmp_path, d)


def test_cross_repo_concurrent_creation_is_create_once(tmp_path, monkeypatch):
    barrier = threading.Barrier(2)
    actual = os.link

    def collide(*args, **kwargs):
        barrier.wait(timeout=5)
        return actual(*args, **kwargs)

    monkeypatch.setattr(os, "link", collide)

    def save_short(short):
        try:
            save(tmp_path, {**record(), "short": short})
            return "created"
        except AgentError:
            return "collision"

    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(save_short, short) for short in ("alpha", "beta")]
        assert sorted(f.result(timeout=10) for f in futures) == ["collision", "created"]
    assert load(tmp_path, "001-01")["record_revision"] == 1


def test_independent_process_same_repo_refuses_other_repo_proceeds(tmp_path):
    package = Path(__file__).resolve().parents[1] / "src"
    code = "import sys; sys.path.insert(0,sys.argv[1]); from pathlib import Path; from agent_cli.dispatch.guards import area_guard; from agent_cli.core import AgentError\ntry:\n with area_guard(Path(sys.argv[2]),sys.argv[3],timeout=.15): print('acquired')\nexcept AgentError: print('busy')"
    with area_guard(tmp_path, "repo:alpha"):
        for area, expected in (("repo:alpha", "busy"), ("repo:beta", "acquired")):
            result = subprocess.run(
                [sys.executable, "-I", "-c", code, str(package), str(tmp_path), area],
                capture_output=True,
                text=True,
                timeout=4,
                env={"PATH": os.defpath},
            )
            assert result.returncode == 0 and result.stdout.strip() == expected


def test_processes_taking_a_new_area_at_once_all_get_the_guard(tmp_path):
    """macOS gives the loser of a concurrent `openat(O_CREAT)` ENOENT; before the retry, one of
    two processes failed in every fresh race."""
    for attempt in range(5):
        root = tmp_path / f"root{attempt}"
        root.mkdir()
        gate = root / "go"
        script = (
            "import sys, time\nfrom pathlib import Path\nfrom agent_cli.dispatch.guards import area_guard\n"
            f"while not Path({str(gate)!r}).exists():\n    time.sleep(0.002)\n"
            f"with area_guard(Path({str(root)!r}), 'peer:fresh'):\n    pass\n"
        )
        children = [subprocess.Popen([sys.executable, "-c", script]) for _ in range(3)]
        time.sleep(0.5)  # let all three reach the gate
        gate.write_text("go")
        assert [c.wait(30) for c in children] == [0, 0, 0]
