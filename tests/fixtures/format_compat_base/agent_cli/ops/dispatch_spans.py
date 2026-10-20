# ruff: noqa
from __future__ import annotations
from pathlib import Path
from ._shared import Op


def dispatch_spans(root: Path, *, dispatch_id: str) -> Op:
    from ..dispatch import spans
    from ..dispatch.state import load

    data = spans.spans(root, load(root, dispatch_id))
    return Op(data, spans.text(data))
