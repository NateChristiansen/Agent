"""Shared plumbing for the ops package: the Op result type every op returns, and the one
`render_views` implementation shared with `dispatch.state.render`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .. import status
from ..core import EXIT_OK


@dataclass
class Op:
    """The result of one action: machine data, human text, exit code."""

    data: Any
    text: str
    code: int = EXIT_OK


# Re-render the generated files after a write, and report a failure rather than raise it. One
# implementation in status.py, shared with `dispatch.state.render`.
render_views = status.render_quietly
