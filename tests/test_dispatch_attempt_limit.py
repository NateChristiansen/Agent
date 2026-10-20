"""`new(max_attempts=...)`: validated before any launch side effect, saved before the supervisor."""

from pathlib import Path
from typing import Any

import pytest

from agent_cli import dispatch
from agent_cli.core import AgentError
from agent_cli.dispatch import allocation, lifecycle
from agent_cli.dispatch.state import load_all
from dispatch_helpers import _new_fast, droot  # noqa: F401 -- shared pytest fixture


def _observe_start(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture the saved record at the supervisor-start boundary instead of starting one."""
    seen: list[dict[str, Any]] = []

    def start(root: Path, d: dict[str, Any], watch: bool) -> dict[str, Any]:
        seen.append({"passed": dict(d), "saved": dispatch.load(root, d["id"])})
        return d

    monkeypatch.setattr(lifecycle, "start", start)
    return seen


def test_omitted_limit_keeps_the_default(droot: Path, monkeypatch: pytest.MonkeyPatch):
    preview = _new_fast(droot, dry_run=True)
    seen = _observe_start(monkeypatch)
    _new_fast(droot)
    assert preview["max_attempts"] == dispatch.MAX_ATTEMPTS
    assert seen[0]["saved"]["max_attempts"] == dispatch.MAX_ATTEMPTS


def test_single_attempt_is_saved_before_the_supervisor_starts(droot: Path, monkeypatch: pytest.MonkeyPatch):
    preview = _new_fast(droot, dry_run=True, max_attempts=1)
    seen = _observe_start(monkeypatch)
    _new_fast(droot, max_attempts=1)
    assert len(seen) == 1
    assert seen[0]["saved"]["max_attempts"] == seen[0]["passed"]["max_attempts"] == 1
    assert preview["max_attempts"] == 1
    assert seen[0]["saved"]["attempt"] == 1


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("value", [0, -1, dispatch.MAX_ATTEMPTS + 1, True, "1", 1.0])
def test_bad_limit_refused_before_any_launch_side_effect(
    droot: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool, value: object
):
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("an id was allocated before the attempt limit was validated")

    monkeypatch.setattr(allocation, "allocate", forbidden)
    seen = _observe_start(monkeypatch)
    with pytest.raises(AgentError, match="max_attempts"):
        _new_fast(droot, dry_run=dry_run, max_attempts=value)
    assert seen == []
    assert load_all(droot) == []
