"""SQL query execution tool for the ScoutIX agent.

Provides the ``execute_sql`` tool that runs SQL queries against configured
databases (MySQL / PostgreSQL) and returns formatted results for the LLM
to summarise.

Destructive queries (INSERT, UPDATE, DELETE, DROP, etc.) trigger a
confirmation prompt before execution.
"""

from __future__ import annotations

import logging

from .registry import tool

logger = logging.getLogger(__name__)


def _sql_confirm_condition(database: str, query: str) -> str | None:
    """Return a confirmation prompt for destructive queries, or None."""
    try:
        from app.services.db_service import is_destructive
    except ImportError:
        return None
    if is_destructive(query):
        preview = query.strip()[:200]
        if len(query.strip()) > 200:
            preview += "..."
        return (
            f"! This query modifies data on {database}:\n"
            f"  {preview}\n"
            f"Execute anyway?"
        )
    return None


@tool(
    locality="local",
    hint=(
        "Use execute_sql to run SQL queries against internal databases. "
        "The 'database' parameter must be one of the configured database names "
        "(call sql_extract_schema first to see them). Always generate the SQL "
        "query first based on the schema context, then call this tool to execute it. "
        "For destructive queries (INSERT/UPDATE/DELETE), the user will be "
        "asked to confirm before execution."
    ),
    confirm_condition=_sql_confirm_condition,
)
def execute_sql(database: str, query: str) -> str:
    """Execute a SQL query against a named internal database.

    Runs the query and returns formatted results (columns + rows) or an
    error message.  Destructive queries require user confirmation.

    database: Logical database name matching config (one of the configured databases)
    query: The SQL query to execute
    """
    try:
        from app.services.db_service import db_service
    except ImportError:
        return (
            "ERROR: Database service not available. "
            "The execute_sql tool requires the scout-service runtime."
        )

    # Validate database exists
    if database not in db_service.available_databases:
        available = ", ".join(db_service.available_databases) or "(none configured)"
        return (
            f"ERROR: Database '{database}' is not configured.\n"
            f"Available databases: {available}"
        )

    # Log the query (truncated)
    query_preview = query.strip()[:200]
    logger.info(
        "execute_sql(%s): %s%s",
        database,
        query_preview,
        "..." if len(query.strip()) > 200 else "",
    )

    try:
        result = db_service.execute(database, query)
        return result.to_text()
    except RuntimeError as e:
        return f"ERROR: {e}"
    except Exception as e:
        logger.exception("Unexpected error in execute_sql")
        return f"ERROR: Unexpected error: {e}"


@tool(
    locality="local",
    hint=(
        "Use db_query_plan to analyse query performance. "
        "Wraps EXPLAIN ANALYZE to show execution plan, row estimates, "
        "sequential vs index scans, and timing. "
        "Use this BEFORE optimising a query to understand where time is spent."
    ),
)
def db_query_plan(database: str, query: str) -> str:
    """Run EXPLAIN ANALYZE on a SQL query and return the execution plan.

    Shows the query planner's strategy, actual vs. estimated row counts,
    sequential scans, index usage, and timing for each operation.
    Use this to diagnose slow queries before optimising.

    database: Logical database name matching a configured database
    query: The SELECT/INSERT/UPDATE/DELETE query to analyse
    """
    try:
        from app.services.db_service import db_service
    except ImportError:
        return (
            "ERROR: Database service not available. "
            "The db_query_plan tool requires the scout-service runtime."
        )

    if database not in db_service.available_databases:
        available = ", ".join(db_service.available_databases) or "(none configured)"
        return (
            f"ERROR: Database '{database}' is not configured.\n"
            f"Available databases: {available}"
        )

    # Determine dialect for EXPLAIN syntax
    try:
        dialect = db_service.get_dialect(database)
    except Exception:
        dialect = "unknown"

    # Build EXPLAIN command based on dialect
    clean_query = query.strip().rstrip(";")
    if dialect == "postgresql":
        explain_query = f"EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) {clean_query}"
    elif dialect == "mysql":
        explain_query = f"EXPLAIN ANALYZE {clean_query}"
    else:
        # Fallback: basic EXPLAIN
        explain_query = f"EXPLAIN {clean_query}"

    logger.info("db_query_plan(%s): %s", database, clean_query[:200])

    try:
        result = db_service.execute(database, explain_query)
        plan_text = result.to_text()

        # Add a summary header
        header = f"=== Query Plan for {database} ({dialect}) ===\n"
        header += f"Query: {clean_query[:300]}\n"
        header += "=" * 60 + "\n\n"

        return header + plan_text
    except RuntimeError as e:
        return f"ERROR: {e}"
    except Exception as e:
        logger.exception("Unexpected error in db_query_plan")
        return f"ERROR: Unexpected error: {e}"


def register_sql_tools(registry) -> int:
    """Register execute_sql and db_query_plan. Skips names already registered."""
    registry.register_category_hint(
        "Database",
        "SQL tools execute queries against internal databases. "
        "Always verify the schema context before writing SQL. "
        "Call sql_extract_schema to discover the configured databases.",
    )
    count = 0
    for func in (execute_sql, db_query_plan):
        if func.__name__ in registry:
            continue
        registry.register(func, category="Database")
        count += 1
    return count
