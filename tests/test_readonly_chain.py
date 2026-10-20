"""The relaxed no-chaining rule: read-only chains pass, anything that can write or run does not."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli.hooks import guards
from agent_cli.hooks.readonly_chain import read_only_chain


@pytest.mark.parametrize(
    "cmd",
    [
        "grep -n foo src/a.py ; wc -l src/a.py",
        "ls src && echo ---; ls tests",
        "cat a.json | jq '.x' | head -5",
        "grep -rn 'a && b' src && tail -3 log.txt",
        "head -20 README.md; echo; tail -20 README.md",
        "/bin/ls -la && /usr/bin/wc -l x",
    ],
)
def test_read_only_chains_pass(cmd: str) -> None:
    assert read_only_chain(cmd)
    assert guards.no_chaining(Path("."), {"tool_input": {"command": cmd}}) == ""


@pytest.mark.parametrize(
    "cmd",
    [
        "ls ; sed -n 'w out' in.txt",
        "ls && git log --output=out",
        "ls && git diff",  # diff.external / textconv run with no flag on the line
        "git status && git log --oneline -3",
        "ls ; find . -delete",
        "rg --pre ./run.sh x src ; ls",
        "rg --pre=./run.sh x ; ls",
        "rg -iz x ; ls",
        "jq -f prog.jq a.json ; ls",
        "RIPGREP_CONFIG_PATH=x rg foo ; ls",
        "ls $(rm -rf x) ; ls",
        "ls `touch x` ; ls",
        "echo hi > out.txt ; ls",
        "cat < in ; ls",
        "ls || ls",
        "ls & ls",
        "ls\nls",
        "cat <<EOF ; ls\nx\nEOF",
        "rm -rf build ; ls",
        "./ls ; ls",
        "python3 -c 'print(1)' ; ls",
        # Codex review, phases 2-3: ANSI-C quoting hid `--pre`, and a traversing path ran another program
        "rg $'--pre=/tmp/x' needle f; echo done",
        "rg -n needle src ; ls",
        "/usr/bin/../../tmp/x/ls ; echo done",
        "echo $HOME ; ls",
        "grep ${PAT} f ; ls",
        "jq --arg key -- -f program.jq data.json ; ls",
    ],
)
def test_writes_and_program_runs_do_not_qualify(cmd: str) -> None:
    assert not read_only_chain(cmd)
    out = guards.no_chaining(Path("."), {"tool_input": {"command": cmd}})
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_single_command_is_not_a_chain() -> None:
    assert not read_only_chain("ls -la")
