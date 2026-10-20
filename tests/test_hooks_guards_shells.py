"""no-chaining and shell calls (#64): a shell that runs an inline script is a chain; a shell given
a script file, or only asked its version, is one command. Refusing `/bin/bash --version` and
`bash setup.sh` cost two sessions on two Macs a retry each (2026-09-29)."""

from __future__ import annotations

import pytest

from agent_cli.hooks import is_chained
from hooks_test_helpers import _decision, _reason, bash


@pytest.mark.parametrize(
    "cmd",
    [
        "bash -lc 'a; b'",
        "bash -s",
        "bash",
        "sh <<EOF\na\nb\nEOF",
        "bash < script.sh",
        "env FOO=1 bash -c 'a'",
        "source env.sh",
        ". env.sh",
        "eval ls",
        # Codex review: -c wins over a later --version, and an option operand is not a script
        'bash -c "a; b" --version',
        "bash -o pipefail",
        "bash -O extglob",
        "bash --rcfile x.sh",
        "bash --",
    ],
)
def test_a_shell_running_an_inline_script_is_still_refused(cmd: str) -> None:
    assert is_chained(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        "/bin/bash --version",
        "bash setup.sh",
        "bash -n setup.sh",
        "/bin/bash ./setup.sh --check",
        "sh -e script.sh",
        "env PATH=/x /bin/bash old-setup.sh",
        "bash -o pipefail setup.sh",
        "bash -- setup.sh",
        "bash --norc setup.sh --check",
    ],
)
def test_a_shell_given_a_script_file_is_one_command(cmd: str) -> None:
    assert not is_chained(cmd)


@pytest.mark.parametrize("cmd", ("cd repo && git status", "echo $(git push)"))
def test_no_chaining_codex_denial_text(tmp_root, run, monkeypatch, cmd: str) -> None:
    monkeypatch.setenv("CODEX_SESSION_ID", "codex-test")
    r = run(["hook", "no-chaining"], stdin=bash(cmd))
    reason = _reason(r)
    assert _decision(r) == "deny"
    assert "Bash call" not in reason
    assert "shell call" in reason
    assert not reason.endswith(".")
    assert ".. Command:" not in reason + ". Command: " + cmd
    if cmd.startswith("cd "):
        assert "Run `cd repo` first" in reason
