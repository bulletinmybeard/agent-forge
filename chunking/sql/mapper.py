"""SQL Schema mapper: transforms parsed DatabaseSchema into Qdrant-ready chunks.

Takes the intermediate dataclasses from the parser and produces:
- One DatabaseSummaryChunk per database
- One TableChunk per table/view
- One RelationshipMapChunk per database (if relations exist)

Each chunk contains a natural-language text field (for embedding) and
a structured payload (for Qdrant filtered search).
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import defaultdict

from chunking.models import (
    ChunkType,
    DatabaseSummaryChunk,
    DatabaseSummaryPayload,
    RelationshipMapChunk,
    RelationshipMapPayload,
    SourceType,
    TableChunk,
    TablePayload,
)
from chunking.sql.types import (
    ColumnInfo,
    ConstraintInfo,
    DatabaseSchema,
    IndexInfo,
    RelationInfo,
    TableInfo,
)

logger = logging.getLogger(__name__)

# Stop words to exclude from tag inference
_TAG_STOP_WORDS = {
    "id",
    "ids",
    "the",
    "a",
    "an",
    "and",
    "or",
    "for",
    "in",
    "to",
    "from",
    "with",
    "by",
    "of",
    "is",
    "on",
    "at",
    "fk",
    "pk",
    "idx",
    "key",
}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Tag inference
# ---------------------------------------------------------------------------


def _infer_tags_from_name(name: str) -> list[str]:
    """Extract meaningful tags from a table or column name."""
    words = re.split(r"[_\-\s]+", name.lower())
    return [w for w in words if w and len(w) > 2 and w not in _TAG_STOP_WORDS]


def _infer_tags_for_table(table: TableInfo, relations: list[RelationInfo]) -> list[str]:
    """Generate enrichment tags for a table."""
    tags: set[str] = set()

    # From table name
    tags.update(_infer_tags_from_name(table.name))

    # From comment
    if table.comment:
        words = re.findall(r"[A-Za-z]+", table.comment)
        tags.update(w.lower() for w in words if len(w) > 2 and w.lower() not in _TAG_STOP_WORDS)

    # From column names (limit to avoid tag explosion)
    for col in table.columns[:15]:
        tags.update(_infer_tags_from_name(col.name))

    # From related table names
    for rel in relations:
        if rel.table == table.name:
            tags.update(_infer_tags_from_name(rel.parent_table))
        elif rel.parent_table == table.name:
            tags.update(_infer_tags_from_name(rel.table))

    # Table type tag
    if table.table_type.upper() == "VIEW":
        tags.add("view")

    return sorted(tags)


# ---------------------------------------------------------------------------
# Relationship helpers
# ---------------------------------------------------------------------------


def _get_fk_tables(table_name: str, relations: list[RelationInfo]) -> list[str]:
    """Get tables that this table references via foreign keys."""
    return sorted({r.parent_table for r in relations if r.table == table_name})


def _get_referenced_by_tables(table_name: str, relations: list[RelationInfo]) -> list[str]:
    """Get tables that reference this table via foreign keys."""
    return sorted({r.table for r in relations if r.parent_table == table_name})


# ---------------------------------------------------------------------------
# Text generation: natural-language chunk text for embedding
# ---------------------------------------------------------------------------


def _format_column(col: ColumnInfo) -> str:
    """Format a single column for the chunk text."""
    nullable = "nullable" if col.nullable else "not null"
    line = f"  - {col.name} ({col.col_type}, {nullable})"
    if col.default is not None:
        line += f" [default: {col.default}]"
    if col.extra_def:
        line += f" [{col.extra_def}]"
    if col.comment:
        line += f" — {col.comment}"
    return line


def _format_index(idx: IndexInfo) -> str:
    """Format a single index for the chunk text."""
    cols = ", ".join(idx.columns) if idx.columns else "unknown"
    return f"  - {idx.name} on ({cols})"


def _format_constraint(con: ConstraintInfo) -> str:
    """Format a single constraint for the chunk text."""
    cols = ", ".join(con.columns) if con.columns else ""
    line = f"  - {con.name} ({con.constraint_type})"
    if cols:
        line += f" on ({cols})"
    if con.referenced_table:
        ref_cols = ", ".join(con.referenced_columns) if con.referenced_columns else ""
        line += f" → {con.referenced_table}({ref_cols})"
    return line


def _generate_database_summary_text(schema: DatabaseSchema) -> str:
    """Generate the natural-language text for the database summary chunk."""
    engine = schema.driver.name or "unknown"
    version = schema.driver.database_version or "unknown"

    lines = [
        f"Database: {schema.name}",
        f"Engine: {engine} {version}",
    ]

    if schema.description:
        lines.append(f"Description: {schema.description}")

    lines.append(f"Tables: {len(schema.tables)}")
    lines.append(f"Foreign key relationships: {len(schema.relations)}")

    if schema.enums:
        lines.append(f"Enum types: {len(schema.enums)}")

    if schema.functions:
        lines.append(f"Functions/procedures: {len(schema.functions)}")

    lines.append("")
    lines.append("Table listing:")
    for table in schema.tables:
        col_count = len(table.columns)
        type_label = " (view)" if table.table_type.upper() == "VIEW" else ""
        comment = f" — {table.comment}" if table.comment else ""
        lines.append(f"  - {table.name}{type_label}: {col_count} columns{comment}")

    # Enum types
    if schema.enums:
        lines.append("")
        lines.append("Enum types:")
        for enum in schema.enums:
            values = ", ".join(repr(v) for v in enum.values[:10])
            suffix = f" (and {len(enum.values) - 10} more)" if len(enum.values) > 10 else ""
            lines.append(f"  - {enum.name}: {values}{suffix}")

    return "\n".join(lines)


def _generate_table_text(
    schema: DatabaseSchema,
    table: TableInfo,
    relations: list[RelationInfo],
) -> str:
    """Generate the natural-language text for a table chunk."""
    engine = schema.driver.name or "unknown"

    lines = [
        f"Database: {schema.name} ({engine}) — Table: {table.name}",
    ]

    if table.table_type.upper() == "VIEW":
        lines[0] = f"Database: {schema.name} ({engine}) — View: {table.name}"

    if table.comment:
        lines.append(f"Description: {table.comment}")

    # Columns
    lines.append("")
    lines.append(f"Columns ({len(table.columns)}):")
    for col in table.columns:
        lines.append(_format_column(col))

    # Indexes
    if table.indexes:
        lines.append("")
        lines.append(f"Indexes ({len(table.indexes)}):")
        for idx in table.indexes:
            lines.append(_format_index(idx))

    # Constraints
    if table.constraints:
        lines.append("")
        lines.append(f"Constraints ({len(table.constraints)}):")
        for con in table.constraints:
            lines.append(_format_constraint(con))

    # Foreign key relationships involving this table
    table_relations = [r for r in relations if r.table == table.name or r.parent_table == table.name]
    if table_relations:
        lines.append("")
        lines.append("Relationships:")
        for rel in table_relations:
            child_cols = ", ".join(rel.columns)
            parent_cols = ", ".join(rel.parent_columns)
            if rel.table == table.name:
                lines.append(f"  - {table.name}({child_cols}) → {rel.parent_table}({parent_cols})")
            else:
                lines.append(f"  - {rel.table}({child_cols}) → {table.name}({parent_cols})")

    # Triggers
    if table.triggers:
        lines.append("")
        lines.append(f"Triggers ({len(table.triggers)}):")
        for trigger in table.triggers:
            lines.append(f"  - {trigger.name}")

    return "\n".join(lines)


def _generate_relationship_map_text(schema: DatabaseSchema) -> str:
    """Generate the natural-language text for the relationship map chunk."""
    engine = schema.driver.name or "unknown"

    lines = [
        f"Database: {schema.name} ({engine}) — Foreign Key Relationship Map",
        f"Total relationships: {len(schema.relations)}",
        "",
    ]

    # Group relations by child table
    by_table: dict[str, list[RelationInfo]] = defaultdict(list)
    for rel in schema.relations:
        by_table[rel.table].append(rel)

    for table_name in sorted(by_table.keys()):
        rels = by_table[table_name]
        lines.append(f"{table_name}:")
        for rel in rels:
            child_cols = ", ".join(rel.columns)
            parent_cols = ", ".join(rel.parent_columns)
            cardinality = ""
            if rel.cardinality and rel.parent_cardinality:
                cardinality = f" [{rel.cardinality} → {rel.parent_cardinality}]"
            lines.append(f"  - ({child_cols}) → {rel.parent_table}({parent_cols}){cardinality}")
        lines.append("")

    # Summary of tables with most connections
    connection_count: dict[str, int] = defaultdict(int)
    for rel in schema.relations:
        connection_count[rel.table] += 1
        connection_count[rel.parent_table] += 1

    if connection_count:
        top_tables = sorted(connection_count.items(), key=lambda x: x[1], reverse=True)[:10]
        lines.append("Most connected tables:")
        for table_name, count in top_tables:
            lines.append(f"  - {table_name}: {count} relationships")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main mapping function
# ---------------------------------------------------------------------------


def map_schema_to_chunks(
    schema: DatabaseSchema,
) -> tuple[DatabaseSummaryChunk, list[TableChunk], RelationshipMapChunk | None]:
    """Transform a parsed DatabaseSchema into Qdrant-ready chunks."""
    source_name = schema.source_name_slug

    # --- Database Summary Chunk ---
    summary_text = _generate_database_summary_text(schema)
    summary_hash = _sha256(summary_text)

    db_summary = DatabaseSummaryChunk(
        source_type=SourceType.SQL_SCHEMA,
        source_name=source_name,
        chunk_id=f"{source_name}:db-summary",
        chunk_type=ChunkType.DATABASE_SUMMARY,
        text=summary_text,
        content_hash=summary_hash,
        payload=DatabaseSummaryPayload(
            chunk_id=f"{source_name}:db-summary",
            source_name=source_name,
            db_name=schema.name,
            db_engine=schema.driver.name,
            db_version=schema.driver.database_version,
            table_count=len(schema.tables),
            relation_count=len(schema.relations),
            enum_count=len(schema.enums),
            function_count=len(schema.functions),
            table_names=[t.name for t in schema.tables],
            tags=sorted({tag for t in schema.tables for tag in _infer_tags_from_name(t.name)}),
            content_hash=summary_hash,
        ),
    )

    # --- Table Chunks ---
    table_chunks: list[TableChunk] = []
    for table in schema.tables:
        text = _generate_table_text(schema, table, schema.relations)
        content_hash = _sha256(text)
        tags = _infer_tags_for_table(table, schema.relations)

        fk_tables = _get_fk_tables(table.name, schema.relations)
        ref_by_tables = _get_referenced_by_tables(table.name, schema.relations)

        chunk_id = f"{source_name}:table:{table.name}"

        table_chunks.append(
            TableChunk(
                source_type=SourceType.SQL_SCHEMA,
                source_name=source_name,
                chunk_id=chunk_id,
                chunk_type=ChunkType.TABLE,
                text=text,
                content_hash=content_hash,
                payload=TablePayload(
                    source_name=source_name,
                    chunk_id=chunk_id,
                    db_name=schema.name,
                    table_name=table.name,
                    table_type=table.table_type,
                    table_comment=table.comment,
                    column_count=len(table.columns),
                    index_count=len(table.indexes),
                    constraint_count=len(table.constraints),
                    has_foreign_keys=bool(fk_tables),
                    foreign_key_tables=fk_tables,
                    referenced_by_tables=ref_by_tables,
                    tags=tags,
                    content_hash=content_hash,
                ),
            )
        )

    # --- Relationship Map Chunk (only if relations exist) ---
    relationship_chunk: RelationshipMapChunk | None = None
    if schema.relations:
        rel_text = _generate_relationship_map_text(schema)
        rel_hash = _sha256(rel_text)

        tables_involved = sorted({t for r in schema.relations for t in (r.table, r.parent_table)})

        chunk_id = f"{source_name}:relationships"

        relationship_chunk = RelationshipMapChunk(
            source_type=SourceType.SQL_SCHEMA,
            source_name=source_name,
            chunk_id=chunk_id,
            chunk_type=ChunkType.RELATIONSHIP_MAP,
            text=rel_text,
            content_hash=rel_hash,
            payload=RelationshipMapPayload(
                source_name=source_name,
                chunk_id=chunk_id,
                db_name=schema.name,
                relation_count=len(schema.relations),
                tables_involved=tables_involved,
                tags=["relationships", "foreign-keys", "joins"],
                content_hash=rel_hash,
            ),
        )

    logger.info(
        "Mapped %s: 1 summary + %d table + %s relationship chunks",
        source_name,
        len(table_chunks),
        "1" if relationship_chunk else "0",
    )

    return db_summary, table_chunks, relationship_chunk
