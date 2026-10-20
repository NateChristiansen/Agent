# ruff: noqa
from __future__ import annotations
import datetime as dt
from collections.abc import Mapping
from ..records.narrow import as_dict, as_dict_or_none, as_int

SCHEMA = 1
CLASSES = ("manager", "subagent", "worker", "reviewer", "probe", "eval", "drafter", "other")
TOKEN_SOURCES = ("claude", "codex")
SOURCES = (*TOKEN_SOURCES, "costs")
STATUSES = ("complete", "partial", "unavailable")
FIELDS = ("input", "output", "cache_write", "cache_read")
MODEL_KEYS = ("requests", *FIELDS)
COUNT_KEYS = ("total_tokens", "requests", *(f"tokens_{c}" for c in CLASSES))
TOKEN_KEYS = (*COUNT_KEYS, "models")
COST_KEYS = ("manager_usd", "manager_complete", "dispatches", "dispatch_usd", "probe_usd", "spend_complete")
RECORD_TAG = "usage"
CHECKPOINT_TAG = "usage-checkpoint"


def overall_status(statuses: list[str]) -> str:
    if all((s == "complete" for s in statuses)):
        return "complete"
    if all((s == "unavailable" for s in statuses)):
        return "unavailable"
    return "partial"


def _is_count(value: object) -> bool:
    return type(value) is int and value >= 0


def _is_money(value: object) -> bool:
    return isinstance(value, (int, float)) and (not isinstance(value, bool)) and (value >= 0)


def _is_date(value: object) -> bool:
    if isinstance(value, dt.datetime):
        return False
    if isinstance(value, dt.date):
        return True
    try:
        dt.date.fromisoformat(str(value))
    except ValueError:
        return False
    return True


def stamp(value: object) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        try:
            parsed = dt.datetime.fromisoformat(str(value))
        except ValueError:
            return None
    return parsed if parsed.tzinfo is not None else None


def _header(meta: Mapping[str, object], tag: str) -> list[str]:
    problems: list[str] = []
    tags = meta.get("tags")
    if not isinstance(tags, list) or tag not in tags:
        problems.append(f"tags must include {tag!r}")
    if meta.get("schema") != SCHEMA:
        problems.append(f"schema must be {SCHEMA}")
    machine = meta.get("machine")
    if not isinstance(machine, str) or not machine:
        problems.append("machine must be a non-empty string")
    if stamp(meta.get("updated")) is None:
        problems.append("updated must be a timestamp with a UTC offset")
    return problems


def _source_problems(name: str, value: object, *, counted: bool) -> list[str]:
    source = as_dict_or_none(value)
    if source is None:
        return [f"sources.{name} is missing"]
    status = source.get("status")
    if status not in STATUSES:
        return [f"sources.{name}.status must be one of {', '.join(STATUSES)}"]
    problems: list[str] = []
    if status != "complete" and (not source.get("reason")):
        problems.append(f"sources.{name} is {status} and needs a reason")
    if not counted:
        return problems
    for key in ("files", "requests"):
        if status == "unavailable" and key in source:
            problems.append(f"sources.{name} is unavailable and must not carry {key}")
        elif status != "unavailable" and (not _is_count(source.get(key))):
            problems.append(f"sources.{name}.{key} must be a non-negative integer")
    return problems + _absent_problems(name, source)


def _absent_problems(name: str, source: dict[str, object]) -> list[str]:
    if "absent" not in source and "path" not in source:
        return []
    if source.get("absent") is not True:
        return [f"sources.{name}.absent must be true when present, with path"]
    problems: list[str] = []
    if not isinstance(source.get("path"), str) or not source.get("path"):
        problems.append(f"sources.{name} is absent and needs the path checked")
    if source.get("status") == "unavailable" or source.get("files") != 0:
        problems.append(f"sources.{name} is absent, so it was read as zero files, not unavailable")
    return problems


def _token_problems(meta: Mapping[str, object], sources: dict[str, object]) -> list[str]:
    readable = [
        as_dict(sources[n]) for n in TOKEN_SOURCES if as_dict(sources[n]).get("status") != "unavailable"
    ]
    if not readable:
        return [f"no token source was read, so {key} must be absent" for key in TOKEN_KEYS if key in meta]
    problems = [f"{key} must be a non-negative integer" for key in COUNT_KEYS if not _is_count(meta.get(key))]
    models = as_dict_or_none(meta.get("models"))
    if models is None:
        problems.append("models must be a mapping")
    if problems or models is None:
        return problems
    total = as_int(meta.get("total_tokens"))
    requests = as_int(meta.get("requests"))
    by_class = sum((as_int(meta.get(f"tokens_{c}")) for c in CLASSES))
    if by_class != total:
        problems.append(f"tokens_* sum to {by_class}, not total_tokens {total}")
    model_tokens = model_requests = 0
    for model, value in models.items():
        row = as_dict(value)
        if not all((_is_count(row.get(k)) for k in MODEL_KEYS)):
            problems.append(f"models.{model} needs non-negative integer {', '.join(MODEL_KEYS)}")
            continue
        model_tokens += sum((as_int(row.get(k)) for k in FIELDS))
        model_requests += as_int(row.get("requests"))
    if problems:
        return problems
    if model_tokens != total:
        problems.append(f"models sum to {model_tokens} tokens, not total_tokens {total}")
    if model_requests != requests:
        problems.append(f"models sum to {model_requests} requests, not requests {requests}")
    source_requests = sum((as_int(s.get("requests")) for s in readable))
    if source_requests != requests:
        problems.append(f"sources sum to {source_requests} requests, not requests {requests}")
    return problems


def _cost_problems(meta: Mapping[str, object], costs: dict[str, object]) -> list[str]:
    if costs.get("status") == "unavailable":
        return [f"costs are unavailable, so {key} must be absent" for key in COST_KEYS if key in meta]
    problems = [
        f"{key} must be a non-negative number"
        for key in ("manager_usd", "dispatch_usd", "probe_usd")
        if not _is_money(meta.get(key))
    ]
    if not _is_count(meta.get("dispatches")):
        problems.append("dispatches must be a non-negative integer")
    problems += [
        f"{key} must be true or false"
        for key in ("manager_complete", "spend_complete")
        if not isinstance(meta.get(key), bool)
    ]
    return problems


def record_problems(meta: Mapping[str, object]) -> list[str]:
    problems = _header(meta, RECORD_TAG)
    if not _is_date(meta.get("date")):
        problems.append("date must be YYYY-MM-DD")
    sources = as_dict_or_none(meta.get("sources"))
    if sources is None:
        return [*problems, "sources must be a mapping of claude, codex and costs"]
    for name in SOURCES:
        problems += _source_problems(name, sources.get(name), counted=name in TOKEN_SOURCES)
    if problems:
        return problems
    want = overall_status([str(as_dict(sources[name]).get("status")) for name in SOURCES])
    if meta.get("status") != want:
        problems.append(f"status must be {want} for these sources, not {meta.get('status')!r}")
    problems += _token_problems(meta, sources)
    problems += _cost_problems(meta, as_dict(sources["costs"]))
    return problems


def checkpoint_problems(meta: Mapping[str, object]) -> list[str]:
    problems = _header(meta, CHECKPOINT_TAG)
    covered = stamp(meta.get("covered_through"))
    if covered is None:
        problems.append("covered_through must be a timestamp with a UTC offset")
    sources = as_dict_or_none(meta.get("sources"))
    if sources is None:
        return [*problems, "sources must be a mapping of claude, codex and costs"]
    marks: list[dt.datetime] = []
    for name in SOURCES:
        source = as_dict_or_none(sources.get(name))
        if source is None:
            problems.append(f"sources.{name} is missing")
            continue
        if source.get("status") not in STATUSES:
            problems.append(f"sources.{name}.status must be one of {', '.join(STATUSES)}")
        mark = stamp(source.get("covered_through"))
        if mark is None:
            problems.append(f"sources.{name}.covered_through must be a timestamp with a UTC offset")
        else:
            marks.append(mark)
    if covered is not None and len(marks) == len(SOURCES) and (covered != min(marks)):
        problems.append("covered_through must be the minimum over the sources")
    return problems
