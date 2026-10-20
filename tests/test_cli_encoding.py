"""The CLI writes UTF-8 on a console that defaults to cp1252.

The first Windows CI run crashed printing `agent setup`'s own report: the guide line for the
Obsidian CLI holds a `→`, which cp1252 cannot encode.
"""

from __future__ import annotations

import io
import sys

import pytest

from agent_cli.entry import utf8_streams


def test_a_cp1252_console_gets_utf8_output(monkeypatch: pytest.MonkeyPatch):
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252")
    with pytest.raises(UnicodeEncodeError):
        console.write("Obsidian → Settings")
        console.flush()
    console = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", console)
    utf8_streams()
    sys.stdout.write("Obsidian → Settings")
    sys.stdout.flush()
    assert console.buffer.getvalue().decode("utf-8") == "Obsidian → Settings"  # type: ignore[attr-defined]
