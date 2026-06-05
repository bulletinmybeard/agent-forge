"""LLM-powered query refinement for semantic search.

Adapted from py-ai-agent-system's GrammarRefiner pattern:
- Same JSON response contract (corrected, has_changes, changes)
- Same robust parsing logic for inconsistent model outputs
- Routed through agentforge AIClient so it follows the active provider
- Combined grammar + domain synonym expansion in one prompt

The refined query is used for embedding; the original query is preserved
for intent detection and display.
"""

import json
import logging
from string import Template

from agentforge.client import AIClient
from app.config import settings

logger = logging.getLogger(__name__)

# Optional domain hint for API query expansion (persona.domain_context in
# config.yaml). Empty by default so published prompts carry no org specifics.
_DOMAIN_CONTEXT = (
    f"\nDomain context:\n{settings.persona.domain_context}\n" if settings.persona.domain_context.strip() else ""
)

# ── Prompt templates ─────────────────────────────────────────────────────────
# Context-aware refinement: the prompt adapts based on source_type so that
# CLI tool queries stay literal while API queries get synonym expansion.

_JSON_CONTRACT = """
Please return your response as JSON:
{{
    "corrected": "the refined query text",
    "has_changes": true or false,
    "changes": ["list of specific changes made"]
}}

If no changes are needed, return the original text with has_changes set to false."""

# ── docs (CLI tools: git, kubectl, docker, etc.) ────────────────────────────
_DOCS_PROMPT = Template(
    """Fix any spelling, grammar, and capitalisation errors in the following CLI documentation search query.  Do NOT add synonyms, alternatives, or reinterpret any technical terms.  CLI commands have exact meanings — preserve them literally.

Query to refine: "${user_prompt}"

Guidelines:
• Fix typos and spelling mistakes only
• Ensure proper capitalisation for technical terms (Git, CLI, API, JSON, etc.)
• Do NOT expand, rephrase, or add alternative terms
• Do NOT change the meaning or scope of the query in any way
• Keep the query as close to the original as possible
"""
    + _JSON_CONTRACT
)

# ── api (OpenAPI / REST API documentation) ───────────────────────────────────
_API_PROMPT = Template(
    """Please correct any grammar, spelling, punctuation, and capitalisation errors in the following search query.  Additionally, expand the query with domain-specific synonyms so it matches better against REST API endpoint descriptions.

Query to refine: "${user_prompt}"

Guidelines:
• Fix spelling mistakes, typos, and grammar errors
• Ensure proper capitalisation for technical terms (API, REST, JSON, OAuth, etc.)
• Expand abbreviations and include common synonyms side by side:
  - "quote" should also include "quotation" (and vice versa)
  - "BOM" should also include "bill of materials"
  - "auth" should also include "authentication" / "authorization"
  - "peering" should also include "interconnect"
  - "customer" should also include "client" / "account"
  - "port" should also include "interface" / "connection"
• If the user mentions a business concept, also include the technical API
  term that typically maps to it (and vice versa)
• Keep the refined query concise — one or two sentences maximum
• Preserve the user's original intent exactly
"""
    + _DOMAIN_CONTEXT
    + _JSON_CONTRACT
)

# ── schema (database schemas) ────────────────────────────────────────────────
_SCHEMA_PROMPT = Template(
    """Fix any spelling, grammar, and capitalisation errors in the following database schema search query.  You may expand common abbreviations but do NOT add unrelated synonyms or reinterpret the query.

Query to refine: "${user_prompt}"

Guidelines:
• Fix typos and spelling mistakes
• Ensure proper capitalisation for technical terms (SQL, VARCHAR, UUID, etc.)
• Expand common abbreviations (e.g., "col" → "column", "tbl" → "table")
• Do NOT add synonyms that change the meaning of the query
• Keep the query concise and close to the original
"""
    + _JSON_CONTRACT
)

# ── fallback (no source context or unknown type) ─────────────────────────────
_DEFAULT_PROMPT = Template(
    """Fix any spelling, grammar, and capitalisation errors in the following search query.  Be conservative — only fix clear errors and do not reinterpret the query.

Query to refine: "${user_prompt}"

Guidelines:
• Fix typos, spelling mistakes, and grammar errors
• Ensure proper capitalisation for technical terms
• Do NOT add synonyms, alternative terms, or expand the query
• Preserve the user's original intent exactly
"""
    + _JSON_CONTRACT
)

# Map source_type values to the appropriate prompt template
_PROMPT_BY_SOURCE_TYPE: dict[str | None, Template] = {
    "docs": _DOCS_PROMPT,
    "api": _API_PROMPT,
    "schema": _SCHEMA_PROMPT,
    None: _DEFAULT_PROMPT,
}


def _parse_response(content, original_query: str) -> dict:
    """Parse LLM response into a standardised result dict.

    Ported from GrammarRefiner.correct() — handles all the weird ways
    models return JSON (nested dicts, stringified JSON, alternative keys,
    missing fields, etc.).
    """
    corrected = original_query
    has_changes = False
    changes: list = []

    if isinstance(content, dict):
        # Alternative format: response/data instead of corrected/changes
        if "response" in content and "data" in content:
            corrected = content.get("response", original_query)
            changes = content.get("data", [])
            has_changes = str(corrected).lower().strip() != original_query.lower().strip()
        else:
            corrected_value = content.get("corrected", original_query)
            # If corrected is itself a dict (nested), extract the text
            if isinstance(corrected_value, dict):
                corrected = corrected_value.get("corrected", original_query)
                has_changes = corrected_value.get("has_changes", False)
                changes = corrected_value.get("changes", [])
            else:
                corrected = corrected_value
                has_changes = content.get("has_changes", False)
                changes = content.get("changes", [])

    elif isinstance(content, str):
        # Strip markdown code fences that some models wrap around JSON
        # (e.g., ```json\n{...}\n```)
        stripped = content.strip()
        if stripped.startswith("```"):
            stripped = stripped.split("\n", 1)[-1]  # drop opening ```json line
            if stripped.endswith("```"):
                stripped = stripped[:-3].strip()
            content = stripped
        try:
            data = json.loads(content)
            if "response" in data and "data" in data:
                corrected = data.get("response", original_query)
                changes = data.get("data", [])
                has_changes = str(corrected).lower().strip() != original_query.lower().strip()
            else:
                corrected = data.get("corrected", original_query)
                has_changes = data.get("has_changes", False)
                changes = data.get("changes", [])
        except Exception:
            corrected = original_query
            has_changes = False
            changes = []

    # Ensure corrected is always a string (not a dict)
    if isinstance(corrected, dict):
        corrected = corrected.get("corrected", str(corrected))
    corrected = str(corrected) if corrected else original_query

    # Handle double-wrapped JSON (some models return a JSON string inside "corrected")
    if isinstance(corrected, str) and corrected.strip().startswith("{") and corrected.strip().endswith("}"):
        try:
            parsed_json = json.loads(corrected)
            if "corrected" in parsed_json:
                corrected = parsed_json["corrected"]
                if "has_changes" in parsed_json:
                    has_changes = parsed_json["has_changes"]
                if "changes" in parsed_json:
                    changes = parsed_json["changes"]
        except json.JSONDecodeError:
            pass

    # Ensure changes is always a list of strings
    if isinstance(changes, list):
        changes = [str(c) for c in changes]

    # Fallback change detection: model didn't set has_changes but text differs
    if not has_changes and corrected and original_query:
        if original_query.strip().lower() != str(corrected).strip().lower():
            has_changes = True
            if not changes:
                changes = ["Text was refined (model didn't specify changes)"]
            logger.debug("Fallback detection: model made changes but didn't set has_changes=true")

    return {
        "corrected": corrected,
        "original": original_query,
        "has_changes": has_changes,
        "changes": changes,
    }


class QueryRefiner:
    """Refines search queries via Ollama /chat for better embedding similarity."""

    def __init__(self) -> None:
        # Route through AIClient so query refinement follows the active provider.
        # Sampling + JSON mode live on the `query-refiner` profile
        # (framework-config.yaml). AIClient exposes both sync (.chat) and async
        # (.achat) entry points on one instance, so the worker-thread fallback
        # below swaps async->sync without a second client.
        self._client = AIClient(profile="query-refiner")
        logger.info(
            "QueryRefiner using profile 'query-refiner' → %s (provider=%s)",
            self._client.model,
            self._client.profile.provider,
        )

    async def refine(
        self,
        query: str,
        source_type: str | None = None,
        source_name: str | None = None,
    ) -> tuple[str, bool]:
        """Refine a user query using the LLM.

        The prompt adapts based on *source_type* so that CLI tool queries
        (``docs``) stay literal while API queries get synonym expansion.
        """
        template = _PROMPT_BY_SOURCE_TYPE.get(source_type, _DEFAULT_PROMPT)
        prompt = template.substitute(user_prompt=query)
        logger.debug(
            "Refinement using %s prompt (source_type=%s, source_name=%s)",
            source_type or "default",
            source_type,
            source_name,
        )

        try:
            response = await self._client.achat(
                messages=[{"role": "user", "content": prompt}],
            )

            content = response.content
            logger.debug("Raw refinement response: %r", content)
            result = _parse_response(content, query)
            logger.debug(
                "Parsed refinement result: has_changes=%s, corrected='%s'",
                result["has_changes"],
                result["corrected"][:200],
            )

            if result["has_changes"]:
                logger.info(
                    "Query refined: '%s' → '%s' (%d changes)", query, result["corrected"], len(result["changes"])
                )
                return result["corrected"], True
            else:
                logger.info("No refinement needed for: '%s'", query)
                return query, False

        except Exception as e:
            # Detect event-loop mismatch errors that occur when refine() is
            # called from a worker thread (e.g., @pipeline search_knowledge_base
            # tool calls run inside asyncio.run() on a fresh thread-local loop).
            # In that case retry synchronously using the shared sync client.
            err_str = str(e).lower()
            if any(tok in err_str for tok in ("loop", "remove(x)", "closed", "bound to")):
                try:
                    template = _PROMPT_BY_SOURCE_TYPE.get(source_type, _DEFAULT_PROMPT)
                    prompt = template.substitute(user_prompt=query)
                    response = self._client.chat(
                        messages=[{"role": "user", "content": prompt}],
                    )
                    content = response.content
                    result = _parse_response(content, query)
                    if result["has_changes"]:
                        logger.info(
                            "Query refined (sync): '%s' → '%s' (%d changes)",
                            query,
                            result["corrected"],
                            len(result["changes"]),
                        )
                        return result["corrected"], True
                    else:
                        logger.info("No refinement needed for (sync): '%s'", query)
                        return query, False
                except Exception as sync_e:
                    logger.warning("Query refinement sync fallback also failed: %s", sync_e)
            else:
                logger.warning("Query refinement failed (falling back to original): %s", e)
            return query, False


query_refiner = QueryRefiner()
