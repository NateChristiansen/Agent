"""`request`: records too small for a feature, in the same numbered store."""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import features as features_mod
from .. import requests
from ..core import Output
from ..ops import requests as request_ops
from .subparsers import Subparsers


def _creation(p: argparse.ArgumentParser, categories: tuple[str, ...]) -> None:
    p.add_argument("slug")
    p.add_argument("--title", required=True)
    p.add_argument("--ask", required=True, help="what the owner asked for; the worker reads it as Context")
    p.add_argument("--category", required=True, choices=categories)
    p.add_argument("--repo", nargs="*", action="extend", default=[])
    p.add_argument("--session-id")


def add_parser(sub: Subparsers) -> None:
    r = sub.add_parser(
        "request", help="request records: asks, reviews, research and bounded fixes"
    ).add_subparsers(dest="sub", required=True)
    rn = r.add_parser("new", help="create an active request and map this session to it")
    _creation(rn, requests.CATEGORIES)
    rn.add_argument("--no-use", dest="use", action="store_false", help="leave the session mapping alone")
    rc = r.add_parser("capture", help="record a bug or idea as proposed; not an authorization to execute")
    _creation(rc, requests.CAPTURE_CATEGORIES)
    rc.add_argument("--use", action="store_true", help="also map this session to it")
    ru = r.add_parser("use", help="map a session to a request")
    ru.add_argument("request")
    ru.add_argument("--session-id")
    ru.add_argument("--switch", action="store_true", help="owner-requested ownership switch")
    rs = r.add_parser("show", help="one request: ask, result, status, next action, dispatch work owed")
    rs.add_argument("request", nargs="?", default="current")
    rs.add_argument("--session-id")
    rl = r.add_parser("list", help="active and captured requests")
    rl.add_argument("--all", action="store_true", help="include resolved requests")
    rx = r.add_parser("complete", help="record the delivered result and close the request")
    rx.add_argument("request")
    rx.add_argument("--result", required=True, help="what was delivered")
    rx.add_argument("--status", default="done", choices=features_mod.TERMINAL_STATUSES)
    rx.add_argument("--note", help="log a closing decision entry")
    rx.add_argument("--session-id")
    rp = r.add_parser("promote", help="make the request a feature in place, on the owner's approval")
    rp.add_argument("request")
    rp.add_argument("--reason", required=True, help="the owner's approval for the broader work")
    rp.add_argument("--session-id")


def _create(root: Path, args: argparse.Namespace, out: Output, capture: bool) -> int:
    create = request_ops.request_capture if capture else request_ops.request_new
    return out.done(
        create(
            root,
            slug=args.slug,
            title=args.title,
            ask=args.ask,
            category=args.category,
            repos=args.repo,
            use=args.use,
            session_id=args.session_id,
        )
    )


def request_new(root: Path, args: argparse.Namespace, out: Output) -> int:
    return _create(root, args, out, capture=False)


def request_capture(root: Path, args: argparse.Namespace, out: Output) -> int:
    return _create(root, args, out, capture=True)


def request_use(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(
        request_ops.request_use(root, request=args.request, session_id=args.session_id, switch=args.switch)
    )


def request_show(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(request_ops.request_show(root, request=args.request, session_id=args.session_id))


def request_list(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(request_ops.request_list(root, include_closed=args.all))


def request_complete(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(
        request_ops.request_complete(
            root,
            request=args.request,
            result=args.result,
            status=args.status,
            note=args.note,
            session_id=args.session_id,
        )
    )


def request_promote(root: Path, args: argparse.Namespace, out: Output) -> int:
    return out.done(
        request_ops.request_promote(
            root, request=args.request, reason=args.reason, session_id=args.session_id
        )
    )


HANDLERS = {
    ("request", "new"): request_new,
    ("request", "capture"): request_capture,
    ("request", "use"): request_use,
    ("request", "show"): request_show,
    ("request", "list"): request_list,
    ("request", "complete"): request_complete,
    ("request", "promote"): request_promote,
}
