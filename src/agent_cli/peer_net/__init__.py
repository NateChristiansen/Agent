"""Cross-device peer messages over a restricted SSH hop (see ADR-0035).

This package holds the receiving side: the envelope, the durable store and the forced-command
receiver. Enrollment and device-qualified routing come in later diffs.
"""

from __future__ import annotations

from .errors import PeerNetError
from .receiver import receive

__all__ = ["PeerNetError", "receive"]
