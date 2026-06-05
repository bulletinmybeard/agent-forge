"""Optional opening-prompt refinement for the Prompt Lab + agent endpoints.

Rewrites the user's initial prompt for clarity/grammar/facts before the model
runs, using the ``prompt_refinement.profile`` LLM (default ``input-refiner``).
Gated by ``prompt_refinement.enabled`` in config.yaml.

Never raises: when refinement is disabled, the input is blank, the refiner
returns nothing, or the backend errors, the original prompt is returned
unchanged — a refiner hiccup must never break a prompt run.

Distinct from ``app/services/query_refiner.py``, which refines the *search
query* for embedding in RAG/search mode.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from agentforge.client import AIClient
from agentforge.steps.refiner import DEFAULT_REFINE_PROMPT
from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RefineResult:
    original: str
    refined: str
    changed: bool


def is_prompt_refinement_enabled() -> bool:
    """Whether opening-prompt refinement is turned on in config."""
    return bool(settings.prompt_refinement.enabled)


async def refine_prompt(text: str) -> RefineResult:
    """Refine *text* via the configured prompt-refiner profile.

    Returns the original unchanged when refinement is disabled, the input is
    blank, the refiner returns nothing, or the backend errors.
    """
    original = text or ""
    if not is_prompt_refinement_enabled() or not original.strip():
        return RefineResult(original=original, refined=original, changed=False)

    try:
        client = AIClient(profile=settings.prompt_refinement.profile)
        response = await client.achat(
            [
                {"role": "system", "content": DEFAULT_REFINE_PROMPT},
                {"role": "user", "content": original},
            ],
            stream=False,
        )
        refined = (response.content or "").strip()
    except Exception as exc:  # noqa: BLE001 — never break a run on a refiner error
        logger.warning("prompt refinement failed, using original: %s", exc)
        return RefineResult(original=original, refined=original, changed=False)

    if not refined:
        logger.info("prompt refiner returned empty — using original")
        return RefineResult(original=original, refined=original, changed=False)

    changed = refined != original.strip()
    if changed:
        logger.info("refined prompt: %s -> %s", original[:80], refined[:80])
    return RefineResult(original=original, refined=refined, changed=changed)
