"""The stale-tool-list warning fires on a real tool change only.

It hashed the raw bytes of the tool-definition files, so strict-typing edits inside function
bodies kept it on every MCP call for hours, and it never covered `specs_harness.py`."""

from __future__ import annotations

from pathlib import Path

from agent_cli.mcp.staleness import schema_hash

SPEC = '''
def harness(ctx, action: str, ref: str | None = None) -> dict:
    """Run a harness action."""
    value = {}
    return value


TOOLS = {"harness": ("harness", "Run a harness check or maintenance action")}
'''


def _tree(root: Path, text: str, name: str = "specs_harness.py") -> Path:
    mcp = root / "src" / "agent_cli" / "mcp"
    mcp.mkdir(parents=True, exist_ok=True)
    (mcp / "tools.py").write_text("TOOLS = {}\n", encoding="utf-8")
    (mcp / name).write_text(text, encoding="utf-8")
    return root


def test_an_edit_inside_a_function_body_or_a_comment_is_no_change(tmp_path: Path):
    before = schema_hash(_tree(tmp_path, SPEC))
    edited = SPEC.replace("value = {}", "value: dict[str, object] = {}  # strict typing").replace(
        "\ndef harness", "\n# a comment\ndef harness"
    )
    assert schema_hash(_tree(tmp_path, edited)) == before


def test_a_parameter_or_description_change_is_a_change(tmp_path: Path):
    before = schema_hash(_tree(tmp_path, SPEC))
    assert schema_hash(_tree(tmp_path, SPEC.replace("ref: str | None = None", "ref: str = ''"))) != before
    assert schema_hash(_tree(tmp_path, SPEC.replace("maintenance action", "maintenance step"))) != before


def test_the_harness_specs_are_covered(tmp_path: Path):
    before = schema_hash(_tree(tmp_path, SPEC))
    assert schema_hash(_tree(tmp_path, SPEC + "\nEXTRA = 'a new tool'\n")) != before
