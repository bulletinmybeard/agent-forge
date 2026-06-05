"""Runtime database connection manager for the execute_sql tool.

Maintains a pool of SQLAlchemy engines keyed by logical database name
(matching Qdrant source_name).  Supports MySQL and PostgreSQL.

Usage::

    from app.services.db_service import db_service

    result = db_service.execute("mydb", "SELECT COUNT(*) FROM relation2")
    # → {"columns": ["COUNT(*)"], "rows": [[42]], "row_count": 1, "truncated": False}
"""

import logging
import re

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from app.config import SqlDatabaseEntry, settings

logger = logging.getLogger(__name__)

_DESTRUCTIVE_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE|RENAME|GRANT|REVOKE)\b",
    re.IGNORECASE,
)

# Strip SQL comments and string literals before checking destructiveness
_COMMENT_PATTERN = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_STRING_PATTERN = re.compile(r"'[^']*'|\"[^\"]*\"")


def is_destructive(query: str) -> bool:
    """Return True if the query contains destructive SQL statements."""
    cleaned = _COMMENT_PATTERN.sub("", query)
    cleaned = _STRING_PATTERN.sub("", cleaned)
    return bool(_DESTRUCTIVE_PATTERN.search(cleaned))


# ── Query result type ───────────────────────────────────────────────────────


class QueryResult:
    """Structured result from a SQL query execution."""

    def __init__(
        self,
        columns: list[str],
        rows: list[list],
        row_count: int,
        truncated: bool,
        database: str,
        engine: str,
        affected_rows: int | None = None,
    ):
        self.columns = columns
        self.rows = rows
        self.row_count = row_count
        self.truncated = truncated
        self.database = database
        self.engine = engine
        self.affected_rows = affected_rows

    def to_text(self, max_col_width: int = 40) -> str:
        """Format as human-readable text for the LLM to summarise."""
        lines = []

        if self.affected_rows is not None:
            lines.append(f"Query executed on {self.database} ({self.engine}). {self.affected_rows} row(s) affected.")
            return "\n".join(lines)

        lines.append(f"Query executed on {self.database} ({self.engine}). {self.row_count} row(s) returned.")

        if not self.rows:
            return "\n".join(lines)

        # Build a simple markdown table
        # Truncate wide columns for readability
        def _trunc(val: object) -> str:
            s = str(val) if val is not None else "NULL"
            return s if len(s) <= max_col_width else s[: max_col_width - 1] + "…"

        headers = [_trunc(c) for c in self.columns]
        separators = ["-" * len(h) for h in headers]
        table_rows = []
        for row in self.rows:
            table_rows.append([_trunc(v) for v in row])

        lines.append("| " + " | ".join(headers) + " |")
        lines.append("| " + " | ".join(separators) + " |")
        for row in table_rows:
            # Pad each cell to match header width
            padded = [v.ljust(len(h)) for v, h in zip(row, headers)]
            lines.append("| " + " | ".join(padded) + " |")

        if self.truncated:
            lines.append(f"(showing {len(self.rows)} of {self.row_count} rows)")

        return "\n".join(lines)


class DatabaseService:
    """Manages SQLAlchemy engine pools for configured databases."""

    def __init__(self) -> None:
        self._engines: dict[str, Engine] = {}
        self._configs: dict[str, SqlDatabaseEntry] = {}
        self._init_engines()

    def _init_engines(self) -> None:
        """Create engines from config.yaml sql_databases section."""
        for name, entry in settings.sql_databases.databases.items():
            if not entry.url:
                logger.warning("sql_databases.%s has no URL — skipping", name)
                continue
            try:
                engine = create_engine(
                    entry.url,
                    pool_size=2,
                    max_overflow=3,
                    pool_timeout=10,
                    pool_recycle=300,
                    pool_pre_ping=True,
                )
                # For a read-only MySQL DB, force every pooled connection's
                # default transaction to READ ONLY so writes are rejected by the
                # server, not just by the advisory regex. (Postgres is handled
                # per-transaction in execute().) The real boundary is still a
                # least-privilege DB user — see execute().
                if entry.readonly and entry.engine == "mysql":
                    self._install_mysql_readonly(engine)

                self._engines[name] = engine
                self._configs[name] = entry
                logger.info(
                    "SQL database '%s' [%s] (%s) ready%s",
                    name,
                    entry.name,
                    entry.engine,
                    " [read-only]" if entry.readonly else "",
                )
            except Exception as e:
                logger.warning("Failed to create engine for '%s': %s", name, e)

    @staticmethod
    def _install_mysql_readonly(engine: Engine) -> None:
        """Set SESSION TRANSACTION READ ONLY on each new MySQL connection."""

        @event.listens_for(engine, "connect")
        def _set_read_only(dbapi_conn, _record) -> None:  # noqa: ANN001
            cursor = dbapi_conn.cursor()
            try:
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
            finally:
                cursor.close()

    @staticmethod
    def _collect_rows(result, max_rows: int) -> tuple[list[str], list[list], int, bool]:
        """Fetch a result set with the configured row cap and a total estimate."""
        if not result.returns_rows:
            return [], [], 0, False
        columns = list(result.keys())
        fetched = result.fetchmany(max_rows + 1)
        truncated = len(fetched) > max_rows
        rows = [list(row) for row in fetched[:max_rows]]
        if truncated:
            remaining = result.fetchall()
            total = max_rows + 1 + len(remaining)
        else:
            total = len(rows)
        return columns, rows, total, truncated

    @property
    def available_databases(self) -> list[str]:
        return list(self._engines.keys())

    @property
    def available_databases_display(self) -> list[str]:
        """Return formatted list like ['mydb (My Database, mysql)']."""
        items = []
        for key in self._engines:
            entry = self._configs.get(key)
            if entry and entry.name:
                items.append(f"{key} ({entry.name}, {entry.engine})")
            elif entry:
                items.append(f"{key} ({entry.engine})")
            else:
                items.append(key)
        return items

    def get_engine_type(self, database: str) -> str | None:
        """Return 'mysql' or 'postgres' for a named database."""
        entry = self._configs.get(database)
        return entry.engine if entry else None

    def execute(self, database: str, query: str) -> QueryResult:
        """Execute a SQL query against a named database."""
        engine = self._engines.get(database)
        config = self._configs.get(database)

        if engine is None or config is None:
            available = ", ".join(self._engines.keys()) or "(none)"
            raise ValueError(f"Database '{database}' is not configured. Available databases: {available}")

        # The regex is ADVISORY only (drives the UI/confirm badge). On a
        # read-only DB the security boundary is the read-only transaction +
        # never-commit below, not this flag — so a write the regex misses still
        # can't persist. For hard guarantees, point the config at a
        # least-privilege / replica DB user (grant-level read-only).
        destructive = is_destructive(query)
        max_rows = config.max_rows

        try:
            with engine.connect() as conn:
                if config.readonly:
                    # Reject writes/DDL up front. The transaction-level guard below
                    # is not sufficient on MySQL: DDL (CREATE/DROP/ALTER/TRUNCATE)
                    # implicitly commits and would bypass the READ ONLY transaction
                    # + rollback. (is_destructive is advisory/regex; the hard
                    # boundary remains a least-privilege DB grant — see config docs.)
                    if destructive:
                        raise RuntimeError(
                            "Refusing to run a write/DDL statement on a read-only database "
                            "connection. Use a writable connection or rephrase as a read-only query."
                        )
                    # Belt-and-suspenders: pin the transaction read-only too.
                    # Postgres rejects writes outright; MySQL inherits the session
                    # default set at connect (see _install_mysql_readonly).
                    if config.engine == "postgres":
                        conn.execute(text("SET TRANSACTION READ ONLY"))
                    try:
                        result = conn.execute(text(query))
                        columns, rows, total, truncated = self._collect_rows(result, max_rows)
                    finally:
                        conn.rollback()  # discard any (blocked/attempted) write side effects
                    logger.info(
                        "SQL executed on %s (read-only): %d rows returned%s",
                        database,
                        total,
                        f" (truncated to {max_rows})" if truncated else "",
                    )
                    return QueryResult(
                        columns=columns,
                        rows=rows,
                        row_count=total,
                        truncated=truncated,
                        database=database,
                        engine=config.engine,
                    )

                # Writable DB (destructive statements commit and report rowcount).
                result = conn.execute(text(query))
                if destructive:
                    affected = result.rowcount
                    conn.commit()
                    logger.info(
                        "SQL executed on %s (destructive): %d rows affected",
                        database,
                        affected,
                    )
                    return QueryResult(
                        columns=[],
                        rows=[],
                        row_count=0,
                        truncated=False,
                        database=database,
                        engine=config.engine,
                        affected_rows=affected,
                    )

                columns, rows, total, truncated = self._collect_rows(result, max_rows)
                logger.info(
                    "SQL executed on %s: %d rows returned%s",
                    database,
                    total,
                    f" (truncated to {max_rows})" if truncated else "",
                )
                return QueryResult(
                    columns=columns,
                    rows=rows,
                    row_count=total,
                    truncated=truncated,
                    database=database,
                    engine=config.engine,
                )

        except Exception as e:
            error_msg = str(e)
            # Clean up SQLAlchemy's verbose error wrapping
            if ")" in error_msg and error_msg.startswith("("):
                error_msg = error_msg.split("\n")[0]
            logger.warning("SQL execution failed on %s: %s", database, error_msg)
            raise RuntimeError(f"Query failed on {database} ({config.engine}): {error_msg}") from e


db_service = DatabaseService()
