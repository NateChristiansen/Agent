"""The homepage plugin never closes tabs (#66). On the fresh Windows clone (2026-09-30) its ribbon
button closed every leaf, pinned manager terminals included, and killed three managers: setup
installed the plugin while Obsidian ran, the plugin's `loaded` flag stayed false, and the ribbon
fell back to openMode "Replace all open notes". The owner chose "Keep open notes"."""

from __future__ import annotations

import json
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1] / "harness/obsidian/config/plugins/homepage/data.json"


def test_no_homepage_mode_replaces_open_tabs() -> None:
    homepages = json.loads(TEMPLATE.read_text(encoding="utf-8"))["homepages"]
    for page in homepages.values():
        assert page["openMode"] == "Keep open notes"
        assert page["manualOpenMode"] == "Keep open notes"


def test_setup_stops_an_existing_homepage_from_replacing_tabs(tmp_root: Path, run) -> None:
    """#80: the seed only fills a missing file, so a vault set up before the seed changed kept
    "Replace all open notes". Setup turns that mode off in an existing file and leaves the rest."""
    path = tmp_root / ".obsidian" / "plugins" / "homepage" / "data.json"
    path.parent.mkdir(parents=True)
    page = {
        "value": "My note",
        "openMode": "Replace all open notes",
        "manualOpenMode": "Replace last note",
        "pin": True,
    }
    other = {"value": "Other", "openMode": "Replace last note", "manualOpenMode": "Replace all open notes"}
    path.write_text(
        json.dumps({"version": 4, "homepages": {"Main Homepage": page, "Second": other}}), encoding="utf-8"
    )
    run(["obsidian", "setup"])
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 4,
        "homepages": {
            "Main Homepage": {**page, "openMode": "Keep open notes"},
            "Second": {**other, "manualOpenMode": "Keep open notes"},
        },
    }
    before = path.read_text(encoding="utf-8")
    run(["obsidian", "setup"])
    assert path.read_text(encoding="utf-8") == before
