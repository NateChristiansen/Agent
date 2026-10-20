# ruff: noqa
from __future__ import annotations
from pathlib import Path
from typing import Any
import yaml
from .core import AgentError, atomic_write
from .records.narrow import as_dict_or_none

DELIM = "---"


def split(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith(DELIM + "\n"):
        raise AgentError("missing frontmatter: file must start with '---'")
    rest = text[len(DELIM) + 1 :]
    if rest.startswith(DELIM + "\n") or rest == DELIM:
        return ({}, rest[len(DELIM) + 1 :].lstrip("\n"))
    end = rest.find("\n" + DELIM + "\n")
    if end == -1:
        if rest.endswith("\n" + DELIM):
            return (_load(rest[: -len(DELIM) - 1]), "")
        raise AgentError("unterminated frontmatter: no closing '---'")
    return (_load(rest[:end]), rest[end + len(DELIM) + 2 :].lstrip("\n"))


def _load(block: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        raise AgentError(f"invalid frontmatter YAML: {exc}") from exc
    mapping: dict[str, Any] | None = as_dict_or_none(data) if data else {}
    if mapping is None:
        raise AgentError("frontmatter must be a mapping")
    return mapping


def join(meta: dict[str, Any], body: str) -> str:
    dumped = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, default_flow_style=False, width=10000)
    body = body.lstrip("\n")
    if not body:
        return f"{DELIM}\n{dumped}{DELIM}\n"
    if not body.endswith("\n"):
        body += "\n"
    return f"{DELIM}\n{dumped}{DELIM}\n\n{body}"


def load(path: Path) -> tuple[dict[str, Any], str]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise AgentError(f"no such file: {path}") from exc
    try:
        return split(text)
    except AgentError as exc:
        raise AgentError(f"{path}: {exc.message}") from exc


def dump(path: Path, meta: dict[str, Any], body: str) -> None:
    atomic_write(path, join(meta, body))
