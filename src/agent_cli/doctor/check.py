"""The doctor row: one check's name, level (`ok`, `warn`, `fail`) and detail."""

from __future__ import annotations

from typing import Any

Check = dict[str, Any]


def new_check(name: str, level: str, detail: str = "") -> Check:
    return {"check": name, "level": level, "detail": detail}
