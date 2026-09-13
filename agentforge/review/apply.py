"""Promote apply-from-review follow-ups to @agent with the review + repo path."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from agentforge.review.output import parse_review_output_path
from agentforge.review.style import looks_like_project_path, parse_review_query

# "implement" is too common in ticket/branch names (feat-implement-…).
_APPLY_RE = re.compile(
    r"\b(apply|patch)\b.{0,80}\b("
    r"review|finding|worth[\s-]?fixing|blocker|"
    r"proposed\s+code\s+changes|the\s+fixes?|the\s+changes?"
    r")\b"
    r"|\bfix(es)?\s+(everything|the\s+(issues?|findings?|items?))\b"
    r"|\breview\s+document\b",
    re.IGNORECASE | re.DOTALL,
)

_REVIEW_REQUEST_RE = re.compile(
    r"\bsecond opinion\b|\bversus\s+[`']?(master|main)\b|"
    r"\bstore the (response|review)\b|\bunpushed changes\b|"
    r"\bonly focus on the changes\b",
    re.IGNORECASE,
)

_DOC_PATH_RE = re.compile(
    r"((?:~|/|\./)[^\s`'\"),;]+\.md)",
    re.IGNORECASE,
)

_REVIEW_BODY_MAX = 80_000


@dataclass
class ReviewApply:
    query: str
    target: str
    branch: str
    review_path: str = ""


def is_review_apply_intent(
    query: str,
    *,
    last_mode: str = "",
    forced_mode: str | None = None,
    classified_mode: str = "",
    messages: list[dict] | None = None,
) -> bool:
    """True when the user wants the previous review's findings written to disk."""
    text = query or ""
    if _REVIEW_REQUEST_RE.search(text):
        return False
    has_apply = bool(_APPLY_RE.search(text))
    doc = parse_review_document_path(text)
    if doc and has_apply:
        return True
    if not has_apply:
        return False
    if last_mode == "review" or forced_mode == "review":
        return True
    return _session_has_review(messages)


def parse_review_document_path(query: str) -> str | None:
    """First ``.md`` path that is a review *source*, not a save destination."""
    match = _DOC_PATH_RE.search(query or "")
    if not match:
        return None
    raw = match.group(1).rstrip(".,;:!?)]}'\"")
    expanded = os.path.expanduser(raw)
    dest = parse_review_output_path(query or "")
    if dest:
        dest_exp = os.path.expanduser(dest)
        if expanded.rstrip("/") == dest_exp.rstrip("/") or expanded.startswith(
            dest_exp.rstrip("/") + "/",
        ):
            return None
        if dest_exp.rstrip("/") == expanded.rstrip("/"):
            return None
    return expanded


def build_review_apply_query(
    *,
    user: str,
    target: str,
    branch: str,
    review: str,
    review_path: str = "",
) -> str:
    repo = (
        target
        or "(not recorded — infer from file paths in the review; do not edit AgentForge or the worker cwd unless that is the reviewed project)"
    )
    branch_line = f"Branch: `{branch}`\n" if branch else ""
    read_line = ""
    if review_path and not Path(review_path).is_file():
        read_line = (
            f"The review markdown may only exist on the host filesystem. Read it with read_file: `{review_path}`\n"
        )
    return (
        "Apply only the Blockers and Worth fixing items from this code review. "
        "Do not re-review. Do not invent extra refactors. Do not edit files outside "
        "the repository below.\n\n"
        f"Repository: `{repo}`\n"
        f"{branch_line}"
        f"{read_line}"
        f"User: {user}\n\n"
        "--- review ---\n"
        f"{review.strip() or '(no review body found in this session — use the file path above)'}\n"
        "--- end review ---\n"
    )


def resolve_review_apply(
    query: str,
    *,
    last_mode: str = "",
    forced_mode: str | None = None,
    classified_mode: str = "",
    messages: list[dict] | None = None,
) -> ReviewApply | None:
    if not is_review_apply_intent(
        query,
        last_mode=last_mode,
        forced_mode=forced_mode,
        classified_mode=classified_mode,
        messages=messages,
    ):
        return None

    doc_path = parse_review_document_path(query)
    review = _read_review_file(doc_path) if doc_path else ""
    if not review:
        review = _first_review_body(messages)

    target, branch = _target_from_messages(messages)
    if not target:
        parsed = parse_review_query(query)
        if looks_like_project_path(parsed.target):
            target, branch = parsed.target, parsed.branch or branch

    rewritten = build_review_apply_query(
        user=_strip_mode_token(query),
        target=target,
        branch=branch,
        review=review,
        review_path=doc_path or "",
    )
    return ReviewApply(query=rewritten, target=target, branch=branch, review_path=doc_path or "")


def _strip_mode_token(query: str) -> str:
    return re.sub(r"^@(?:coding|code|review|agent)\s+", "", (query or "").strip(), flags=re.I)


def _read_review_file(path: str | None) -> str:
    if not path:
        return ""
    p = Path(path)
    if not p.is_file():
        return ""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) > _REVIEW_BODY_MAX:
        text = text[:_REVIEW_BODY_MAX] + "\n... [truncated]\n"
    return text


def _meta(msg: dict) -> dict:
    raw = msg.get("metadata") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _session_has_review(messages: list[dict] | None) -> bool:
    for msg in messages or []:
        if msg.get("type") != "result":
            continue
        if _meta(msg).get("review_target"):
            return True
        content = (msg.get("content") or "").lstrip()
        if content.startswith("# Review") and "planner declined" not in content.lower():
            return True
    return False


def _first_review_body(messages: list[dict] | None) -> str:
    """Original review in the thread — not a later misfire on the wrong tree."""
    for msg in messages or []:
        if msg.get("type") != "result":
            continue
        content = (msg.get("content") or "").strip()
        if not content.startswith("# Review"):
            continue
        if "planner declined" in content.lower():
            continue
        if len(content) > _REVIEW_BODY_MAX:
            return content[:_REVIEW_BODY_MAX] + "\n... [truncated]\n"
        return content
    return ""


def _target_from_messages(messages: list[dict] | None) -> tuple[str, str]:
    for msg in messages or []:
        meta = _meta(msg)
        target = (meta.get("review_target") or "").strip()
        if looks_like_project_path(target):
            return target, (meta.get("review_branch") or "").strip()
        if msg.get("type") == "query":
            parsed = parse_review_query(msg.get("content") or "")
            if looks_like_project_path(parsed.target):
                return parsed.target, parsed.branch
    return "", ""
