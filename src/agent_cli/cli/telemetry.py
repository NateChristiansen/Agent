"""Fixed telemetry summaries for a day or a timestamp window."""

from __future__ import annotations

import argparse
from pathlib import Path

from ..core import Output
from ..mcp.tools import Op
from ..telemetry.summary import summary
from .subparsers import Subparsers


def add_parser(sub: Subparsers) -> None:
    parser = sub.add_parser("telemetry", help="read recorded telemetry").add_subparsers(
        dest="sub", required=True
    )
    report = parser.add_parser("summary", help="fixed counts, spans and health for a day or window")
    report.add_argument("--date", help="local calendar day, YYYY-MM-DD (default: today)")
    report.add_argument("--since", help="window start, ISO timestamp with UTC offset")
    report.add_argument("--until", help="window end, ISO timestamp with UTC offset (default: now)")


def telemetry_summary(root: Path, args: argparse.Namespace, out: Output) -> int:
    data = summary(root, day=args.date, since=args.since, until=args.until)
    return out.done(Op(data, f"Telemetry {data['since']} to {data['until']}; complete={data['complete']}"))


HANDLERS = {("telemetry", "summary"): telemetry_summary}
