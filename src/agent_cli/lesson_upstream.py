"""`lesson draft_check`: the one typed step of filing a lesson upstream (see ADR-0048).

The manager writes an issue body to a scratch file, then calls this before showing the draft to
the user and running `gh issue create`. It returns the upstream repo and a capped fallback URL
for an install without a working `gh`.

The upstream is never a config key: `agent.yaml` lives in the private notes repo, so a key there
would not ship with a clone. It is origin, or origin's parent when origin is a fork.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .core import AgentError
from .records.narrow import as_dict_or_none, as_str, dig

TITLE_PREFIX = "[lesson]"
MARKER = re.compile(r"^harness-lesson: L-\d{8}-(?:[a-z][a-z0-9]{0,7}-)?\d{2,}$")
#: The whole encoded fallback URL. A chosen conservative budget, not a measured browser limit.
URL_BUDGET = 8000
_HOST = "github.com"
_PREFIXES = tuple(
    f"{scheme}{_HOST}{sep}" for scheme, sep in (("git@", ":"), ("https://", "/"), ("ssh://git@", "/"))
)


def _run(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=60)
    except OSError, subprocess.SubprocessError:
        return None


def parse_origin(url: str) -> str:
    text = url.strip()
    lowered = text.lower()
    for prefix in _PREFIXES:
        if lowered.startswith(prefix):
            path = text[len(prefix) :].removesuffix("/").removesuffix(".git").strip("/")
            parts = path.split("/")
            if len(parts) == 2 and all(parts):
                return path
            break
    raise AgentError(
        f"origin {text!r} is not a github.com repository; there is no upstream to file a lesson on"
    )


def _origin(root: Path) -> str:
    proc = _run(["git", "-C", str(root), "remote", "get-url", "origin"])
    if proc is None or proc.returncode != 0 or not proc.stdout.strip():
        raise AgentError("this clone has no origin remote; there is no upstream to file a lesson on")
    return parse_origin(proc.stdout)


def upstream(root: Path) -> tuple[str, str]:
    """`(upstream, parent_lookup)`. `parent_lookup` is `ok` when `gh` answered, `unavailable`
    when it is missing, not logged in, or failed; then origin stands in for the upstream."""
    origin = _origin(root)
    if shutil.which("gh") is None:
        return origin, "unavailable"
    proc = _run(["gh", "repo", "view", origin, "--json", "parent,nameWithOwner"])
    if proc is None or proc.returncode != 0:
        return origin, "unavailable"
    try:
        data = as_dict_or_none(json.loads(proc.stdout))
    except ValueError:
        return origin, "unavailable"
    if data is None:
        return origin, "unavailable"
    if not data.get("parent"):
        return origin, "ok"
    named = as_str(dig(data, "parent", "nameWithOwner"))
    if named:
        return named, "ok"
    login, name = as_str(dig(data, "parent", "owner", "login")), as_str(dig(data, "parent", "name"))
    if login and name:
        return f"{login}/{name}", "ok"
    return origin, "unavailable"


def _url(repo: str, title: str, body: str) -> str:
    return f"https://{_HOST}/{repo}/issues/new?title={quote(title, safe='')}&body={quote(body, safe='')}"


def fallback_url(repo: str, title: str, body: str) -> tuple[str | None, bool]:
    """`(url, truncated)`. The body is cut at a character boundary until the whole URL fits
    `URL_BUDGET` bytes, and its closing marker line is kept. None when even an empty head
    does not fit (a very long title)."""
    full = _url(repo, title, body)
    if len(full) <= URL_BUDGET:
        return full, False
    lines = body.rstrip().split("\n")
    marker, head = lines[-1], "\n".join(lines[:-1]).rstrip()

    def fits(n: int) -> bool:
        return len(_url(repo, title, f"{head[:n].rstrip()}\n\n{marker}")) <= URL_BUDGET

    if not fits(0):
        return None, True
    lo, hi = 0, len(head)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(mid):
            lo = mid
        else:
            hi = mid - 1
    return _url(repo, title, f"{head[:lo].rstrip()}\n\n{marker}"), True


def _body(root: Path, body_file: str) -> tuple[Path, str]:
    path = Path(body_file).expanduser()
    if not path.is_absolute():
        path = root / path
    try:
        return path, path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise AgentError(f"body_file {path} does not exist; write the issue body there first") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise AgentError(f"body_file {path} cannot be read as UTF-8 text: {exc}") from exc


def draft_check(root: Path, *, title: str, body_file: str) -> dict[str, Any]:
    title = " ".join(title.split())
    if not title.startswith(TITLE_PREFIX):
        raise AgentError(f"the issue title must start with {TITLE_PREFIX}")
    path, body = _body(root, body_file)
    last = body.rstrip().split("\n")[-1].strip() if body.strip() else ""
    if not MARKER.match(last):
        raise AgentError("the body must end with the line `harness-lesson: <id>`, the queue entry's id")
    repo, lookup = upstream(root)
    url, truncated = fallback_url(repo, title, body)
    return {
        "upstream": repo,
        "parent_lookup": lookup,
        "fallback_url": url,
        "truncated": truncated,
        "title": title,
        "body_file": str(path),
        "marker": last,
    }
