"""Run the CLI through python -m agent_cli without locking the Windows agent.exe shim. uv sync must
replace that shim, and Windows refuses replacement while a process uses it. Interpreter launch
keeps long-lived servers and supervisors updateable. Launchers add -I so cwd and PYTHONPATH cannot
shadow the harness.
"""

from __future__ import annotations

from .entry import main

if __name__ == "__main__":
    raise SystemExit(main())
