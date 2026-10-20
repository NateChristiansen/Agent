"""Builds the small product repo the onboarding procedure is exercised against.

    uv run python -m tests.onboarding_repo /tmp/onboarding-fixture

Creates `<dest>/remote.git` (bare) and `<dest>/product` (a clone) and prints the remote path,
the default branch and the commit SHA. The commit is byte-identical on every machine: fixed
identity and dates, LF endings, no user or system git config, and the LFS attribute is only text
so `git-lfs` is never needed. Every script in it is an inert file.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BRANCH = "main"
STAMP = "2026-01-01T00:00:00+0000"

_AGENTS = """# Product

Branches are `feat/<id>-<slug>`. Commits read `PRD-<id> <summary>`. Never rebase a pushed branch.
`docs/generated.md` is never hand-edited.

## Ask first

- Ask before running `bin/prodctl merge` or `bin/prodctl publish`.
- Ask before starting the app: it holds port 4317 and `.run/app.lock`, so one runs at a time.
- Ask before changing anything under `migrations/`.

## Setup

Copy `.env.local.example` to `.env.local` and set `TOOLCHAIN_DIR`. It is gitignored, so a fresh
worktree lacks it.
"""

_CLAUDE_SETTINGS = """{
  "hooks": {
    "PreToolUse": [
      {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 .claude/hooks/block_publish.py"}]}
    ],
    "Stop": [
      {"hooks": [{"type": "command", "command": "python3 .claude/hooks/on_stop.py"}]}
    ],
    "WorktreeCreate": [
      {"hooks": [{"type": "command", "command": "python3 .claude/hooks/worktree_create.py"}]}
    ]
  }
}
"""

_CODEX_HOOKS = """{
  "hooks": {
    "PreToolUse": [
      {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 .claude/hooks/block_publish.py"}]}
    ]
  }
}
"""

_CODEX_CONFIG = """approval_policy = "on-request"

[mcp_servers.product_docs]
command = "node"
args = ["tools/docs-mcp/server.js"]
"""

_MCP = """{
  "mcpServers": {
    "product_db": {"command": "npx", "args": ["--no-install", "product-db-mcp"]}
  }
}
"""

_CI = """name: ci
on: [push, pull_request]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm ci
      - run: npm run lint
      - run: npm test
"""

_PRODCTL = """#!/bin/sh
# Repo CLI. Inert in the fixture: it only prints what the real command would do.
case "$1" in
  worktree) echo "would create a worktree for $2" ;;
  merge) echo "would merge $2 into main" ;;
  publish) echo "would publish the current build" ;;
  release) echo "would tag a release" ;;
  *) echo "usage: prodctl worktree|merge|publish|release" ;;
esac
"""

_HOOK_SCRIPT = '"""Inert fixture hook."""\n'

FILES: dict[str, str] = {
    "AGENTS.md": _AGENTS,
    "CLAUDE.md": "See AGENTS.md.\n",
    ".agents/workflow.md": "# Workflow\n\nOpen a branch, run `npm test`, ask before merge.\n",
    ".claude/settings.json": _CLAUDE_SETTINGS,
    ".claude/hooks/block_publish.py": _HOOK_SCRIPT,
    ".claude/hooks/on_stop.py": _HOOK_SCRIPT,
    ".claude/hooks/worktree_create.py": _HOOK_SCRIPT,
    ".codex/hooks.json": _CODEX_HOOKS,
    ".codex/config.toml": _CODEX_CONFIG,
    ".mcp.json": _MCP,
    ".githooks/pre-commit": "#!/bin/sh\n# needs `npx lint-staged` installed\nexit 0\n",
    "bin/prodctl": _PRODCTL,
    ".github/workflows/ci.yml": _CI,
    ".gitignore": ".env.local\n.run/\nnode_modules/\n",
    ".env.local.example": "TOOLCHAIN_DIR=\n",
    ".gitattributes": "* text=auto eol=lf\n*.bin filter=lfs diff=lfs merge=lfs -text\n",
    "assets/model.bin": (
        "version https://git-lfs.github.com/spec/v1\n"
        "oid sha256:4d7a214614ab2935c943f9e0ff69d22eadbb8f32b1258daaa5e2ca24d17e2393\n"
        "size 1048576\n"
    ),
    "docs/runtime.md": "The app listens on port 4317 and writes `.run/app.lock`.\n",
    "package.json": '{"name": "product", "scripts": {"lint": "echo lint", "test": "echo test"}}\n',
    "README.md": "# Product\n",
}

EXECUTABLE = ("bin/prodctl", ".githooks/pre-commit")


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Fixture",
        GIT_AUTHOR_EMAIL="fixture@example.com",
        GIT_COMMITTER_NAME="Fixture",
        GIT_COMMITTER_EMAIL="fixture@example.com",
        GIT_AUTHOR_DATE=STAMP,
        GIT_COMMITTER_DATE=STAMP,
    )
    return env


def _git(args: list[str], cwd: Path, *, seed: bool = False) -> str:
    # seed commands pin core.fileMode so the index modes come only from update-index --chmod
    pin = ["-c", "core.fileMode=false"] if seed else []
    cmd = ["git", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false", *pin, *args]
    proc = subprocess.run(cmd, cwd=cwd, env=_env(), capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def build(dest: Path) -> tuple[Path, str, str]:
    """Build the bare remote and the clone under `dest`; return (remote, branch, sha)."""
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.iterdir()):
        raise SystemExit(f"{dest} is not empty")
    remote = dest / "remote.git"
    work = dest / "seed"
    product = dest / "product"
    remote.mkdir()
    work.mkdir()
    _git(["init", "-q", "--bare", f"--initial-branch={BRANCH}", "--template=", str(remote)], dest)
    _git(["init", "-q", f"--initial-branch={BRANCH}", "--template="], work, seed=True)
    for rel, text in FILES.items():
        path = work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    _git(["add", "-A"], work, seed=True)
    for rel in EXECUTABLE:
        _git(["update-index", "--chmod=+x", rel], work, seed=True)
    _git(["commit", "-q", "-m", "PRD-1 initial product"], work, seed=True)
    sha = _git(["rev-parse", "HEAD"], work, seed=True)
    _git(["push", "-q", str(remote), BRANCH], work, seed=True)
    _git(["clone", "-q", "--template=", str(remote), str(product)], dest)
    _git(["config", "core.hooksPath", ".githooks"], product)
    (product / ".env.local").write_text("TOOLCHAIN_DIR=/opt/toolchain\n", encoding="utf-8")
    return remote, BRANCH, sha


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m tests.onboarding_repo <dest>", file=sys.stderr)
        return 2
    remote, branch, sha = build(Path(argv[0]))
    print(f"remote: {remote}")
    print(f"branch: {branch}")
    print(f"sha: {sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
