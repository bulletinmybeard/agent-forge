"""Parse @review prompt flags into a style + target + instruction."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

REVIEW_STYLES = frozenset({"single", "deep", "classic"})
DEFAULT_STYLE = "single"

_REVIEW_STYLE_REASON = {
    "single": "Code review — one large-context reviewer",
    "deep": "Parallel code review — 4 specialists + merge",
    "classic": "Parallel code review — 4 specialised sub-agents",
}


def style_reason(style: str) -> str:
    return _REVIEW_STYLE_REASON.get(style, _REVIEW_STYLE_REASON[DEFAULT_STYLE])


def resolve_review_max_workers(configured: int) -> int:
    """Provider-capped specialist concurrency; falls back to *configured*."""
    try:
        from agentforge.config import get_config

        cap = get_config().get_by_provider("review", "max_workers", default=None)
        if cap is not None and int(cap) > 0:
            return int(cap)
    except Exception:
        pass
    return max(1, int(configured or 4))


_STYLE_FLAGS = {
    "--single": "single",
    "--deep": "deep",
    "--classic": "classic",
}

_PREFIX_RE = re.compile(r"^@review\s*", re.IGNORECASE)


@dataclass(frozen=True)
class ReviewQuery:
    style: str
    target: str
    instruction: str
    branch: str = ""


def _normalise_style(value: str, fallback: str) -> str:
    style = (value or "").strip().lower()
    if style in REVIEW_STYLES:
        return style
    if fallback in REVIEW_STYLES:
        return fallback
    return DEFAULT_STYLE


def parse_review_query(query: str, default_style: str = DEFAULT_STYLE) -> ReviewQuery:
    """Strip ``@review``, style flags, and an optional target path.

    Last recognised style flag wins. Unknown ``--style=...`` values are dropped
    (not passed through as instruction text) and the default is kept.
    """
    style = _normalise_style(default_style, DEFAULT_STYLE)
    rest = _PREFIX_RE.sub("", (query or "").strip()).strip()
    parts = rest.split()
    kept: list[str] = []
    i = 0
    while i < len(parts):
        part = parts[i]
        if part in _STYLE_FLAGS:
            style = _STYLE_FLAGS[part]
            i += 1
            continue
        if part.startswith("--style="):
            val = part.split("=", 1)[1]
            if val.strip().lower() in REVIEW_STYLES:
                style = val.strip().lower()
            i += 1
            continue
        if part == "--style" and i + 1 < len(parts):
            nxt = parts[i + 1]
            if nxt.strip().lower() in REVIEW_STYLES:
                style = nxt.strip().lower()
                i += 2
                continue
            i += 1
            continue
        kept.append(part)
        i += 1

    clean = " ".join(kept).strip()
    target, instruction = _extract_target(clean)
    if not instruction:
        instruction = "Review all current and unpushed changes"
    return ReviewQuery(
        style=style,
        target=target,
        instruction=instruction,
        branch=_extract_branch(query or ""),
    )


_PATH_TRAIL = ".,;:!?)]}'\"`"


def _extract_branch(query: str) -> str:
    """Best-effort branch name from the prompt (``branch `foo` ``)."""
    match = re.search(r"\bbranch\s+`([^`]+)`", query, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    match = re.search(r"\bbranch\s+['\"]([^'\"]+)['\"]", query, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    match = re.search(r"\bbranch\s+([A-Za-z0-9._][A-Za-z0-9._/-]{2,})", query, re.IGNORECASE)
    if match:
        name = match.group(1).rstrip(".,;:)")
        if name.lower() not in {"master", "main", "develop", "trunk", "head"}:
            return name
    return ""


_SKIP_TARGET_NAMES = frozenset({"downloads", "desktop", "documents", "tmp", "temp"})
_SKIP_TARGET_SUFFIXES = (".md", ".pdf", ".txt", ".docx", ".html")


def looks_like_project_path(path: str) -> bool:
    """True for a repo/workdir, not a save folder or a review document."""
    raw = (path or "").strip().rstrip("/\\")
    if not raw:
        return False
    name = Path(raw).name.lower()
    if name in _SKIP_TARGET_NAMES:
        return False
    if raw.lower().endswith(_SKIP_TARGET_SUFFIXES):
        return False
    return True


def _extract_target(clean: str) -> tuple[str, str]:
    """Pull a project path out of the remaining instruction.

    Prefers a repo-like path over ``~/Downloads`` (report dest) and ``.md`` files.
    """
    candidates: list[tuple[str, str]] = []
    for match in re.finditer(r"(~/[^\s]+)|(/[^\s]+)", clean):
        raw = _strip_path_token(match.group(0))
        if match.group(1):
            expanded = os.path.expanduser(raw)
        else:
            expanded = raw
            if "/" not in expanded or len(expanded) <= 3:
                continue
        candidates.append((expanded, match.group(0)))

    for expanded, original in candidates:
        if looks_like_project_path(expanded):
            return expanded, clean.replace(original, "").strip()
    return "", clean


def _strip_path_token(token: str) -> str:
    return token.rstrip(_PATH_TRAIL)
