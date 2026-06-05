"""tbls JSON parser.

Reads a tbls schema.json file (output of `tbls out -t json`) and extracts
structured data into intermediate dataclasses (types.py).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

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
    TriggerInfo,
)

logger = logging.getLogger(__name__)


def _load_json(filepath: Path) -> dict:
    """Load a JSON file, handling UTF-8 BOM encoding."""
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            with open(filepath, encoding=encoding) as f:
                return json.load(f)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    raise ValueError(f"Failed to parse JSON from {filepath}")


def _derive_source_name_slug(filepath: Path, db_name: str) -> str:
    """Derive a source_name_slug from the filename or database name.

    schema-portal-db.json → "portal-db"
    schema.json with db_name="portal" → "portal"
    """
    stem = filepath.stem  # "schema-portal-db" or "schema"
    # Strip common prefixes
    slug = re.sub(r"^(schema|tbls)[_-]?", "", stem, flags=re.IGNORECASE)
    if slug:
        return slug.lower()
    # Fallback to database name
    if db_name:
        return re.sub(r"[^a-z0-9]+", "-", db_name.lower()).strip("-")
    return "unknown-db"


def _parse_column(raw: dict) -> ColumnInfo:
    """Parse a single column from the tbls JSON."""
    return ColumnInfo(
        name=raw.get("name", ""),
        col_type=raw.get("type", ""),
        nullable=raw.get("nullable", False),
        default=raw.get("default") if raw.get("default") is not None else None,
        comment=raw.get("comment", ""),
        extra_def=raw.get("extra_def", ""),
    )


def _parse_index(raw: dict) -> IndexInfo:
    """Parse a single index from the tbls JSON."""
    return IndexInfo(
        name=raw.get("name", ""),
        definition=raw.get("def", ""),
        table=raw.get("table", ""),
        columns=raw.get("columns", []),
        comment=raw.get("comment", ""),
    )


def _parse_constraint(raw: dict) -> ConstraintInfo:
    """Parse a single constraint from the tbls JSON."""
    return ConstraintInfo(
        name=raw.get("name", ""),
        constraint_type=raw.get("type", ""),
        definition=raw.get("def", ""),
        table=raw.get("table", ""),
        columns=raw.get("columns", []),
        referenced_table=raw.get("referenced_table", ""),
        referenced_columns=raw.get("referenced_columns", []),
        comment=raw.get("comment", ""),
    )


def _parse_trigger(raw: dict) -> TriggerInfo:
    """Parse a single trigger from the tbls JSON."""
    return TriggerInfo(
        name=raw.get("name", ""),
        definition=raw.get("def", ""),
        comment=raw.get("comment", ""),
    )


def _parse_table(raw: dict) -> TableInfo:
    """Parse a single table object from the tbls JSON."""
    return TableInfo(
        name=raw.get("name", ""),
        table_type=raw.get("type", "BASE TABLE"),
        comment=raw.get("comment", ""),
        definition=raw.get("def", ""),
        columns=[_parse_column(c) for c in raw.get("columns", [])],
        indexes=[_parse_index(i) for i in raw.get("indexes", [])],
        constraints=[_parse_constraint(c) for c in raw.get("constraints", [])],
        triggers=[_parse_trigger(t) for t in raw.get("triggers", [])],
        referenced_tables=raw.get("referenced_tables", []),
    )


def _parse_relation(raw: dict) -> RelationInfo:
    """Parse a single relation object from the tbls JSON."""
    return RelationInfo(
        table=raw.get("table", ""),
        columns=raw.get("columns", []),
        parent_table=raw.get("parent_table", ""),
        parent_columns=raw.get("parent_columns", []),
        definition=raw.get("def", ""),
        cardinality=raw.get("cardinality", ""),
        parent_cardinality=raw.get("parent_cardinality", ""),
        virtual=raw.get("virtual", False),
    )


def _parse_enum(raw: dict) -> EnumInfo:
    """Parse a single enum object from the tbls JSON."""
    return EnumInfo(
        name=raw.get("name", ""),
        values=raw.get("values", []),
    )


def _parse_function(raw: dict) -> FunctionInfo:
    """Parse a single function object from the tbls JSON."""
    return FunctionInfo(
        name=raw.get("name", ""),
        return_type=raw.get("return_type", ""),
        arguments=raw.get("arguments", ""),
        func_type=raw.get("type", ""),
    )


def _parse_driver(raw: dict) -> DriverInfo:
    """Parse the driver object from the tbls JSON."""
    meta = raw.get("meta", {})
    return DriverInfo(
        name=raw.get("name", ""),
        database_version=raw.get("database_version", ""),
        current_schema=meta.get("current_schema", ""),
        search_paths=meta.get("search_paths", []),
    )


def parse_tbls_json(filepath: Path) -> DatabaseSchema:
    """Parse a tbls schema.json file into a structured DatabaseSchema."""
    logger.info("Parsing tbls JSON file: %s", filepath)
    raw = _load_json(filepath)

    db_name = raw.get("name", "")
    description = raw.get("desc", "")

    # Driver info
    driver = _parse_driver(raw.get("driver", {})) if raw.get("driver") else DriverInfo()

    # Tables
    tables = [_parse_table(t) for t in raw.get("tables", [])]

    # Relations
    relations = [_parse_relation(r) for r in raw.get("relations", [])]

    # Enums
    enums = [_parse_enum(e) for e in raw.get("enums", [])]

    # Functions
    functions = [_parse_function(f) for f in raw.get("functions", [])]

    source_name_slug = _derive_source_name_slug(filepath, db_name)

    schema = DatabaseSchema(
        name=db_name,
        description=description,
        driver=driver,
        tables=tables,
        relations=relations,
        enums=enums,
        functions=functions,
        source_filename=filepath.name,
        source_name_slug=source_name_slug,
    )

    logger.info(
        "Parsed %s: %d tables, %d relations, %d enums, %d functions",
        source_name_slug,
        len(tables),
        len(relations),
        len(enums),
        len(functions),
    )

    return schema
