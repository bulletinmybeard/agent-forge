"""Parse @plan / @build prompts and follow-up gates."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from agentforge.review.style import looks_like_project_path

_PREFIX_RE = re.compile(r"^@(plan|build)\s*", re.IGNORECASE)
_APPROVE_RE = re.compile(
    r"^\s*(i\s+)?approve(\s+the\s+plan|\s+this\s+plan)?\b"
    r"|\bapprove\b.{0,40}\bplan\b",
    re.IGNORECASE,
)
_CANCEL_RE = re.compile(r"^\s*cancel(\s+the\s+plan|\s+this\s+plan)?\b", re.IGNORECASE)
_START_OVER_RE = re.compile(r"^\s*start\s+over\b", re.IGNORECASE)
_UNDO_RE = re.compile(
    r"^\s*(@build\s+)?(undo|rollback|revert)"
    r"(\s+(the\s+)?(last\s+)?(build|plan|changes|writes|files))?\s*[.!]?\s*$",
    re.IGNORECASE,
)
_REDO_RE = re.compile(
    r"^\s*(@build\s+)?(redo|re-?apply)(\s+(the\s+)?(build|plan|changes))?\s*[.!]?\s*$"
    r"|^\s*put\s+(them|the\s+changes)\s+back\s*[.!]?\s*$"
    r"|^\s*apply\s+(the\s+)?(plan|build|changes|those\s+changes)\s+(again|back)\s*[.!]?\s*$",
    re.IGNORECASE,
)
_BRANCH_RE = re.compile(r"\bbranch\s+`([^`]+)`", re.IGNORECASE)
_PATH_TRAIL = ".,;:!?)]}'\"`"


@dataclass(frozen=True)
class BuilderQuery:
    phase: str  # plan | build
    target: str
    branch: str
    instruction: str


def parse_builder_query(query: str, default_phase: str = "plan") -> BuilderQuery:
    text = (query or "").strip()
    phase = default_phase if default_phase in {"plan", "build"} else "plan"
    match = _PREFIX_RE.match(text)
    if match:
        phase = match.group(1).lower()
        text = text[match.end() :].strip()
    target, text = _extract_target(text)
    branch = ""
    bmatch = _BRANCH_RE.search(query or "")
    if bmatch:
        branch = bmatch.group(1).strip()
    if not text:
        text = "Draft a build plan for this repository" if phase == "plan" else "Execute the approved plan"
    return BuilderQuery(phase=phase, target=target, branch=branch, instruction=text)


def is_approve_plan(query: str) -> bool:
    return bool(_APPROVE_RE.match(query or ""))


def is_cancel_plan(query: str) -> bool:
    return bool(_CANCEL_RE.match(query or ""))


def is_start_over_plan(query: str) -> bool:
    return bool(_START_OVER_RE.match(query or ""))


def is_undo_build(query: str) -> bool:
    return bool(_UNDO_RE.search(query or ""))


def is_redo_build(query: str) -> bool:
    if is_approve_plan(query) or is_undo_build(query):
        return False
    return bool(_REDO_RE.search(query or ""))


def _extract_target(clean: str) -> tuple[str, str]:
    for match in re.finditer(r"(~/[^\s]+)|(/[^\s]+)", clean):
        raw = match.group(0).rstrip(_PATH_TRAIL)
        expanded = os.path.expanduser(raw) if match.group(1) else raw
        if looks_like_project_path(expanded):
            return expanded, clean.replace(match.group(0), "").strip()
    return "", clean
