"""Dataclasses for intermediate parsing of OpenAPI JSON structures.

These represent the raw extracted data from the OpenAPI spec before
the mapper transforms them into Qdrant-ready chunk models.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ParameterInfo:
    """A single endpoint parameter (query, path, header, cookie)."""

    name: str
    location: str  # "query", "path", "header", "cookie"
    required: bool
    param_type: str  # "string", "integer", "boolean", etc.
    title: str = ""
    format: str = ""  # "date", "date-time", etc.
    default: str | None = None
    enum_values: list[str] = field(default_factory=list)
    pattern: str = ""
    minimum: float | None = None
    maximum: float | None = None


@dataclass
class SchemaFieldInfo:
    """A single field within a schema."""

    name: str
    field_type: str  # "string", "integer", "array", "object", etc.
    title: str = ""
    format: str = ""
    required: bool = False
    default: str | None = None
    ref: str = ""  # resolved $ref schema name, e.g., "LoaDetails"
    items_ref: str = ""  # for array fields, the $ref of the items
    enum_values: list[str] = field(default_factory=list)
    pattern: str = ""
    minimum: float | None = None
    maximum: float | None = None
    union_types: list[str] = field(default_factory=list)  # from anyOf/oneOf


@dataclass
class RequestBodyInfo:
    """Extracted request body information."""

    required: bool
    media_type: str  # e.g., "application/json"
    schema_ref: str  # resolved $ref schema name
    additional_media_types: list[str] = field(default_factory=list)


@dataclass
class ResponseInfo:
    """Extracted response information for a single status code."""

    status_code: str  # "200", "422", etc.
    description: str
    schema_ref: str = ""  # resolved $ref schema name
    schema_type: str = ""  # "object", "array", etc.
    items_ref: str = ""  # for array responses, the $ref of items


@dataclass
class EndpointInfo:
    """All extracted data for a single endpoint (path + method)."""

    path: str
    method: str  # uppercase: "GET", "POST", etc.
    summary: str
    description: str = ""
    operation_id: str = ""
    tags: list[str] = field(default_factory=list)
    parameters: list[ParameterInfo] = field(default_factory=list)
    request_body: RequestBodyInfo | None = None
    responses: list[ResponseInfo] = field(default_factory=list)
    security: list[str] = field(default_factory=list)
    # Original JSON strings for AI model consumption
    parameters_raw: str = ""
    request_body_raw: str = ""
    responses_raw: str = ""


@dataclass
class SchemaInfo:
    """All extracted data for a single schema from components.schemas."""

    name: str  # original name, e.g., "intranet_api__schema__finance__read__Contract"
    title: str = ""
    description: str = ""
    schema_type: str = "object"  # "object", "string" (for enums), etc.
    fields: list[SchemaFieldInfo] = field(default_factory=list)
    required_fields: list[str] = field(default_factory=list)
    enum_values: list[str] = field(default_factory=list)  # for enum-type schemas
    referenced_by: list[str] = field(default_factory=list)  # e.g., ["get:/demarcation-paths"]
    references: list[str] = field(default_factory=list)  # schema names this schema references


@dataclass
class SecuritySchemeInfo:
    """Extracted security scheme information."""

    name: str
    scheme_type: str  # "apiKey", "http", "oauth2", etc.
    location: str = ""  # "header", "query", "cookie" (for apiKey)
    param_name: str = ""  # e.g., "api-key" (the actual header/query name)


@dataclass
class OpenApiSpec:
    """Complete parsed representation of an OpenAPI JSON file."""

    spec_version: str  # e.g., "3.1.0"
    title: str
    version: str  # API version, e.g., "1.2.34"
    description: str = ""
    base_url: str = ""
    security_schemes: list[SecuritySchemeInfo] = field(default_factory=list)
    endpoints: list[EndpointInfo] = field(default_factory=list)
    schemas: list[SchemaInfo] = field(default_factory=list)
    # Derived
    source_filename: str = ""
    api_name_slug: str = ""  # e.g., "intranet-api"
