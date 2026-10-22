"""`protect-generated` (paths and Bash) and `no-chaining`, plus `render-after-plan-edit` and the
generic unknown-hook-name check: the split of `src/agent_cli/hooks/guards.py` (and
`src/agent_cli/hooks/session.py`'s `render_after_plan_edit`, `src/agent_cli/hooks/run.py`'s
dispatcher), mirroring the source split of the old hooks.py.
"""

import json
from pathlib import Path

from hooks_test_helpers import _decision, _reason, bash, edit

# ---------------------------------------------------------------- protect-generated: paths


# Still denied by the hook itself: a literal repos/ path that isn't a registered repo has no
# native equivalent. (Raw captures, unassigned or per-feature, now live under `.agent/raw/**`,
# which is covered by the native `Edit(...)` deny rules -- see NATIVE_DENY_EDITS below.)
DENY_EDITS = [
    ("Write", "{root}/repos/gamma/app.py", "manager"),
]

# Outside-the-harness-root writes still get an "ask" from the hook itself (no native rule
# can express "resolves outside this project").
ASK_EDITS = [
    ("Write", "/Users/someone/.zshrc", "outside the harness root"),
    ("Write", "/opt/elsewhere/notes.md", "outside the harness root"),
]

# 2026-09-08 (plan 2.1): these used to be denied by the hook directly. They're now covered by
# native `Edit(...)` deny rules in .claude/settings.json (NATIVE_GENERATED_DENY), which Claude
# Code consults for Write, MultiEdit and NotebookEdit too -- so the hook itself is silent on
# them now; test_native_rules_cover_generated_and_guardrail_paths checks the settings.json half.
NATIVE_DENY_EDITS = [
    ("Write", "STATUS.md"),
    ("Edit", "{root}/STATUS.md"),
    ("Write", "status.md"),  # APFS is case-insensitive
    ("Edit", "./STATUS.md"),
    ("Edit", "features/../STATUS.md"),
    ("Write", "Dashboard.md"),
    ("Write", "bases/features.base"),
    ("Edit", "features/003-x/log.md"),
    ("Edit", "{root}/Features/003-x/Log.md"),
    ("MultiEdit", "{root}/.agent/raw/003-x/2026-09-03.jsonl"),
    ("Write", ".agent/raw/unassigned/2026-09-03.jsonl"),
    ("Edit", ".agent/sessions/abc.json"),
    ("Write", "adrs/README.md"),
    ("Write", "journal/2026-09-03.collect.md"),
    ("Write", "features/003-x/handoffs/01-x.md"),
]


def test_agent_scratch_is_writable_and_the_rest_of_agent_is_not(tmp_root: Path, run):
    """`.agent/scratch/` holds agents' drafts: the native deny list names the
    CLI-owned subtrees of `.agent/` instead of `.agent/**`, and the hook's Bash-write regex
    carves scratch out the same way."""
    from agent_cli.hooks import NATIVE_GENERATED_DENY, PROTECTED_PATH_RE

    assert "Edit(/.agent/**)" not in NATIVE_GENERATED_DENY
    assert not any(p.startswith("Edit(/.agent/scratch") for p in NATIVE_GENERATED_DENY)
    # `peers` rather than `browser`: browser integration left with ADR-0013, and the peer
    # inboxes three managers coordinate through were the one CLI-owned subtree an agent
    # could hand-edit — `peers.send` appends because two senders race, so an edit drops
    # whichever message it had not read.
    for sub in ("dispatches", "sessions", "leases", "journal", "peers", "raw"):
        assert f"Edit(/.agent/{sub}/**)" in NATIVE_GENERATED_DENY
    assert not any(p.startswith("Edit(/.agent/browser") for p in NATIVE_GENERATED_DENY)
    assert PROTECTED_PATH_RE.search(".agent/dispatches/001-01.json")
    assert PROTECTED_PATH_RE.search(".agent/status.json")
    assert PROTECTED_PATH_RE.search(".agent")
    assert not PROTECTED_PATH_RE.search(".agent/scratch/journal/2026-09-14.md")
    assert not PROTECTED_PATH_RE.search(".agent/scratch")
    r = run(["hook", "protect-generated"], stdin=bash("cp x.json .agent/dispatches/001-01.json"))
    assert _decision(r) == "deny"


def test_a_new_agent_subtree_reaches_both_derived_places():
    """`core.AGENT_SUBTREES` is the one registry behind the Bash-write regex and the native
    `Edit(...)` deny rules (T4 item 3). Before that they were hand-maintained spellings that had
    to agree; this adds a name to the registry and asserts it shows up in each of them. (Doctor's
    settings.json check was the third place; it left in phase 6b.)
    """
    import re

    from agent_cli import core
    from agent_cli import hooks as hooks_mod

    with_widgets = (*core.AGENT_SUBTREE_DIRS, "widgets")

    # 1. the Bash-write regex (hooks.PROTECTED_PATH_RE's `.agent` branch)
    added = re.compile(core.agent_path_pattern(with_widgets) + "$")
    assert added.match(".agent/widgets/config.json")
    assert not re.compile(core.agent_path_pattern() + "$").match(".agent/widgets/config.json")
    # scratch stays writable however the registry grows, and the registered names still match
    assert not added.match(".agent/scratch/plans/draft.md")
    assert added.match(".agent/dispatches/001-01.json")

    # 2. the native Edit(...) deny rules
    assert "Edit(/.agent/widgets/**)" in core.agent_deny_patterns(with_widgets)
    assert "Edit(/.agent/widgets/**)" not in hooks_mod.NATIVE_GENERATED_DENY


# Same split for the guardrail/owner's-file "ask": GUARDRAIL_PREFIXES and ASK_PREFIXES are
# gone, because the settings.json `ask` list already carried every one of these paths.
NATIVE_ASK_EDITS = [
    ("Edit", "bin/agent"),
    ("Edit", ".claude/settings.json"),
    ("Edit", ".mcp.json"),
    ("Write", "{root}/.mcp.json"),
    ("Write", "src/agent_cli/hooks/guards.py"),
    ("Edit", "harness/owner/rules.md"),
    ("Edit", "harness/voice.md"),
]

ALLOW_EDITS = [
    ("Edit", "features/003-x/plan.md"),
    ("Write", "adrs/0002-foo.md"),
    ("Edit", "AGENTS.md"),
    ("Edit", "journal/2026-09-03.md"),
    ("Write", "src/agent_cli/features.py"),
    ("Write", "/tmp/elsewhere/STATUS.md"),
]


def test_protect_generated_edits(tmp_root: Path, run, notes_remote, monkeypatch):
    monkeypatch.delenv("AGENT_DISPATCH_ID", raising=False)
    run(["feature", "new", "edit-owner", "--title", "Edit owner"])
    run(["feature", "use", "1", "--session-id", "s1"])
    for tool, path, want in DENY_EDITS:
        r = run(["hook", "protect-generated"], stdin=edit(tool, path.format(root=tmp_root)))
        assert _decision(r) == "deny", (tool, path)
        assert want in _reason(r), (tool, path)
    for tool, path, want in ASK_EDITS:
        r = run(["hook", "protect-generated"], stdin=edit(tool, path.format(root=tmp_root)))
        assert _decision(r) == "ask", (tool, path)
        assert want in _reason(r), (tool, path)
    for tool, path in NATIVE_DENY_EDITS + NATIVE_ASK_EDITS + ALLOW_EDITS:
        r = run(["hook", "protect-generated"], stdin=_edit_s(tool, path.format(root=tmp_root), "s1"))
        assert r.out == "", (tool, path)


def _edit_s(tool: str, path: str, session_id: str) -> str:
    return json.dumps({"tool_name": tool, "tool_input": {"file_path": path}, "session_id": session_id})


def test_protect_generated_takes_a_lease_then_touches_on_repeat(tmp_root: Path, run, notes_remote):
    """Item 4: the first edit under a leaseable area registers this session's hold; a later
    edit from the same session just touches it (no takeover, no denial). Since then the
    area is the narrowest path, so the same file's area is touched and a sibling gets its own."""
    from agent_cli import leases

    run(["feature", "new", "edit-owner", "--title", "Edit owner"])
    run(["feature", "use", "1", "--session-id", "s1"])
    r = run(["hook", "protect-generated"], stdin=_edit_s("Edit", "src/agent_cli/features.py", "s1"))
    assert r.out == ""
    lease = leases.get(tmp_root, "vault:src/agent_cli/features.py")
    assert lease is not None and lease["session_id"] == "s1"
    taken_at = lease["taken_at"]
    r = run(["hook", "protect-generated"], stdin=_edit_s("Edit", "src/agent_cli/features.py", "s1"))
    assert r.out == ""
    again = leases.get(tmp_root, "vault:src/agent_cli/features.py")
    assert again is not None
    assert again["session_id"] == "s1" and again["taken_at"] == taken_at


def test_protect_generated_lets_two_sessions_edit_sibling_packages(tmp_root: Path, run, notes_remote):
    """Two managers editing different parts of src used to queue for one vault:src lease. The
    second edit here is a different package, so it is allowed and takes its own area."""
    from agent_cli import leases

    run(["feature", "new", "edit-owner", "--title", "Edit owner"])
    for session in ("s1", "s2"):
        run(["feature", "use", "1", "--session-id", session])
    assert run(["hook", "protect-generated"], stdin=_edit_s("Edit", "src/agent_cli/cli/x.py", "s1")).out == ""
    second = run(["hook", "protect-generated"], stdin=_edit_s("Edit", "src/agent_cli/hooks/y.py", "s2"))
    assert second.out == ""
    held = {lease["area"]: lease["session_id"] for lease in leases.list_all(tmp_root)}
    assert held == {"vault:src/agent_cli/cli": "s1", "vault:src/agent_cli/hooks": "s2"}


def test_protect_generated_denies_a_live_holder_with_the_wait_line(tmp_root: Path, run, notes_remote):
    import os

    from agent_cli import leases

    run(["feature", "new", "edit-owner", "--title", "Edit owner"])
    run(["feature", "use", "1", "--session-id", "s1"])
    # the coarse area still covers everything inside it, so a holder of vault:src blocks a
    # narrow edit under it and the refusal names the area actually held
    leases.take(tmp_root, "vault:src", session_id="other", pid=os.getpid())
    r = run(["hook", "protect-generated"], stdin=_edit_s("Edit", "src/agent_cli/features.py", "s1"))
    assert _decision(r) == "deny"
    assert "vault:src is held by other" in _reason(r)
    # the guard shows exactly what `take` would have said, including both ways out: ask the
    # holder, or wait. Naming only the wait is what sent three conflicts to the owner.
    assert "action `wait`, area `vault:src`" in _reason(r)
    assert "without `ask`, timeout 120" in _reason(r)
    # unheld by this session: the file still is not touched, so the lease is untouched too
    lease = leases.get(tmp_root, "vault:src")
    assert lease is not None
    assert lease["session_id"] == "other"


def test_protect_generated_ignores_paths_outside_leaseable_areas(tmp_root: Path, run):
    """A live `vault:src` holder never blocks an edit under an area leases does not cover
    (features/, adrs/, journal/, ...)."""
    import os

    from agent_cli import leases

    leases.take(tmp_root, "vault:src", session_id="other", pid=os.getpid())
    r = run(["hook", "protect-generated"], stdin=_edit_s("Edit", "features/003-x/plan.md", "s1"))
    assert r.out == ""


def test_native_rules_cover_generated_and_guardrail_paths(tmp_root: Path, run):
    """protect-generated no longer denies an Edit to STATUS.md (or the rest of
    NATIVE_DENY_EDITS/NATIVE_ASK_EDITS above): this asserts the native rule exists instead,
    in the actual project settings.json this task edited, not the test fixture's."""
    from agent_cli.hooks import NATIVE_GENERATED_DENY

    real_settings = json.loads(
        (Path(__file__).resolve().parents[1] / ".claude" / "settings.json").read_text()
    )
    perms = real_settings.get("permissions") or {}
    deny = perms.get("deny") or []
    ask = perms.get("ask") or []
    for pattern in NATIVE_GENERATED_DENY:
        assert pattern in deny, pattern
    for pattern in (
        "Edit(bin/agent)",
        "Edit(.claude/settings.json)",
        "Edit(.mcp.json)",
        "Edit(src/agent_cli/hooks/**)",
        "Edit(harness/owner/rules.md)",
        "Edit(harness/voice.md)",
    ):
        assert pattern in ask, pattern


def test_a_registered_repo_write_is_denied_in_every_absolute_spelling(tmp_root: Path, run):
    """On Windows `str()` gives `C:\\...` and `as_posix()` gives `C:/...`; both name the repo a
    POSIX `/...` does, and the guard used to treat only a leading `/` as absolute."""
    sibling = tmp_root.parent / "sibling" / "Gamma"
    sibling.mkdir(parents=True)
    reg = tmp_root / "agent.yaml"
    reg.write_text(
        reg.read_text()
        + "  - name: Gamma\n    short: gamma\n    path: ../sibling/Gamma\n    remote: git@github.com:example/Gamma.git\n"
    )
    for cmd in (
        f"cp a.py '{sibling}/x.py'",
        f"cp a.py {sibling.as_posix()}/x.py",
        f"sed -i s/a/b/ '{sibling}/x.py'",
        f"sed -i s/a/b/ {sibling.as_posix()}/x.py",
    ):
        r = run(["hook", "protect-generated"], stdin=bash(cmd))
        assert _decision(r) == "deny", cmd


def test_protect_generated_denies_registered_repo_outside_root(tmp_root: Path, run):
    sibling = tmp_root.parent / "sibling" / "Gamma"
    sibling.mkdir(parents=True)
    reg = tmp_root / "agent.yaml"
    reg.write_text(
        reg.read_text()
        + "  - name: Gamma\n    short: gamma\n    path: ../sibling/Gamma\n    remote: git@github.com:example/Gamma.git\n"
    )
    r = run(["hook", "protect-generated"], stdin=edit("Edit", str(sibling / "config" / "local.yaml")))
    assert _decision(r) == "deny"
    assert "manager" in _reason(r)
    r = run(["hook", "protect-generated"], stdin=edit("Edit", "../sibling/Gamma/x.py"))
    assert _decision(r) == "deny"
    r = run(["hook", "protect-generated"], stdin=bash(f"echo x > {sibling}/x.py"))
    assert _decision(r) == "deny"
    r = run(["hook", "protect-generated"], stdin=bash(f"cp a.py {sibling}/x.py"))
    assert _decision(r) == "deny"
    r = run(["hook", "protect-generated"], stdin=bash(f"git -C {sibling} status"))
    assert r.out == ""
    # Push-guard's own override spelling is a push, not a product-code write, and an env
    # prefix no longer hides a real write from the guard
    for cmd in (
        f"git -C {sibling} push origin feature/x",
        f"AGENT_PUSH_UNREVIEWED=1 git -C {sibling} push origin feature/x",
    ):
        r = run(["hook", "protect-generated"], stdin=bash(cmd))
        assert r.out == "", cmd
    r = run(["hook", "protect-generated"], stdin=bash(f"FOO=1 cp a.py {sibling}/x.py"))
    assert _decision(r) == "deny"
    r = run(["hook", "protect-generated"], stdin=bash("FOO=1 rm -rf features/003-x"))
    assert _decision(r) == "deny"
    r = run(["hook", "protect-generated"], stdin=bash(f"grep -rn foo {sibling}"))
    assert r.out == ""
    # building and testing are not writing code
    for cmd in (
        f"uv build {sibling}",
        f"time npm test --prefix {sibling}",
        f"pnpm test --dir {sibling}",
    ):
        r = run(["hook", "protect-generated"], stdin=bash(cmd))
        assert r.out == "", cmd
    r = run(["hook", "protect-generated"], stdin=bash(f"uv init {sibling}/new"))
    assert _decision(r) == "deny"
    # installing and running are operating, not writing code; the --dir/--filter forms count
    # the same
    for cmd in (
        f"pnpm --dir {sibling} lint",
        f"pnpm --dir {sibling} install --prefer-offline",
        f"pnpm --filter=web --dir {sibling} start",
        f"pnpm -C {sibling} test",
    ):
        r = run(["hook", "protect-generated"], stdin=bash(cmd))
        assert r.out == "", cmd
    # but a package manager reaching for a write verb is still a write
    for cmd in (f"pnpm --dir {sibling} exec rm -rf dist", f"pnpm --dir {sibling} patch left-pad"):
        r = run(["hook", "protect-generated"], stdin=bash(cmd))
        assert _decision(r) == "deny", cmd
    # a stream editor without an in-place flag reads (2026-09-10: `sed -n` on a skill file
    # in a repo was denied as writing product code); with one it still writes
    for cmd in (
        f"sed -n 1,60p {sibling}/SKILL.md",
        f"awk '/x/' {sibling}/a.ts",
        f"perl -ne 'print' {sibling}/a.ts",
    ):
        r = run(["hook", "protect-generated"], stdin=bash(cmd))
        assert r.out == "", cmd
    for cmd in (f"sed -i '' s/a/b/ {sibling}/a.ts", f"perl -pi -e s/a/b/ {sibling}/a.ts"):
        r = run(["hook", "protect-generated"], stdin=bash(cmd))
        assert _decision(r) == "deny", cmd


# ---------------------------------------------------------------- protect-generated: bash


DENY_BASH = [
    "rm -rf features/003-x",
    "rm -r adrs",
    "rm -fr features",
    "/bin/rm -Rf features",
    "rm -f features/003-x/log.md",
    'rm -rf "$PWD/features"',
    "cd x | rm -Rf .agent/",
    "find features -name '*.md' -delete",
    "find . -exec rm {} \\;",
    "echo hi >> STATUS.md",
    "echo hi > Status.md",
    "echo x >| STATUS.md",
    "echo hi > features/003-x/log.md",
    "echo x > Dashboard.md",
    "echo x > Index.md",  # generated too; the 022 research found it missing from this guard
    "echo x > bases/features.base",
    "printf 'x' | tee -a features/003-x/log.md",
    "printf 'x' | tee --append features/003-x/log.md",
    "sed -i '' s/a/b/ features/003-x/log.md",
    "sed --in-place s/a/b/ features/003-x/log.md",
    "perl -pi -e s/a/b/ STATUS.md",
    "cp /tmp/x STATUS.md",
    "mv a features/003-x/log.md",
    "truncate -s 0 STATUS.md",
    "dd if=/dev/zero of=STATUS.md",
    "echo '{}' >> .agent/raw/unassigned/2026-09-03.jsonl",
    "cat x > .agent/sessions/abc.json",
    "echo x > adrs/README.md",
    "echo x > 'features/003-x/log.md'",
]

ALLOW_BASH = [
    "rm .agent/raw/003-x/old.jsonl",
    "rm -rf .venv",
    "rm -rf /tmp/scratch",
    "grep -n 'STATUS.md >' AGENTS.md",
    'bin/agent log current --kind note "the hook denies echo x > STATUS.md"',
    "bin/agent status render",
    "cat STATUS.md",
    "git diff features/003-x/plan.md",
    "echo hi > /tmp/scratch.txt",
    "sed -n '1,5p' features/003-x/log.md",
    "sed -i '' s/a/b/ AGENTS.md",
    "tee /tmp/out.txt",
    "cp a b",
    "cp /tmp/design.md features/001-x/design.md",
    "mv notes.md adrs/0009-draft.md",
    "echo x > features/001-x/plan.md",
    "obsidian eval code=\"(() => document.querySelector('.agent/x')) => 1\"",
    "find features -name '*.md'",
    "ls -la .agent/",
]


def test_protect_generated_bash(tmp_root: Path, run):
    for cmd in DENY_BASH:
        r = run(["hook", "protect-generated"], stdin=bash(cmd))
        assert _decision(r) == "deny", cmd
    for cmd in ALLOW_BASH:
        r = run(["hook", "protect-generated"], stdin=bash(cmd))
        assert r.out == "", cmd
    r = run(["hook", "protect-generated"], stdin="garbage")
    assert r.code == 0 and r.out == ""


# ---------------------------------------------------------------- no-chaining


def test_no_chaining_hook(tmp_root: Path, run):
    r = run(["hook", "no-chaining"], stdin=bash("cd repo && git status"))
    assert _decision(r) == "deny"
    assert "AGENTS.md standing rules" in _reason(r)
    r = run(["hook", "no-chaining"], stdin=bash("git status"))
    assert r.out == ""
    r = run(["hook", "no-chaining"], stdin=json.dumps({"tool_name": "Bash", "tool_input": {}}))
    assert r.out == ""
    r = run(["hook", "no-chaining"], stdin="garbage")
    assert r.code == 0 and r.out == ""


def test_no_chaining_hint_names_the_first_segment(tmp_root: Path, run):
    """A denial should cost the model one retry, not two: it should be able to run the
    first segment alone in its next Bash call rather than re-guess a split."""
    from agent_cli.hooks import NO_CHAIN_REASON
    from agent_cli.hooks.paths import PROTECTED_NOTE

    r = run(["hook", "no-chaining"], stdin=bash("cd repo && git status"))
    assert (
        _reason(r)
        == f"{NO_CHAIN_REASON} Run `cd repo` first, then the next command in a new Bash call. {PROTECTED_NOTE}"
    )
    r = run(["hook", "no-chaining"], stdin=bash("ls; pwd"))
    assert "Run `ls` first" in _reason(r)
    # no clean split point: substitution and shell-wrapper denials get the static reason alone
    for cmd in ("echo $(git push)", 'bash -c "git add . && git commit -m x; git push"'):
        r = run(["hook", "no-chaining"], stdin=bash(cmd))
        assert _reason(r) == f"{NO_CHAIN_REASON} {PROTECTED_NOTE}", cmd


# ---------------------------------------------------------------- render-after-plan-edit


def test_render_after_plan_edit(tmp_root: Path, notes_remote: Path, run):
    run(["feature", "new", "x", "--title", "X"])
    status_path = tmp_root / "Dashboard.md"  # Audit 10 plan.md:113 (K1)
    for path in ("features/001-x/plan.md", f"{tmp_root}/features/001-x/plan.md"):
        status_path.unlink(missing_ok=True)
        r = run(["hook", "render-after-plan-edit"], stdin=edit("Edit", path))
        assert r.code == 0 and r.out == ""
        assert status_path.exists(), path
    for path in ("features/001-x/log.md", "adrs/0001-a.md", "AGENTS.md", f"{tmp_root}/plan.md"):
        status_path.unlink(missing_ok=True)
        run(["hook", "render-after-plan-edit"], stdin=edit("Write", path))
        assert not status_path.exists(), path
    r = run(["hook", "render-after-plan-edit"], stdin="garbage")
    assert r.code == 0


def test_unknown_hook_name_is_usage_error(tmp_root: Path, run):
    assert run(["hook", "bogus"], stdin="{}").code == 2
