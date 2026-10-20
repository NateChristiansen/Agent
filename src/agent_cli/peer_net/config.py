"""This device's cross-device settings, as the receiver reads them.

Enrollment lives in the CLI-owned store (`.agent/peer-net/state.db`), written only by the
person-only `peer-net` commands. It moved out of `agent.local.yaml`
because rewriting the owner's YAML would drop its comments; a `peer_net:` block left there
fails closed so an old setup is never half-read. Reading never writes enrollment or intents,
and a device that never enrolled has no store at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config_file import LOCAL
from .errors import PeerNetError

ALIAS_RE = re.compile(r"[a-z][a-z0-9-]{0,15}")


@dataclass(frozen=True)
class NetConfig:
    enabled: bool
    device: str
    admitted: frozenset[str] = field(default_factory=frozenset[str])  # inbound, active, not revoked

    def admits(self, alias: str) -> bool:
        return alias in self.admitted


def valid_alias(value: object) -> bool:
    return isinstance(value, str) and ALIAS_RE.fullmatch(value) is not None


def legacy_block(root: Path) -> bool:
    """A `peer_net:` key left in `agent.local.yaml` from before the store held enrollment."""
    import yaml

    try:
        data = yaml.safe_load((root / LOCAL).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except OSError, ValueError, yaml.YAMLError:
        return True  # unreadable: refuse rather than guess
    return isinstance(data, dict) and "peer_net" in data


def load(root: Path) -> NetConfig | None:
    """None when this device never enrolled. A malformed store or a legacy YAML block raises
    `config`, with no path or content in the detail: the response crosses the network."""
    from . import store

    if legacy_block(root):
        raise PeerNetError("config", "legacy peer_net block")
    if not store.exists(root):
        return None
    conn = store.connect(root)
    try:
        with store.transaction(conn):
            values = dict(conn.execute("SELECT k, v FROM enrollment").fetchall())
            admitted = {
                alias
                for (alias,) in conn.execute(
                    "SELECT alias FROM inbound WHERE state='active' AND revoked=0"
                ).fetchall()
            }
    finally:
        conn.close()
    device = values.get("device")
    if device is None:
        return None
    if not valid_alias(device) or values.get("enabled") not in {"0", "1"}:
        raise PeerNetError("config", "enrollment malformed")
    if any(not valid_alias(a) for a in admitted):
        raise PeerNetError("config", "enrollment malformed")
    return NetConfig(enabled=values["enabled"] == "1", device=str(device), admitted=frozenset(admitted))
