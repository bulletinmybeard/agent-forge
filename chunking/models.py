"""Pydantic2 models for chunk types and their payloads.

These models represent the final output of the mapping pipeline — the chunk files
that will be written to disk and subsequently indexed into Qdrant.

Covers all source types: OpenAPI, SQL Schema, CLI Docs, Code, Document.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class SourceType(str, Enum):
    """Top-level discriminator for knowledge source types."""

    OPENAPI = "openapi"
    SQL_SCHEMA = "sql-schema"
    DOCS = "docs"
    CODE = "code"
    DOCUMENT = "document"


class ChunkType(str, Enum):
    # OpenAPI chunk types
    API_SUMMARY = "api_summary"
    ENDPOINT = "endpoint"
    SCHEMA = "schema"
    # SQL Schema chunk types
    DATABASE_SUMMARY = "database_summary"
    TABLE = "table"
    RELATIONSHIP_MAP = "relationship_map"
    # Docs chunk types (CLI man pages, app documentation)
    DOCS_SUMMARY = "docs_summary"
    COMMAND = "command"
    COMMAND_OPTIONS = "command_options"
    # Code chunk types (Python/Django source code)
    CODE_SUMMARY = "code_summary"
    CODE_CLASS = "code_class"
    CODE_FUNCTION = "code_function"
    CODE_MODULE = "code_module"
    # Document chunk types (Markdown, PDF, etc.)
    DOCUMENT_SUMMARY = "document_summary"
    DOCUMENT_SECTION = "document_section"


class ActionType(str, Enum):
    RETRIEVE = "retrieve"
    LIST = "list"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    UNKNOWN = "unknown"


class SchemaComplexity(str, Enum):
    SIMPLE = "simple"
    MEDIUM = "medium"
    COMPLEX = "complex"
    ENUM = "enum"
    UTILITY = "utility"


# ---------------------------------------------------------------------------
# Payload models (stored as Qdrant metadata for filtered search)
# ---------------------------------------------------------------------------


class ApiSummaryPayload(BaseModel):
    """Qdrant payload fields for an API summary chunk."""

    source_type: SourceType = Field(description="Knowledge source type, e.g., 'openapi', 'sql-schema'")
    source_name: str = Field(description="Generic source identifier (same as api_name for OpenAPI)")
    chunk_type: ChunkType = ChunkType.API_SUMMARY
    chunk_id: str = Field(description="Deterministic ID: {api_name}:summary")
    api_name: str = Field(description="Slug derived from filename or info.title, e.g., 'intranet-api'")
    api_title: str = Field(description="Human-readable API title from info.title")
    api_version: str = Field(description="API version from info.version")
    api_description: str = Field(default="", description="API description from info.description")
    spec_version: str = Field(description="OpenAPI spec version, e.g., '3.1.0'")
    api_base_url: str = Field(default="", description="Base URL from servers[0].url or external config")
    auth_schemes: list[str] = Field(default_factory=list, description="Security scheme names")
    endpoint_count: int = Field(description="Total number of endpoints (path+method combos)")
    domain_groups: list[str] = Field(default_factory=list, description="Inferred domain groups from path prefixes")
    tags: list[str] = Field(default_factory=list, description="Enrichment tags for improved retrieval")
    version_is_placeholder: bool = Field(default=False, description="True if version is '0.0.0' or empty")
    content_hash: str = Field(description="SHA256 of the chunk text for dedup on re-index")
    last_indexed: datetime = Field(default_factory=datetime.utcnow)


class EndpointPayload(BaseModel):
    """Qdrant payload fields for an endpoint chunk."""

    source_type: SourceType = Field(description="Knowledge source type, e.g., 'openapi', 'sql-schema'")
    source_name: str = Field(description="Generic source identifier (same as api_name for OpenAPI)")
    chunk_type: ChunkType = ChunkType.ENDPOINT
    chunk_id: str = Field(description="Deterministic ID: {api_name}:{method}:{path}")
    api_name: str
    api_version: str
    path: str = Field(description="Endpoint path, e.g., '/demarcation-paths'")
    method: str = Field(description="HTTP method uppercase, e.g., 'GET'")
    summary: str = Field(default="")
    operation_id: str = Field(default="")
    domain_group: str = Field(default="", description="Inferred from path prefix")
    action_type: ActionType = Field(default=ActionType.UNKNOWN)
    tags: list[str] = Field(default_factory=list)
    parameters_raw: Optional[str] = Field(default=None, description="Original parameters JSON for AI model")
    request_body_raw: Optional[str] = Field(default=None, description="Original requestBody JSON for AI model")
    response_raw: Optional[str] = Field(default=None, description="Original responses JSON for AI model")
    security: list[str] = Field(default_factory=list)
    has_request_body: bool = Field(default=False)
    param_count: int = Field(default=0)
    response_schema: str = Field(default="", description="e.g., 'DemarcationDetails[]' or 'SalesOrderV0'")
    content_hash: str = Field(default="")
    last_indexed: datetime = Field(default_factory=datetime.utcnow)


class SchemaPayload(BaseModel):
    """Qdrant payload fields for a schema chunk."""

    source_type: SourceType = Field(description="Knowledge source type, e.g., 'openapi', 'sql-schema'")
    source_name: str = Field(description="Generic source identifier (same as api_name for OpenAPI)")
    chunk_type: ChunkType = ChunkType.SCHEMA
    chunk_id: str = Field(description="Deterministic ID: {api_name}:schema:{schema_name}")
    api_name: str
    api_version: str
    schema_name: str = Field(description="Original schema name from components.schemas")
    schema_display_name: str = Field(description="Cleaned display name")
    schema_type: str = Field(default="object", description="'object', 'string' (enum), etc.")
    complexity: SchemaComplexity = Field(default=SchemaComplexity.SIMPLE)
    field_count: int = Field(default=0)
    required_field_count: int = Field(default=0)
    referenced_by_endpoints: list[str] = Field(default_factory=list, description="e.g., ['get:/demarcation-paths']")
    references_schemas: list[str] = Field(default_factory=list, description="Schema names this schema references")
    tags: list[str] = Field(default_factory=list)
    content_hash: str = Field(default="")
    last_indexed: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# SQL Schema payload models
# ---------------------------------------------------------------------------


class DatabaseSummaryPayload(BaseModel):
    """Qdrant payload fields for a database summary chunk."""

    source_type: SourceType = Field(default=SourceType.SQL_SCHEMA)
    source_name: str = Field(description="Database source name slug, e.g., 'portal-db'")
    chunk_type: ChunkType = Field(default=ChunkType.DATABASE_SUMMARY)
    chunk_id: str = Field(description="Deterministic ID: {source_name}:db-summary")
    db_name: str = Field(description="Database name from tbls output")
    db_engine: str = Field(default="", description="Database engine, e.g., 'mysql', 'postgresql'")
    db_version: str = Field(default="", description="Database server version if available")
    table_count: int = Field(default=0)
    relation_count: int = Field(default=0)
    enum_count: int = Field(default=0)
    function_count: int = Field(default=0)
    table_names: list[str] = Field(default_factory=list, description="All table names for quick lookup")
    tags: list[str] = Field(default_factory=list)
    content_hash: str = Field(default="")
    last_indexed: datetime = Field(default_factory=datetime.utcnow)


class TablePayload(BaseModel):
    """Qdrant payload fields for a table chunk."""

    source_type: SourceType = Field(default=SourceType.SQL_SCHEMA)
    source_name: str = Field(description="Database source name slug")
    chunk_type: ChunkType = Field(default=ChunkType.TABLE)
    chunk_id: str = Field(description="Deterministic ID: {source_name}:table:{table_name}")
    db_name: str = Field(description="Database name")
    table_name: str = Field(description="Table name, e.g., 'users'")
    table_type: str = Field(default="BASE TABLE", description="e.g., 'BASE TABLE', 'VIEW'")
    table_comment: str = Field(default="")
    column_count: int = Field(default=0)
    index_count: int = Field(default=0)
    constraint_count: int = Field(default=0)
    has_foreign_keys: bool = Field(default=False)
    foreign_key_tables: list[str] = Field(default_factory=list, description="Tables this table references via FK")
    referenced_by_tables: list[str] = Field(default_factory=list, description="Tables that reference this table via FK")
    tags: list[str] = Field(default_factory=list)
    content_hash: str = Field(default="")
    last_indexed: datetime = Field(default_factory=datetime.utcnow)


class RelationshipMapPayload(BaseModel):
    """Qdrant payload fields for a relationship map chunk."""

    source_type: SourceType = Field(default=SourceType.SQL_SCHEMA)
    source_name: str = Field(description="Database source name slug")
    chunk_type: ChunkType = Field(default=ChunkType.RELATIONSHIP_MAP)
    chunk_id: str = Field(description="Deterministic ID: {source_name}:relationships")
    db_name: str = Field(description="Database name")
    relation_count: int = Field(default=0)
    tables_involved: list[str] = Field(default_factory=list, description="All tables that participate in relations")
    tags: list[str] = Field(default_factory=list)
    content_hash: str = Field(default="")
    last_indexed: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Top-level chunk models (text + payload, written as JSON files)
# ---------------------------------------------------------------------------


class Chunk(BaseModel):
    """Base chunk model representing a single indexable unit."""

    source_type: SourceType = Field(description="Knowledge source type")
    source_name: str = Field(description="Generic source identifier")
    chunk_id: str
    chunk_type: ChunkType
    text: str = Field(description="Natural-language text for embedding")
    content_hash: str = Field(description="SHA256 of the text field")


class ApiSummaryChunk(Chunk):
    """A chunk representing the full API summary."""

    chunk_type: ChunkType = ChunkType.API_SUMMARY
    payload: ApiSummaryPayload


class EndpointChunk(Chunk):
    """A chunk representing a single endpoint (path + method)."""

    chunk_type: ChunkType = ChunkType.ENDPOINT
    payload: EndpointPayload


class SchemaChunk(Chunk):
    """A chunk representing a reusable schema from components.schemas."""

    chunk_type: ChunkType = ChunkType.SCHEMA
    payload: SchemaPayload


# ---------------------------------------------------------------------------
# SQL Schema chunk models
# ---------------------------------------------------------------------------


class DatabaseSummaryChunk(Chunk):
    """A chunk representing the full database summary."""

    chunk_type: ChunkType = ChunkType.DATABASE_SUMMARY
    payload: DatabaseSummaryPayload


class TableChunk(Chunk):
    """A chunk representing a single database table with columns, indexes, and constraints."""

    chunk_type: ChunkType = ChunkType.TABLE
    payload: TablePayload


class RelationshipMapChunk(Chunk):
    """A chunk describing all foreign key relationships across the database."""

    chunk_type: ChunkType = ChunkType.RELATIONSHIP_MAP
    payload: RelationshipMapPayload


# ---------------------------------------------------------------------------
# Docs payload models (CLI man pages, app documentation)
# ---------------------------------------------------------------------------


class DocsSummaryPayload(BaseModel):
    """Qdrant payload fields for a docs/CLI tool summary chunk."""

    source_type: SourceType = Field(default=SourceType.DOCS)
    source_name: str = Field(description="Tool source name slug, e.g., 'gitcli'")
    chunk_type: ChunkType = Field(default=ChunkType.DOCS_SUMMARY)
    chunk_id: str = Field(description="Deterministic ID: {source_name}:docs-summary")
    tool_name: str = Field(description="CLI tool name, e.g., 'git'")
    tool_version: str = Field(default="", description="Tool version if available")
    description: str = Field(default="", description="One-line tool description from NAME section")
    command_count: int = Field(default=0, description="Total number of subcommands")
    command_names: list[str] = Field(default_factory=list, description="All subcommand names")
    tags: list[str] = Field(default_factory=list)
    content_hash: str = Field(default="")
    last_indexed: datetime = Field(default_factory=datetime.utcnow)


class CommandPayload(BaseModel):
    """Qdrant payload fields for a single CLI command/subcommand chunk."""

    source_type: SourceType = Field(default=SourceType.DOCS)
    source_name: str = Field(description="Tool source name slug, e.g., 'gitcli'")
    chunk_type: ChunkType = Field(default=ChunkType.COMMAND)
    chunk_id: str = Field(description="Deterministic ID: {source_name}:cmd:{command}")
    tool_name: str = Field(description="CLI tool name, e.g., 'git'")
    command: str = Field(description="Full command, e.g., 'git commit'")
    summary: str = Field(default="", description="One-line description from NAME section")
    has_subcommands: bool = Field(default=False)
    option_count: int = Field(default=0, description="Number of options/flags")
    has_examples: bool = Field(default=False)
    tags: list[str] = Field(default_factory=list)
    content_hash: str = Field(default="")
    last_indexed: datetime = Field(default_factory=datetime.utcnow)


class CommandOptionsPayload(BaseModel):
    """Qdrant payload fields for an overflow options chunk (large commands)."""

    source_type: SourceType = Field(default=SourceType.DOCS)
    source_name: str = Field(description="Tool source name slug, e.g., 'gitcli'")
    chunk_type: ChunkType = Field(default=ChunkType.COMMAND_OPTIONS)
    chunk_id: str = Field(description="Deterministic ID: {source_name}:cmd-opts:{command}")
    tool_name: str = Field(description="CLI tool name, e.g., 'git'")
    command: str = Field(description="Full command this options chunk belongs to")
    option_count: int = Field(default=0)
    tags: list[str] = Field(default_factory=list)
    content_hash: str = Field(default="")
    last_indexed: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Docs chunk models
# ---------------------------------------------------------------------------


class DocsSummaryChunk(Chunk):
    """A chunk representing the full CLI tool summary."""

    chunk_type: ChunkType = ChunkType.DOCS_SUMMARY
    payload: DocsSummaryPayload


class CommandChunk(Chunk):
    """A chunk representing a single CLI command/subcommand."""

    chunk_type: ChunkType = ChunkType.COMMAND
    payload: CommandPayload


class CommandOptionsChunk(Chunk):
    """A chunk for overflow OPTIONS when a command is too large."""

    chunk_type: ChunkType = ChunkType.COMMAND_OPTIONS
    payload: CommandOptionsPayload


# ---------------------------------------------------------------------------
# Code payload models (Python/Django source code)
# ---------------------------------------------------------------------------


class CodeSummaryPayload(BaseModel):
    """Qdrant payload fields for a code project summary chunk."""

    source_type: SourceType = Field(default=SourceType.CODE)
    source_name: str = Field(description="Project source name slug, e.g., 'my-api'")
    chunk_type: ChunkType = Field(default=ChunkType.CODE_SUMMARY)
    chunk_id: str = Field(description="Deterministic ID: {source_name}:code-summary")
    project_name: str = Field(description="Human-readable project name")
    language: str = Field(default="python", description="Primary language")
    framework: str = Field(default="", description="Detected framework, e.g., 'django', 'fastapi'")
    file_count: int = Field(default=0, description="Total Python files processed")
    class_count: int = Field(default=0)
    function_count: int = Field(default=0)
    class_names: list[str] = Field(default_factory=list, description="All class names for quick lookup")
    tag_distribution: dict[str, int] = Field(
        default_factory=dict, description="e.g., {'model': 134, 'serializer': 199}"
    )
    tags: list[str] = Field(default_factory=list)
    content_hash: str = Field(default="")
    last_indexed: datetime = Field(default_factory=datetime.utcnow)


class CodeClassPayload(BaseModel):
    """Qdrant payload fields for a code class chunk."""

    source_type: SourceType = Field(default=SourceType.CODE)
    source_name: str = Field(description="Project source name slug")
    chunk_type: ChunkType = Field(default=ChunkType.CODE_CLASS)
    chunk_id: str = Field(description="Deterministic ID: {source_name}:class:{class_name}")
    project_name: str = Field(description="Human-readable project name")
    class_name: str = Field(description="Class name, e.g., 'UserProfile'")
    tag: str = Field(default="class", description="Django tag: model, serializer, view, viewset, etc.")
    file_path: str = Field(description="Relative file path within the project")
    line_number: int = Field(default=0)
    bases: list[str] = Field(default_factory=list, description="Base class names")
    decorators: list[str] = Field(default_factory=list)
    method_count: int = Field(default=0)
    method_names: list[str] = Field(default_factory=list, description="Public method names")
    has_docstring: bool = Field(default=False)
    tags: list[str] = Field(default_factory=list, description="Enrichment tags for retrieval")
    content_hash: str = Field(default="")
    last_indexed: datetime = Field(default_factory=datetime.utcnow)


class CodeFunctionPayload(BaseModel):
    """Qdrant payload fields for a top-level function chunk."""

    source_type: SourceType = Field(default=SourceType.CODE)
    source_name: str = Field(description="Project source name slug")
    chunk_type: ChunkType = Field(default=ChunkType.CODE_FUNCTION)
    chunk_id: str = Field(description="Deterministic ID: {source_name}:func:{function_name}")
    project_name: str = Field(description="Human-readable project name")
    function_name: str = Field(description="Function name")
    tag: str = Field(default="function", description="Detected tag: function, route, task, signal_handler, etc.")
    file_path: str = Field(description="Relative file path within the project")
    line_number: int = Field(default=0)
    signature: str = Field(default="", description="Function signature string")
    decorators: list[str] = Field(default_factory=list)
    is_async: bool = Field(default=False)
    has_docstring: bool = Field(default=False)
    tags: list[str] = Field(default_factory=list)
    content_hash: str = Field(default="")
    last_indexed: datetime = Field(default_factory=datetime.utcnow)


class CodeModulePayload(BaseModel):
    """Qdrant payload fields for a module-level docstring chunk."""

    source_type: SourceType = Field(default=SourceType.CODE)
    source_name: str = Field(description="Project source name slug")
    chunk_type: ChunkType = Field(default=ChunkType.CODE_MODULE)
    chunk_id: str = Field(description="Deterministic ID: {source_name}:module:{module_path}")
    project_name: str = Field(description="Human-readable project name")
    module_name: str = Field(description="Module filename without extension")
    file_path: str = Field(description="Relative file path within the project")
    has_docstring: bool = Field(default=True)
    tags: list[str] = Field(default_factory=list)
    content_hash: str = Field(default="")
    last_indexed: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Code chunk models
# ---------------------------------------------------------------------------


class CodeSummaryChunk(Chunk):
    """A chunk representing the full code project summary."""

    chunk_type: ChunkType = ChunkType.CODE_SUMMARY
    payload: CodeSummaryPayload


class CodeClassChunk(Chunk):
    """A chunk representing a single class (model, serializer, view, etc.)."""

    chunk_type: ChunkType = ChunkType.CODE_CLASS
    payload: CodeClassPayload


class CodeFunctionChunk(Chunk):
    """A chunk representing a top-level function."""

    chunk_type: ChunkType = ChunkType.CODE_FUNCTION
    payload: CodeFunctionPayload


class CodeModuleChunk(Chunk):
    """A chunk representing a module-level docstring."""

    chunk_type: ChunkType = ChunkType.CODE_MODULE
    payload: CodeModulePayload


# ---------------------------------------------------------------------------
# Document payload models (Markdown, PDF, etc.)
# ---------------------------------------------------------------------------


class DocumentSummaryPayload(BaseModel):
    """Qdrant payload fields for a document source summary chunk."""

    source_type: SourceType = Field(default=SourceType.DOCUMENT)
    source_name: str = Field(description="Document source name slug, e.g., 'agentforge-docs'")
    chunk_type: ChunkType = Field(default=ChunkType.DOCUMENT_SUMMARY)
    chunk_id: str = Field(description="Deterministic ID: {source_name}:doc-summary")
    document_count: int = Field(default=0, description="Number of documents in this source")
    section_count: int = Field(default=0, description="Total sections across all documents")
    document_names: list[str] = Field(default_factory=list, description="All document filenames")
    document_types: list[str] = Field(default_factory=list, description="Distinct document types found")
    tags: list[str] = Field(default_factory=list)
    content_hash: str = Field(default="")
    last_indexed: datetime = Field(default_factory=datetime.utcnow)


class DocumentSectionPayload(BaseModel):
    """Qdrant payload fields for a single document section chunk."""

    source_type: SourceType = Field(default=SourceType.DOCUMENT)
    source_name: str = Field(description="Document source name slug")
    chunk_type: ChunkType = Field(default=ChunkType.DOCUMENT_SECTION)
    chunk_id: str = Field(description="Deterministic ID: {source_name}:section:{doc_name}:{section_slug}")
    document_name: str = Field(description="Filename without extension, e.g., 'CHANGELOG'")
    document_type: str = Field(
        default="general",
        description="Semantic category: changelog, readme, guide, release-notes, general",
    )
    document_ext: str = Field(default=".md", description="Original file extension, e.g., '.md', '.pdf'")
    file_path: str = Field(default="", description="Relative path from source root")
    section_title: str = Field(default="", description="Heading text that starts this section")
    section_level: int = Field(default=1, description="Heading depth (1 = #, 2 = ##, etc.)")
    section_index: int = Field(default=0, description="Position within the document (ordering)")
    word_count: int = Field(default=0)
    has_code_blocks: bool = Field(default=False)
    tags: list[str] = Field(default_factory=list)
    content_hash: str = Field(default="")
    last_indexed: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Document chunk models
# ---------------------------------------------------------------------------


class DocumentSummaryChunk(Chunk):
    """A chunk representing the summary of a document source."""

    chunk_type: ChunkType = ChunkType.DOCUMENT_SUMMARY
    payload: DocumentSummaryPayload


class DocumentSectionChunk(Chunk):
    """A chunk representing a single section within a document."""

    chunk_type: ChunkType = ChunkType.DOCUMENT_SECTION
    payload: DocumentSectionPayload
