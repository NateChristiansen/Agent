"""A worker `cp` or `mv` cannot write outside its dispatch worktree.

The base allow list grants `Bash(cp:*)` and `Bash(mv:*)`, so `cp evil ~/.ssh/authorized_keys`
wrote outside the worktree with no redirect for the #14 guard to see.
"""

from pathlib import Path

import pytest

from agent_cli import links
from agent_cli.hooks.worker_redirect import outside_redirect


@pytest.fixture
def wt(tmp_path: Path) -> Path:
    w = tmp_path / "worktree"
    (w / "src").mkdir(parents=True)
    return w


@pytest.mark.parametrize(
    "command",
    [
        "cp evil ~/.ssh/authorized_keys",
        "cp -r build /tmp/out",
        "cp -t ~/.ssh evil",
        "cp --target-directory=/etc evil",
        "cp -rt /tmp evil",
        "cp -- evil ../outside",
        "mv src/a.txt ~/a.txt",
        "mv ~/.ssh/authorized_keys src/keys",  # a move deletes its source
        "git status && cp evil ~/.ssh/authorized_keys",
        "FOO=1 cp evil /tmp/x",
        "/bin/cp evil /tmp/x",
        "cp evil $HOME/x",
        "cp evil ~/.ssh/*",
        "find . -name '*.pem' -exec cp {} /tmp ;",
        "ls | xargs cp -t /tmp",
        "cp 'unbalanced",
        # Codex review of #15: a newline separates commands, and a launcher's own options can
        # sit between it and cp/mv.
        "echo ok\ncp evil /tmp/x",
        "ls | xargs -0 cp -t /tmp",
        "find . -exec env cp {} /tmp ;",
        "env -i cp evil /tmp/x",
        "sh -c 'cp evil /tmp/x'",
        "time mv src/a.txt src/b.txt",  # safe, but refused: a launcher hides cp/mv arguments
    ],
)
def test_writes_outside_the_worktree_are_refused(wt: Path, command: str):
    assert outside_redirect(command, wt), command


@pytest.mark.parametrize(
    "command",
    [
        "cp src/a.txt src/b.txt",
        "cp -r src build",
        "cp ~/.gitconfig src/gitconfig",  # reading from outside is not a write outside
        "cp -t src a.txt b.txt",
        "mv src/a.txt src/b.txt",
        "git mv src/a.txt src/b.txt",
        "cp src/a.txt src/b.txt 2> /dev/null",
        "echo cp is a word, not a command",
        "cp",
        "echo 'line one\ncp evil /tmp/x'",  # a quoted newline is text, not a command boundary
        "echo ok\ncp src/a.txt src/b.txt",
    ],
)
def test_writes_inside_the_worktree_pass(wt: Path, command: str):
    assert outside_redirect(command, wt) is None, command


def test_a_symlink_out_of_the_worktree_is_refused(wt: Path, tmp_path: Path):
    links.make(wt / "escape", tmp_path, is_dir=True)
    assert outside_redirect("cp evil escape/out.txt", wt)
