"""Telemetry: versioned JSON-line events in `<main>/.agent/telemetry/events/` (see ADR-0038).

Stdlib only, so a hook can import it cheaply. `record` is the explicit entry for code that knows
its main checkout; `record_implicit` routes from the environment; `health` probes and reads.
"""

from __future__ import annotations

from .health import health
from .recorder import Recorder, process_recorder, record, record_implicit, resolve, sink_root
from .schema import SCHEMA_VERSION
from .sink import Sink

__all__ = [
    "SCHEMA_VERSION",
    "Recorder",
    "Sink",
    "health",
    "process_recorder",
    "record",
    "record_implicit",
    "resolve",
    "sink_root",
]
