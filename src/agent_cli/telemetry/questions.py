"""The owner-question events: one when a question is raised, one when it ends.

Both are best effort and carry ids, the route and timings only, never the question or the
answer. The lifecycle that calls them is `hooks.attention`'s.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from . import recorder
from .recorder import main_of


def raised(root: Path, session: str, feature: str | None, question_id: str, route: str) -> str | None:
    """`workflow.owner_question` under the question's id. `route` is how it was asked: `manual`
    (the `owner_question` tool), `sync` (an interactive prompt) or `async` (Codex's async
    prompt). Returns the id when written; a loss is counted by the recorder and never raised."""
    try:
        return recorder.record(
            main_of(root),
            "workflow.owner_question",
            event_id=question_id,
            session=session,
            feature=feature,
            data={"question_id": question_id, "route": route},
        )
    except Exception:  # noqa: BLE001 — the question shows whether or not its event lands
        return None


def cleared(
    root: Path,
    session: str,
    feature: str | None,
    question_id: str,
    route: str,
    reason: str,
    open_s: int | None,
) -> str | None:
    """`workflow.owner_question_cleared`: the question ended, and how long it was open. Its event
    id is derived from the question's, so a replay writes the same one."""
    data: dict[str, Any] = {"question_id": question_id, "route": route, "reason": reason}
    if open_s is not None:
        data["open_s"] = open_s
    try:
        return recorder.record(
            main_of(root),
            "workflow.owner_question_cleared",
            event_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"agent:owner-question-cleared:{question_id}")),
            session=session,
            feature=feature,
            data=data,
        )
    except Exception:  # noqa: BLE001 — a lost event never changes the question's state
        return None
