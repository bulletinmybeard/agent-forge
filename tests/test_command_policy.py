from agentforge.tools.command_policy import (
    CommandPolicy,
    evaluate,
    merge_policies,
)


def test_allowlist_denies_unknown_command():
    policy = CommandPolicy(mode="allowlist", allowed_commands=("git", "ls"))
    v = evaluate("shell", "npm install", policy)
    assert v.action == "deny"
    assert "not allowed" in v.reason.lower()


def test_allowlist_allows_chained_if_each_segment_ok():
    policy = CommandPolicy(mode="allowlist", allowed_commands=("git", "ls"))
    v = evaluate("shell", "git status && ls -la", policy)
    assert v.action == "allow"


def test_allowlist_denies_chained_with_writer():
    policy = CommandPolicy(mode="allowlist", allowed_commands=("git", "ls"))
    v = evaluate("shell", "git status && npm install", policy)
    assert v.action == "deny"


def test_denylist_blocks_pattern_any_segment():
    policy = CommandPolicy(mode="denylist", blocked_patterns=(r"git\s+push",))
    v = evaluate("shell", "git status && git push origin main", policy)
    assert v.action == "deny"
    assert v.source == "policy_denylist"


def test_confirm_mode_blocked_pattern_denies_without_confirm():
    policy = CommandPolicy(
        mode="confirm",
        blocked_patterns=(r"rm\s+-rf",),
    )
    v = evaluate("shell", "rm -rf /tmp/x", policy)
    assert v.action == "deny"


def test_confirm_mode_safe_command_defers_to_guard():
    policy = CommandPolicy(mode="confirm")
    v = evaluate("shell", "git status", policy)
    assert v.action == "confirm"


def test_allowed_pattern_matches_segment():
    policy = CommandPolicy(
        mode="allowlist",
        allowed_patterns=(r"^git\s+(status|log|diff)\b",),
    )
    assert evaluate("shell", "git status", policy).action == "allow"
    assert evaluate("shell", "git push", policy).action == "deny"


def test_merge_policies_override_wins():
    base = CommandPolicy(mode="confirm", blocked_patterns=(r"foo",))
    override = CommandPolicy(mode="allowlist", allowed_commands=("ls",))
    merged = merge_policies(base, override)
    assert merged.mode == "allowlist"
    assert merged.allowed_commands == ("ls",)
