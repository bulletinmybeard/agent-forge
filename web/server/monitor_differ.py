"""monitor_differ — change detection and diff summarization for @monitor.

Two-phase comparison:
  1. **Hash pre-check** — SHA-256 comparison.  If identical, skip the diff.
  2. **Text diff** — Line-by-line unified diff via ``difflib``.

Optional LLM-powered summarization condenses a raw diff into a brief
human-readable description (e.g., "Pricing page added Enterprise tier at $999/mo").
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class DiffResult:
    """Result of comparing two content snapshots."""

    changed: bool
    lines_added: int = 0
    lines_removed: int = 0
    unified_diff: str = ""
    changed_sections: list[str] = field(default_factory=list)
    summary: str = ""  # LLM-generated or heuristic


def quick_check(prev_hash: str, current_hash: str) -> bool:
    """Fast pre-check: return True if content has changed."""
    return prev_hash != current_hash


def compute_diff(prev_content: str, current_content: str) -> DiffResult:
    """Compute a line-by-line diff between two content strings.

    Returns a ``DiffResult`` with added/removed line counts, unified diff text,
    and a list of changed section headers (lines starting with ##, #, etc.).
    """
    if prev_content == current_content:
        return DiffResult(changed=False)

    prev_lines = prev_content.splitlines(keepends=True)
    curr_lines = current_content.splitlines(keepends=True)

    diff_lines = list(
        difflib.unified_diff(
            prev_lines,
            curr_lines,
            fromfile="previous",
            tofile="current",
            lineterm="",
        )
    )

    added = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))

    # Extract changed section headers (markdown headings near diff hunks)
    changed_sections = []
    for line in diff_lines:
        stripped = line.lstrip("+-").strip()
        if stripped.startswith("#"):
            if stripped not in changed_sections:
                changed_sections.append(stripped)

    unified = "\n".join(diff_lines[:500])  # cap at 500 lines
    if len(diff_lines) > 500:
        unified += f"\n... ({len(diff_lines) - 500} more diff lines)"

    return DiffResult(
        changed=True,
        lines_added=added,
        lines_removed=removed,
        unified_diff=unified,
        changed_sections=changed_sections,
    )


def generate_heuristic_summary(diff: DiffResult, url: str = "") -> str:
    """Generate a simple summary without LLM — based on diff stats."""
    if not diff.changed:
        return "No changes detected."

    parts = []
    if diff.lines_added and diff.lines_removed:
        parts.append(f"{diff.lines_added} lines added, {diff.lines_removed} removed")
    elif diff.lines_added:
        parts.append(f"{diff.lines_added} lines added")
    elif diff.lines_removed:
        parts.append(f"{diff.lines_removed} lines removed")

    if diff.changed_sections:
        sections = ", ".join(diff.changed_sections[:3])
        parts.append(f"in sections: {sections}")

    return " — ".join(parts) if parts else "Content changed"


async def generate_llm_summary(
    prev_content: str,
    current_content: str,
    diff: DiffResult,
    url: str = "",
) -> str:
    """Use a lightweight LLM call to summarize what changed.

    Falls back to heuristic summary on failure.
    """
    try:
        from agentforge.client import AIClient

        # Use cloud-light profile (fast, cheap)
        client = AIClient(profile="cloud-light")

        # Prepare a compact context: first 300 chars of diff + stats
        diff_preview = diff.unified_diff[:2000] if diff.unified_diff else ""
        prompt = (
            f"A monitored web page at {url} has changed.\n\n"
            f"Stats: {diff.lines_added} lines added, {diff.lines_removed} lines removed.\n"
            f"Changed sections: {', '.join(diff.changed_sections[:5]) or 'unknown'}\n\n"
            f"Diff preview:\n```\n{diff_preview}\n```\n\n"
            "Summarize what changed in 1-2 sentences. Be specific about content changes, "
            "not technical details. Example: 'Pricing page added a new Enterprise tier at $999/mo'"
        )

        messages = [
            {
                "role": "system",
                "content": "You summarize website changes concisely. Output only the summary, nothing else.",
            },
            {"role": "user", "content": prompt},
        ]

        import asyncio

        response = await asyncio.to_thread(client.chat, messages)

        if response and isinstance(response, str) and len(response.strip()) > 5:
            return response.strip()

    except Exception as exc:
        logger.debug("LLM diff summary failed: %s — falling back to heuristic", exc)

    return generate_heuristic_summary(diff, url)
