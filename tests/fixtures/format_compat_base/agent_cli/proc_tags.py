# ruff: noqa
from __future__ import annotations
import re
from pathlib import Path

TAG_SESSION = "CLAUDE_CODE_SESSION_ID"
TAG_DISPATCH = "AGENT_DISPATCH_ID"
TAG_ROOT = "AGENT_ROOT"
"A dispatch id is unique only inside one harness root: `001-01` exists in every vault copy,\neval sandbox and test root. Everything that carries `AGENT_DISPATCH_ID` also carries the\n`AGENT_ROOT` it belongs to, and a dispatch reap matches both. Matching the id alone let a\nsupervisor ending in one root SIGTERM another root's live worker, verifier and supervisor."
_TAG_RES = {
    TAG_SESSION: re.compile(f"(?:^|\\s){TAG_SESSION}=(\\S+)"),
    TAG_DISPATCH: re.compile(f"(?:^|\\s){TAG_DISPATCH}=(\\S+)"),
    TAG_ROOT: re.compile(f"(?:^|\\s){TAG_ROOT}=(.+?)(?=\\s+[A-Za-z_][A-Za-z0-9_]*=|\\s*$)"),
}


def parse_tags(env_line: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for tag, pattern in _TAG_RES.items():
        m = pattern.search(env_line)
        if m:
            out[tag] = m.group(1)
    return out


def ps_tags(output: str, read: set[int], tags: dict[int, dict[str, str]]) -> None:
    for line in output.splitlines():
        head = line.strip().split(None, 1)
        if head and head[0].isdigit():
            read.add(int(head[0]))
            tags[int(head[0])] = parse_tags(head[1] if len(head) == 2 else "")


def resolved_root(path: str) -> str:
    if not path:
        return ""
    try:
        return str(Path(path).resolve())
    except OSError, RuntimeError, ValueError:
        return ""


def linux_tags(pids: list[int], proc: Path = Path("/proc")) -> tuple[set[int], dict[int, dict[str, str]]]:
    read: set[int] = set()
    tags: dict[int, dict[str, str]] = {}
    for pid in pids:
        try:
            raw = (proc / str(pid) / "environ").read_bytes()
        except PermissionError:
            raw = b""
        except OSError:
            continue
        env = dict((item.split("=", 1) for item in raw.decode(errors="replace").split("\x00") if "=" in item))
        read.add(pid)
        tags[pid] = {name: env[name] for name in (TAG_SESSION, TAG_DISPATCH, TAG_ROOT) if env.get(name)}
    return (read, tags)
