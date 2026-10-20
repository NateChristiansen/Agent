"""The permission linter. Written after four dispatch attempts died on rules that did not
match the commands they were written for. The dead-prefix case is the one a machine can catch."""

import pytest

from agent_cli import permissions as perms
from agent_cli.quote_mask import mask_quotes


@pytest.mark.parametrize(
    "text, expected",
    [
        ("", ""),
        ("plain", "plain"),
        ("a'b", "a'x"),
        ('a"b; c"d', 'a"xxxx"d'),
        ("'a\"b'", "'xxx'"),
        ("'a\\'b", "'xx'b"),
    ],
)
def test_quote_mask_preserves_delimiters_length_and_literal_backslashes(text: str, expected: str):
    assert mask_quotes(text) == expected
    assert len(mask_quotes(text)) == len(text)


def test_mid_token_prefix_is_dead():
    """`Bash(gh api repos/:*)` never matched `gh api repos/<org>/...`."""
    problems = perms.lint(["Bash(gh api repos/:*)"], label="worker allow")
    assert len(problems) == 1
    assert "stops mid-token" in problems[0]
    assert "Bash(gh api repos:*)" in problems[0]


def test_whole_token_prefixes_are_fine():
    ok = [
        "Bash(gh api:*)",
        "Bash(git status:*)",
        "Bash(git -C /wt/alpha/001-x restore --staged:*)",
        "Bash(git branch)",
        "Bash(git * push*)",
        "Bash(rm -*r*)",
        "Edit",
        "WebFetch",
        "mcp__agent__*",
        "mcp__tracker__get_ticket",
    ]
    assert perms.lint(ok, label="worker allow") == []


def test_wildcard_mixed_with_trailing_prefix_is_dead():
    """Claude Code reads `Bash(git * push:*)` as one literal prefix and warns that it never
    matches. Lint says so, and the matcher agrees with Claude."""
    problems = perms.lint(["Bash(git * push:*)"], label="worker deny")
    assert len(problems) == 1
    assert "literal prefix" in problems[0] and "Bash(git * push*)" in problems[0]
    assert perms.matches("git -C /wt push origin main", [], ["Bash(git * push:*)"]) == ("no-rule", None)
    assert perms.matches("git -C /wt push origin main", [], ["Bash(git * push*)"]) == (
        "denied",
        "Bash(git * push*)",
    )


def test_real_settings_have_no_mixed_wildcard_prefix_rules():
    import json
    from pathlib import Path

    settings = Path(__file__).resolve().parents[1] / ".claude/settings.json"
    block = json.loads(settings.read_text())["permissions"]
    rules = [r for key in ("allow", "ask", "deny") for r in block.get(key) or []]
    assert [r for r in rules if r.endswith(":*)") and "*" in r[:-3]] == []


@pytest.mark.parametrize(
    "command",
    [
        "gh pr merge 3 --squash",
        "gh pr close 3",
        "glab mr create --fill --squash-before-merge --remove-source-branch --yes",
        "glab mr merge 1 --yes",
        "glab mr update 1 --ready",
        "glab mr note 1 -m reply",
        "glab mr approve 1",
        "glab mr unapprove 1",
        "glab mr close 1",
        "glab issue create --title x",
        "glab api projects/1 -X POST",
        "glab api projects/1 --method POST",
        "glab api projects/1 -f key=value",
        "glab api projects/1 -F key=@file",
        "glab api projects/1 --field key=value",
        "glab api projects/1 --raw-field key=value",
        "glab api projects/1 --input body.json",
        "glab api -X POST projects/1/merge_requests/1/notes",
        "glab api --method POST projects/1/merge_requests/1/notes",
    ],
)
def test_manager_forge_writes_ask_instead_of_deny(command: str):
    """A manager runs a repo's own merge flow after the owner's yes, so every gh and glab write
    prompts rather than being refused."""
    import json
    from pathlib import Path

    settings = Path(__file__).resolve().parents[1] / ".claude/settings.json"
    block = json.loads(settings.read_text())["permissions"]
    assert perms.matches(command, [], block.get("deny") or [])[0] == "no-rule"
    assert perms.matches(command, block.get("ask") or [], [])[0] == "allowed"


def test_wildcard_prefix_is_not_flagged_for_its_last_character():
    """A rule with a wildcard is doing something deliberate. Do not second-guess its shape."""
    assert perms.lint(["Bash(gh api * --field *)"], label="deny") == []


def test_stops_mid_token_exempts_a_bare_double_dash():
    """`Bash(git checkout --:*)` is a deliberate rule for git's own "end of options" marker,
    not a rule that stops mid-argument the way `Bash(gh api repos/:*)` did (1.7). A prefix
    ending in a single trailing dash, as in `-x-`, is still flagged."""
    assert perms._stops_mid_token("git checkout --") is False
    assert perms._stops_mid_token("git -C /wt/alpha/001-x checkout --") is False
    assert perms._stops_mid_token("git foo -x-") is True
    assert perms.lint(["Bash(git checkout --:*)"], label="worker allow") == []
    assert perms.lint(["Bash(git foo -x-:*)"], label="worker allow") != []


def test_catch_all_and_duplicates():
    problems = perms.lint(["Bash(:*)", "Edit", "Edit"], label="allow")
    assert any("matches every Bash command" in p for p in problems)
    assert any("listed 2 times" in p for p in problems)


def test_unparseable_rule():
    problems = perms.lint(["Bash(git status", "(nope)"], label="allow")
    assert len(problems) == 2
    assert all("is not a permission rule" in p for p in problems)


def test_allow_cancelled_by_deny():
    problems = perms.lint_all(allow=["Bash(git push:*)"], deny=["Bash(git push:*)"], label="worker")
    assert any("cancelled by the same rule in deny" in p for p in problems)


def test_the_real_worker_lists_are_clean():
    from agent_cli import dispatch as dispatch_mod

    assert (
        perms.lint_all(allow=list(dispatch_mod.BASE_ALLOW), deny=list(dispatch_mod.BASE_DENY), label="worker")
        == []
    )


def test_prefix_rule_stops_on_a_token_boundary_not_a_flag_or_slash():
    """`git -C <worktree> status` is a different command from `git status` to a prefix rule:
    the `-C <worktree>` in the middle means the exact-path form needs its own rule (003-03
    attempt 1)."""
    assert perms.matches("git -C /wt/alpha/001-x status", ["Bash(git status:*)"], []) == ("no-rule", None)
    rule = "Bash(git -C /wt/alpha/001-x status:*)"
    assert perms.matches("git -C /wt/alpha/001-x status", [rule], []) == ("allowed", rule)


def test_mid_token_prefix_is_dead_at_match_time_too():
    """`Bash(gh api repos/:*)` reads as a tighter rule than `Bash(gh api:*)` but matches
    nothing: `/` is not a token boundary, so the real command never reaches it (003-03
    attempt 3)."""
    cmd = "gh api repos/example-org/api-service/pulls/1/comments"
    assert perms.matches(cmd, ["Bash(gh api repos/:*)"], []) == ("no-rule", None)
    assert perms.matches(cmd, ["Bash(gh api:*)"], []) == ("allowed", "Bash(gh api:*)")


def test_prefix_rule_requires_the_whole_first_argument():
    """`Bash(npm run:*)` expects `run` to be npm's first argument; a `--prefix`
    flag in front of it means the rule never fires."""
    cmd = "npm --prefix web run lint"
    assert perms.matches(cmd, ["Bash(npm run:*)"], []) == ("no-rule", None)
    assert perms.matches(cmd, ["Bash(npm:*)"], []) == ("allowed", "Bash(npm:*)")


def test_deny_wins_over_the_same_rule_in_allow():
    rule = "Bash(git push:*)"
    assert perms.matches("git push origin main", [rule], [rule]) == ("denied", rule)


def test_wildcard_prefix_denies_the_dangerous_spelling():
    rule = "Bash(rm -r*)"
    assert perms.matches("rm -rf /tmp/x", [], [rule]) == ("denied", rule)


def test_bare_wildcard_matches_across_spaces():
    rule = "Bash(git * --force*)"
    assert perms.matches("git push origin main --force", [], [rule]) == ("denied", rule)


@pytest.mark.parametrize("rule", ["Bash(rg:*)", "Bash(rg *)"])
def test_granted_prefix_matches_literal_newlines_in_an_argument(rule: str):
    command = "rg -n 'first\nsecond' src"
    assert perms.matches(command, [rule], []) == ("allowed", rule)
    assert perms.matches(command, [rule], [rule]) == ("denied", rule)


def test_compound_command_denied_when_any_part_is_denied():
    """`git status && rm -rf x` must not read as allowed just because the first half is.
    The harness never gets a second look once the worker is headless."""
    allow, deny = ["Bash(git status:*)"], ["Bash(rm -r*)"]
    assert perms.matches("git status && rm -rf x", allow, deny) == ("denied", "Bash(rm -r*)")


def test_compound_command_needs_every_part_allowed():
    allow = ["Bash(git status:*)", "Bash(git diff:*)"]
    assert perms.matches("git status && git diff", allow, []) == ("allowed", "Bash(git status:*)")
    assert perms.matches("git status && rm -rf x", allow, []) == ("no-rule", None)


def test_exact_rule_does_not_match_a_longer_command_name():
    assert perms.matches("lsof", ["Bash(ls)"], []) == ("no-rule", None)
    assert perms.matches("ls", ["Bash(ls)"], []) == ("allowed", "Bash(ls)")


def test_non_bash_rules_are_ignored_by_matches():
    """`Edit(...)` and `mcp__x__y` rules never apply to a shell command string. They are not
    even parsed as Bash prefixes."""
    assert perms.matches("git status", ["Edit", "mcp__agent__*"], []) == ("no-rule", None)


def test_every_base_deny_rule_matches_a_real_command():
    """Each `BASE_DENY` rule earns its place by actually matching something. A rule that
    cannot fire is exactly the dead-prefix failure `lint()` catches for allow lists, just on
    the deny side, where it would silently let a dangerous command through instead."""
    from agent_cli import dispatch as dispatch_mod

    samples: dict[str, str] = {
        "Bash(git push)": "git push",
        "Bash(git push:*)": "git push origin main",
        "Bash(gh pr create:*)": "gh pr create --title x",
        "Bash(gh pr merge:*)": "gh pr merge 123",
        "Bash(gh repo:*)": "gh repo clone foo/bar",
        "Bash(gh pr comment:*)": "gh pr comment 123 --body hi",
        "Bash(gh pr review:*)": "gh pr review 123 --approve",
        "Bash(gh pr edit:*)": "gh pr edit 123 --title y",
        "Bash(gh pr close:*)": "gh pr close 123",
        "Bash(gh pr reopen:*)": "gh pr reopen 123",
        "Bash(gh pr ready:*)": "gh pr ready 123",
        "Bash(gh issue:*)": "gh issue create --title z",
        "Bash(gh release:*)": "gh release create v1.0",
        "Bash(gh workflow:*)": "gh workflow run ci.yml",
        "Bash(gh run:*)": "gh run list",
        "Bash(gh secret:*)": "gh secret set FOO",
        "Bash(gh auth token:*)": "gh auth token --hostname github.com",
        "Bash(gh api -X:*)": "gh api -X POST repos/x",
        "Bash(gh api --method:*)": "gh api --method POST repos/x",
        "Bash(gh api * -X *)": "gh api repos/x -X POST",
        "Bash(gh api * --method *)": "gh api repos/x --method POST",
        "Bash(gh api * -f *)": "gh api repos/x -f key=val",
        "Bash(gh api * -F *)": "gh api repos/x -F key=val",
        "Bash(gh api * --field *)": "gh api repos/x --field key=val",
        "Bash(gh api * --raw-field *)": "gh api repos/x --raw-field key=val",
        "Bash(gh api * --input *)": "gh api repos/x --input body.json",
        "Bash(gh api graphql:*)": "gh api graphql -f query=x",
        "Bash(git reset --hard:*)": "git reset --hard HEAD~1",
        "Bash(git clean:*)": "git clean -fd",
        "Bash(git branch -D:*)": "git branch -D feature-x",
        "Bash(git worktree:*)": "git worktree remove /tmp/x",
        "Bash(git * --force*)": "git push origin main --force",
        "Bash(git * -f *)": "git push origin -f main",
        "Bash(rm -r*)": "rm -rf /tmp/x",
        "Bash(rm -R*)": "rm -Rf /tmp/x",
        "Bash(rm -f*)": "rm -f /tmp/x",
        "Bash(rm -*r*)": "rm -xrf /tmp/x",
        "Bash(rm -*R*)": "rm -xRf /tmp/x",
        "Bash(rm -*f*)": "rm -xf /tmp/x",
        "Bash(find * -delete*)": "find . -type f -delete",
        "Bash(claude)": "claude",
        "Bash(claude:*)": "claude mcp serve",
    }
    assert set(samples) == set(dispatch_mod.BASE_DENY), "every BASE_DENY rule needs a sample command here"
    for rule, cmd in samples.items():
        assert perms.matches(cmd, [], [rule]) == ("denied", rule), f"{rule!r} did not match {cmd!r}"


def test_matches_against_the_real_worker_allow_and_deny_lists(tmp_root):
    """`git checkout <branch>` stays unlisted on purpose. A headless worker cannot answer an
    ask, so unlisted is a deny. `git checkout -- <path>` (1.7) is allowed."""
    from agent_cli import dispatch as dispatch_mod
    from agent_cli import repos as repos_mod

    repo = repos_mod.load_registry(tmp_root).require("alpha")
    settings = dispatch_mod.build_settings(tmp_root, repo, worktree="/wt/alpha/001-x")
    allow, deny = settings["permissions"]["allow"], settings["permissions"]["deny"]
    assert perms.matches("git checkout main", allow, deny) == ("no-rule", None)
    assert perms.matches("git checkout -- README.md", allow, deny) == ("allowed", "Bash(git checkout --:*)")


def test_dispatch_refuses_to_launch_on_a_broken_rule(tmp_root, monkeypatch):
    """A dead rule reads as a deliberate denial to a headless worker, so it must never reach
    one: building the worker settings fails instead."""
    import pytest

    from agent_cli import dispatch as dispatch_mod
    from agent_cli import repos as repos_mod
    from agent_cli.core import AgentError

    repo = repos_mod.load_registry(tmp_root).require("alpha")
    # Patch the module that OWNS the name, per rules.py's docstring: the lists and
    # `build_settings` moved to `dispatch/settings.py` in and `rules.BASE_ALLOW` is
    # now a re-export that `build_settings` never reads.
    monkeypatch.setattr(
        dispatch_mod.settings,
        "BASE_ALLOW",
        list(dispatch_mod.BASE_ALLOW) + ["Bash(gh api repos/:*)"],
    )
    with pytest.raises(AgentError) as exc:
        dispatch_mod.build_settings(tmp_root, repo, worktree="/wt/alpha/001-x")
    assert "stops mid-token" in str(exc.value)


@pytest.mark.parametrize("allow", [["mcp__acme__read)"], ["mcp__acme__read)", "mcp__acme__*"]])
def test_a_malformed_mcp_grant_is_refused_by_settings_and_codex(tmp_root, monkeypatch, allow):
    from agent_cli import repos as repos_mod
    from agent_cli.core import AgentError
    from agent_cli.dispatch import codex_permissions, settings
    from dispatch_helpers import _set_alpha_dispatch  # pyright: ignore[reportPrivateUsage]

    _set_alpha_dispatch(tmp_root, allowed_tools=allow, repo_mcp=["acme"])
    repo = repos_mod.load_registry(tmp_root).require("alpha")
    d = {"id": "999-99", "repo": "alpha", "worktree": str(tmp_root)}
    monkeypatch.setattr(codex_permissions.state, "load", lambda root, dispatch_id: d)

    with pytest.raises(AgentError):
        settings.build_settings(tmp_root, repo, worktree=str(tmp_root))
    with pytest.raises(AgentError):
        codex_permissions._decision(tmp_root, "999-99", {"tool_name": "mcp__acme__read", "tool_input": {}})


RAW_SERVERS = {"url": "https://secret.example/token", "unspellable": "bad)name"}


@pytest.mark.parametrize("kind", RAW_SERVERS)
def test_settings_and_codex_refuse_a_raw_server_rule_without_echoing_the_name(tmp_root, monkeypatch, kind):
    from agent_cli import repos as repos_mod
    from agent_cli.core import AgentError
    from agent_cli.dispatch import codex_permissions, settings
    from dispatch_helpers import _set_alpha_dispatch  # pyright: ignore[reportPrivateUsage]

    server = RAW_SERVERS[kind]
    monkeypatch.setattr(settings, "BASE_DENY", [*settings.BASE_DENY, f"mcp__{server}__write"])
    _set_alpha_dispatch(
        tmp_root, allowed_tools=[f"mcp__{server}__read", f"mcp__{server}__*"], repo_mcp=[server]
    )
    repo = repos_mod.load_registry(tmp_root).require("alpha")
    d = {"id": "999-99", "repo": "alpha", "worktree": str(tmp_root)}
    monkeypatch.setattr(codex_permissions.state, "load", lambda root, dispatch_id: d)

    with pytest.raises(AgentError) as built:
        settings.build_settings(tmp_root, repo, worktree=str(tmp_root))
    with pytest.raises(AgentError) as hooked:
        codex_permissions._decision(
            tmp_root, "999-99", {"tool_name": f"mcp__{server}__read", "tool_input": {}}
        )
    not_a_rule = "is not a permission rule (expected Tool, Tool(arg) or Tool(prefix:*))"
    for text in (str(built.value), str(hooked.value)):
        assert text.splitlines()[1:] == [
            f"  alpha worker allow: [redacted-mcp-rule] {not_a_rule}",
            f"  alpha worker allow: [redacted-mcp-rule] {not_a_rule}",
            f"  alpha worker deny: [redacted-mcp-rule] {not_a_rule}",
        ], text


NOT_A_RULE = "is not a permission rule (expected Tool, Tool(arg) or Tool(prefix:*))"


@pytest.mark.parametrize("hide", [[], ["safe"], ["https://secret.example/token"]])
def test_a_malformed_mcp_rule_is_never_echoed_with_or_without_a_selection(hide):
    """The server segment can be safe while the rest is raw; no part of the rule is printed."""
    for rule in ("mcp__safe__https://secret.example/token", "mcp__safe__bad)name"):
        problems = perms.lint_all(allow=[rule, rule], deny=[rule], ask=[rule], label="s", hide=hide)
        assert problems == [
            f"s allow: [redacted-mcp-rule] {NOT_A_RULE}",
            f"s allow: [redacted-mcp-rule] {NOT_A_RULE}",
            "s allow: [redacted-mcp-rule] listed 2 times",
            f"s deny: [redacted-mcp-rule] {NOT_A_RULE}",
            f"s ask: [redacted-mcp-rule] {NOT_A_RULE}",
            "allow [redacted-mcp-rule] is cancelled by the same rule in deny",
            "allow [redacted-mcp-rule] is cancelled by the same rule in deny",
        ], (rule, problems)
    with_arg = "mcp__safe__read(https://secret.example/token)"
    assert perms.lint([with_arg, with_arg], label="s", hide=hide) == ["s: [redacted-mcp-rule] listed 2 times"]
    assert perms.mcp_verdict("mcp__safe__bad)name", [], ["mcp__safe__bad)name"])[0] == "denied"


SHORT, LONG = "https://secret.example", "https://secret.example/token"


@pytest.mark.parametrize("hide", [[SHORT, LONG], [LONG, SHORT], [LONG], ["a.b", LONG, "x"], []])
def test_overlapping_selected_names_withhold_the_whole_rule_and_its_suggestion(hide):
    """A URL is withheld whether or not a selection names it (`[]`)."""
    rules = [
        f"Bash(curl {LONG}/:*)",
        f"Bash(curl {LONG} * x:*)",
        "Bash(gh api repos/:*)",
        f"Bash(curl {LONG})",
    ]
    problems = perms.lint_all(allow=rules + rules[:1], deny=rules[3:], label="w", hide=hide)
    assert problems == [
        "w allow: [redacted-rule] stops mid-token, so it matches nothing. A prefix rule has to end on a "
        "whole token: write a prefix that ends on a whole token or spell the command out.",
        "w allow: [redacted-rule] mixes `*` with the trailing `:*`, so Claude Code reads the whole rule "
        "as a literal prefix and it matches nothing. Write the prefix with one trailing `*` and no `:`.",
        "w allow: 'Bash(gh api repos/:*)' stops mid-token, so it matches nothing. A prefix rule has to end "
        "on a whole token: write `Bash(gh api repos:*)` or spell the command out.",
        "w allow: [redacted-rule] stops mid-token, so it matches nothing. A prefix rule has to end on a "
        "whole token: write a prefix that ends on a whole token or spell the command out.",
        "w allow: [redacted-rule] listed 2 times",
        "allow [redacted-rule] is cancelled by the same rule in deny",
    ], problems


def test_safe_identifiers_and_well_formed_mcp_rules_stay_quoted():
    problems = perms.lint_all(
        allow=["mcp__acme__*", "mcp__acme__*", "mcp__a.b__read", "Bash(curl a.b/:*)"],
        deny=["mcp__acme__*"],
        label="x",
        hide=["acme", "a.b"],
    )
    assert problems == [
        f"x allow: [redacted-mcp-rule] {NOT_A_RULE}",
        "x allow: 'Bash(curl a.b/:*)' stops mid-token, so it matches nothing. A prefix rule has to end on a "
        "whole token: write `Bash(curl a.b:*)` or spell the command out.",
        "x allow: 'mcp__acme__*' listed 2 times",
        "allow 'mcp__acme__*' is cancelled by the same rule in deny",
        "allow 'mcp__acme__*' is cancelled by the same rule in deny",
    ], problems


def test_a_selected_name_holding_the_rule_delimiter_is_ambiguous():
    for server in ("a__b", "a_", "__a", "x__https://y"):
        assert perms.mcp_server_ambiguous(server), server
        assert perms.mcp_server_grant(server, [f"mcp__{server}__read", f"mcp__{server}__*"]) == (
            "ambiguous",
            [],
        )
    for server in ("a", "ab", "_a", "a_b", "a-b", "acme"):
        assert not perms.mcp_server_ambiguous(server), server
    assert perms.mcp_server_grant("a", ["mcp__a__b__read"]) == ("granted", ["mcp__a__b__read"])
    assert perms.mcp_selection_problems(["a", "x__https://y"]) == [
        "repo_mcp selects '[redacted-name]', whose name runs into the MCP rule delimiter `__`, so no "
        "mcp__<server>__<tool> rule says which server it grants; rename it or drop it from repo_mcp"
    ]
    assert perms.mcp_selection_problems(["a_"], "dispatch.mcp profile 'p'", "drop the profile") == [
        "dispatch.mcp profile 'p' selects 'a_', whose name runs into the MCP rule delimiter `__`, so no "
        "mcp__<server>__<tool> rule says which server it grants; rename it or drop the profile"
    ]


def test_a_tool_name_is_stored_whole_or_not_at_all():
    for safe in ("Bash", "mcp__acme__read", "mcp__a.b__read", "mcp__a__b__read"):
        assert perms.shown_tool(safe) == safe
    assert perms.shown_tool("mcp__https://h.example/v1/token__read") == "[redacted-mcp-tool]"
    assert perms.shown_tool("mcp__q w__read") == "[redacted-mcp-tool]"
    assert perms.shown_tool("Tool\nx") == "[redacted-tool]"


def test_free_text_withholds_each_endpoint_through_its_suffix_and_keeps_the_rest():
    url = "https://helper.internal/v1/zq7suffix"
    assert perms.shown_text(f"Worker cp escapes the dispatch worktree: /tmp/{url}") == (
        "Worker cp escapes the dispatch worktree: /tmp/[redacted-endpoint]"
    )
    assert perms.shown_text(f'/bin/zsh -lc "curl {url}?a=1 -o out" and sse+tcp://x:9/y') == (
        '/bin/zsh -lc "curl [redacted-endpoint] -o out" and [redacted-endpoint]'
    )
    for safe in ("git add src/a.py", "needs .agent/browser/staging.json; npx playwright open", "a: b"):
        assert perms.shown_text(safe) == safe


def test_mcp_canonical_spellings():
    assert perms.mcp_canonical("acme", "mcp__acme__*")
    assert perms.mcp_canonical("acme", "mcp__acme__read_memory")
    for rule in ("mcp__acme", "mcp__acme__", "mcp__acme__re*", "mcp__acme__[rw]", "mcp__acmeplus__*"):
        assert not perms.mcp_canonical("acme", rule), rule
    for rule in ("mcp__acme__read)", "mcp__acme__read(x)", "mcp__acme__read\n", " mcp__acme__read"):
        assert not perms.mcp_canonical("acme", rule), rule
        assert perms.mcp_server_grant("acme", [rule])[0] != "granted", rule
    assert not perms.mcp_canonical("https://secret.example/token", "mcp__https://secret.example/token__*")
    assert perms.mcp_server_grant("acme", ["mcp__acmeplus__*"]) == ("none", [])
    assert perms.mcp_server_grant("acme", ["mcp__*"]) == ("other", ["mcp__*"])


def test_a_profile_keeps_canonical_and_bash_rules_and_refuses_the_rest():
    good = ["mcp__acme__*", "mcp__acme__read", "Bash(stat x:*)", "Read"]
    assert perms.mcp_profile_problems("acme", good, ["mcp__acme__write", "Bash(stat x y:*)"], "p") == []
    bad = ["mcp__acme", "mcp__acme__re*", "mcp__other__read", "mcp__acme__read(x)"]
    problems = perms.mcp_profile_problems("acme", bad[:2], bad[2:], "p")
    assert [p.split(" is ")[0] for p in problems] == ["p allow 'mcp__acme'", "p allow 'mcp__acme__re*'"] + [
        "p deny 'mcp__other__read'",
        "p deny [redacted-mcp-rule]",
    ]
    assert all("write `mcp__acme__*` for the whole server" in p for p in problems)
