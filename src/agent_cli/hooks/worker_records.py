"""A worker never touches the pass record store or runs `agent verify` (see ADR-0041).

Records live in the main checkout's `.agent/verify/`, outside a Codex worker's sandbox. A Claude
worker has no OS sandbox yet and its `cp`, `mv` and `python` grants reach any path, so this is a
bar-raiser, not a boundary: a command that names the store or the verb is refused on both
providers, and a deliberately disguised one is out of scope until worker containment lands.
"""

from __future__ import annotations

import re

from ..core import agent_dir_name, agent_parts

_STORE_RE = re.compile(
    rf"{re.escape(agent_dir_name())}[\\/]+(?:\.[\\/]+)*{re.escape(agent_parts('verify')[1])}\b"
)
_VERB_RE = re.compile(r"(?:^|[\s/;&|(=])(?:agent|agent_cli)(?:\s+-\S+(?:\s+[^\s-]\S*)?)*\s+verify\b")
REASON = (
    "A worker never reads or writes the pass record store or runs `agent verify`: records are "
    "the manager's evidence for landing and pushing. Return what you changed; the supervisor verifies it."
)


def record_reach(command: str) -> str | None:
    return REASON if _STORE_RE.search(command) or _VERB_RE.search(command) else None
