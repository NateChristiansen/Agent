"""`peer send` and `peer list` across paired devices.

A name `<alias>/<who>` routes to the paired device `<alias>`, which resolves `<who>` there.
Results say only what an ACK proves: the other device accepted the message.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core import AgentError, table
from ..records.narrow import as_str, dict_list
from ._shared import Op


def clock_for(to: str):
    """One budget for a remote send, started here and shared by every step after it."""
    from ..peer_net import sender
    from ..peer_net.channel import Clock

    return Clock(sender.SEND_BUDGET_S) if "/" in (to or "") else None


def target(root: Path, to: str, clock: Any = None) -> tuple[str | None, str]:
    """(device alias, name) for a remote name, or (None, name) for a local one, including
    `<this device>/<name>`."""
    from ..peer_net import sender
    from ..peer_net.channel import Clock
    from ..peer_net.errors import PeerNetError

    try:
        alias, rest = sender.split(to)
        if alias is None:
            return None, to
        own = sender.own_device(root, clock or Clock(sender.SEND_BUDGET_S))
    except PeerNetError as exc:
        raise AgentError(f"refused: {exc.code} ({exc.detail})") from None
    return (None, rest) if alias == own else (alias, rest)


def send(
    root: Path, *, alias: str, who: str, message: str, frm: str, record: dict[str, Any], clock: Any = None
) -> Op:
    from ..peer_net import sender
    from ..peer_net.errors import PeerNetError

    try:
        result = sender.send(
            root,
            alias=alias,
            to=who,
            text=message,
            from_session=frm,
            from_name=str(record.get("name") or ""),
            clock=clock,
        )
    except PeerNetError as exc:
        raise AgentError(f"refused: {exc.code} ({exc.detail})") from None
    return Op(result, summary(result, alias))


def summary(result: dict[str, Any], alias: str) -> str:
    """One truthful sentence per state the sender can return. Only `accepted` claims anything
    happened there, and even that means recorded, not read."""
    outcome, answer, msg = result["outcome"], result.get("answer"), result["id"]
    if outcome == "accepted":
        if answer:
            text = (
                f"accepted on {alias} for {answer.get('to_name')} (queue {answer.get('queue')}, "
                f"wake {answer.get('wake')}). Accepted means recorded there, not read."
            )
        else:
            text = (
                f"accepted on {alias} (recorded by another attempt). Accepted means recorded there, not read."
            )
    elif outcome.startswith("refused:"):
        code = outcome.split(":", 1)[1]
        detail = f" ({answer.get('detail')})" if answer else ""
        text = (
            f"refused by {alias}: {code}{detail}. It will not be retried. If an earlier attempt's "
            "answer was lost, that attempt may still have been accepted."
        )
    elif outcome == "unconfirmed":
        text = (
            f"no answer from {alias}. Message {msg} stays open and is resent with the next send to "
            f"{alias} while it is unexpired (10 minutes); until then, acceptance is unknown."
        )
    elif outcome == "acceptance-unknown":
        text = f"message {msg} expired before {alias} confirmed it. It is closed and not resent; it may or may not have arrived."
    elif outcome == "abandoned:route-changed":
        text = (
            f"message {msg} gets no further attempt: the route to {alias} changed since it was "
            "written. Whether an earlier attempt was accepted is unknown."
        )
    elif outcome == "abandoned:no-route":
        text = (
            f"message {msg} gets no further attempt: the route to {alias} was removed. Whether an "
            "earlier attempt was accepted is unknown."
        )
    elif outcome == "skipped":
        text = f"message {msg} is being handled by another attempt right now; its outcome is not known here."
    else:  # uncertain
        text = (
            f"the outcome of message {msg} to {alias} could not be confirmed within the time budget. "
            f"If it is still open and unexpired, the next send to {alias} retries it."
        )
    earlier = dict_list(result.get("earlier"))
    if earlier:
        text += " Earlier: " + ", ".join(f"{as_str(e['id'])[:8]} {e['outcome']}" for e in earlier) + "."
    backlog = result.get("backlog")
    if backlog is None:
        text += f" The number of earlier unconfirmed messages to {alias} is unknown (out of time)."
    elif backlog:
        text += f" {backlog} earlier message(s) to {alias} still unconfirmed."
    return text


def listing(root: Path) -> tuple[list[dict[str, Any]], str, str]:
    """(items, table text, footer) for the paired devices. Never raises."""
    from ..peer_net import sender
    from .peers import THIS_MACHINE_ONLY

    blocks, notes = sender.list_remote(root)
    items: list[dict[str, Any]] = []
    rows: list[list[str]] = []
    for block in blocks:
        alias = block["alias"]
        if block["reason"]:
            items.append(
                {
                    "name": f"{alias}/*",
                    "tab": f"{alias}/*",
                    "kind": "remote",
                    "state": "unknown",
                    "reason": block["reason"],
                }
            )
            rows.append([f"{alias}/*", "—", "—", "—", f"unknown ({block['reason']})"])
        for row in block["rows"]:
            item = {
                "name": f"{alias}/{row['name'] or '—'}",
                "tab": f"{alias}/{row['tab'] or '—'}",
                "provider": row["provider"],
                "feature": row["feature"],
                "state": row["state"],
                "kind": "remote",
            }
            items.append(item)
            feature = f"{int(row['feature']):03d}" if row["feature"] else "—"
            rows.append(
                [item["name"], item["tab"], str(row["provider"] or "—"), feature, str(row["state"] or "—")]
            )
        if block["dropped"]:
            notes.append(f"{alias}: {block['dropped']} row(s) dropped as malformed")
        if block["backlog"]:
            notes.append(f"{alias}: {block['backlog']} sent message(s) not yet confirmed")
    text = table(rows, ["name", "tab", "provider", "feature", "state"]) if rows else ""
    if blocks:
        aliases = ", ".join(b["alias"] for b in blocks)
        footer = (
            f"Peers on this machine and on paired devices ({aliases}). Address a remote session as "
            "<device>/<name>; a reply goes to the `from` its message arrived with."
        )
    else:
        footer = THIS_MACHINE_ONLY
    if notes:
        footer += "\n" + "\n".join(notes)
    return items, text, footer
