# ruff: noqa
from __future__ import annotations
import shutil
from dataclasses import dataclass

MACOS = "Darwin"
WINDOWS = "Windows"
WINGET = ("winget", "install", "-e", "--accept-source-agreements", "--accept-package-agreements", "--id")


@dataclass(frozen=True)
class Tool:
    name: str
    binary: str
    url: str
    macos: str
    linux: str
    windows: str
    note: str = ""
    auto_macos: tuple[str, ...] = ()
    auto_linux: tuple[str, ...] = ()
    auto_windows: tuple[str, ...] = ()

    def guide(self, system: str) -> str:
        line = self.windows if system == WINDOWS else self.macos if system == MACOS else self.linux
        parts = [line, self.url]
        if self.note:
            parts.insert(1, self.note)
        return "  ".join((p for p in parts if p))

    def auto(self, system: str) -> tuple[str, ...]:
        argv = (
            self.auto_windows
            if system == WINDOWS
            else self.auto_macos
            if system == MACOS
            else self.auto_linux
        )
        return argv if argv and shutil.which(argv[0]) else ()


CLAUDE_NPM = ("npm", "install", "-g", "@anthropic-ai/claude-code")
TOOLS = (
    Tool(
        "git",
        "git",
        "https://git-scm.com/downloads",
        macos="brew install git",
        linux="sudo apt install git  (dnf: sudo dnf install git)",
        windows="winget install --id Git.Git -e",
        auto_macos=("brew", "install", "git"),
        auto_windows=(*WINGET, "Git.Git"),
    ),
    Tool(
        "uv",
        "uv",
        "https://docs.astral.sh/uv/",
        macos="brew install uv",
        linux="curl -LsSf https://astral.sh/uv/install.sh | sh",
        windows="winget install --id astral-sh.uv -e",
        auto_macos=("brew", "install", "uv"),
        auto_windows=(*WINGET, "astral-sh.uv"),
    ),
    Tool(
        "node",
        "node",
        "https://nodejs.org/en/download",
        macos="brew install node",
        linux="sudo apt install nodejs  (dnf: sudo dnf install nodejs)",
        windows="winget install --id OpenJS.NodeJS.LTS -e",
        auto_macos=("brew", "install", "node"),
        auto_windows=(*WINGET, "OpenJS.NodeJS.LTS"),
    ),
    Tool(
        "claude",
        "claude",
        "https://code.claude.com/docs/en/overview",
        macos="npm install -g @anthropic-ai/claude-code",
        linux="npm install -g @anthropic-ai/claude-code",
        windows="npm install -g @anthropic-ai/claude-code",
        auto_macos=CLAUDE_NPM,
        auto_linux=CLAUDE_NPM,
        auto_windows=CLAUDE_NPM,
    ),
    Tool(
        "codex",
        "codex",
        "https://developers.openai.com/codex/cli",
        macos="npm install -g @openai/codex  (or: brew install --cask codex)",
        linux="npm install -g @openai/codex",
        windows="npm install -g @openai/codex",
    ),
    Tool(
        "gh",
        "gh",
        "https://cli.github.com",
        macos="brew install gh",
        linux="sudo apt install gh  (dnf: sudo dnf install gh)",
        windows="winget install --id GitHub.cli -e",
        auto_macos=("brew", "install", "gh"),
        auto_windows=(*WINGET, "GitHub.cli"),
    ),
    Tool(
        "jq",
        "jq",
        "https://jqlang.github.io/jq/download/",
        macos="brew install jq",
        linux="sudo apt install jq  (dnf: sudo dnf install jq)",
        windows="winget install --id jqlang.jq -e",
        auto_macos=("brew", "install", "jq"),
        auto_windows=(*WINGET, "jqlang.jq"),
    ),
    Tool(
        "obsidian",
        "obsidian",
        "https://help.obsidian.md",
        macos="Obsidian → Settings → General → Command line interface",
        linux="Obsidian → Settings → General → Command line interface",
        windows="Obsidian → Settings → General → Command line interface",
        note="required: without it terminal tabs get no titles or state glyphs, managers cannot see their tabs, and `agent obsidian open|daily|search` do nothing",
    ),
)
OBSIDIAN_APP = Tool(
    "obsidian app",
    "",
    "https://obsidian.md/download",
    macos="brew install --cask obsidian",
    linux="flatpak install flathub md.obsidian.Obsidian  (or the AppImage from obsidian.md/download)",
    windows="winget install --id Obsidian.Obsidian -e",
)
