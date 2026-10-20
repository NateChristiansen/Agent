"""The CLI parser is built once per process and each parse stays independent: argparse hands an omitted list-valued option its default object itself, so a reused
parser must not let one call's namespace leak into the next."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

from agent_cli import cli


def test_the_parser_is_built_once(monkeypatch: pytest.MonkeyPatch):
    built: list[int] = []
    real = cli.build_parser

    def counting() -> argparse.ArgumentParser:
        built.append(1)
        return real()

    cli._parser.cache_clear()  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(cli, "build_parser", counting)
    for _ in range(3):
        cli.parse(["feature", "list"])
    assert built == [1]
    cli._parser.cache_clear()  # pyright: ignore[reportPrivateUsage]


def test_omitted_list_flags_are_fresh_objects_each_parse():
    first = cli.parse(["feature", "new", "alpha", "--title", "A"])
    second = cli.parse(["feature", "new", "beta", "--title", "B"])
    for dest in ("repo", "ticket", "tag"):
        if hasattr(first, dest) and isinstance(getattr(first, dest), list):
            assert getattr(first, dest) is not getattr(second, dest), dest


def test_mutating_one_namespace_never_reaches_the_next():
    first = cli.parse(["feature", "new", "alpha", "--title", "A"])
    lists = [d for d, v in vars(first).items() if isinstance(v, list)]
    assert lists, "feature new has list-valued options"
    for dest in lists:
        getattr(first, dest).append("leaked")
    second = cli.parse(["feature", "new", "beta", "--title", "B"])
    assert all("leaked" not in getattr(second, dest) for dest in lists)


def test_given_list_flags_still_parse(tmp_root: Path):
    args = cli.parse(["feature", "new", "alpha", "--title", "A", "--repo", "alpha"])
    assert args.repo == ["alpha"]
    again = cli.parse(["feature", "new", "beta", "--title", "B"])
    assert again.repo == []


def test_repeated_main_calls_and_a_replaced_handler(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch, run: Any
):
    assert run(["feature", "list"]).code == 0
    seen: list[str] = []

    def handler(_root: Path, args: argparse.Namespace, out: Any) -> int:
        seen.append(args.cmd)
        return 0

    monkeypatch.setitem(cli.HANDLERS, ("feature", "list"), handler)
    assert run(["feature", "list"]).code == 0
    assert seen == ["feature"]
