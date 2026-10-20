"""This machine's dispatch label and install token (see ADR-0036).

The label tags every new dispatch id, `NNN-<tag>-SS`, so two machines sharing one notes repo
never allocate the same id, handoff, branch or worktree path. It is a label the owner sets:
`machine:` in `agent.local.yaml` wins, else the ADR-0035 enrolled device name. Either is
lowercased and must fit `[a-z][a-z0-9]{0,7}`; a value that does not fit is never used, and
`dispatch new` refuses until `machine:` is set. Only `agent setup` writes it, with the owner's
answer or a hostname-derived default.

The install token is a random value minted once per install under `.agent/`. Handoffs carry it
as `install:` so doctor can tell two machines sharing one label from a reinstall. It never
appears in an id.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config_file import LOCAL
from ..core import AgentError, agent_path, iso, now
from ..records.narrow import as_dict, dig
from .ids import TAG_RE

MACHINE_KEY = "machine"
INSTALL_FILE = agent_path(Path(), "install.json")
# The ADR-0035 enrollment store, written only by the person-only `peer-net enroll`.
DEVICE_STORE = agent_path(Path(), "peer-net", "state.db")
RULE = "1-8 lowercase letters and digits, starting with a letter"
FIX = f"set `machine: <tag>` in {LOCAL} ({RULE}, different on each machine; ADR-0036)"


@dataclass(frozen=True)
class Resolved:
    tag: str | None
    source: str | None  # "machine", "device", or None when no label resolves
    problem: str | None


ABSENT = object()  # no `machine:` key at all, as distinct from `machine: null`


def configured(root: Path) -> object:
    """The raw `machine:` value, `None` included when the key is present but null, or `ABSENT`
    when there is no key. Only `ABSENT` lets the device name stand in; a present value that is
    null, not a string or not a valid label refuses. An unreadable file is `load_local`'s to
    report."""
    import yaml

    try:
        data = yaml.safe_load((root / LOCAL).read_text(encoding="utf-8"))
    except OSError, ValueError, yaml.YAMLError:
        return ABSENT
    return as_dict(data).get(MACHINE_KEY, ABSENT)


def default_label() -> str:
    """The suggested label: the hostname's first part cut to fit, or `machine`."""
    import re
    import socket

    host = socket.gethostname().split(".")[0].lower()
    tag = re.sub(r"[^a-z0-9]", "", host).lstrip("0123456789")[:8]
    return tag or "machine"


def label_problem(answer: str) -> str | None:
    return None if TAG_RE.match(answer.strip().lower()) else f"a machine label is {RULE}"


def set_label(root: Path, label: str) -> tuple[str, str]:
    """Write `machine: <label>` to `agent.local.yaml` as a setup step's `(state, detail)`. A
    label that already resolves, from the key or the enrolled device, is kept; the rest of the
    file is appended to, never rewritten. The value is quoted so `yes` or `null` stay strings."""
    tag = label.strip().lower()
    problem = label_problem(tag)
    if problem:
        return "failed", problem
    resolved = resolve(root)
    if resolved.tag:
        return "already", f"machine label is {resolved.tag} (from the {resolved.source})"
    if configured(root) is not ABSENT:
        return "failed", str(resolved.problem)
    path = root / LOCAL
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        lead = "" if not text or text.endswith("\n") else "\n"
        path.write_text(f"{text}{lead}{MACHINE_KEY}: {json.dumps(tag)}\n", encoding="utf-8", newline="\n")
    except OSError as exc:
        return "failed", f"cannot write {path}: {exc}"
    return "done", f"machine label is {tag}"


def device_name(root: Path) -> str | None:
    """The ADR-0035 enrolled device name, read-only; None when never enrolled or unreadable."""
    path = root / DEVICE_STORE
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT v FROM enrollment WHERE k='device'").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return str(row[0]) if row and row[0] else None


def resolve(root: Path) -> Resolved:
    value = configured(root)
    if value is not ABSENT:
        tag = value.strip().lower() if isinstance(value, str) else ""
        if TAG_RE.match(tag):
            return Resolved(tag, "machine", None)
        return Resolved(None, None, f"`machine: {value}` in {LOCAL} is not a valid label; {FIX}")
    device = device_name(root)
    if device:
        tag = device.strip().lower()
        if TAG_RE.match(tag):
            return Resolved(tag, "device", None)
        problem = f"the enrolled device name {device!r} does not fit a dispatch label; {FIX}"
        return Resolved(None, None, problem)
    return Resolved(None, None, f"no machine label is set; {FIX}")


def require_tag(root: Path) -> str:
    """The label `dispatch new` tags its id with, or AgentError naming the fix. Never falls back
    to an untagged id."""
    resolved = resolve(root)
    if resolved.tag is None:
        raise AgentError(f"dispatch new needs this machine's label: {resolved.problem}")
    return resolved.tag


def launch_label(root: Path, repo: Any) -> str:
    """What `dispatch new` checks before it allocates an id: a `branch_template` that names
    `{suffix}` and a valid label. Each refusal names its fix."""
    from ..repos_validate import branch_suffix_problem

    problem = branch_suffix_problem(repo.branch_template)
    if problem:
        raise AgentError(f"dispatch new refuses: repo {repo.short} {problem}; fix it in agent.yaml")
    return require_tag(root)


def provenance(root: Path, tag: str, *, dry_run: bool, inspect_claim: bool = True) -> dict[str, Any]:
    """The record's and handoff's `machine:` and `install:`. A dry run mints no token. A caller
    that replaces `label_claim` with a verified claim before anything reads it passes
    `inspect_claim=False` and skips the read-only inspection."""
    from . import label_claims

    return {
        "machine": tag,
        "install": None if dry_run else install(root)["token"],
        "label_claim": label_claims.inspect(root, tag) if inspect_claim else None,
    }


def require_claim(root: Path, tag: str | None) -> dict[str, Any]:
    from . import label_claims

    return label_claims.require(root, tag or require_tag(root))


def no_local_runtime(root: Path, dispatch_id: str) -> dict[str, Any] | None:
    """For `dispatch show` on a tagged id this machine holds no record for: the synced handoff
    that carries it, classified by tag and install token. None when there is no such handoff
    (a legacy id, or nothing synced yet), so the caller raises its usual not-found."""
    from .. import features as features_mod
    from .. import frontmatter
    from .ids import parse_id

    dispatch_id = dispatch_id.strip()
    parsed = parse_id(dispatch_id)
    if parsed is None or parsed.tag is None:
        return None
    try:
        feature = features_mod.resolve(root, str(parsed.feature))
    except AgentError:
        return None
    seq = dispatch_id.rsplit("-", 1)[1]
    ours = read_install(root)
    for path in sorted((feature.dir / "handoffs").glob(f"{parsed.tag}-{seq}-*.md")):
        try:
            meta, _ = frontmatter.load(path)
        except AgentError:
            continue
        if str(meta.get("id")) != dispatch_id:
            continue
        install_token = str(meta.get("install") or "") or None
        return {
            "id": dispatch_id,
            "local_runtime": False,
            "machine": str(meta.get("machine") or parsed.tag),
            "install": install_token,
            "this_install": bool(ours and install_token == ours["token"]),
            "status": meta.get("status"),
            "handoff": path.relative_to(root).as_posix(),
        }
    return None


def read_install(root: Path) -> dict[str, Any] | None:
    """This install's `{token, created}`, or None when none was minted yet or the file is bad."""
    try:
        data = json.loads((root / INSTALL_FILE).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    token, created = dig(data, "token"), dig(data, "created")
    if not isinstance(token, str) or len(token) < 16:
        return None
    if not isinstance(created, str):
        return None
    result: dict[str, Any] = {"token": token, "created": created}
    claim = dig(data, "label_claim")
    if isinstance(claim, dict):
        result["label_claim"] = claim
    return result


def install(root: Path) -> dict[str, Any]:
    """This install's token, minted on first use. Created with a hard link from a finished temp
    file, so a concurrent first dispatch reads the winner's token, never a torn file."""
    path = root / INSTALL_FILE
    current = read_install(root)
    if current is not None:
        return current
    if path.exists():
        raise AgentError(f"{path} is not a valid install token file; delete it and a new token is minted")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"token": secrets.token_hex(8), "created": iso(now())}) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".install-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        os.unlink(temporary)
    minted = read_install(root)
    if minted is None:
        raise AgentError(f"{path} could not be read back after it was written")
    return minted
