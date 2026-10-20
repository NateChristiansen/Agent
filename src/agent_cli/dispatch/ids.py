"""Dispatch id and handoff file name grammar (see ADR-0036). No imports from the rest of the
package, so the evidence parser can share it.

New ids read `NNN-<tag>-SS`, for example `NNN-mac-SS`; legacy `NNN-SS` ids stay valid with no
migration. The tag is `[a-z][a-z0-9]{0,7}`: it starts with a letter and has no hyphen, so an id
splits without ambiguity. Both widths of the numbers are minima, so seq 100 parses.
"""

from __future__ import annotations

import re
from typing import NamedTuple

TAG_PATTERN = r"[a-z][a-z0-9]{0,7}"
TAG_RE = re.compile(rf"^{TAG_PATTERN}$")
ID_RE = re.compile(rf"^(?P<feature>\d{{3,}})-(?:(?P<tag>{TAG_PATTERN})-)?(?P<seq>\d{{2,}})$")
HANDOFF_FILE_RE = re.compile(
    rf"^(?:(?P<tag>{TAG_PATTERN})-)?(?P<seq>\d{{2,}})-(?P<short>[a-z0-9][a-z0-9._-]*)\.md$"
)
ID_FORMS = "NNN-<tag>-SS (e.g. 001-mac-01) or legacy NNN-SS (e.g. 001-01)"


class ParsedId(NamedTuple):
    feature: int
    tag: str | None
    seq: int


def parse_id(value: object) -> ParsedId | None:
    m = ID_RE.match(value) if isinstance(value, str) else None
    if m is None:
        return None
    return ParsedId(int(m.group("feature")), m.group("tag"), int(m.group("seq")))


def format_id(feature: int, seq: int, tag: str | None) -> str:
    return f"{feature:03d}-{tag}-{seq:02d}" if tag else f"{feature:03d}-{seq:02d}"
