"""Direct database schema extraction using SQLAlchemy.

Replaces the tbls dependency by connecting to PostgreSQL (or MySQL) via
SQLAlchemy's inspector, extracting tables, columns, indexes, constraints,
foreign keys, views, enums, and functions, and producing the same
DatabaseSchema dataclass that the tbls parser outputs.

The rest of the pipeline (mapper → writer → indexer) works unchanged.

Usage:
    from chunking.db.schema_extractor import extract_schema

    schema = extract_schema(
        url="postgresql+psycopg2://user:pass@host:5432/mydb",
        source_name="mydb",
    )
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, Inspector

from chunking.sql.types import (
    ColumnInfo,
    ConstraintInfo,
    DatabaseSchema,
    DriverInfo,
    EnumInfo,
    FunctionInfo,
    IndexInfo,
    RelationInfo,
    TableInfo,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Engine helpers
# ---------------------------------------------------------------------------


def _detect_engine_name(engine: Engine) -> str:
    """Return a normalised engine name like 'postgres' or 'mysql'."""
    dialect = engine.dialect.name  # e.g., "postgresql", "mysql"
    if dialect.startswith("postgres"):
        return "postgres"
    return dialect


def _get_database_version(engine: Engine) -> str:
    """Return the server version string."""
    try:
        with engine.connect() as conn:
            if engine.dialect.name.startswith("postgres"):
                row = conn.execute(text("SHOW server_version")).fetchone()
                return row[0] if row else ""
            elif engine.dialect.name == "mysql":
                row = conn.execute(text("SELECT VERSION()")).fetchone()
                return row[0] if row else ""
    except Exception as e:
        logger.warning("Could not determine database version: %s", e)
    return ""


def _get_database_name(engine: Engine) -> str:
    """Return the connected database name."""
    return engine.url.database or ""


# ---------------------------------------------------------------------------
# Column extraction
# ---------------------------------------------------------------------------


def _extract_column(col: Mapping[str, Any]) -> ColumnInfo:
    """Convert an SQLAlchemy column dict to ColumnInfo."""
    col_type = str(col.get("type", ""))
    nullable = col.get("nullable", True)
    default = col.get("default")
    comment = col.get("comment") or ""

    # autoincrement / extra info
    extra_parts = []
    if col.get("autoincrement") is True:
        extra_parts.append("auto_increment")
    extra_def = ", ".join(extra_parts)

    return ColumnInfo(
        name=col["name"],
        col_type=col_type,
        nullable=nullable,
        default=str(default) if default is not None else None,
        comment=comment,
        extra_def=extra_def,
    )


# ---------------------------------------------------------------------------
# Index extraction
# ---------------------------------------------------------------------------


def _extract_indexes(inspector: Inspector, table_name: str, schema: str | None) -> list[IndexInfo]:
    """Extract indexes for a table."""
    indexes: list[IndexInfo] = []
    try:
        for idx in inspector.get_indexes(table_name, schema=schema):
            columns = [str(c) for c in idx.get("column_names", []) if c]
            unique = idx.get("unique", False)
            definition = f"{'UNIQUE ' if unique else ''}INDEX on ({', '.join(columns)})"
            indexes.append(
                IndexInfo(
                    name=str(idx.get("name") or ""),
                    definition=definition,
                    table=table_name,
                    columns=columns,
                )
            )
    except Exception as e:
        logger.debug("Could not get indexes for %s: %s", table_name, e)
    return indexes


# ---------------------------------------------------------------------------
# Constraint extraction
# ---------------------------------------------------------------------------


def _extract_constraints(
    inspector: Inspector,
    table_name: str,
    schema: str | None,
) -> list[ConstraintInfo]:
    """Extract PK, unique, and check constraints."""
    constraints: list[ConstraintInfo] = []

    # Primary key
    try:
        pk = inspector.get_pk_constraint(table_name, schema=schema)
        if pk and pk.get("constrained_columns"):
            cols = pk["constrained_columns"]
            constraints.append(
                ConstraintInfo(
                    name=str(pk.get("name") or "PRIMARY"),
                    constraint_type="PRIMARY KEY",
                    definition=f"PRIMARY KEY ({', '.join(cols)})",
                    table=table_name,
                    columns=cols,
                )
            )
    except Exception as e:
        logger.debug("Could not get PK for %s: %s", table_name, e)

    # Unique constraints
    try:
        for uc in inspector.get_unique_constraints(table_name, schema=schema):
            cols = uc.get("column_names", [])
            constraints.append(
                ConstraintInfo(
                    name=str(uc.get("name") or ""),
                    constraint_type="UNIQUE",
                    definition=f"UNIQUE ({', '.join(cols)})",
                    table=table_name,
                    columns=cols,
                )
            )
    except Exception as e:
        logger.debug("Could not get unique constraints for %s: %s", table_name, e)

    # Check constraints
    try:
        for cc in inspector.get_check_constraints(table_name, schema=schema):
            constraints.append(
                ConstraintInfo(
                    name=str(cc.get("name") or ""),
                    constraint_type="CHECK",
                    definition=cc.get("sqltext", ""),
                    table=table_name,
                )
            )
    except Exception as e:
        logger.debug("Could not get check constraints for %s: %s", table_name, e)

    return constraints


# ---------------------------------------------------------------------------
# Foreign key / relation extraction
# ---------------------------------------------------------------------------


def _extract_relations(
    inspector: Inspector,
    table_name: str,
    schema: str | None,
) -> list[RelationInfo]:
    """Extract foreign key relationships for a table (child side)."""
    relations: list[RelationInfo] = []
    try:
        for fk in inspector.get_foreign_keys(table_name, schema=schema):
            ref_table = fk.get("referred_table", "")
            ref_schema = fk.get("referred_schema")
            # If referred schema differs, prefix the table name
            if ref_schema and ref_schema != schema:
                ref_table = f"{ref_schema}.{ref_table}"

            relations.append(
                RelationInfo(
                    table=table_name,
                    columns=fk.get("constrained_columns", []),
                    parent_table=ref_table,
                    parent_columns=fk.get("referred_columns", []),
                    definition=f"FOREIGN KEY ({', '.join(fk.get('constrained_columns', []))}) REFERENCES {ref_table}({', '.join(fk.get('referred_columns', []))})",
                    cardinality="zero_or_more",
                    parent_cardinality="exactly_one",
                )
            )
    except Exception as e:
        logger.debug("Could not get FKs for %s: %s", table_name, e)
    return relations


# ---------------------------------------------------------------------------
# PostgreSQL-specific: enums
# ---------------------------------------------------------------------------


def _extract_pg_enums(engine: Engine, schema: str | None) -> list[EnumInfo]:
    """Extract PostgreSQL enum types."""
    if not engine.dialect.name.startswith("postgres"):
        return []

    enums: list[EnumInfo] = []
    try:
        inspector = inspect(engine)
        get_enums = getattr(inspector, "get_enums", None)
        if get_enums is not None:
            for enum in get_enums(schema=schema or "public"):
                enums.append(
                    EnumInfo(
                        name=str(enum.get("name") or ""),
                        values=enum.get("labels", []),
                    )
                )
    except Exception as e:
        logger.debug("Could not get enums: %s", e)
    return enums


# ---------------------------------------------------------------------------
# PostgreSQL-specific: functions
# ---------------------------------------------------------------------------


def _extract_pg_functions(engine: Engine, schema: str | None) -> list[FunctionInfo]:
    """Extract PostgreSQL functions and procedures (non-system)."""
    if not engine.dialect.name.startswith("postgres"):
        return []

    functions: list[FunctionInfo] = []
    target_schema = schema or "public"
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                SELECT routine_name, routine_type,
                       data_type AS return_type
                FROM information_schema.routines
                WHERE routine_schema = :schema
                  AND routine_name NOT LIKE 'pg_%'
                ORDER BY routine_name
            """),
                {"schema": target_schema},
            ).fetchall()

            for row in rows:
                functions.append(
                    FunctionInfo(
                        name=row[0],
                        return_type=row[2] or "",
                        func_type=row[1] or "FUNCTION",
                    )
                )
    except Exception as e:
        logger.debug("Could not get functions: %s", e)
    return functions


# ---------------------------------------------------------------------------
# View detection
# ---------------------------------------------------------------------------


def _get_view_names(inspector: Inspector, schema: str | None) -> set[str]:
    """Return a set of view names."""
    try:
        return set(inspector.get_view_names(schema=schema))
    except Exception:
        return set()


# ---------------------------------------------------------------------------
# Table comment extraction
# ---------------------------------------------------------------------------


def _get_table_comment(inspector: Inspector, table_name: str, schema: str | None) -> str:
    """Get the table comment if available."""
    try:
        comment = inspector.get_table_comment(table_name, schema=schema)
        return comment.get("text") or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Main extractor
# ---------------------------------------------------------------------------


def extract_schema(
    url: str,
    source_name: str,
    schema: str | None = None,
) -> DatabaseSchema:
    """Connect to a database and extract a full DatabaseSchema."""
    logger.info("Connecting to database: %s (source: %s)", url.split("@")[-1], source_name)

    engine = create_engine(url, echo=False)
    inspector = inspect(engine)

    db_name = _get_database_name(engine)
    engine_name = _detect_engine_name(engine)
    db_version = _get_database_version(engine)

    # Default schema for PG
    if schema is None and engine_name == "postgres":
        schema = "public"

    view_names = _get_view_names(inspector, schema)

    # Collect all table + view names
    table_names = inspector.get_table_names(schema=schema)
    all_names = sorted(set(table_names) | view_names)

    logger.info("Found %d tables + %d views", len(table_names), len(view_names))

    # Extract tables and views
    tables: list[TableInfo] = []
    all_relations: list[RelationInfo] = []

    for name in all_names:
        is_view = name in view_names
        table_type = "VIEW" if is_view else "BASE TABLE"

        # Columns
        try:
            raw_columns = inspector.get_columns(name, schema=schema)
            columns = [_extract_column(c) for c in raw_columns]
        except Exception as e:
            logger.warning("Could not get columns for %s: %s", name, e)
            columns = []

        # Indexes (views typically don't have indexes)
        indexes = _extract_indexes(inspector, name, schema) if not is_view else []

        # Constraints
        constraints = _extract_constraints(inspector, name, schema) if not is_view else []

        # Foreign keys (produces RelationInfo entries)
        fk_relations = _extract_relations(inspector, name, schema) if not is_view else []
        all_relations.extend(fk_relations)

        # FK constraints (add to constraints list)
        for rel in fk_relations:
            constraints.append(
                ConstraintInfo(
                    name=f"fk_{name}_{'_'.join(rel.columns)}",
                    constraint_type="FOREIGN KEY",
                    definition=rel.definition,
                    table=name,
                    columns=rel.columns,
                    referenced_table=rel.parent_table,
                    referenced_columns=rel.parent_columns,
                )
            )

        # Comment
        comment = _get_table_comment(inspector, name, schema)

        tables.append(
            TableInfo(
                name=name,
                table_type=table_type,
                comment=comment,
                columns=columns,
                indexes=indexes,
                constraints=constraints,
            )
        )

    # Enums (PostgreSQL)
    enums = _extract_pg_enums(engine, schema)

    # Functions (PostgreSQL)
    functions = _extract_pg_functions(engine, schema)

    driver = DriverInfo(
        name=engine_name,
        database_version=db_version,
        current_schema=schema or "",
    )

    db_schema = DatabaseSchema(
        name=db_name,
        driver=driver,
        tables=tables,
        relations=all_relations,
        enums=enums,
        functions=functions,
        source_name_slug=source_name,
    )

    logger.info(
        "Extracted %s: %d tables, %d relations, %d enums, %d functions",
        source_name,
        len(tables),
        len(all_relations),
        len(enums),
        len(functions),
    )

    engine.dispose()
    return db_schema
