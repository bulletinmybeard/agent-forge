"""LLM-powered response refinement for search results.

Takes the user's original query and the top search result payloads,
feeds them into Ollama /chat as context, and generates a proper
conversational answer — including payload details, curl examples,
and relevant notes from the API documentation.

Uses a system prompt registry so each source_type gets its own
tailored prompt. Falls back to a generic prompt for unknown types
or mixed-source results.
"""

import logging
import re
from collections import Counter
from collections.abc import AsyncGenerator
from string import Template

from agentforge.client import AIClient
from app.config import settings

logger = logging.getLogger(__name__)


def _stream_token_usage(raw: object) -> tuple[int, int]:
    """Extract (prompt_tokens, completion_tokens) from a stream chunk's raw
    backend object. Ollama exposes ``prompt_eval_count`` / ``eval_count``;
    other providers may differ, so this is best-effort and returns 0/0 when
    the fields aren't present."""
    if raw is None:
        return 0, 0

    def _read(key: str) -> int:
        v = getattr(raw, key, None)
        if v is None and hasattr(raw, "get"):
            v = raw.get(key)
        return int(v) if v else 0

    return _read("prompt_eval_count"), _read("eval_count")


# Assistant identity for the answer prompts — configurable (persona.name /
# persona.team in config.yaml) so the published prompts carry no org name.
_PERSONA = f"You are {settings.persona.name}, an AI assistant for {settings.persona.team}."


# ── OpenAPI system prompt ────────────────────────────────────────────────────

OPENAPI_SYSTEM_PROMPT_BASE = (
    _PERSONA
    + """  Your job is to
answer questions about your internal REST APIs based on the search
results provided below.

Rules:
1. Answer the user's question directly in clear, natural sentences.
2. If the search results contain endpoints that answer the question, describe
   them — explain what they do and which parameters they accept.
3. If the search results do NOT contain a good answer, say so honestly.
   Do not invent endpoints or parameters that aren't in the context.
4. When listing parameters, mention whether they are required or optional,
   and include the data type.
5. Keep your answer concise but complete.
6. Do not repeat the raw JSON payload back verbatim — summarise it in a
   readable way.
7. If multiple endpoints are relevant, cover each one briefly.
8. Use markdown formatting for readability (headers, code blocks, bold for
   parameter names).
9. Be strict about relevance. Only include an endpoint in your answer if it
   genuinely matches what the user asked for. Do NOT stretch or reinterpret
   field names — for example, if the user asks for endpoints with an
   "opportunity ID" in the path, only include endpoints whose path literally
   contains {opportunity_id}, not {account_id} or {salesforce_account_id}.
   When in doubt, leave it out.
10. If only one or a few results truly match, say so. Do not pad your answer
    with loosely related endpoints just to make the list longer.
11. Respect the user's requested output format. If they ask to "only list
    URLs", give a simple bullet list of URLs — no descriptions, parameters,
    or curl examples. If they ask for details, give details. Match the level
    of detail the user is asking for.
"""
)

OPENAPI_EXAMPLES_ADDENDUM = """
12. Include a curl example for each relevant endpoint. Use short code blocks
    for curl examples and payload samples. For curl examples, ALWAYS use
    "https://api.example.com" as the base URL — never guess or invent a
    real domain name (e.g., do NOT use api.salesforce.com, api.domain.net, etc.).
"""

OPENAPI_NO_EXAMPLES_ADDENDUM = """
12. Do NOT include curl examples unless the user explicitly asks for them.
    Focus on describing the endpoints, their purpose, and their parameters.
"""

# ── Brief mode addendum (appended to ANY source-type prompt) ────────────────
# Activated by the --brief flag. Overrides the default verbosity with a
# strong conciseness instruction that the model should respect regardless
# of how the question was phrased.
BRIEF_MODE_ADDENDUM = """

IMPORTANT — CONCISE MODE:
Respond with the shortest useful answer. A few sentences at most.
• Give only the direct answer — no background, no alternatives, no suggestions.
• For commands: give the command and nothing else unless clarification is essential.
• No bullet-point lists unless the user explicitly asks for one.
• No examples, no "see also", no follow-up recommendations.
• If the answer is a single value, name, or command — just state it.
"""


# ── SQL Schema system prompt ────────────────────────────────────────────────

SQL_SCHEMA_SYSTEM_PROMPT = (
    _PERSONA
    + """  Answer questions
about internal database schemas using ONLY the search results below.

CRITICAL — JOIN SAFETY:
The search results include a "VALID JOINS" reference block at the top. This block
lists EVERY allowed join condition with data types. When writing SQL with JOINs:
  • ONLY use joins listed in the VALID JOINS block. If a join is not listed, it
    does NOT exist — do not invent it, even if it "makes sense" logically.
  • Check data types: do NOT join columns of incompatible types (e.g., INTEGER ≠ UUID).
  • If the VALID JOINS block is absent or empty, do NOT write any JOINs — only
    query single tables.

Rules:
1. ONLY use column names from the "Column names:" line of the SAME table.
   Each result section has a "Column names:" line listing every visible column.
   Before writing `table.column` in SQL, find that table's result section and
   confirm the column appears in its "Column names:" line. If it does not, do
   NOT use it. A column existing on one table does NOT mean it exists on another.
   THIS IS CRITICAL: inventing a column causes runtime SQL errors. When in doubt,
   say "additional columns may exist — verify with: SELECT * FROM <table> LIMIT 1".
2. Each result shows its database. Tables on different databases CANNOT be
   joined — write a separate query for each and say which database to run it on.
3. Use plain table names (e.g., "pricing_order"), never with a database prefix.
4. Use markdown. Be concise but complete.
5. If the results don't answer the question, say so honestly.
6. When the user asks to "trace", "show the full chain", or "full breakdown",
   write a single JOIN query that follows the foreign key relationships shown
   in the VALID JOINS block — do not split into separate queries.
7. Before writing a JOIN, verify that BOTH sides of the join condition appear
   in the VALID JOINS block with compatible data types. If they don't, say the
   join cannot be verified and suggest the user check the schema.
8. When writing SQL, add a comment next to any table whose full column list
   is NOT visible in the results, e.g.,: "-- columns not fully visible, verify schema".
9. ONLY use foreign key relationships from the VALID JOINS block. Do NOT infer
   relationships based on column names or logical assumptions (e.g., do not assume
   "reference_id" links to auth_user just because it sounds plausible).
10. Before outputting SQL, mentally verify: (a) every table alias in SELECT
    is defined in FROM/JOIN, (b) every join condition matches a line in the
    VALID JOINS block, (c) data types on both sides are compatible,
    (d) every column in SELECT/WHERE exists in the TABLE COLUMNS list for
    THAT specific table (e.g., "display_name" on table X ≠ "display_name" on table Y).
"""
)

SQL_DIALECT_MYSQL = """
SQL dialect: Target MySQL 5.7 compatibility. Do NOT use CTEs (WITH ... AS),
window functions, JSON_TABLE, or lateral joins. Use standard SQL.
"""

SQL_DIALECT_POSTGRES = """
SQL dialect: Target PostgreSQL. CTEs (WITH ... AS), window functions, JSONB
operators, and lateral joins are all available.
Do NOT use MySQL-specific functions. In particular:
  • Use STRING_AGG(col, ', ') instead of GROUP_CONCAT(col)
  • Use TRUE/FALSE instead of 1/0 for booleans
  • Use LIMIT ... OFFSET instead of LIMIT n, m
"""

SQL_DIALECT_MIXED = """
SQL dialect: Results span multiple database engines. Write separate queries per
database. Check the "Database: <name> (<engine>)" header in each result to
determine the correct SQL dialect (MySQL 5.7 or PostgreSQL).
"""


CODE_SYSTEM_PROMPT = (
    _PERSONA
    + """  Your job is to
answer questions about your Python/Django codebase based on the search
results provided below.

Rules:
1. Answer the user's question directly in clear, natural sentences.
2. When source code snippets are provided, use them to give concrete,
   accurate answers about implementation details. Show relevant code in
   fenced code blocks.
3. When usage sites are provided, explain where and how the code is used.
   If no usage sites are found for a symbol, note that it may be unused
   (potential dead code) but recommend manual verification.
4. If the search results do NOT contain enough information, say so honestly.
   Do not invent classes, functions, or implementation details.
5. Use markdown formatting for readability (headers, code blocks, bold for
   class/function names).
6. Be strict about relevance. Only include classes, functions, or modules
   that genuinely match what the user asked for.
7. When explaining how something works, trace the flow through the relevant
   classes and methods. Reference file paths so the developer can navigate
   to the code.
8. When the user asks about implementing or extending functionality, base
   your suggestions on the actual patterns visible in the source code.
"""
)


GENERIC_SYSTEM_PROMPT = (
    _PERSONA
    + """  Your job is to
answer questions about your internal technical documentation based on
the search results provided below.

Rules:
1. Answer the user's question directly in clear, natural sentences.
2. If the search results contain information that answers the question,
   explain it clearly and completely.
3. If the search results do NOT contain a good answer, say so honestly.
   Do not invent information that isn't in the context.
4. Keep your answer concise but complete.
5. Use markdown formatting for readability (headers, code blocks, bold for
   key terms).
6. Be strict about relevance. Only include information that genuinely matches
   what the user asked for. When in doubt, leave it out.
7. Respect the user's requested output format. Match the level of detail
   the user is asking for.
"""
)


DOCUMENT_SYSTEM_PROMPT = (
    _PERSONA
    + """  Your job is to
answer questions about your internal documentation (changelogs, READMEs,
guides, release notes) based on the search results provided below.

Rules:
1. Answer the user's question directly in clear, natural sentences.
2. If the search results contain documentation sections that answer the question,
   explain them clearly — reference the document name and section when helpful.
3. If the search results do NOT contain a good answer, say so honestly.
   Do not invent information that isn't in the context.
4. For changelog/release-notes queries, present changes in a structured way —
   group by version if multiple versions are relevant.
5. Keep your answer concise but complete.
6. Use markdown formatting for readability (headers, code blocks, bold for
   key terms).
7. Be strict about relevance. Only include information that genuinely matches
   what the user asked for. When in doubt, leave it out.
8. Respect the user's requested output format. Match the level of detail
   the user is asking for.
"""
)


GENERAL_KNOWLEDGE_SYSTEM_PROMPT = (
    _PERSONA
    + """

The user asked a question but **no relevant results were found** in the indexed
internal documentation.  You may answer using your general technical knowledge
and any prior conversation context, but you MUST follow these rules:

Rules:
1. **State clearly** that no matching internal documentation was found.
2. Do NOT fabricate, guess, or speculate about internal projects,
   tools, changelogs, features, or release history.  You do not know what
   these internal systems contain — do not pretend otherwise.
3. If the question is about specific internal projects (e.g., your internal tools,   portals, or APIs), say you found no data and
   suggest the user try a narrower or per-project query.
4. You MAY offer general technical guidance that does not require internal
   knowledge (e.g., explaining a git concept, a Kubernetes pattern).
5. If prior conversation messages provide useful context, build on them.
6. Keep your answer concise.  Use markdown formatting for readability.
"""
)


# Each source_type maps to a callable that takes (include_examples: bool)
# and returns the full system prompt string.


def _openapi_prompt(include_examples: bool) -> str:
    return OPENAPI_SYSTEM_PROMPT_BASE + (
        OPENAPI_EXAMPLES_ADDENDUM if include_examples else OPENAPI_NO_EXAMPLES_ADDENDUM
    )


def _detect_sql_engines(results: list[dict]) -> set[str]:
    """Extract database engine names (mysql, postgres, ...) from sql-schema results.

    The chunk text starts with 'Database: <name> (<engine>) — Table: ...'.
    """
    engines: set[str] = set()

    pattern = re.compile(r"Database:\s+\w+\s+\((\w+)\)")
    for r in results:
        text = r.get("payload", {}).get("text") or r.get("text", "")
        m = pattern.search(text)
        if m:
            engines.add(m.group(1).lower())
    return engines


def _sql_dialect_addendum(results: list[dict]) -> str:
    """Return the SQL dialect guidance based on detected database engines."""
    engines = _detect_sql_engines(results)
    if not engines:
        return SQL_DIALECT_MYSQL  # safe default
    if engines == {"postgres"}:
        return SQL_DIALECT_POSTGRES
    if engines == {"mysql"}:
        return SQL_DIALECT_MYSQL
    return SQL_DIALECT_MIXED  # multiple engines in one query


def _sql_schema_prompt(include_examples: bool) -> str:
    return SQL_SCHEMA_SYSTEM_PROMPT


def _code_prompt(include_examples: bool) -> str:
    return CODE_SYSTEM_PROMPT


def _generic_prompt(include_examples: bool) -> str:
    return GENERIC_SYSTEM_PROMPT


def _document_prompt(include_examples: bool) -> str:
    return DOCUMENT_SYSTEM_PROMPT


SYSTEM_PROMPT_REGISTRY: dict[str, callable] = {
    "openapi": _openapi_prompt,
    "sql-schema": _sql_schema_prompt,
    "code": _code_prompt,
    "document": _document_prompt,
}

DEFAULT_PROMPT_BUILDER = _generic_prompt


USER_PROMPT = Template("""Question: ${query}

Here are the most relevant documentation results I found:

${context}

Please answer the question based on these results.""")


# ── Context builder ──────────────────────────────────────────────────────────


def _common_column_prefix(names: list[str]) -> str:
    """Return the shared prefix of column names if it's meaningful.

    Many tables use a naming convention where every column starts with
    the table name (e.g., ``sales_order_id``, ``sales_order_owner_name``).
    Detecting this prefix lets the Column names summary show short names
    (``id``, ``owner_name``) which are easier for the LLM to match
    against natural-language terms in the user's question.

    Returns the prefix (including trailing ``_``) when ≥ 80 % of columns
    share it and it is at least 4 characters long.  Returns ``""``
    otherwise.
    """
    if len(names) < 3:
        return ""
    # Use the shortest name's prefix candidates
    first = min(names, key=len)
    # Try progressively shorter prefixes ending with "_"
    for i in range(len(first), 3, -1):
        candidate = first[:i]
        if not candidate.endswith("_"):
            continue
        matches = sum(1 for n in names if n.startswith(candidate))
        if matches / len(names) >= 0.8:
            return candidate
    return ""


def _trim_sql_text(text: str) -> str:
    """Strip noise from sql-schema chunk text and inject a column summary.

    Three transformations to maximise signal for the LLM:

    1. **Column name summary** — a single ``Column names: ...`` line is
       injected right after the table header so the LLM sees every column
       name before the detailed definitions.  This directly supports
       system-prompt rule 1 ("use column names from the Column names
       section").

    2. **Default value stripping** — ``[default: ...]`` annotations are
       removed from column lines.  They are long (especially the
       ``nextval(...)`` sequences) and not useful for query generation.

    3. **Indexes / Constraints stripping** — these sections duplicate
       information already present in the Columns and Relationships
       sections.  Removing them frees token budget.
    """
    out_lines: list[str] = []
    column_names: list[str] = []
    skip = False
    in_columns = False

    for line in text.splitlines():
        # Skip entire Indexes(...) and Constraints(...) sections
        if line.startswith("Indexes (") or line.startswith("Constraints ("):
            skip = True
            continue
        if skip:
            if line and not line.startswith("  "):
                skip = False
            else:
                continue

        # Track the Columns section to collect names and strip defaults
        if line.startswith("Columns ("):
            in_columns = True
            out_lines.append(line)
            continue

        if in_columns:
            if line.startswith("  - "):
                # Extract column name: everything between "  - " and " ("
                col_name = line[4:].split(" (")[0].strip()
                column_names.append(col_name)
                # Strip [default: ...] noise but keep [auto_increment]
                line = re.sub(r"\s*\[default:[^\]]*\]", "", line)
            elif line == "":
                # Blank line separating Columns from next section
                in_columns = False
            else:
                in_columns = False

        out_lines.append(line)

    # Inject column-name summary right after the header line
    # (line 0 = "Database: ... — Table: ...", line 1 = blank)
    #
    # When columns share a long common prefix (e.g., "sales_order_" on the
    # sales_order table), strip it in the summary so the LLM can match
    # user terms like "owner" to "owner_name" more easily.  The prefix is
    # noted so the model can reconstruct full column names for SQL.
    if column_names and len(out_lines) > 1:
        # NOTE: prefix-stripping was tried here (showing short names like
        # "owner_name" instead of "sales_order_owner_name") but it caused
        # the 7B model to use the short names directly in SQL, breaking
        # every query.  Keep full column names in the summary.
        summary = f"Column names: {', '.join(column_names)}"
        out_lines.insert(1, summary)

    return "\n".join(out_lines)


# Maximum characters per individual chunk text block.
MAX_TEXT_CHARS = 1500


def _build_result_section(result: dict, index: int) -> str:
    """Build a single result section string from a search result payload.

    For sql-schema chunks the text field already contains database name,
    table name, columns, and relationships — so we skip the duplicate
    structured metadata fields and only add a minimal header.  This
    keeps the context compact and avoids confusing the LLM with
    redundant information.
    """
    payload = result.get("payload", {})
    score = result.get("score", 0)
    source_type = payload.get("source_type", "")

    lines = [f"--- Result {index} (score: {score:.2f}) ---"]

    # ── SQL Schema chunks: lean output, let the text speak ──────────
    if source_type == "sql-schema":
        # Only add comment if present (not in the text block)
        if payload.get("table_comment"):
            lines.append(f"Comment: {payload['table_comment']}")

        text = payload.get("text") or result.get("text", "")
        if text:
            text = _trim_sql_text(text)
            if len(text) > MAX_TEXT_CHARS:
                text = text[:MAX_TEXT_CHARS] + "\n[... truncated]"
            lines.append(text)

        return "\n".join(lines)

    # ── Code chunks: class/function/module metadata + enrichment ────
    if source_type == "code":
        if payload.get("file_path"):
            lines.append(f"File: {payload['file_path']}")
        if payload.get("line_number"):
            lines.append(f"Line: {payload['line_number']}")

        chunk_type = payload.get("chunk_type", "")
        if chunk_type == "code_class":
            tag_label = payload.get("tag", "class")
            lines.append(f"Class: {payload.get('class_name', '')} ({tag_label})")
            if payload.get("bases"):
                lines.append(f"Extends: {', '.join(payload['bases'])}")
            if payload.get("method_names"):
                lines.append(f"Methods: {', '.join(payload['method_names'])}")
        elif chunk_type == "code_function":
            lines.append(f"Function: {payload.get('function_name', '')}")
            if payload.get("signature"):
                lines.append(f"Signature: {payload['signature']}")
        elif chunk_type == "code_module":
            lines.append(f"Module: {payload.get('module_name', '')}")

        # The indexed NL text (always present).
        text = payload.get("text") or result.get("text", "")
        if text:
            if len(text) > MAX_TEXT_CHARS:
                text = text[:MAX_TEXT_CHARS] + "\n[... truncated]"
            lines.append(f"Description:\n{text}")

        # Enriched source snippet (added by code_context_service).
        snippet = payload.get("_source_snippet")
        if snippet:
            lines.append(f"Source code:\n```python\n{snippet}\n```")

        # Enriched usage sites (added by code_context_service).
        usages = payload.get("_usage_sites")
        if usages:
            lines.append(f"Used in:\n{usages}")
        elif payload.get("class_name") or payload.get("function_name"):
            # Signal absence — helpful for dead code detection queries.
            lines.append("Used in: (no usages found in project)")

        return "\n".join(lines)

    # ── Document chunks: section-oriented, compact ───────────────────
    if source_type == "document":
        if payload.get("document_name"):
            doc_label = payload["document_name"]
            if payload.get("document_type") and payload["document_type"] != "general":
                doc_label += f" ({payload['document_type']})"
            lines.append(f"Document: {doc_label}")

        if payload.get("section_title"):
            lines.append(f"Section: {payload['section_title']}")

        if payload.get("source_name"):
            lines.append(f"Source: {payload['source_name']}")

        text = payload.get("text") or result.get("text", "")
        if text:
            if len(text) > MAX_TEXT_CHARS:
                text = text[:MAX_TEXT_CHARS] + "\n[... truncated]"
            lines.append(text)

        return "\n".join(lines)

    # ── OpenAPI / other chunks: full metadata ───────────────────────
    if payload.get("source_name"):
        lines.append(f"Source: {payload['source_name']}")

    if payload.get("path"):
        method = payload.get("method", "")
        lines.append(f"Endpoint: {method} {payload['path']}")
    if payload.get("summary"):
        lines.append(f"Summary: {payload['summary']}")
    if payload.get("api_name"):
        lines.append(f"API: {payload['api_name']}")
    if payload.get("domain_group"):
        lines.append(f"Domain: {payload['domain_group']}")
    if payload.get("action_type"):
        lines.append(f"Action: {payload['action_type']}")

    # Parameters
    if payload.get("parameters"):
        lines.append(f"Parameters: {payload['parameters']}")
    if payload.get("request_body"):
        lines.append(f"Request body: {payload['request_body']}")
    if payload.get("response_schema"):
        lines.append(f"Response: {payload['response_schema']}")

    # Security / tags
    if payload.get("security"):
        lines.append(f"Security: {payload['security']}")
    if payload.get("tags"):
        lines.append(f"Tags: {payload['tags']}")

    # OpenAPI schema chunks
    if payload.get("schema_name"):
        lines.append(f"Schema: {payload['schema_name']}")
    if payload.get("schema_fields"):
        lines.append(f"Fields: {payload['schema_fields']}")

    # The full text chunk (contains the richest info).
    # Cap individual chunk text so that one oversized result (e.g., a
    # database summary listing 180 tables) can't monopolise the budget.
    text = payload.get("text") or result.get("text", "")
    if text:
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS] + "\n[... truncated]"
        lines.append(f"Documentation:\n{text}")

    return "\n".join(lines)


def _prioritise_results(results: list[dict]) -> list[dict]:
    """Re-order results so specific chunks come before broad overview chunks.

    Specific chunk types (table, endpoint, schema) contain the columns, FK
    relationships, and parameters that the LLM needs for detailed answers.
    Broad overview chunks (database_summary, relationship_map, api_summary)
    are useful for high-level questions but actively mislead the model when
    it needs to write SQL — they can cause it to mis-attribute tables to the
    wrong database.

    Within each priority tier the original relevance order is preserved.
    """
    LOW_PRIORITY_TYPES = {"database_summary", "relationship_map", "api_summary", "document_summary"}

    specific: list[dict] = []
    broad: list[dict] = []

    for r in results:
        chunk_type = r.get("payload", {}).get("chunk_type", "")
        if chunk_type in LOW_PRIORITY_TYPES:
            broad.append(r)
        else:
            specific.append(r)

    if broad:
        logger.debug(
            "Chunk prioritisation: %d specific + %d broad overview chunks",
            len(specific),
            len(broad),
        )

    return specific + broad


def _extract_valid_joins(results: list[dict]) -> str:
    """Build a VALID JOINS reference block from sql-schema results.

    Parses Relationships and Columns sections from each sql-schema chunk to
    produce a compact lookup table of allowed join conditions with data types.
    This gives the LLM an explicit, easy-to-scan list of joins it may use,
    preventing hallucinated joins on columns that don't actually relate.

    Returns an empty string when no sql-schema results or no relationships
    are found.
    """
    # Only process sql-schema results
    sql_results = [r for r in results if r.get("payload", {}).get("source_type") == "sql-schema"]
    if not sql_results:
        return ""

    # Build a column type lookup → { "table_name.column_name": "TYPE" }
    col_type_map: dict[str, str] = {}
    col_pattern = re.compile(r"^\s+-\s+(\w+)\s+\(([^,)]+)")  # "  - col_name (TYPE, ...)"

    for r in sql_results:
        text = r.get("payload", {}).get("text") or r.get("text", "")
        table_name = r.get("payload", {}).get("table_name", "")
        if not table_name or not text:
            continue
        in_columns = False
        for line in text.splitlines():
            if line.startswith("Columns ("):
                in_columns = True
                continue
            if in_columns:
                m = col_pattern.match(line)
                if m:
                    col_name = m.group(1)
                    col_type = m.group(2).strip()
                    col_type_map[f"{table_name}.{col_name}"] = col_type
                elif not line.startswith("  "):
                    in_columns = False

    # Extract relationship lines → "src_table(src_col) → dst_table(dst_col)"
    rel_pattern = re.compile(r"(\w+)\((\w+)\)\s*→\s*(\w+)\((\w+)\)")
    joins: list[str] = []
    seen_joins: set[str] = set()

    for r in sql_results:
        text = r.get("payload", {}).get("text") or r.get("text", "")
        if not text:
            continue
        in_rels = False
        for line in text.splitlines():
            if line.strip().startswith("Relationships"):
                in_rels = True
                continue
            if in_rels:
                if not line.startswith("  "):
                    in_rels = False
                    continue
                m = rel_pattern.search(line)
                if m:
                    src_tbl, src_col, dst_tbl, dst_col = m.groups()
                    join_key = f"{src_tbl}.{src_col}={dst_tbl}.{dst_col}"
                    if join_key not in seen_joins:
                        seen_joins.add(join_key)
                        src_type = col_type_map.get(f"{src_tbl}.{src_col}", "?")
                        dst_type = col_type_map.get(f"{dst_tbl}.{dst_col}", "?")
                        joins.append(f"  {src_tbl}.{src_col} ({src_type}) → {dst_tbl}.{dst_col} ({dst_type})")

    if not joins:
        return ""

    # Build per-table column summary for tables with visible columns.
    # This puts column names right next to the join reference so the LLM
    # doesn't have to hunt through result sections.
    table_columns: dict[str, list[str]] = {}
    for key in col_type_map:
        tbl, col = key.rsplit(".", 1)
        table_columns.setdefault(tbl, []).append(col)

    col_block = ""
    if table_columns:
        col_lines = ["", "─── TABLE COLUMNS (use ONLY these in SELECT/WHERE) ───"]
        for tbl in sorted(table_columns):
            cols = table_columns[tbl]
            col_lines.append(f"  {tbl}: {', '.join(cols)}")
        col_block = "\n".join(col_lines)

    header = (
        "═══ VALID JOINS (use ONLY these — do NOT invent joins) ═══\n"
        "Each line shows: fk_table.fk_column (TYPE) → pk_table.pk_column (TYPE)\n"
        "If a join is NOT listed here, it does NOT exist. Do not assume it.\n"
    )
    return header + "\n".join(joins) + col_block + "\n═══ END VALID JOINS ═══"


def _build_context(results: list[dict], max_chars: int = 0) -> str:
    """Build a context string from search result payloads."""
    # NOTE: chunk prioritisation is available but currently disabled.
    # Uncomment the line below to push database_summary / relationship_map
    # / api_summary chunks to the end of the context so the LLM reads
    # specific table/endpoint metadata first.
    # results = _prioritise_results(results)

    # For sql-schema results, prepend a VALID JOINS reference block so the
    # LLM has a single, easy-to-scan list of allowed join conditions.  This
    # is the most effective anti-hallucination measure: the model no longer
    # needs to hunt through individual result sections for FK relationships.
    joins_preamble = _extract_valid_joins(results)

    if not max_chars:
        # No budget — include everything (legacy behaviour).
        sections = [_build_result_section(r, i) for i, r in enumerate(results, 1)]
        body = "\n\n".join(sections)
        return f"{joins_preamble}\n\n{body}" if joins_preamble else body

    # Budget-aware assembly: add full sections while they fit, then
    # truncate the next section to fill remaining space.
    sections: list[str] = []
    used = 0

    # Reserve budget for the joins preamble (if any).
    if joins_preamble:
        preamble_cost = len(joins_preamble) + 2  # +2 for "\n\n" separator
        if preamble_cost < max_chars:
            used = preamble_cost
        else:
            # Preamble alone exceeds budget — skip it.
            joins_preamble = ""

    for i, result in enumerate(results, 1):
        section = _build_result_section(result, i)
        separator_cost = 2 if sections else 0  # "\n\n" between sections

        if used + separator_cost + len(section) <= max_chars:
            sections.append(section)
            used += separator_cost + len(section)
        else:
            # Truncate this section to fill remaining budget.
            remaining = max_chars - used - separator_cost
            if remaining > 100:  # only worth adding if meaningful
                sections.append(section[:remaining] + "\n[truncated]")
            break

    context = "\n\n".join(sections)
    if joins_preamble:
        context = f"{joins_preamble}\n\n{context}"
    if used < max_chars and len(sections) < len(results):
        logger.debug(
            "Context budget %d chars: included %d/%d results",
            max_chars,
            len(sections),
            len(results),
        )
    return context


def _detect_dominant_source_type(results: list[dict]) -> str | None:
    """Detect the most common source_type in the result set."""
    types = [r.get("payload", {}).get("source_type") for r in results]
    types = [t for t in types if t]
    if not types:
        return None
    counter = Counter(types)
    return counter.most_common(1)[0][0]


class ResponseRefiner:
    """Generates conversational answers from search results via Ollama /chat."""

    def __init__(self) -> None:
        # Non-model options (context budget, result cap) still come from the
        # answer_generation role; the model call goes through AIClient so it
        # follows the active provider. Sampling lives on the `answer-refiner`
        # profile (framework-config.yaml).
        role = settings.ollama.get_role("answer_generation")
        self._refiner_max_context_chars = role.refiner_max_context_chars
        self.refiner_max_results = role.refiner_max_results
        self._client = AIClient(profile="answer-refiner")
        logger.info(
            "ResponseRefiner using profile 'answer-refiner' → %s (provider=%s)",
            self._client.model,
            self._client.profile.provider,
        )

    async def refine(
        self,
        query: str,
        results: list[dict],
        include_examples: bool | None = None,
        brief: bool = False,
        conversation_history: list[dict[str, str]] | None = None,
        general_knowledge: bool = False,
    ) -> str:
        """Generate a conversational answer from search results."""
        # General-knowledge mode: no useful indexed results — answer directly.
        if general_knowledge or not results:
            if not general_knowledge and not results:
                # Legacy path: no results AND not explicitly flagged.
                return (
                    "I couldn't find any relevant results for your question. Try rephrasing or broadening your search."
                )

            logger.info("General-knowledge mode: answering '%s' without RAG context", query[:80])
            gk_prompt = GENERAL_KNOWLEDGE_SYSTEM_PROMPT
            if brief:
                gk_prompt += BRIEF_MODE_ADDENDUM
            messages = [{"role": "system", "content": gk_prompt}]
            if conversation_history:
                messages.extend(conversation_history)
                logger.debug("Injecting %d history turns into general-knowledge context", len(conversation_history))
            messages.append({"role": "user", "content": query})

            try:
                response = await self._client.achat(messages=messages)
                answer = response.content.strip()
                if answer:
                    logger.info("General-knowledge answer: %d chars", len(answer))
                    return answer
                return "I couldn't generate an answer for your question. Try rephrasing it."
            except Exception as e:
                logger.warning("General-knowledge answer generation failed: %s", e)
                return "Answer generation failed. Please try again."

        # Decide whether to include examples
        if include_examples is None:
            include_examples = settings.refinement.output_examples

        # Pick the right system prompt based on dominant source_type
        source_type = _detect_dominant_source_type(results)
        prompt_builder = (
            SYSTEM_PROMPT_REGISTRY.get(source_type, DEFAULT_PROMPT_BUILDER) if source_type else DEFAULT_PROMPT_BUILDER
        )
        system_prompt = prompt_builder(include_examples)

        # Append SQL dialect guidance when results are sql-schema
        if source_type == "sql-schema":
            system_prompt += _sql_dialect_addendum(results)

        if brief:
            system_prompt += BRIEF_MODE_ADDENDUM

        context = _build_context(results, max_chars=self._refiner_max_context_chars)
        user_message = USER_PROMPT.substitute(query=query, context=context)

        # Build message list: system → (optional history) → current user turn
        messages = [{"role": "system", "content": system_prompt}]
        if conversation_history:
            messages.extend(conversation_history)
            logger.debug("Injecting %d history turns into refiner context", len(conversation_history))
        messages.append({"role": "user", "content": user_message})

        try:
            response = await self._client.achat(messages=messages)

            answer = response.content.strip()

            if not answer:
                logger.warning("Response refiner returned empty output")
                return "I found some results but couldn't generate a summary. Check the raw results below."

            logger.info(
                "Response refined: %d chars from %d results (source_type=%s)", len(answer), len(results), source_type
            )
            return answer

        except Exception as e:
            logger.warning("Response refinement failed: %s", e)
            return "I found some results but the answer generation failed. Check the raw results below."

    async def refine_stream(
        self,
        query: str,
        results: list[dict],
        include_examples: bool | None = None,
        brief: bool = False,
        conversation_history: list[dict[str, str]] | None = None,
        general_knowledge: bool = False,
        token_usage: dict | None = None,
    ) -> AsyncGenerator[str, None]:
        """Streaming variant of :meth:`refine` — yields token chunks.

        Uses the same prompt-building logic as ``refine()`` but calls the
        Ollama async client with ``stream=True``, yielding each content
        token as it arrives.  Falls back to the full ``refine()`` call
        (yielded as one chunk) on error.

        If *token_usage* is a mutable dict, it will be populated with
        ``prompt_tokens``, ``completion_tokens``, and ``total_tokens``
        extracted from the final streaming chunk.
        """
        # General-knowledge mode
        if general_knowledge or not results:
            if not general_knowledge and not results:
                yield "I couldn't find any relevant results for your question. Try rephrasing or broadening your search."
                return

            logger.info("General-knowledge stream mode: '%s'", query[:80])
            gk_prompt = GENERAL_KNOWLEDGE_SYSTEM_PROMPT
            if brief:
                gk_prompt += BRIEF_MODE_ADDENDUM
            messages = [{"role": "system", "content": gk_prompt}]
            if conversation_history:
                messages.extend(conversation_history)
            messages.append({"role": "user", "content": query})

            try:
                stream = await self._client.achat(messages=messages, stream=True)
                async for chunk in stream:
                    token = chunk.get("content", "")
                    if token:
                        yield token
                    if chunk.get("done") and token_usage is not None:
                        pt, ct = _stream_token_usage(chunk.get("raw"))
                        token_usage["prompt_tokens"] = pt
                        token_usage["completion_tokens"] = ct
                        token_usage["total_tokens"] = pt + ct
                return
            except Exception as e:
                logger.warning("General-knowledge stream failed: %s — falling back", e)
                yield "Answer generation failed. Please try again."
                return

        # RAG mode — build context and messages (same as refine())
        if include_examples is None:
            include_examples = settings.refinement.output_examples

        source_type = _detect_dominant_source_type(results)
        prompt_builder = (
            SYSTEM_PROMPT_REGISTRY.get(source_type, DEFAULT_PROMPT_BUILDER) if source_type else DEFAULT_PROMPT_BUILDER
        )
        system_prompt = prompt_builder(include_examples)

        if source_type == "sql-schema":
            system_prompt += _sql_dialect_addendum(results)
        if brief:
            system_prompt += BRIEF_MODE_ADDENDUM

        context = _build_context(results, max_chars=self._refiner_max_context_chars)
        user_message = USER_PROMPT.substitute(query=query, context=context)

        messages = [{"role": "system", "content": system_prompt}]
        if conversation_history:
            messages.extend(conversation_history)
        messages.append({"role": "user", "content": user_message})

        try:
            stream = await self._client.achat(messages=messages, stream=True)
            async for chunk in stream:
                token = chunk.get("content", "")
                if token:
                    yield token
                if chunk.get("done") and token_usage is not None:
                    pt, ct = _stream_token_usage(chunk.get("raw"))
                    token_usage["prompt_tokens"] = pt
                    token_usage["completion_tokens"] = ct
                    token_usage["total_tokens"] = pt + ct
        except Exception as e:
            logger.warning("Response stream failed: %s — falling back to sync refine", e)
            fallback = await self.refine(
                query, results, include_examples, brief, conversation_history, general_knowledge
            )
            yield fallback


response_refiner = ResponseRefiner()
