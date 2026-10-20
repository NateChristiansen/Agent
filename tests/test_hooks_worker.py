"""`precompact`: the split of `src/agent_cli/hooks/worker.py`, mirroring the source split of
the old hooks.py. (`worker-stop` has its coverage in `test_dispatch.py`, alongside the rest of
the dispatch lifecycle it records onto.)
"""

import json
from pathlib import Path

# ---------------------------------------------------------------- precompact (worker)


def test_precompact_restates_the_nonce_and_the_return_shape(tmp_root: Path, run, monkeypatch):
    """T5 item 9. The nonce and the return schema live in the worker's FIRST message and
    nowhere else, and a nonce mismatch is a terminal `failed` in `evaluate` with no retry —
    so a worker long enough to compact could lose the one string its whole return is checked
    against, with no way back to it. The dispatch id arrives in AGENT_DISPATCH_ID, which is
    environment rather than context and survives the compaction untouched."""
    from agent_cli import dispatch as dispatch_mod

    base = tmp_root / ".agent" / "dispatches"
    base.mkdir(parents=True, exist_ok=True)
    (base / "001-01.json").write_text(
        json.dumps(
            {
                "id": "001-01",
                "nonce": "001-01-beef",
                "worktree": "/wt/alpha/001-x",
                "branch": "agent/001-x",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.delenv("AGENT_DISPATCH_ID", raising=False)
    assert run(["hook", "precompact"], stdin="{}").out.strip() == "", "silent outside a worker"

    monkeypatch.setenv("AGENT_DISPATCH_ID", "001-01")
    r = run(["hook", "precompact"], stdin=json.dumps({"session_id": "w", "trigger": "auto"}))
    assert r.code == 0
    out = json.loads(r.out)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreCompact"
    ctx = out["additionalContext"]
    assert "dispatch 001-01" in ctx
    assert "nonce: 001-01-beef" in ctx
    assert "mismatch fails the dispatch" in ctx
    for field in dispatch_mod.RETURN_SCHEMA["required"]:
        assert field in ctx, field
    assert "/wt/alpha/001-x" in ctx and "agent/001-x" in ctx
    assert "never push" in ctx

    monkeypatch.setenv("AGENT_DISPATCH_ID", "002-99")
    assert run(["hook", "precompact"], stdin="{}").out.strip() == "", "no record: fail open"
