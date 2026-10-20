"""What no-chaining calls more than one command: shell lists, substitutions, inline shell scripts and
python one-liners that shell out. A plain python one-liner is one command."""

from __future__ import annotations

from agent_cli.hooks import is_chained

CHAINED = [
    "cd repo && git status",
    "make || echo failed",
    "ls; pwd",
    "a=1; echo $a",
    "brew install uv && uv --version",
    "true &&false",
    "ls\npwd",
    "sleep 100 & ls",
    "ls |& tee x",
    "cat > /tmp/x.py <<EOF && echo done\nline\nEOF",
    # bypasses found in review
    "cat <<EOF > note.txt\nhello\nEOF\ngit push origin main",
    "grep '<<' x\nrm -rf /",
    "echo \"don't\" && echo 'x'",
    "echo $(git push)",
    "echo `git push`",
    "diff <(ls a) <(ls b)",
    'bash -c "git add . && git commit -m x; git push"',
    "sh -c 'a; b'",
    "zsh -c 'a && b'",
    'eval "a && b"',
    "env bash -c 'a; b'",
    'uv run bash -c "git push"',
    "uv run python -c \"import os; os.system('a && b')\"",
    'python3 -c \'import subprocess; subprocess.run(["git", "push"])\'',
    "cat x | bash -c 'a; b'",
]

NOT_CHAINED = [
    "git status",
    "cat STATUS.md | grep active",
    "grep -n '&&' AGENTS.md",
    'echo "a; b"',
    'bin/agent log current --kind note "did x && y"',
    "cat > /tmp/x.py <<'EOF'\nimport os; print(1)\nif a && b: pass\nEOF",
    "git log \\\n  --oneline",
    "uv run pytest tests -q",
    "uv run pytest tests -q 2>&1 | tail -5",
    "make &> /dev/null",
    "cmd 2> err.txt",
    "ls # comment && more",
    "echo 'it''s fine'",
    'git commit -m "a \\" && b"',
    # python one-liners are one command; a tool's own -c option is not an inline shell
    "python3 -c 'import os; print(1)'",
    "bin/agent --json status check | python3 -c 'import sys; print(sys.stdin.read())'",
    "uv run pytest -c pyproject.toml tests",
    "uv run bash script.sh",
]


def test_is_chained():
    for cmd in CHAINED:
        assert is_chained(cmd), cmd
    for cmd in NOT_CHAINED:
        assert not is_chained(cmd), cmd
