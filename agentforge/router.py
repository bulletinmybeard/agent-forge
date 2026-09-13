"""ProfileRouter — pick intensity (and the composed profile) for a locked capability.

Mode / @prefix already chose the family (coder, agent, …). This hop runs once
per user message on the cheap ``tool`` profile and returns ``coder-light`` /
``coder`` / ``coder-heavy`` (same pattern for other families).

Usage::

    from agentforge.client import AIClient
    from agentforge.router import ProfileRouter

    router = ProfileRouter(AIClient(profile="tool"))
    result = router.select("add a size kwarg", capability="coder")
    print(result.profile)   # "coder-light"
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from chalkbox.logging.bridge import get_logger

from .backends._thinking import strip_inline_think
from .client import AIClient

logger = get_logger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "profile_router.md"
ROUTER_SYSTEM_PROMPT = _PROMPT_PATH.read_text()

INTENSITIES = ("light", "default", "heavy")
_SINGLETONS = {"visual": "vision", "vision": "vision", "embed": "embed"}

MODE_CAPABILITY: dict[str, str] = {
    "coding": "coder",
    "agent": "agent",
    "logs": "agent",
    "sql": "agent",
    "discover": "agent",
    "pipeline": "agent",
    "review": "agent",
    "research": "agent",
    "web_search": "agent",
    "chat": "general",
}

# Legacy allow-list kept so older callers that omit *capability* still parse.
VALID_PROFILES = {
    "fast",
    "default",
    "thinker",
    "agent",
    "agent-light",
    "agent-heavy",
    "vision",
    "coder",
    "coder-light",
    "coder-heavy",
    "worker",
    "worker-light",
    "worker-heavy",
    "reasoner",
    "reasoner-heavy",
    "general",
    "general-light",
    "general-heavy",
}


@dataclass
class RouteResult:
    """The router's decision: which profile to use and why."""

    profile: str
    reason: str

    def __repr__(self) -> str:
        return f"RouteResult(profile={self.profile!r}, reason={self.reason!r})"


def capability_for_mode(mode: str) -> str:
    """Map an execution mode to a capability family."""
    base = mode.split(":", 1)[-1] if mode.startswith("custom:") else mode
    return MODE_CAPABILITY.get(base, "agent")


def intensity_profile_name(capability: str, intensity: str) -> str:
    """Compose the public profile name for *capability* × *intensity*."""
    cap = (capability or "").strip().lower()
    inten = (intensity or "default").strip().lower()
    if cap in _SINGLETONS:
        return _SINGLETONS[cap]
    if inten in ("", "default"):
        return cap
    if inten in ("light", "heavy"):
        return f"{cap}-{inten}"
    return cap


def valid_profiles(capability: str) -> set[str]:
    """Profiles the router may return for a locked *capability*."""
    cap = (capability or "agent").strip().lower()
    if cap in _SINGLETONS:
        return {_SINGLETONS[cap]}
    names = {cap, f"{cap}-light", f"{cap}-heavy"}
    if cap == "agent":
        names.add("vision")
    return names


def _extract_json_object(raw: str) -> dict | None:
    """Parse a JSON object out of model output (fences, CoT, surrounding prose)."""
    if not raw or not str(raw).strip():
        return None
    text, _ = strip_inline_think(str(raw))
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, TypeError):
        pass
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(text[start:], start):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(text[start : i + 1])
                    return data if isinstance(data, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


def _coerce_profile(
    *,
    raw_profile: str,
    raw_intensity: str,
    capability: str,
    allowed: set[str],
) -> str | None:
    """Turn JSON intensity/profile fields into a name in *allowed*, or None."""
    intensity = (raw_intensity or "").strip().lower()
    if intensity in INTENSITIES:
        name = intensity_profile_name(capability, intensity)
        if name in allowed:
            return name
    profile = (raw_profile or "").strip().lower()
    if profile in allowed:
        return profile
    if profile in INTENSITIES:
        name = intensity_profile_name(capability, profile)
        if name in allowed:
            return name
    return None


# Cheap, non-thinking lane for the classifier hop. ``tool`` inherits
# ``general`` (MiniMax + parse_thinking) and routinely returns CoT instead
# of JSON — that's the (router parse failure) we kept hitting in the UI.
ROUTER_PROFILE = "cloud-light"


def _silence_thinking(client: object) -> None:
    """Turn off Ollama/OpenRouter thinking on the router client in place."""
    profile = getattr(client, "profile", None)
    if profile is None:
        return
    extra = dict(getattr(profile, "extra_body", None) or {})
    provider = (getattr(profile, "provider", None) or "ollama").lower()
    if provider == "ollama":
        extra["think"] = False
    elif provider == "openrouter":
        extra["reasoning"] = {"enabled": False}
    else:
        return
    profile.extra_body = extra


def _keyword_profile(raw: str, capability: str, allowed: set[str]) -> str | None:
    """Last-ditch: pick an allowed name / intensity word out of free text."""
    text = (raw or "").lower()
    if not text:
        return None
    for name in sorted(allowed, key=len, reverse=True):
        if name != capability and name in text:
            return name
    for inten in ("heavy", "light", "default"):
        if re.search(rf"\b{inten}\b", text):
            name = intensity_profile_name(capability, inten)
            if name in allowed:
                return name
    return None


class ProfileRouter:
    """Classify a prompt's intensity for a locked capability family."""

    def __init__(self, client: AIClient, *, fallback: str | None = None) -> None:
        self._client = client
        self._fallback = fallback
        _silence_thinking(client)

    def select(self, query: str, *, capability: str | None = None) -> RouteResult:
        """Analyse *query* and return the composed profile for *capability*.

        *capability* is the family already chosen by mode / @prefix (``coder``,
        ``agent``, …). Omitted → ``agent``. Unknown JSON falls back to the
        bare family name (``coder``, not ``coder-heavy``).
        """
        cap = (capability or "agent").strip().lower() or "agent"
        allowed = valid_profiles(cap)
        fallback = self._fallback or (cap if cap in allowed else next(iter(allowed)))
        if fallback not in allowed:
            fallback = cap if cap in allowed else next(iter(allowed))

        user = f"capability: {cap}\nvalid: {', '.join(sorted(allowed))}\n\n{query}"
        messages = [
            {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]

        response = self._client.chat(messages)
        content = response.content or ""
        thinking = getattr(response, "thinking", None) or ""
        data = _extract_json_object(content) or _extract_json_object(thinking)
        if data is None:
            blob = f"{content}\n{thinking}"
            guessed = _keyword_profile(blob, cap, allowed)
            if guessed:
                logger.info("Router keyword-fallback '%s' from non-JSON output", guessed)
                return RouteResult(profile=guessed, reason="(router keyword fallback)")
            logger.warning(
                "Router returned non-JSON: content=%r thinking=%r — using fallback '%s'",
                content[:160],
                thinking[:160],
                fallback,
            )
            return RouteResult(profile=fallback, reason="(router parse failure)")

        reason = str(data.get("reason") or "").strip()
        profile = _coerce_profile(
            raw_profile=str(data.get("profile") or ""),
            raw_intensity=str(data.get("intensity") or ""),
            capability=cap,
            allowed=allowed,
        )
        if profile is None:
            logger.warning(
                "Router picked unknown intensity/profile %s — using fallback '%s'",
                data,
                fallback,
            )
            return RouteResult(profile=fallback, reason=reason or "(unknown profile)")

        logger.debug("Router selected profile '%s': %s", profile, reason)
        return RouteResult(profile=profile, reason=reason)
