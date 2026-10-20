"""Doctor's row for the example repo entry: a fresh clone showed "repo
example on disk" for a repo nobody had cloned."""

from __future__ import annotations

from pathlib import Path


def test_the_example_repo_row_says_agent_yaml_not_on_disk(tmp_root: Path, run):
    (tmp_root / "agent.yaml").write_text(
        "version: 1\nrepos:\n  - name: example-service\n    short: example\n"
        "    path: ../repos/example-service\n    remote: git@github.com:your-org/example-service.git\n"
        "    default_branch: main\n",
        encoding="utf-8",
    )
    levels = {c["check"]: c["level"] for c in run(["--json", "doctor"]).json}
    assert levels["repo example in agent.yaml"] == "warn"
    assert "repo example on disk" not in levels
