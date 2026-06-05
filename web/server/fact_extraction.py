"""Fact Extraction Service — extract structured facts from conversations.

After each completed agent run, a lightweight LLM pass extracts user preferences,
system details, named entities, and decisions from the query+response pair.
Facts are upserted into the ``user_facts`` SQLite table, deduplicated by key.

The extraction runs asynchronously and is fire-and-forget — errors are logged
but never disrupt the main chat flow.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from web.server.database.manager import ChatDatabase

logger = logging.getLogger(__name__)

# The extraction prompt — kept tight to minimise latency on the cloud-light model.
_EXTRACT_PROMPT = """\
Extract structured facts from this conversation exchange. Return a JSON array of facts.
Each fact should have: "type" (preference|system|entity|decision), "key" (unique snake_case identifier), "value" (concise text), "confidence" (0.0-1.0).

Rules:
- Only extract CONCRETE, SPECIFIC facts — not vague observations
- Preferences: things the user explicitly prefers ("save to ~/Downloads", "use Python 3.12")
- System: technical details about the user's environment (shell, OS, DB host, port, paths)
- Entity: named things the user works with (project names, team members, services)
- Decision: choices the user made ("we'll use PostgreSQL", "deploy to staging first")
- Skip trivial facts like greetings or generic questions
- Dedupe key should be descriptive: "preferred_download_dir", "default_shell", "db_host_production"
- If NO meaningful facts exist, return an empty array: []

Exchange:
User: {query}
Assistant: {response}

Return ONLY the JSON array, no markdown fencing:"""


def extract_and_store_facts(
    db: "ChatDatabase",
    session_id: str,
    query: str,
    response: str,
    *,
    mode: str = "",
    incognito: bool = False,
) -> int:
    """Extract facts from a query+response and store them in the DB.

    Returns the number of facts upserted. Fire-and-forget — catches all
    exceptions.

    Gated by ``memory_policy.should_extract_facts`` — only FULL tier
    modes extract, and never when ``incognito`` is true. A fact whose
    ``value`` contains anything that ``agentforge.secret_redactor`` would
    redact is dropped entirely (not partially stored with a ``[REDACTED]``
    marker — that produces useless facts and obscures the leak).
    """
    from web.server.memory_policy import should_extract_facts

    if not should_extract_facts(mode, incognito=incognito):
        logger.debug(
            "fact_extraction skipped by policy (mode=%r, incognito=%s)",
            mode,
            incognito,
        )
        return 0

    try:
        redactor = None
        try:
            from agentforge.secret_redactor import get_redactor

            redactor = get_redactor()
        except Exception:
            redactor = None  # graceful fallback

        facts = _extract_facts_via_llm(query, response)
        if not facts:
            return 0

        stored = 0
        dropped_for_secrets = 0
        for fact in facts:
            try:
                value = fact["value"]
                key = fact["key"]

                if redactor is not None:
                    redacted = redactor.redact(value).text
                    if redacted != value:
                        dropped_for_secrets += 1
                        logger.debug(
                            "Dropped fact %r — value contained secret material",
                            key,
                        )
                        continue

                db.upsert_fact(
                    fact_type=fact.get("type", "entity"),
                    key=key,
                    value=value,
                    source_session=session_id,
                    confidence=float(fact.get("confidence", 0.7)),
                )
                stored += 1
            except Exception as exc:
                logger.debug("Failed to upsert fact %r: %s", fact.get("key"), exc)

        if stored:
            logger.info("Extracted %d fact(s) from session %s", stored, session_id[:12])
        if dropped_for_secrets:
            logger.info(
                "Dropped %d fact(s) containing secret material (session %s)",
                dropped_for_secrets,
                session_id[:12],
            )
        return stored

    except Exception as exc:
        logger.warning("Fact extraction failed: %s", exc)
        return 0


def get_relevant_facts_for_context(
    db: "ChatDatabase",
    limit: int = 15,
    min_confidence: float = 0.5,
    stale_days: int = 30,
) -> list[dict[str, str]]:
    """Retrieve all facts above confidence threshold, formatted for injection.

    Returns a list with a single system message containing known facts,
    or an empty list if no facts exist.  Facts older than *stale_days*
    are annotated with ``[stale -- verify]`` so the model knows to
    double-check them via tools.
    """
    try:
        facts = db.get_all_facts(min_confidence=min_confidence)
        if not facts:
            return []

        now = datetime.now()

        # Group by type for cleaner presentation
        grouped: dict[str, list[str]] = {}
        for fact in facts[:limit]:
            t = fact.fact_type or "other"
            # Age annotation
            age_days = (now - fact.updated_at).days if fact.updated_at else 0
            if age_days < 1:
                age_label = "today"
            elif age_days < 60:
                age_label = f"{age_days}d ago"
            else:
                age_label = f"{age_days // 30}mo ago"
            stale_marker = "  [stale -- verify]" if age_days > stale_days else ""
            grouped.setdefault(t, []).append(f"- {fact.key}: {fact.value} ({age_label}){stale_marker}")

        lines = [
            "[Known facts about the user and their environment]",
            "Facts may be outdated. Always verify via tools before acting on entries marked [stale].",
        ]
        type_labels = {
            "preference": "User Preferences",
            "system": "System Details",
            "entity": "Named Entities",
            "decision": "Past Decisions",
        }
        for fact_type, items in grouped.items():
            label = type_labels.get(fact_type, fact_type.title())
            lines.append(f"\n{label}:")
            lines.extend(items)

        return [{"role": "system", "content": "\n".join(lines)}]

    except Exception as exc:
        logger.debug("Failed to load facts for context: %s", exc)
        return []


def _extract_facts_via_llm(query: str, response: str) -> list[dict]:
    """Call the LLM to extract structured facts from an exchange.

    Uses the cloud-light profile for speed. Returns parsed JSON array or [].

    The LLM call is wrapped in ``retry_call`` so transient 5xx / network
    hiccups don't silently drop facts that would otherwise be preserved
    across sessions.
    """
    from agentforge.backends._retry import retry_call
    from agentforge.client import AIClient

    prompt = _EXTRACT_PROMPT.format(
        query=query[:600],
        response=response[:1200],
    )

    client = AIClient(profile="cloud-light")

    def _call():
        return client.chat(
            messages=[
                {"role": "system", "content": "You are a precise fact extractor. Output only valid JSON."},
                {"role": "user", "content": prompt},
            ],
        )

    try:
        result = retry_call(_call, max_attempts=3, context="fact-extraction")
    except Exception as exc:
        logger.debug("Fact extraction failed after retries: %s", exc)
        return []

    text = result.content.strip() if hasattr(result, "content") else ""
    if not text:
        return []

    # Strip markdown code fencing if present
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            # Validate each fact has required fields
            return [f for f in parsed if isinstance(f, dict) and "key" in f and "value" in f]
        return []
    except json.JSONDecodeError:
        logger.debug("Fact extraction returned invalid JSON: %s", text[:200])
        return []
