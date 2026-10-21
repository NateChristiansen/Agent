"""`git` and `gh` are faked at the subprocess boundary; nothing here touches the network."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_cli import lesson_upstream, notes
from agent_cli.core import AgentError

TITLE = "[lesson] check the question hold before calling a run stuck"
BODY = "A worker returned a question in 15 seconds.\n\nThe run looked hung for 10 minutes.\n\nharness-lesson: L-20260903-01\n"
HTTPS = "https://github.com/coworker/agent.git"


REAL_RUN = subprocess.run


class Fake:
    def __init__(self, origin: str = HTTPS, gh: tuple[int, str] | None = (0, '{"parent": null}')):
        self.origin, self.gh, self.calls = origin, gh, []

    def __call__(self, argv, **kwargs):
        if argv[0] == "git" and argv[-3:] == ["remote", "get-url", "origin"]:
            self.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, f"{self.origin}\n", "")
        if argv[0] == "gh":
            self.calls.append(list(argv))
            if self.gh is None:
                raise FileNotFoundError(argv[0])
            return subprocess.CompletedProcess(argv, self.gh[0], self.gh[1], "")
        return REAL_RUN(argv, **kwargs)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Fake:
    f = Fake()
    monkeypatch.setattr(lesson_upstream.subprocess, "run", f)
    real_which = lesson_upstream.shutil.which

    def which(name: str, *args, **kwargs):
        if name != "gh":
            return real_which(name, *args, **kwargs)
        return "/usr/bin/gh" if f.gh else None

    monkeypatch.setattr(lesson_upstream.shutil, "which", which)
    return f


def _body(root: Path, text: str = BODY) -> str:
    path = root / ".agent" / "scratch" / "issue.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def _check(root: Path, title: str = TITLE, body: str = BODY) -> dict:
    return lesson_upstream.draft_check(root, title=title, body_file=_body(root, body))


def _linked(root: Path) -> None:
    repo = root.parent / "notes-linked"
    repo.mkdir(exist_ok=True)
    notes.write_notes_root(root, repo)


def test_a_fork_files_on_its_parent(tmp_root: Path, fake: Fake):
    fake.gh = (
        0,
        json.dumps(
            {"nameWithOwner": "coworker/agent", "parent": {"name": "agent", "owner": {"login": "owner"}}}
        ),
    )
    result = _check(tmp_root)
    assert (result["upstream"], result["parent_lookup"]) == ("owner/agent", "ok")
    assert ["gh", "repo", "view", "coworker/agent", "--json", "parent,nameWithOwner"] in fake.calls
    assert result["fallback_url"].startswith("https://github.com/owner/agent/issues/new?title=")


def test_a_non_fork_files_on_origin(tmp_root: Path, fake: Fake):
    fake.gh = (0, json.dumps({"nameWithOwner": "coworker/agent", "parent": None}))
    result = _check(tmp_root)
    assert (result["upstream"], result["parent_lookup"]) == ("coworker/agent", "ok")


def test_no_gh_falls_back_to_origin(tmp_root: Path, fake: Fake):
    fake.gh = None
    result = _check(tmp_root)
    assert (result["upstream"], result["parent_lookup"]) == ("coworker/agent", "unavailable")
    assert not any(c[0] == "gh" for c in fake.calls)
    assert result["fallback_url"]


def test_gh_not_logged_in_falls_back_to_origin(tmp_root: Path, fake: Fake):
    fake.gh = (4, "")  # `gh` exits 4 when it needs `gh auth login`
    result = _check(tmp_root)
    assert (result["upstream"], result["parent_lookup"]) == ("coworker/agent", "unavailable")


@pytest.mark.parametrize(
    "origin",
    [
        "https://github.com/coworker/agent.git",
        "https://github.com/coworker/agent",
        "git@github.com:coworker/agent.git",
        "ssh://git@github.com/coworker/agent.git",
        "HTTPS://GitHub.com/coworker/agent/",
    ],
)
def test_ssh_and_https_origins_parse_to_owner_and_repo(origin: str):
    assert lesson_upstream.parse_origin(origin) == "coworker/agent"


@pytest.mark.parametrize(
    "origin",
    [
        "https://gitlab.com/coworker/agent.git",
        "git@example.com:coworker/agent.git",
        "https://github.com/agent",
    ],
)
def test_a_non_github_origin_refuses(tmp_root: Path, fake: Fake, origin: str):
    fake.origin = origin
    with pytest.raises(AgentError, match=r"not a github\.com repository"):
        _check(tmp_root)


def test_a_linked_notes_repo_still_returns_the_upstream_and_the_url(tmp_root: Path, fake: Fake):
    _linked(tmp_root)
    result = _check(tmp_root)
    assert result["upstream"] == "coworker/agent"
    assert result["fallback_url"].startswith("https://github.com/coworker/agent/issues/new?title=")
    assert set(result) == {
        "upstream",
        "parent_lookup",
        "fallback_url",
        "truncated",
        "title",
        "body_file",
        "marker",
    }


def test_the_draft_must_carry_its_title_prefix_and_marker(tmp_root: Path, fake: Fake):
    with pytest.raises(AgentError, match=r"start with \[lesson\]"):
        _check(tmp_root, title="check the question hold")
    with pytest.raises(AgentError, match="harness-lesson"):
        _check(tmp_root, body="no marker here\n")
    assert _check(tmp_root, body="detail\n\nharness-lesson: L-20260903-mac-01\n")["marker"]


def test_a_short_body_gives_the_whole_draft_in_the_url(tmp_root: Path, fake: Fake):
    result = _check(tmp_root)
    query = parse_qs(urlsplit(result["fallback_url"]).query)
    assert query["title"] == [TITLE] and query["body"] == [BODY]
    assert result["truncated"] is False


def test_a_long_body_is_cut_to_the_budget_and_keeps_its_marker(tmp_root: Path, fake: Fake):
    body = "".join(f"line {n}: é 漢字 detail\n" for n in range(2000)) + "\nharness-lesson: L-20260903-01\n"
    result = _check(tmp_root, body=body)
    url = result["fallback_url"]
    assert result["truncated"] is True
    assert len(url.encode("utf-8")) <= 8000
    assert len(url) > 8000 - 40, "the cut uses most of the budget"
    sent = parse_qs(urlsplit(url).query)["body"][0]
    assert sent.endswith("\n\nharness-lesson: L-20260903-01")
    assert body.startswith(sent.split("\n\nharness-lesson:")[0])


def test_draft_check_over_the_cli(tmp_root: Path, run, fake: Fake):
    r = run(["--json", "lesson", "draft-check", "--title", TITLE, "--body-file", _body(tmp_root)])
    assert r.code == 0, r
    assert r.json["upstream"] == "coworker/agent" and r.json["truncated"] is False
