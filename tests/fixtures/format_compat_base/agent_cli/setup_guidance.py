# ruff: noqa
from __future__ import annotations
from pathlib import Path


def fresh_clone_warnings(root: Path) -> list[str]:
    out: list[str] = []
    venv_agent = (root / ".venv" / "bin" / "agent", root / ".venv" / "Scripts" / "agent.exe")
    if not any((p.exists() for p in venv_agent)):
        out.append(".venv is missing, so hooks may time out and fail open. Run `uv sync`.")
    return out
