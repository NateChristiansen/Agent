# ruff: noqa
from __future__ import annotations
from collections.abc import Mapping
from typing import TypedDict, cast
from .narrow import as_dict, as_dict_or_none, as_list, dict_list


class CommitRow(TypedDict, total=False):
    sha: str
    subject: str


class CommandRow(TypedDict, total=False):
    cmd: str
    exit: int
    note: str


class WorkerReturn(TypedDict, total=False):
    nonce: str
    status: str
    summary: str
    open_questions: list[str]
    next_steps: list[str]
    notes: str
    comments: list[dict[str, object]]
    conventions: list[dict[str, object]]
    branch: str
    commits: list[CommitRow]
    files_touched: list[str]
    commands: list[CommandRow]


class GitFacts(TypedDict):
    commits: list[CommitRow]
    files: list[str]


ThreadEntry = TypedDict(
    "ThreadEntry",
    {
        "from": str,
        "text": str,
        "ts": str,
        "status": str,
        "attempt": int,
        "cost_usd": float | None,
        "steer": bool,
        "auto": bool,
    },
    total=False,
)


def result_of(d: Mapping[str, object]) -> WorkerReturn:
    return cast("WorkerReturn", as_dict(d.get("result")))


def git_of(d: Mapping[str, object]) -> GitFacts | None:
    git = as_dict_or_none(d.get("git"))
    if git is None:
        return None
    return {
        "commits": cast("list[CommitRow]", dict_list(git.get("commits"))),
        "files": [str(f) for f in as_list(git.get("files"))],
    }


def thread_of(d: Mapping[str, object]) -> list[ThreadEntry]:
    return cast("list[ThreadEntry]", dict_list(d.get("thread")))


def denials_of(d: Mapping[str, object]) -> list[dict[str, object] | str]:
    return cast("list[dict[str, object] | str]", as_list(d.get("permission_denials")))


def scopes_of(d: Mapping[str, object]) -> list[dict[str, object]]:
    return dict_list(d.get("verification_scopes"))
