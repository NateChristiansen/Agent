# ruff: noqa
from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from .. import status
from ..core import EXIT_OK


@dataclass
class Op:
    data: Any
    text: str
    code: int = EXIT_OK


render_views = status.render_quietly
