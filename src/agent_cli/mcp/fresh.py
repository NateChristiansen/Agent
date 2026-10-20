"""Run each MCP operation in a fresh process so source edits take effect without server restart. The
long-lived server retains only tool schemas and wire handling.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..core import AgentError
from ..records.narrow import as_dict_or_none

FRESH_TIMEOUT_S = 3600.0  # a `dispatch` with `watch` or `wait` can legitimately run long
STDERR_TAIL_CHARS = 2000


def _session_env(root: Path) -> dict[str, str]:
    """Pin the child to this server's session. The child's parent is the server, not Claude
    Code, so its own pid walk would miss the session-start hook's by-pid map. Resolve here and
    hand the pid down as CLAUDE_PID, which `sessions.candidate_pids` trusts first."""
    from .. import sessions

    if os.environ.get("CLAUDE_PID"):
        return {}
    for pid in sessions.candidate_pids():
        if sessions.read_by_pid(root, pid):
            return {"CLAUDE_PID": str(pid)}
    return {}


def run_fresh(root: Path, profile: str, name: str, kw: dict[str, Any]) -> dict[str, Any]:
    """One tool call in a child process. An AgentError in the child comes back as an
    AgentError here, so the server turns it into a tool error the model can act on."""
    env = {**os.environ, **_session_env(root), "AGENT_NO_AUTOOPEN": "1"}
    request = json.dumps({"root": str(root), "profile": profile, "name": name, "kw": kw}, default=str)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "agent_cli.mcp.fresh"],
            input=request,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(root),
            timeout=FRESH_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AgentError(f"{name} ran past {FRESH_TIMEOUT_S:.0f}s in its fresh process") from exc
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    try:
        reply = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else None
    except json.JSONDecodeError:
        reply = None
    found = as_dict_or_none(reply)
    if found is None:
        tail = (proc.stderr or "")[-STDERR_TAIL_CHARS:]
        raise AgentError(f"{name} failed in its fresh process (exit {proc.returncode}): {tail.strip()}")
    if "error" in found:
        raise AgentError(str(found["error"]))
    ok = found.get("ok")
    if ok is None:
        return {}
    result = as_dict_or_none(ok)
    if result is None:
        raise AgentError(f"{name} returned a reply that is not an object from its fresh process")
    return dict(result)


def main() -> int:
    """The child: read one call from stdin, run it on the current code, print one JSON line.
    Anything an op prints goes to stderr; stdout carries only the reply."""
    from .server import PROFILES, shape_result, worker_ctx
    from .tools import Ctx

    request = json.loads(sys.stdin.read())
    root = Path(request["root"])
    profile, name = request["profile"], request["name"]
    kw: dict[str, object] = request.get("kw") or {}
    try:
        with contextlib.redirect_stdout(sys.stderr):
            spec = PROFILES[profile].get(name)
            if spec is None:
                raise AgentError(f"no {profile} tool named {name!r} in the current code; reconnect /mcp")
            ctx = worker_ctx(root) if profile == "worker" else Ctx(root=root, profile=profile)
            reply: dict[str, Any] = {"ok": shape_result(spec.fn(ctx, **kw))}
    except AgentError as exc:
        reply = {"error": exc.message}
    sys.stdout.write(json.dumps(reply) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
