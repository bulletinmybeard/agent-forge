"""Botty Analysis Engine — session awareness layer with nudge generation.

Two engine tiers:

1. **BottyHeuristicEngine** (in botty_endpoint.py) — lightweight, rule-based MVP
   that generates nudges from keyword matching and simple heuristics.  No external
   dependencies beyond the database.  Used by default.

2. **BottyLLMEngine** (this module) — full LLM-powered analysis pipeline that
   classifies conversation state, recalls cross-session memory, and generates
   natural-language nudges via the cloud-light AI profile.  Requires the
   ``agentforge.client.AIClient`` package and a running Ollama relay.

The endpoint uses the heuristic engine out of the box.  To upgrade, import
``BottyLLMEngine`` from this module and pass it to the WebSocket handler.

Both engines share the same nudge output format so the frontend doesn't care
which one is active.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from web.server.database.manager import ChatDatabase

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration defaults (can be overridden via settings)
# ---------------------------------------------------------------------------
_DEFAULT_ANALYSIS_INTERVAL = 10  # analyze every N completed runs
_DEFAULT_INTERVENTION_THRESHOLD = 0.6  # score >= 0.6 to intervene
_DEFAULT_MAX_FREQUENCY_SECONDS = 300  # max 1 nudge every 5 minutes
_DEFAULT_DISMISSAL_COOLDOWN_SECONDS = 900  # if dismissed, wait 15 minutes before same pattern


# ---------------------------------------------------------------------------
# Classification result
# ---------------------------------------------------------------------------


@dataclass
class BottyClassification:
    """Result of classifying a conversation's current state."""

    phase: str
    """Current conversation phase: exploring|converging|executing|stuck|reviewing"""

    momentum: str
    """Emotional/cognitive momentum: flowing|slowing|stalled|frustrated"""

    pattern: str
    """Repeating conversation pattern: none|repetition|circling|escalating_errors|brainstorm_overflow|deep_dive"""

    topic_fingerprint: str
    """Hash of the current topic focus — detects topic drift"""

    intervention_score: float
    """0.0-1.0: confidence that an intervention would be helpful"""

    suggested_action: str
    """What Botty should do: stay_quiet|surface_memory|suggest_reframe|propose_decision|offer_summary|warn_complexity"""

    reasoning: str
    """Explanation of the classification (for debugging)"""


# ---------------------------------------------------------------------------
# Botty Engine
# ---------------------------------------------------------------------------


class BottyLLMEngine:
    """LLM-powered session-aware analysis and nudge generation.

    Uses the cloud-light AI profile for conversation classification and
    nudge text generation.  Falls back gracefully if the framework client
    is not available.
    """

    def __init__(
        self,
        db: ChatDatabase,
        session_id: str,
        analysis_interval: int = _DEFAULT_ANALYSIS_INTERVAL,
        intervention_threshold: float = _DEFAULT_INTERVENTION_THRESHOLD,
        max_frequency_seconds: int = _DEFAULT_MAX_FREQUENCY_SECONDS,
        dismissal_cooldown_seconds: int = _DEFAULT_DISMISSAL_COOLDOWN_SECONDS,
    ) -> None:
        """Initialize Botty for a session."""
        self.db = db
        self.session_id = session_id
        self.analysis_interval = analysis_interval
        self.intervention_threshold = intervention_threshold
        self.max_frequency_seconds = max_frequency_seconds
        self.dismissal_cooldown_seconds = dismissal_cooldown_seconds

        self.message_count = 0
        self._last_nudge_time = 0.0
        self._dismissed_patterns: dict[str, float] = {}  # pattern -> dismiss timestamp

    async def on_run_completed(self, event: dict) -> None:
        """Called after each completed run. May trigger analysis at interval."""
        try:
            self.message_count += 1

            # Trigger analysis periodically
            if self.message_count % self.analysis_interval == 0:
                await self.analyse()
        except Exception as exc:
            logger.debug("Botty.on_run_completed error: %s", exc)

    async def analyse(self) -> dict | None:
        """Run full analysis pipeline: classify -> recall -> intervene.

        Returns a nudge dict ready for WebSocket transmission, or None to stay silent.
        """
        try:
            # Get recent messages from DB
            messages = self.db.get_messages(self.session_id)
            if not messages:
                logger.debug("Botty: no messages to analyse")
                return None

            # Classify the conversation state
            classification = await self._classify(messages)
            if classification is None:
                logger.debug("Botty: classification failed or returned None")
                return None

            logger.debug(
                "Botty classification: %s (score=%.2f)",
                classification.suggested_action,
                classification.intervention_score,
            )

            # Skip if score below threshold or suggested action is stay_quiet
            if classification.intervention_score < self.intervention_threshold:
                logger.debug(
                    "Botty: score %.2f below threshold %.2f",
                    classification.intervention_score,
                    self.intervention_threshold,
                )
                return None

            if classification.suggested_action == "stay_quiet":
                logger.debug("Botty: suggested action is stay_quiet")
                return None

            # Check cooldowns
            if not self._should_analyse():
                logger.debug("Botty: frequency cooldown active")
                return None

            if not self._can_nudge(classification.pattern):
                logger.debug("Botty: dismissal cooldown active for pattern %s", classification.pattern)
                return None

            # Recall cross-session context
            recall = await self._recall(classification)

            # Generate nudge
            nudge_text = await self._intervene(classification, recall)
            if nudge_text is None or nudge_text == "SKIP":
                logger.debug("Botty: intervener returned None/SKIP")
                return None

            nudge_id = self._make_nudge_id(self.session_id, nudge_text)
            self._last_nudge_time = time.time()

            nudge = {
                "type": "botty.nudge",
                "nudge_id": nudge_id,
                "pattern": classification.pattern,
                "text": nudge_text,
                "confidence": classification.intervention_score,
            }

            logger.info("Botty nudge sent (pattern=%s, id=%s)", classification.pattern, nudge_id[:8])
            return nudge

        except Exception as exc:
            logger.debug("Botty.analyse error: %s", exc, exc_info=True)
            return None

    async def _classify(self, messages: list) -> BottyClassification | None:
        """Classify the conversation state via LLM."""
        try:
            # Extract text from recent messages
            recent = messages[-20:] if len(messages) > 20 else messages

            msg_lines = []
            for msg in recent:
                role = msg.role.upper()
                text = msg.content or "[no content]"
                # Truncate very long messages
                if len(text) > 300:
                    text = text[:300] + "…"
                msg_lines.append(f"{role}: {text}")

            convo_text = "\n".join(msg_lines)

            prompt = f"""\
Classify this conversation's state. Return ONLY valid JSON (no markdown).

{convo_text}

Return JSON with these fields:
- phase: "exploring" (discovery), "converging" (narrowing focus), "executing" (taking action), "stuck" (blocked), "reviewing" (analysis)
- momentum: "flowing" (energetic), "slowing" (tiring), "stalled" (blocked), "frustrated" (giving up)
- pattern: "none", "repetition" (same q/a twice), "circling" (same ideas rephrased), "escalating_errors" (failures mounting), "brainstorm_overflow" (too many ideas), "deep_dive" (focused investigation)
- intervention_score: 0.0-1.0 (how much this conversation needs help)
- suggested_action: "stay_quiet", "surface_memory" (past similar convos), "suggest_reframe" (different perspective), "propose_decision" (commit to direction), "offer_summary" (recap progress), "warn_complexity" (scope creep alert)
- reasoning: brief explanation

Example:
{{"phase":"converging","momentum":"slowing","pattern":"circling","intervention_score":0.72,"suggested_action":"offer_summary","reasoning":"Same 3 ideas rephrased 4 times; fatigue setting in"}}"""

            # Use AIClient if available, otherwise skip
            from agentforge.client import AIClient

            client = AIClient(profile="cloud-light")
            response = client.chat(
                messages=[
                    {"role": "system", "content": "You are a conversation analyst. Output only valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
            )

            text = response.content.strip() if hasattr(response, "content") else ""
            if not text:
                logger.debug("Botty._classify: empty response")
                return None

            # Strip markdown if present
            if text.startswith("```"):
                text = text.split("\n", 1)[-1]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()

            data = json.loads(text)

            # Compute topic fingerprint from conversation text
            topic_hash = hashlib.md5(convo_text[:1000].encode()).hexdigest()[:16]

            return BottyClassification(
                phase=data.get("phase", "exploring"),
                momentum=data.get("momentum", "flowing"),
                pattern=data.get("pattern", "none"),
                topic_fingerprint=topic_hash,
                intervention_score=float(data.get("intervention_score", 0.5)),
                suggested_action=data.get("suggested_action", "stay_quiet"),
                reasoning=data.get("reasoning", ""),
            )

        except json.JSONDecodeError as exc:
            logger.debug("Botty._classify: JSON decode error: %s", exc)
            return None
        except Exception as exc:
            logger.debug("Botty._classify error: %s", exc)
            return None

    async def _recall(self, classification: BottyClassification) -> dict:
        """Search conversation_memory and user_facts for relevant context."""
        try:
            from web.server.conversation_memory import get_conversation_memory
            from web.server.fact_extraction import get_relevant_facts_for_context

            memories = []
            facts = []

            # Get cross-session conversation memory if available
            cm = get_conversation_memory()
            if cm:
                # Query for similar phase/momentum patterns
                query = f"{classification.phase} {classification.momentum} {classification.pattern}"
                memories = cm.recall(query, top_k=3, exclude_session=self.session_id)
                logger.debug("Botty._recall: found %d conversation memories", len(memories))

            # Get user facts
            facts_list = get_relevant_facts_for_context(self.db, limit=5, min_confidence=0.6)
            if facts_list:
                facts = facts_list
                logger.debug("Botty._recall: found %d fact groups", len(facts_list))

            return {
                "conversation_memories": memories,
                "user_facts": facts,
            }

        except Exception as exc:
            logger.debug("Botty._recall error: %s", exc)
            return {"conversation_memories": [], "user_facts": []}

    async def _intervene(self, classification: BottyClassification, recall: dict) -> str | None:
        """Generate a short nudge message (max 2 sentences)."""
        try:
            # Build context for the LLM
            memories_text = ""
            if recall.get("conversation_memories"):
                mems = recall["conversation_memories"][:2]
                memories_text = "Past similar convos:\n"
                for m in mems:
                    memories_text += f"- Q: {m.get('query', '')[:80]}\n  A: {m.get('response', '')[:80]}\n"

            facts_text = ""
            if recall.get("user_facts"):
                # user_facts is a list of system messages, extract content
                for fact_msg in recall.get("user_facts", []):
                    if isinstance(fact_msg, dict):
                        facts_text += fact_msg.get("content", "")[:200] + "\n"

            prompt = f"""\
The conversation is in a {classification.phase} phase with {classification.momentum} momentum.
Pattern detected: {classification.pattern}
Suggested action: {classification.suggested_action}

{memories_text}
{facts_text}

Generate a SHORT (max 2 sentences) nudge to help the user. The nudge should:
- Be encouraging, not preachy
- Reference past experience or facts if relevant
- Guide toward the suggested action
- End with a question mark if it's a suggestion

If nothing would actually help, return: SKIP

Nudge:"""

            from agentforge.client import AIClient

            client = AIClient(profile="cloud-light")
            response = client.chat(
                messages=[
                    {
                        "role": "system",
                        "content": "You are a helpful session coach. Give short, actionable nudges. Max 2 sentences.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.4,
            )

            text = response.content.strip() if hasattr(response, "content") else ""
            if not text or text == "SKIP" or text == "SKIP\n":
                return "SKIP"

            # Ensure max 2 sentences
            sentences = [s.strip() for s in text.split(".") if s.strip()]
            if len(sentences) > 2:
                text = ".".join(sentences[:2]) + "."
            else:
                text = ".".join(sentences) + "." if sentences else "SKIP"

            return text if text != "." else "SKIP"

        except Exception as exc:
            logger.debug("Botty._intervene error: %s", exc)
            return "SKIP"

    async def search_sessions(self, query: str) -> list[dict]:
        """Global cross-session search triggered by user.

        Searches conversation_memory for past similar exchanges.
        """
        try:
            from web.server.conversation_memory import get_conversation_memory

            cm = get_conversation_memory()
            if not cm:
                logger.debug("Botty.search_sessions: conversation_memory not available")
                return []

            results = cm.recall(query, top_k=10, exclude_session=self.session_id)
            logger.info("Botty search found %d results for query: %s", len(results), query[:50])
            return results

        except Exception as exc:
            logger.debug("Botty.search_sessions error: %s", exc)
            return []

    def _should_analyse(self) -> bool:
        """Check if it's time for a classification pass (respecting frequency limit)."""
        now = time.time()
        elapsed = now - self._last_nudge_time
        return elapsed >= self.max_frequency_seconds

    def _can_nudge(self, pattern: str) -> bool:
        """Check if we can nudge this pattern (respecting dismissal cooldown)."""
        if pattern not in self._dismissed_patterns:
            return True

        now = time.time()
        dismissed_at = self._dismissed_patterns[pattern]
        elapsed = now - dismissed_at
        return elapsed >= self.dismissal_cooldown_seconds

    def on_dismiss(self, nudge_id: str, pattern: str) -> None:
        """Record that user dismissed a nudge of this pattern type."""
        self._dismissed_patterns[pattern] = time.time()
        logger.debug("Botty: nudge dismissed (pattern=%s, id=%s)", pattern, nudge_id[:8])

    # -- Helpers ---------------------------------------------------------------

    @staticmethod
    def _make_nudge_id(session_id: str, text: str) -> str:
        """Deterministic nudge ID from session + text."""
        h = hashlib.md5(f"{session_id}:{text}".encode()).hexdigest()
        return f"nudge-{h[:16]}"
