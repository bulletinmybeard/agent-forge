"""Fixed-template plan builder for ``@coding`` mode (Phase 3).

When ``coding.auto_planner: false``, ``@coding`` uses this module
instead of running the full LLM planner. It extracts a minimal parameter set
from the user prompt via one small LLM call and then builds the canonical
four-step plan: ``code_find → code_narrow → code_transform → code_verify``.

Phase 5 adds a richer planner that can emit arbitrary plan shapes; this module stays as the simple fallback.
"""

from __future__ import annotations

import json
import re
from typing import Any

from chalkbox.logging.bridge import get_logger

from agentforge.coding.driver import Plan, PlanStep

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Parameter extraction
# ---------------------------------------------------------------------------

_EXTRACT_SYSTEM_PROMPT = """\
You turn a user's code-transform request into the five parameters needed
to drive a discover → narrow → transform pipeline. Return STRICT JSON only,
wrapped in a fenced ```json block. No prose, no explanations.

Fields:

- path            absolute filesystem path (file or directory) to search
- glob            file-name pattern like "*.jsx" or "*.{ts,tsx}". Empty
                  string means "all files".
- pattern         ripgrep-compatible regex for the INITIAL discovery step
                  (intentionally broad — e.g., "<Grid(\\s|>|$)")
- narrow_predicate  ripgrep-compatible regex for the FILTER step. Keeps
                    hits whose matched line also matches this regex. Use
                    "" to skip narrowing (returns all discover hits).
- instruction     a concise, imperative one-line description of the edit
                  to apply to each matched site.

Every field must be a string. Escape backslashes and quotes correctly.
If any field cannot be inferred from the user prompt, set it to "" and
let the caller surface the error. Do NOT invent paths that weren't
mentioned.
"""


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def _extract_json_block(text: str) -> dict | None:
    m = _JSON_BLOCK_RE.search(text)
    candidate = m.group(1).strip() if m else text.strip()
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def extract_params(
    prompt: str,
    *,
    profile: str = "coding",
    client_factory: Any = None,
) -> dict:
    """Extract the five template parameters from ``prompt`` via one LLM call.

    Returns a dict with keys ``path``, ``glob``, ``pattern``,
    ``narrow_predicate``, ``instruction``. Missing values come back as
    empty strings — the caller should reject the run if ``path``,
    ``pattern``, or ``instruction`` are empty.
    """
    if client_factory is None:
        from agentforge.client import AIClient

        def _default_factory(prof: str):
            return AIClient(profile=prof)

        client_factory = _default_factory

    client = client_factory(profile)
    messages = [
        {"role": "system", "content": _EXTRACT_SYSTEM_PROMPT},
        {"role": "user", "content": prompt.strip()},
    ]
    resp = client.chat(messages, stream=False, temperature=0.0)
    content = (getattr(resp, "content", "") or "").strip()

    data = _extract_json_block(content) or {}
    params = {
        "path": str(data.get("path", "") or ""),
        "glob": str(data.get("glob", "") or ""),
        "pattern": str(data.get("pattern", "") or ""),
        "narrow_predicate": str(data.get("narrow_predicate", "") or ""),
        "instruction": str(data.get("instruction", "") or ""),
    }
    logger.info(
        "[coding.template] extracted params: path=%r glob=%r pattern=%r narrow=%r instr=%r",
        params["path"],
        params["glob"],
        params["pattern"],
        params["narrow_predicate"],
        params["instruction"][:60],
    )
    return params


# ---------------------------------------------------------------------------
# Plan builder
# ---------------------------------------------------------------------------


def build_fixed_plan(params: dict, *, profile: str = "coding") -> Plan:
    """Build the canonical discover → narrow → transform → verify plan.

    Narrowing step is omitted when ``narrow_predicate`` is empty so the
    driver doesn't run a no-op step. Verify uses the narrow predicate as
    the "before" pattern when present, falls back to the discover pattern
    otherwise — either way Phase 2's verifier scopes its check to the
    proposed file set.
    """
    steps: list[PlanStep] = []

    steps.append(
        PlanStep(
            tool="code_find",
            args={
                "pattern": params["pattern"],
                "glob": params.get("glob", ""),
                "path": params["path"],
            },
            assign="hits",
        )
    )

    if params.get("narrow_predicate"):
        steps.append(
            PlanStep(
                tool="code_narrow",
                args={
                    "hits": "$hits",
                    "predicate_regex": params["narrow_predicate"],
                },
                assign="hits",
            )
        )

    steps.append(
        PlanStep(
            tool="code_transform",
            args={
                "hits": "$hits",
                "instruction": params["instruction"],
                "profile": profile,
            },
            assign="proposed",
        )
    )

    steps.append(
        PlanStep(
            tool="code_verify",
            args={
                "proposed": "$proposed",
                "reverify_pattern": params.get("narrow_predicate") or params["pattern"],
                "reverify_path": params["path"],
                "reverify_glob": params.get("glob", ""),
            },
            assign="verify",
        )
    )

    return Plan(steps=steps)


__all__ = [
    "build_fixed_plan",
    "extract_params",
]
