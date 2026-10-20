"""An unchanged `--no-checks` review takes the no-diff exemption (C10).

Reviews ran with `--no-checks`, which skipped the no-diff check, so the observed, stronger
exemption fired zero times on 2026-10-06."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dispatch_helpers import (
    _new_fast as new_fast,  # pyright: ignore[reportPrivateUsage] -- shared test helper
)
from dispatch_helpers import (
    droot,  # noqa: F401  # pyright: ignore[reportUnusedImport] -- a fixture
    envelope,
    one,
)


def test_an_unchanged_review_is_exempt_as_no_diff(droot: Path, fake_claude: Any):
    fake_claude.scenario([one(envelope())])
    d: dict[str, Any] = new_fast(droot, no_checks=True)
    assert d["no_diff"]["attempt"] == d["attempt"]
    assert d["verification_result"]["status"] == "exempt"
    assert any("no-diff" in r or "unchanged" in r for r in d["verification_result"]["reasons"])


def test_a_review_that_changed_something_keeps_the_explicit_exemption(droot: Path, fake_claude: Any):
    fake_claude.scenario([one(envelope(), commit="a review note")])
    d: dict[str, Any] = new_fast(droot, no_checks=True)
    assert "no_diff" not in d
    assert d["verification_result"]["status"] == "exempt"
