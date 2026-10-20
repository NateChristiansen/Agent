# ruff: noqa
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
DEVICE_STORE = agent_path(Path(), "peer-net", "state.db")
RULE = "1-8 lowercase letters and digits, starting with a letter"
FIX = f"set `machine: <tag>` in {LOCAL} ({RULE}, different on each machine; ADR-0036)"


@dataclass(frozen=True)
class Resolved:
    tag: str | None
    source: str | None
    problem: str | None


ABSENT = object()


def configured(root: Path) -> object:
    import yaml

    try:
        data = yaml.safe_load((root / LOCAL).read_text(encoding="utf-8"))
    except OSError, ValueError, yaml.YAMLError:
        return ABSENT
    return as_dict(data).get(MACHINE_KEY, ABSENT)


def default_label() -> str:
    import re
    import socket

    host = socket.gethostname().split(".")[0].lower()
    tag = re.sub("[^a-z0-9]", "", host).lstrip("0123456789")[:8]
    return tag or "machine"


def label_problem(answer: str) -> str | None:
    return None if TAG_RE.match(answer.strip().lower()) else f"a machine label is {RULE}"


def set_label(root: Path, label: str) -> tuple[str, str]:
    tag = label.strip().lower()
    problem = label_problem(tag)
    if problem:
        return ("failed", problem)
    resolved = resolve(root)
    if resolved.tag:
        return ("already", f"machine label is {resolved.tag} (from the {resolved.source})")
    if configured(root) is not ABSENT:
        return ("failed", str(resolved.problem))
    path = root / LOCAL
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        lead = "" if not text or text.endswith("\n") else "\n"
        path.write_text(f"{text}{lead}{MACHINE_KEY}: {json.dumps(tag)}\n", encoding="utf-8", newline="\n")
    except OSError as exc:
        return ("failed", f"cannot write {path}: {exc}")
    return ("done", f"machine label is {tag}")


def device_name(root: Path) -> str | None:
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
    resolved = resolve(root)
    if resolved.tag is None:
        raise AgentError(f"dispatch new needs this machine's label: {resolved.problem}")
    return resolved.tag


def launch_label(root: Path, repo: Any) -> str:
    from ..repos_validate import branch_suffix_problem

    problem = branch_suffix_problem(repo.branch_template)
    if problem:
        raise AgentError(f"dispatch new refuses: repo {repo.short} {problem}; fix it in agent.yaml")
    return require_tag(root)


def provenance(root: Path, tag: str, *, dry_run: bool) -> dict[str, Any]:
    from . import label_claims

    return {
        "machine": tag,
        "install": None if dry_run else install(root)["token"],
        "label_claim": label_claims.inspect(root, tag),
    }


def require_claim(root: Path, tag: str | None) -> dict[str, Any]:
    from . import label_claims

    return label_claims.require(root, tag or require_tag(root))


def no_local_runtime(root: Path, dispatch_id: str) -> dict[str, Any] | None:
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
    try:
        data = json.loads((root / INSTALL_FILE).read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    token, created = (dig(data, "token"), dig(data, "created"))
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
