"""Dispatch ids carry a machine tag (ADR-0036): the parsers, per-tag allocation and the derived
branch and worktree names.

New ids read `NNN-<tag>-SS`; legacy `NNN-SS` ids stay valid with no migration. The tag is
`[a-z][a-z0-9]{0,7}`, so an id splits on its hyphens without ambiguity.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_cli import dispatch, features, repos
from agent_cli.core import AgentError
from agent_cli.dispatch import ids
from conftest import git
from dispatch_helpers import _new_fast, droot, envelope, one, settle  # noqa: F401 — droot is a pytest fixture

pytestmark = pytest.mark.usefixtures("notes_remote")

# (text, valid id, feature, tag, seq)
ID_TABLE = [
    ("001-01", True, 1, None, 1),
    ("002-mac-01", True, 2, "mac", 1),
    ("001-100", True, 1, None, 100),
    ("001-mac-100", True, 1, "mac", 100),
    ("001-abcdefgh-02", True, 1, "abcdefgh", 2),
    ("001-w1-03", True, 1, "w1", 3),
    ("001-MAC-01", False, None, None, None),
    ("001-Mac-01", False, None, None, None),
    ("001-1ac-01", False, None, None, None),
    ("001-abcdefghi-01", False, None, None, None),
    ("001-ma-c-01", False, None, None, None),
    ("001-mac-1", False, None, None, None),
    ("01-01", False, None, None, None),
    ("001-mac", False, None, None, None),
]


@pytest.mark.parametrize(("text", "valid", "feature", "tag", "seq"), ID_TABLE)
def test_id_parser_table(text: str, valid: bool, feature, tag, seq):
    m = dispatch.ID_RE.match(text)
    assert bool(m) is valid, text
    parsed = ids.parse_id(text)
    if not valid:
        assert parsed is None
        return
    assert m is not None
    assert (int(m.group("feature")), m.group("tag"), int(m.group("seq"))) == (feature, tag, seq)
    assert parsed == (feature, tag, seq)
    assert ids.format_id(feature, seq, tag) == text


@pytest.mark.parametrize(
    ("name", "tag", "seq", "short"),
    [
        ("01-alpha.md", None, 1, "alpha"),
        ("100-alpha.md", None, 100, "alpha"),
        ("mac-01-alpha.md", "mac", 1, "alpha"),
        ("mac-100-beta.web.md", "mac", 100, "beta.web"),
        ("abcdefgh-07-alpha.md", "abcdefgh", 7, "alpha"),
        ("MAC-01-alpha.md", None, None, None),
        ("1ac-01-alpha.md", None, None, None),
        ("abcdefghi-01-alpha.md", None, None, None),
        ("mac-alpha.md", None, None, None),
    ],
)
def test_handoff_file_parser_table(name: str, tag, seq, short):
    m = dispatch.HANDOFF_FILE_RE.match(name)
    if seq is None:
        assert m is None, name
        return
    assert m is not None, name
    assert (m.group("tag"), int(m.group("seq")), m.group("short")) == (tag, seq, short)


def _feature(root: Path, run) -> features.Feature:
    run(["feature", "new", "alpha-work", "--title", "Alpha work", "--ticket", "T-42", "--repo", "alpha"])
    return features.resolve(root, "1")


def test_seqs_count_per_tag(tmp_root: Path, run):
    """Two tags on one feature both start at 01; a legacy file counts only for legacy ids."""
    feat = _feature(tmp_root, run)
    hdir = dispatch.handoffs_dir(feat)
    hdir.mkdir(parents=True)
    for name in ("01-alpha.md", "02-alpha.md", "mac-01-alpha.md", "mac-02-beta.md"):
        (hdir / name).write_text("---\nid: x\n---\n", encoding="utf-8")
    assert dispatch.allocate_seq(tmp_root, feat, "mac") == 3
    assert dispatch.allocate_seq(tmp_root, feat, "win") == 1
    assert dispatch.allocate_seq(tmp_root, feat, None) == 3
    # a local record counts too, so a lost handoff still keeps ids unique
    records = dispatch.dispatches_dir(tmp_root)
    records.mkdir(parents=True)
    (records / "001-win-04.json").write_text("{}", encoding="utf-8")
    assert dispatch.allocate_seq(tmp_root, feat, "win") == 5
    assert dispatch.allocate_seq(tmp_root, feat, "mac") == 3


def test_two_tags_derive_distinct_ids_paths_and_branches(tmp_root: Path, run):
    feat = _feature(tmp_root, run)
    repo = repos.load_registry(tmp_root).require("alpha")
    mac = (
        dispatch.make_id(feat, 1, "mac"),
        dispatch.handoff_path(feat, repo, 1, "mac").name,
        dispatch.worktree_path(tmp_root, repo, feat, 1, "mac").name,
        dispatch.branch_name(feat, 1, repo, tag="mac"),
    )
    win = (
        dispatch.make_id(feat, 1, "win"),
        dispatch.handoff_path(feat, repo, 1, "win").name,
        dispatch.worktree_path(tmp_root, repo, feat, 1, "win").name,
        dispatch.branch_name(feat, 1, repo, tag="win"),
    )
    assert mac == ("001-mac-01", "mac-01-alpha.md", "001-alpha-work-mac-01", "feature/alpha-work-mac-01")
    assert win == ("001-win-01", "win-01-alpha.md", "001-alpha-work-win-01", "feature/alpha-work-win-01")
    # the legacy forms are unchanged: the first dispatch had no suffix
    assert dispatch.make_id(feat, 1, None) == "001-01"
    assert dispatch.branch_name(feat, 1, repo, tag=None) == "feature/alpha-work"


def _template(root: Path, template: str) -> None:
    reg = root / "agent.yaml"
    data = yaml.safe_load(reg.read_text(encoding="utf-8"))
    alpha = next(r for r in data["repos"] if r["short"] == "alpha")
    alpha.setdefault("dispatch", {})["branch_template"] = template
    reg.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


@pytest.mark.parametrize(
    "template",
    [
        "work/{number}-{slug}",
        "feature/{slug}{{suffix}}",  # escaped: renders a literal {suffix} for every machine
        "feature/{slug}{suffix:.0}",  # a precision of 0 drops it
        "feature/{slug}{suffix!r}",  # a conversion changes it
        "feature/{slug}{suffix:.3}",  # truncation keeps `-ma` of `-mac-01` on every seq
    ],
)
def test_a_template_without_a_plain_suffix_fails_dispatch_new(droot: Path, template: str):
    _template(droot, template)
    with pytest.raises(AgentError, match=r"\{suffix\}"):
        _new_fast(droot)
    records = dispatch.dispatches_dir(droot)
    assert not records.exists() or not list(records.glob("*.json"))


def test_a_new_tagged_dispatch_never_reuses_a_legacy_worktree(droot: Path, fake_claude):
    """Rollout: a finished, clean worktree from a legacy `001-01` dispatch is not this machine's
    tagged path, so a tagged dispatch makes its own. The legacy one is left as it was."""
    main = droot / "repos/alpha-api"
    legacy_wt = droot / "repos/_worktrees/alpha/001-alpha-work"
    git(["worktree", "add", "-q", "-b", "feature/alpha-work", str(legacy_wt), "main"], main)
    stamp = "2026-09-01T09:00:00-07:00"
    legacy = {
        "id": "001-01",
        "feature": 1,
        "repo": "alpha-api",
        "short": "alpha",
        "worktree": str(legacy_wt),
        "branch": "feature/alpha-work",
        "handoff": "features/001-alpha-work/handoffs/01-alpha.md",
        "session_id": "legacy-session",
        "state": "done",
        "attempt": 1,
        "budget_usd": 1.0,
        "created": stamp,
        "finished": stamp,
        "actioned_at": stamp,
        "worktree_removed": False,
        "record_revision": 0,
    }
    dispatch.state.save(droot, legacy)
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    assert d["reused"] is False
    assert Path(d["worktree"]).name == "001-alpha-work-mac-01"
    assert git(["rev-parse", "--abbrev-ref", "HEAD"], legacy_wt).strip() == "feature/alpha-work"
    assert dispatch.load(droot, "001-01")["worktree"] == str(legacy_wt)


def test_first_dispatch_branch_and_worktree_carry_the_tag(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope())])
    d = _new_fast(droot)
    assert d["id"] == "001-mac-01"
    assert d["branch"] == "feature/alpha-work-mac-01"
    assert Path(d["worktree"]).name == "001-alpha-work-mac-01"
    assert d["handoff"] == "features/001-alpha-work/handoffs/mac-01-alpha.md"
    assert git(["rev-parse", "--abbrev-ref", "HEAD"], Path(d["worktree"])).strip() == d["branch"]


def test_explicit_branch_and_escalation_keep_their_branch(droot: Path, fake_claude):
    fake_claude.scenario([one(envelope()), one(envelope()), one(envelope())])
    first = _new_fast(droot, branch="shared/review-target")
    assert first["branch"] == "shared/review-target"
    settle(droot, first)
    escalated = _new_fast(droot, escalate_from=first["id"])
    assert escalated["id"] == "001-mac-02"
    assert escalated["branch"] == "shared/review-target"
