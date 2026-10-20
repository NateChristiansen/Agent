"""The example's owner name reads as unset everywhere.

The owner's first Windows run kept agent.example.yaml's `owner: {name: Your Name}`. Setup said
the name was unset while the session-start block printed "owner: Your Name".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli import repos


def _config(root: Path, owner_block: str) -> None:
    (root / "agent.yaml").write_text(f"{owner_block}\nrepos: []\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        ("owner:\n  name: Your Name", ""),
        ("owner:\n  name: '  Your Name  '", ""),
        ("owner:\n  name: Ada", "Ada"),
        ("owner: {}", ""),
        ("", ""),
    ],
)
def test_owner_name_treats_the_placeholder_as_unset(tmp_path: Path, block: str, expected: str) -> None:
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "agent").write_text("", encoding="utf-8")
    _config(tmp_path, block)
    assert repos.owner_name(tmp_path) == expected


def test_example_config_still_ships_the_placeholder() -> None:
    # If the example's name changes, PLACEHOLDER_OWNER has to change with it.
    example = Path(__file__).resolve().parents[1] / "agent.example.yaml"
    assert f"name: {repos.PLACEHOLDER_OWNER}" in example.read_text(encoding="utf-8")
