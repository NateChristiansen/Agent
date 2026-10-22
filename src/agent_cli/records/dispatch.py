"""Shapes of the nested parts of a dispatch record, and accessors that read them.

All fields are optional: records written by older code lack fields the schema now requires. The
accessors cast and do not validate, so a malformed row fails where it always did.
"""

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
    # Legacy: returns written before the supervisor took these from git and the record.
    branch: str
    commits: list[CommitRow]
    files_touched: list[str]
    commands: list[CommandRow]


class ReviewProvenance(TypedDict, total=False):
    """What a review dispatch was pointed at, as the manager supplied it. `compare_base` is the
    commit the artifact is judged against, never the checkout; `pr` is only ever supplied."""

    target: str | None
    compare_base: str | None
    pr: str | None


class GitFacts(TypedDict):
    """What git showed for the branch since the dispatch base when the attempt was evaluated."""

    commits: list[CommitRow]
    files: list[str]


# Functional form: "from" is a keyword.
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
    """The record's git facts, or `None` for a record written before they were kept."""
    git = as_dict_or_none(d.get("git"))
    if git is None:
        return None
    return {
        "commits": cast("list[CommitRow]", dict_list(git.get("commits"))),
        "files": [str(f) for f in as_list(git.get("files"))],
    }


def review_of(d: Mapping[str, object]) -> ReviewProvenance | None:
    """The record's review provenance, or `None` for a non-review or one launched before it was kept."""
    return cast("ReviewProvenance | None", as_dict_or_none(d.get("review")))


def thread_of(d: Mapping[str, object]) -> list[ThreadEntry]:
    return cast("list[ThreadEntry]", dict_list(d.get("thread")))


def denials_of(d: Mapping[str, object]) -> list[dict[str, object] | str]:
    return cast("list[dict[str, object] | str]", as_list(d.get("permission_denials")))


def scopes_of(d: Mapping[str, object]) -> list[dict[str, object]]:
    return dict_list(d.get("verification_scopes"))
