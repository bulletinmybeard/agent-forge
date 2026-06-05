"""Dataclasses for intermediate parsing of tbls JSON output.

These represent the raw extracted data from the tbls schema.json before
the mapper transforms them into Qdrant-ready chunk models.

tbls JSON schema reference:
  https://github.com/k1LoW/tbls/blob/main/spec/tbls.schema.json_schema.json
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ColumnInfo:
    """A single column within a table."""

    name: str
    col_type: str  # e.g., "int(11)", "varchar(255)", "timestamp"
    nullable: bool = False
    default: str | None = None
    comment: str = ""
    extra_def: str = ""  # e.g., "auto_increment", "on update CURRENT_TIMESTAMP"


@dataclass
class IndexInfo:
    """A single index on a table."""

    name: str
    definition: str  # raw DDL, e.g., "CREATE INDEX idx_users_email ON users (email)"
    table: str = ""
    columns: list[str] = field(default_factory=list)
    comment: str = ""


@dataclass
class ConstraintInfo:
    """A single constraint on a table."""

    name: str
    constraint_type: str  # e.g., "PRIMARY KEY", "FOREIGN KEY", "UNIQUE", "CHECK"
    definition: str  # raw DDL
    table: str = ""
    columns: list[str] = field(default_factory=list)
    referenced_table: str = ""
    referenced_columns: list[str] = field(default_factory=list)
    comment: str = ""


@dataclass
class TriggerInfo:
    """A trigger on a table."""

    name: str
    definition: str
    comment: str = ""


@dataclass
class TableInfo:
    """All extracted data for a single table or view."""

    name: str
    table_type: str = "BASE TABLE"  # "BASE TABLE" or "VIEW"
    comment: str = ""
    definition: str = ""  # raw DDL (CREATE TABLE / CREATE VIEW statement)
    columns: list[ColumnInfo] = field(default_factory=list)
    indexes: list[IndexInfo] = field(default_factory=list)
    constraints: list[ConstraintInfo] = field(default_factory=list)
    triggers: list[TriggerInfo] = field(default_factory=list)
    referenced_tables: list[str] = field(default_factory=list)


@dataclass
class RelationInfo:
    """A foreign key relationship between two tables."""

    table: str  # child table
    columns: list[str]  # child columns
    parent_table: str
    parent_columns: list[str]
    definition: str  # raw DDL
    cardinality: str = ""  # e.g., "zero_or_more", "exactly_one"
    parent_cardinality: str = ""
    virtual: bool = False


@dataclass
class EnumInfo:
    """A database enum type (PostgreSQL)."""

    name: str
    values: list[str] = field(default_factory=list)


@dataclass
class FunctionInfo:
    """A database function or stored procedure."""

    name: str
    return_type: str = ""
    arguments: str = ""
    func_type: str = ""  # e.g., "FUNCTION", "PROCEDURE"


@dataclass
class DriverInfo:
    """Database driver metadata from tbls."""

    name: str = ""  # e.g., "mysql", "postgres"
    database_version: str = ""
    current_schema: str = ""
    search_paths: list[str] = field(default_factory=list)


@dataclass
class DatabaseSchema:
    """Complete parsed representation of a tbls schema.json file."""

    name: str  # database name
    description: str = ""
    driver: DriverInfo = field(default_factory=DriverInfo)
    tables: list[TableInfo] = field(default_factory=list)
    relations: list[RelationInfo] = field(default_factory=list)
    enums: list[EnumInfo] = field(default_factory=list)
    functions: list[FunctionInfo] = field(default_factory=list)
    # Derived
    source_filename: str = ""
    source_name_slug: str = ""  # e.g., "portal-db"
