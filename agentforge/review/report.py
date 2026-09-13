"""Assemble @review reports and extra specialist instructions."""

from __future__ import annotations

from typing import Any


def specialist_extra_rules(style: str, *, max_findings: int) -> str:
    """Deep mode only: specialists feed a merge step, so stay short and strict."""
    if style != "deep":
        return ""
    n = max(1, int(max_findings))
    return f"""
## Merge-step rules (deep review)

You are feeding a merge step, not the user. The merge model writes the final report.

- At most {n} findings. Zero findings is success — do not invent issues to fill space.
- Omit MINOR / nits. Only report bugs or real merge risks.
- If you would not comment this on a colleague's MR, omit it.
- Do not inflate severity. A bug is an actual correctness/security/breakage defect.
- Structured findings only:

```
[SEVERITY] file:line — short title
  Problem: <what is wrong and why it matters>
  Suggestion: <concrete fix>
  Merge-blocker: yes|no
```
"""


def build_classic_report(
    *,
    target: str,
    elapsed_s: float,
    sub_agents: list[tuple[str, str, str, str]],
    sub_results: dict[str, dict[str, Any]],
    errors: dict[str, str],
) -> str:
    """Concatenate specialist prose — the current @review behaviour."""
    total_tools = sum(len(sr.get("tool_calls") or []) for sr in sub_results.values())
    parts: list[str] = ["# Code Review Report"]
    if target:
        parts.append(f"\n**Target**: `{target}`")
    parts.append(
        f"**Duration**: {elapsed_s:.1f}s | **Sub-agents**: {len(sub_results)}/{len(sub_agents)} "
        f"| **Tool calls**: {total_tools}"
    )
    parts.append("")

    for sa_id, sa_label, _sa_file, sa_desc in sub_agents:
        sr = sub_results.get(sa_id)
        if sr:
            parts.append(f"---\n\n## {sa_label}")
            parts.append(f"*{sa_desc}*\n")
            parts.append(sr.get("text") or "")
            parts.append("")
        elif sa_id in errors:
            parts.append(f"---\n\n## {sa_label}")
            parts.append(f"Sub-agent failed: {errors[sa_id]}\n")

    return "\n".join(parts)
