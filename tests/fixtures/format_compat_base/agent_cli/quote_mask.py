# ruff: noqa
from __future__ import annotations


def mask_quotes(text: str) -> str:
    out: list[str] = []
    quote = ""
    for c in text:
        if quote:
            out.append(c if c == quote else "x")
            if c == quote:
                quote = ""
            continue
        if c in "'\"":
            quote = c
        out.append(c)
    return "".join(out)
