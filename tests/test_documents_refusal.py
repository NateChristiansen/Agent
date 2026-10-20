"""`notes_document` refuses where it cannot lock, Windows included, and says so in its MCP
description. This runs on every OS; the success cases in test_documents.py skip
on Windows with the capability named."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_cli import documents
from agent_cli.core import AgentError
from agent_cli.mcp.specs_harness import HARNESS_TOOLS
from test_documents import (
    TARGET,
    draft,
    vault,  # noqa: F401  # pyright: ignore[reportUnusedImport] -- a fixture, used as a parameter name
)


def test_unavailable_locking_fails_closed(vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Natively on Windows (no fcntl, no O_NOFOLLOW); simulated elsewhere."""
    if os.name == "posix":
        monkeypatch.setattr(documents, "fcntl", None)
    original = (vault / TARGET).read_bytes()
    digest = documents.hashlib.sha256(original).hexdigest()
    for apply in (False, True):
        with pytest.raises(AgentError, match="requires POSIX"):
            documents.edit(vault, target=TARGET, source=draft(tmp_path), apply=apply, expect_sha256=digest)
    assert (vault / TARGET).read_bytes() == original


def test_the_mcp_description_marks_it_unsupported_on_windows():
    description = HARNESS_TOOLS["harness"].description
    clause = description[description.index("`notes_document`") :].split(". ", 1)[0]
    assert "unsupported on Windows" in clause
