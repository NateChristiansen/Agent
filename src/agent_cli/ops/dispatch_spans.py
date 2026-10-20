"""`dispatch spans`: one dispatch's lifecycle spans and per-attempt cost."""

from __future__ import annotations

from pathlib import Path

from ._shared import Op


def dispatch_spans(root: Path, *, dispatch_id: str) -> Op:
    """How long each attempt of `dispatch_id` spent in each lifecycle phase, with its base and the
    harness revision that ran it, and each attempt's cost by source (`dispatch/spans.py`)."""
    from ..dispatch import spans
    from ..dispatch.state import load

    data = spans.spans(root, load(root, dispatch_id))
    return Op(data, spans.text(data))
