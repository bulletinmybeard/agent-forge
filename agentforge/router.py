"""ProfileRouter — fast LLM call to select the best profile for a given task.

Uses a lightweight model to classify the user's prompt and pick the most
suitable AI profile (model + parameters) before the real work begins.

Usage::

    from agentforge.client import AIClient
    from agentforge.router import ProfileRouter

    router_client = AIClient(profile="tool")   # fast, cheap model
    router = ProfileRouter(router_client)

    result = router.select("list all Python files in my home directory")
    print(result.profile)   # "fast"
    print(result.reason)    # "Simple directory listing — no deep reasoning needed"
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from chalkbox.logging.bridge import get_logger

from .client import AIClient

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Router system prompt
# ---------------------------------------------------------------------------

_PROMPT_PATH = Path(__file__).parent / "prompts" / "profile_router.md"
ROUTER_SYSTEM_PROMPT = _PROMPT_PATH.read_text()


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class RouteResult:
    """The router's decision: which profile to use and why."""

    profile: str
    reason: str

    def __repr__(self) -> str:
        return f"RouteResult(profile={self.profile!r}, reason={self.reason!r})"


# ---------------------------------------------------------------------------
# ProfileRouter
# ---------------------------------------------------------------------------

VALID_PROFILES = {"fast", "default", "thinker", "agent", "vision"}


class ProfileRouter:
    """Use a fast model to classify a prompt and select the best AI profile."""

    def __init__(self, client: AIClient, *, fallback: str = "default") -> None:
        self._client = client
        self._fallback = fallback

    def select(self, query: str) -> RouteResult:
        """Analyse *query* and return the recommended profile.

        Makes a single, non-streaming chat call.  If the model's response
        cannot be parsed or contains an unknown profile name, the *fallback*
        profile is returned.
        """
        messages = [
            {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
            {"role": "user", "content": query},
        ]

        response = self._client.chat(messages)
        raw = response.content.strip()

        # --- strip markdown code fences (```json ... ```) ---------------
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        raw = raw.strip()

        # --- parse JSON -------------------------------------------------
        try:
            data = json.loads(raw)
            profile = data.get("profile", "").strip().lower()
            reason = data.get("reason", "").strip()
        except (json.JSONDecodeError, AttributeError):
            logger.warning("Router returned non-JSON: %s — using fallback '%s'", raw[:120], self._fallback)
            return RouteResult(profile=self._fallback, reason="(router parse failure)")

        # --- validate profile name --------------------------------------
        if profile not in VALID_PROFILES:
            logger.warning("Router picked unknown profile '%s' — using fallback '%s'", profile, self._fallback)
            return RouteResult(profile=self._fallback, reason=reason or "(unknown profile)")

        logger.debug("Router selected profile '%s': %s", profile, reason)
        return RouteResult(profile=profile, reason=reason)
