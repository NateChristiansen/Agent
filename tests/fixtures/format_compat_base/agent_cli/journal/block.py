# ruff: noqa
from __future__ import annotations
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from .. import adrs as adrs_mod
from .. import features as features_mod
from .. import log as log_mod
from ..core import AgentError, atomic_write, today
from ..records.dispatch import result_of
from ..records.narrow import as_dict_or_none
from .paths import ensure_note, note_path

GENERATED_START = "<!-- journal:generated -->"
GENERATED_END = "<!-- /journal:generated -->"
NOTES_HEADING = "## Notes"
_LEGACY_LOG_MARK = "## Log"
SINCE_DEFAULT = date(2026, 9, 3)


def _block(start: str, end: str, content: str) -> str:
    content = content.strip("\n")
    return f"{start}\n{content}\n{end}" if content else f"{start}\n{end}"


def meta_for(
    day: date,
    meta: dict[str, Any] | None,
    spend_usd: float | str | None = None,
    dispatches: int | None = None,
    session_tokens: int | None = None,
    session_models: str | None = None,
    probe_spend_usd: float | str | None = None,
    manager_usd: float | str | None = None,
    spend_complete: bool | None = None,
    manager_complete: bool | None = None,
    usage_machines: dict[str, Any] | None = None,
    tokens_complete: bool | None = None,
) -> dict[str, Any]:
    src = dict(meta or {})
    tags = list(src.pop("tags", None) or [])
    if "journal" not in tags:
        tags.insert(0, "journal")
    src.pop("date", None)
    existing_spend = src.pop("spend_usd", 0.0)
    existing_manager = src.pop("manager_usd", 0.0)
    existing_spend_complete = src.pop("spend_complete", True)
    existing_manager_complete = src.pop("manager_complete", True)
    existing_dispatches = src.pop("dispatches", 0)
    existing_tokens = src.pop("session_tokens", 0)
    existing_models = src.pop("session_models", "")
    existing_probe = src.pop("probe_spend_usd", 0.0)
    existing_machines = src.pop("usage_machines", None)
    existing_complete = src.pop("tokens_complete", None)
    probe_value = existing_probe if probe_spend_usd is None else probe_spend_usd
    spend_value = existing_spend if spend_usd is None else spend_usd
    resolved_probe = None if probe_value in ("unknown", None) else round(float(probe_value), 2)
    resolved_spend = None if spend_value in ("unknown", None) else round(float(spend_value), 2)
    manager_value = existing_manager if manager_usd is None else manager_usd
    resolved_manager = None if manager_value in ("unknown", None) else round(float(manager_value), 2)
    resolved_dispatches = int(dispatches if dispatches is not None else existing_dispatches or 0)
    resolved_tokens = int(session_tokens if session_tokens is not None else existing_tokens or 0)
    resolved_models = str(session_models if session_models is not None else existing_models or "")
    out: dict[str, Any] = {
        "tags": tags,
        "date": day,
        "spend_usd": resolved_spend,
        "spend_complete": existing_spend_complete if spend_complete is None else spend_complete,
        "manager_usd": resolved_manager,
        "manager_complete": existing_manager_complete if manager_complete is None else manager_complete,
        "dispatches": resolved_dispatches,
        "session_tokens": resolved_tokens,
        "session_models": resolved_models,
    }
    machines = existing_machines if usage_machines is None else usage_machines
    if machines is not None:
        out["usage_machines"] = machines
        out["tokens_complete"] = existing_complete if tokens_complete is None else tokens_complete
    if resolved_probe or probe_spend_usd == "unknown":
        out["probe_spend_usd"] = resolved_probe
    out.update(src)
    return out


def render_note(day: date, generated: str, notes_body: str, meta: dict[str, Any] | None = None) -> str:
    from .. import frontmatter

    notes_body = notes_body.strip("\n")
    lines = [
        f"# {day.isoformat()}",
        "",
        _block(GENERATED_START, GENERATED_END, generated),
        "",
        NOTES_HEADING + (f"\n\n{notes_body}" if notes_body else ""),
    ]
    body = "\n".join(lines) + "\n"
    return frontmatter.join(meta_for(day, meta), body)


def skeleton(day: date) -> str:
    return render_note(day, "", "")


def _split_full(text: str) -> tuple[dict[str, Any], str, str]:
    from .. import frontmatter

    meta, body = frontmatter.split(text)
    if GENERATED_START not in body or GENERATED_END not in body:
        raise AgentError(f"{GENERATED_START!r}/{GENERATED_END!r} markers not found in the journal note")
    _, rest = body.split(GENERATED_START, 1)
    generated, rest = rest.split(GENERATED_END, 1)
    if _LEGACY_LOG_MARK in rest:
        _, rest = rest.split(_LEGACY_LOG_MARK, 1)
    if NOTES_HEADING not in rest:
        raise AgentError(f"{NOTES_HEADING!r} heading not found")
    _, notes_body = rest.split(NOTES_HEADING, 1)
    return (meta, generated.strip("\n"), notes_body.strip("\n"))


def split_sections(text: str) -> tuple[str, str]:
    return _split_full(text)[1:]


def write_generated(root: Path, day: date, text: str) -> Path:
    from . import pending as pending_mod
    from .writer import note_lock

    with note_lock(root, day):
        path = ensure_note(root, day)
        meta, _, notes_body = _split_full(path.read_text(encoding="utf-8"))
        atomic_write(path, render_note(day, text, notes_body, _numbered(root, day, meta)))
    pending_mod.clear(root, day)
    return path


def refresh_numbers(root: Path, day: date) -> Path:
    from .. import frontmatter
    from .writer import note_lock

    with note_lock(root, day):
        path = ensure_note(root, day) if _has_note(root, day) else note_path(root, day)
        if not path.exists():
            raise AgentError(f"no journal note for {day.isoformat()}; write it with `journal write`")
        raw = path.read_text(encoding="utf-8")
        meta, generated, _ = _split_full(raw)
        if not generated.strip():
            raise AgentError(f"the note for {day.isoformat()} has no generated block; use `journal write`")
        head = frontmatter.join(_numbered(root, day, meta), "")
        atomic_write(path, head + raw[_body_start(raw) :])
    return path


def _numbered(root: Path, day: date, meta: dict[str, Any]) -> dict[str, Any]:
    from .usage_note import numbers_for

    numbers = numbers_for(root, day, meta)
    held = numbers.pop("held")
    out = meta_for(day, meta, **numbers)
    out.pop("usage_held", None)
    if held:
        out["usage_held"] = True
    return out


def _body_start(raw: str) -> int:
    rest = raw[len("---\n") :]
    if rest.startswith("---\n") or rest == "---":
        return len("---\n") + len("---\n")
    return len("---\n") + rest.find("\n---\n") + len("\n---\n")


def _has_note(root: Path, day: date) -> bool:
    return note_path(root, day).exists()


def _generated_is_empty(root: Path, day: date) -> bool:
    path = note_path(root, day)
    if not path.exists():
        return True
    try:
        meta, generated, _ = _split_full(path.read_text(encoding="utf-8"))
    except AgentError:
        return True
    return not generated.strip() or meta.get("spend_usd") is None or meta.get("manager_usd") is None


def missing_days(root: Path, since: date = SINCE_DEFAULT) -> list[date]:
    from .. import dispatch as dispatch_mod

    end = today() - timedelta(days=1)
    if since > end:
        return []
    activity: set[date] = set()
    for f in features_mod.load_all(root):
        if f.log_path.exists():
            for e in log_mod.parse(f.log_path):
                activity.add(date.fromisoformat(e.date))
        for key in ("opened", "closed"):
            when = f.meta.get(key)
            if isinstance(when, date):
                activity.add(when)
    for d in dispatch_mod.load_all(root):
        finished = d.get("finished")
        if not finished:
            continue
        try:
            activity.add(datetime.fromisoformat(finished).date())
        except ValueError:
            continue
    for scope in ("harness", "notes"):
        if scope == "notes" and (not adrs_mod.notes_ready(root)):
            continue
        for a in adrs_mod.load_all(root, scope):
            when = a.meta.get("date")
            if isinstance(when, date):
                activity.add(when)
    out: list[date] = []
    day = since
    while day <= end:
        has_note = note_path(root, day).exists()
        if (day in activity or has_note) and _generated_is_empty(root, day):
            out.append(day)
        day += timedelta(days=1)
    return out


def first_sentence(text: str, limit: int = 120) -> str:
    text = text.strip()
    if not text:
        return ""
    cut = len(text)
    for pat in (". ", "! ", "? ", "\n"):
        idx = text.find(pat)
        if idx == -1:
            continue
        end = idx + (1 if pat != "\n" else 0)
        cut = min(cut, end)
    sentence = text[:cut].strip()
    if len(sentence) > limit:
        sentence = sentence[: limit - 1].rstrip() + "…"
    return sentence


def _shown_result(value: object) -> dict[str, object] | None:
    from ..dispatch.state import shown_denials

    held = as_dict_or_none(value)
    if held is None or "permission_denials" not in held:
        return held
    return {**held, "permission_denials": shown_denials(held["permission_denials"])}


def dispatches_today(root: Path, day: date) -> list[dict[str, Any]]:
    from .. import dispatch as dispatch_mod

    out: list[dict[str, Any]] = []
    for d in dispatch_mod.load_all(root):
        finished = d.get("finished")
        if not finished:
            continue
        try:
            if datetime.fromisoformat(finished).date() != day:
                continue
        except ValueError:
            continue
        result = result_of(d)
        out.append(
            {
                "id": d.get("id"),
                "kind": d.get("kind") or "work",
                "feature": d.get("feature"),
                "short": d.get("short"),
                "state": d.get("state"),
                "cost_usd": d.get("cost_usd"),
                "last_result": _shown_result(d.get("last_result")),
                "attempt": d.get("attempt"),
                "model": d.get("model"),
                "effort": d.get("effort"),
                "summary": first_sentence(result.get("summary") or d.get("error") or ""),
                "handoff": d.get("handoff"),
            }
        )
    out.sort(key=lambda x: str(x["id"]))
    return out
