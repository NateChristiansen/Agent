# ruff: noqa
from __future__ import annotations
import json
from dataclasses import dataclass
from typing import Any
from ..records.narrow import as_dict_or_none, as_list_or_none
from .sink import Sink

MANIFEST = "exclusions.json"
SCHEMA = 1


@dataclass(frozen=True)
class Exclusions:
    event_ids: frozenset[str]

    def skip(self, event: dict[str, Any]) -> bool:
        return event.get("event_id") in self.event_ids


NONE = Exclusions(frozenset())


def load(sink: Sink) -> Exclusions:
    try:
        data = as_dict_or_none(json.loads((sink.directory / MANIFEST).read_text(encoding="utf-8")))
    except OSError, ValueError:
        return NONE
    if data is None or data.get("schema") != SCHEMA:
        return NONE
    ids = as_list_or_none(data.get("event_ids"))
    if ids is None:
        return NONE
    return Exclusions(frozenset((i for i in ids if isinstance(i, str))))
