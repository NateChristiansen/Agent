# ruff: noqa
from __future__ import annotations
from typing import cast


def as_dict(v: object) -> dict[str, object]:
    return cast("dict[str, object]", v) if isinstance(v, dict) else {}


def as_dict_or_none(v: object) -> dict[str, object] | None:
    return cast("dict[str, object]", v) if isinstance(v, dict) else None


def as_list(v: object) -> list[object]:
    return cast("list[object]", v) if isinstance(v, list) else []


def as_list_or_none(v: object) -> list[object] | None:
    return cast("list[object]", v) if isinstance(v, list) else None


def as_str(v: object, default: str = "") -> str:
    return v if isinstance(v, str) else default


def as_int(v: object, default: int = 0) -> int:
    return v if isinstance(v, int) and (not isinstance(v, bool)) else default


def as_feature_id(v: object) -> int | None:
    if isinstance(v, str):
        return int(v) if v.isascii() and v.isdigit() else None
    return v if isinstance(v, int) and (not isinstance(v, bool)) else None


def as_float(v: object, default: float = 0.0) -> float:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return default
    try:
        return float(v)
    except OverflowError:
        return default


def as_float_or_none(v: object) -> float | None:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    try:
        return float(v)
    except OverflowError:
        return None


def feature_number(v: object) -> str:
    number = as_feature_id(v)
    return "—" if number is None else f"{number:03d}"


def str_list(v: object) -> list[str]:
    return [x for x in as_list(v) if isinstance(x, str)]


def dict_list(v: object) -> list[dict[str, object]]:
    return [cast("dict[str, object]", x) for x in as_list(v) if isinstance(x, dict)]


def dig(v: object, *keys: str) -> object:
    for key in keys:
        if not isinstance(v, dict):
            return None
        v = cast("dict[str, object]", v).get(key)
    return v
