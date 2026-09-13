"""Apply-from-review: detect intent, load the review, rewrite for @agent."""

from __future__ import annotations

from pathlib import Path

from agentforge.review.apply import (
    build_review_apply_query,
    is_review_apply_intent,
    parse_review_document_path,
    resolve_review_apply,
)
from agentforge.review.tools import REVIEW_TOOLS


def test_apply_intent_from_follow_up_after_review():
    assert is_review_apply_intent(
        "Let's apply the changes you believe are worth fixing",
        last_mode="review",
    )
    assert is_review_apply_intent("Let's start fix everything you claimed Worth fixing", last_mode="review")
    assert is_review_apply_intent("apply the fixes now", last_mode="review")


def test_apply_intent_from_coding_plus_review_doc():
    q = (
        "@coding Apply the proposed code changes to fix issues from this review "
        "document: /tmp/reviews/review-feat.md"
    )
    assert is_review_apply_intent(q, forced_mode="coding")
    assert parse_review_document_path(q) == "/tmp/reviews/review-feat.md"


def test_new_review_request_is_not_apply_intent():
    q = (
        "I need your help and a second opinion on this locally checked out branch "
        "`feat-healthcheck` versus `master` "
        "in /opt/api and store the response/review results in "
        "`/tmp/reviews/Merge Request Review.md`"
    )
    assert not is_review_apply_intent(q, forced_mode="review")
    assert not is_review_apply_intent(q, classified_mode="review")
    assert not is_review_apply_intent(q)
    assert resolve_review_apply(q, forced_mode="review") is None


def test_review_apply_follow_up_with_review_prefix():
    assert is_review_apply_intent(
        "@review apply the worth-fixing items",
        forced_mode="review",
        messages=[{"type": "result", "content": "# Review\nReady"}],
    )


def test_plain_coding_rename_is_not_apply_intent():
    assert not is_review_apply_intent(
        "@coding rename Foo to Bar in src/",
        forced_mode="coding",
    )
    assert not is_review_apply_intent(
        "@coding apply the changes in src/",
        forced_mode="coding",
    )
    assert not is_review_apply_intent("what about finding 2?", last_mode="review")


def test_resolve_reads_review_file_and_session_target(tmp_path: Path):
    doc = tmp_path / "review-feat.md"
    doc.write_text("# Review\n\n## Worth fixing\n\n1. Drop the triplicated NOTE.\n")
    messages = [
        {
            "type": "query",
            "content": "@review /opt/api branch `feat-healthcheck`",
        },
        {
            "type": "result",
            "content": "# Review\nReady",
            "metadata": {
                "review_target": "/opt/api",
                "review_branch": "feat-healthcheck",
            },
        },
    ]
    got = resolve_review_apply(
        f"@coding Apply the proposed code changes from this review document: {doc}",
        forced_mode="coding",
        messages=messages,
    )
    assert got is not None
    assert got.target == "/opt/api"
    assert got.branch == "feat-healthcheck"
    assert "triplicated NOTE" in got.query
    assert "Do not re-review" in got.query
    assert str(tmp_path) not in got.target


def test_resolve_follow_up_without_doc_uses_last_review_body():
    messages = [
        {
            "type": "result",
            "content": "# Review\n\n## Worth fixing\n\n1. Normalize at intake.\n",
            "metadata": {
                "type": "agent.result",
                "review_target": "/opt/api",
                "review_branch": "fix-x",
            },
        },
    ]
    got = resolve_review_apply(
        "apply the worth-fixing items",
        last_mode="review",
        messages=messages,
    )
    assert got is not None
    assert got.target == "/opt/api"
    assert "Normalize at intake" in got.query


def test_apply_intent_if_session_already_has_a_review():
    messages = [{"type": "result", "content": "# Review\nReady"}]
    assert is_review_apply_intent(
        "apply the worth-fixing items",
        last_mode="coding",
        messages=messages,
    )


def test_resolve_uses_original_review_not_later_wrong_one():
    messages = [
        {
            "type": "query",
            "content": (
                "branch `feat-healthcheck` (/opt/api) store in ~/Downloads"
            ),
        },
        {"type": "result", "content": "# Review\n\n## Worth fixing\n\n1. Drop the NOTE.\n"},
        {"type": "result", "content": "# Review\n\n**Scope:** Large feature branch — router.py\n"},
    ]
    got = resolve_review_apply(
        "apply the worth-fixing items",
        last_mode="coding",
        messages=messages,
    )
    assert got is not None
    assert got.target == "/opt/api"
    assert "Drop the NOTE" in got.query
    assert "router.py" not in got.query
    assert "/root/Downloads" not in got.query
    assert "Downloads" not in got.target


def test_unreadable_review_md_tells_agent_to_read_the_path():
    messages = [
        {
            "type": "query",
            "content": "@review /opt/api branch `feat-x`",
        },
        {"type": "result", "content": "# Review\n\n## Worth fixing\n\n1. x\n"},
    ]
    got = resolve_review_apply(
        "@coding Apply the proposed code changes from this review document: "
        "/tmp/review-missing.md",
        forced_mode="coding",
        messages=messages,
    )
    assert got is not None
    assert got.target == "/opt/api"
    assert "/tmp/review-missing.md" in got.query


def test_resolve_returns_none_when_not_apply():
    assert resolve_review_apply("@coding rename Foo to Bar in src/", forced_mode="coding") is None


def test_build_apply_query_names_repo_and_findings():
    text = build_review_apply_query(
        user="apply the fixes",
        target="/repo",
        branch="feat",
        review="## Worth fixing\n1. x",
    )
    assert "/repo" in text
    assert "feat" in text
    assert "Worth fixing" in text
    assert "apply the fixes" in text


def test_review_tools_are_read_only():
    assert "shell" not in REVIEW_TOOLS
    assert "write_file" not in REVIEW_TOOLS
    assert "code_edit" not in REVIEW_TOOLS
    assert "read_file" in REVIEW_TOOLS
    assert "git_diff" in REVIEW_TOOLS
