"""Quote masking for command scoring and chaining hints, without shell parsing or hook imports."""

from __future__ import annotations


def mask_quotes(text: str) -> str:
    """Replace quoted contents with `x`, preserving length and quote delimiters.

    An unbalanced quote masks to the end. Backslashes have no special meaning here;
    callers retain their own shell parsing and validation rules.
    """
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
