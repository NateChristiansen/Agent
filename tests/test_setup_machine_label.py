"""Setup names the machine: the dispatch label `dispatch new` refuses without."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_cli import setup as setup_mod
from agent_cli.cli import infra as cli_infra
from agent_cli.dispatch import machine
from agent_cli.doctor import checks_state


@pytest.fixture(autouse=True)
def _fresh_install(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    """The fixture root has a label; a fresh install has none."""
    (tmp_root / "agent.local.yaml").unlink()
    monkeypatch.setattr("socket.gethostname", lambda: "Example-MacBook-Pro.local")


def _answers(monkeypatch: pytest.MonkeyPatch, *answers: str) -> list[str]:
    prompts: list[str] = []
    queue = list(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or queue.pop(0))
    return prompts


def test_default_label_comes_from_the_hostname():
    assert machine.default_label() == "examplem"


def test_default_label_survives_an_unusable_hostname(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("socket.gethostname", lambda: "123-456")
    assert machine.default_label() == "machine"


def test_first_run_takes_the_typed_answer(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    prompts = _answers(monkeypatch, " Studio ")
    assert cli_infra.ask_machine_label(tmp_root) == "studio"
    assert "[examplem]" in prompts[0]
    assert setup_mod.machine_label(tmp_root, "studio")["state"] == "done"
    assert machine.resolve(tmp_root).tag == "studio"
    assert (tmp_root / "agent.local.yaml").read_text(encoding="utf-8").endswith('machine: "studio"\n')


def test_enter_takes_the_default(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    _answers(monkeypatch, "")
    assert cli_infra.ask_machine_label(tmp_root) == "examplem"


def test_an_invalid_answer_asks_again_with_the_rule(
    tmp_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    prompts = _answers(monkeypatch, "my laptop!", "9lives", "laptop")
    assert cli_infra.ask_machine_label(tmp_root) == "laptop"
    assert len(prompts) == 3
    assert capsys.readouterr().out.count(machine.RULE) >= 3


def test_non_interactive_setup_uses_the_default(tmp_root: Path):
    step = setup_mod.machine_label(tmp_root, None)
    assert step["state"] == "done"
    assert machine.resolve(tmp_root).tag == "examplem"


def test_a_rerun_keeps_the_label_and_does_not_ask(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_root / "agent.local.yaml").write_text(
        "harness:\n  role: consumer\nmachine: kept\n", encoding="utf-8"
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked again"))
    assert cli_infra.ask_machine_label(tmp_root) is None
    assert setup_mod.machine_label(tmp_root, "other")["state"] == "already"
    assert machine.resolve(tmp_root).tag == "kept"


def test_the_label_is_appended_to_an_existing_file(tmp_root: Path):
    path = tmp_root / "agent.local.yaml"
    path.write_text("harness:\n  role: consumer", encoding="utf-8")
    setup_mod.machine_label(tmp_root, "box")
    assert path.read_text(encoding="utf-8") == 'harness:\n  role: consumer\nmachine: "box"\n'


def test_doctor_warns_on_a_missing_label_and_not_on_a_set_one(tmp_root: Path):
    [row] = checks_state.machine_label(tmp_root)
    assert row["level"] == "warn" and "agent setup" in row["detail"]
    setup_mod.machine_label(tmp_root, "box")
    assert checks_state.machine_label(tmp_root) == []


@pytest.mark.parametrize("word", ["yes", "no", "on", "off", "true", "false", "null", "y", "n"])
def test_yaml_reserved_words_round_trip_as_labels(tmp_root: Path, word: str):
    assert setup_mod.machine_label(tmp_root, word)["state"] == "done"
    assert machine.resolve(tmp_root).tag == word


def test_an_enrolled_device_name_is_not_overwritten(tmp_root: Path, monkeypatch: pytest.MonkeyPatch):
    from agent_cli.peer_net import store

    conn = store.connect(tmp_root)
    try:
        with store.transaction(conn):
            conn.execute("INSERT OR REPLACE INTO enrollment (k, v) VALUES ('device', 'studio')")
    finally:
        conn.close()
    assert machine.resolve(tmp_root).tag == "studio"
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked"))
    assert cli_infra.ask_machine_label(tmp_root) is None
    step = setup_mod.machine_label(tmp_root, None)
    assert step["state"] == "already" and "studio" in step["detail"]
    assert machine.resolve(tmp_root).tag == "studio"
    assert not (tmp_root / "agent.local.yaml").exists()
