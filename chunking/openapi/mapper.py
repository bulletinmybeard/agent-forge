"""OpenAPI mapper: transforms parsed OpenApiSpec into Qdrant-ready chunks.

Takes the intermediate dataclasses from the parser and produces:
- One ApiSummaryChunk per API
- One EndpointChunk per path+method
- One SchemaChunk per medium/complex schema

Each chunk contains a natural-language text field (for embedding) and
a structured payload (for Qdrant filtered search).
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import defaultdict

from chunking.models import (
    ActionType,
    ApiSummaryChunk,
    ApiSummaryPayload,
    ChunkType,
    EndpointChunk,
    EndpointPayload,
    SchemaChunk,
    SchemaComplexity,
    SchemaPayload,
    SourceType,
)
from chunking.openapi.types import (
    EndpointInfo,
    OpenApiSpec,
    ParameterInfo,
    ResponseInfo,
    SchemaInfo,
    SecuritySchemeInfo,
)

logger = logging.getLogger(__name__)

# Threshold: schemas with this many fields or fewer are considered simple
_INLINE_SCHEMA_MAX_FIELDS = 5

# Stop words to exclude from tag inference
_TAG_STOP_WORDS = {
    "get",
    "post",
    "put",
    "delete",
    "patch",
    "fetch",
    "list",
    "create",
    "update",
    "store",
    "by",
    "of",
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
    "id",
    "ids",
}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Tag inference
# ---------------------------------------------------------------------------


def _infer_tags_from_path(path: str) -> list[str]:
    """Extract meaningful tags from URL path segments."""
    segments = [s for s in path.strip("/").split("/") if s and not s.startswith("{")]
    tags: list[str] = []
    for seg in segments:
        # Split hyphenated and underscored words
        words = re.split(r"[-_]", seg.lower())
        tags.extend(w for w in words if w and w not in _TAG_STOP_WORDS)
    return tags


def _infer_tags_from_summary(summary: str) -> list[str]:
    """Extract meaningful tags from the endpoint summary."""
    words = re.findall(r"[A-Za-z]+", summary)
    return [w.lower() for w in words if len(w) > 2 and w.lower() not in _TAG_STOP_WORDS]


def _infer_tags_for_endpoint(endpoint: EndpointInfo, response_schema: str) -> list[str]:
    """Generate enrichment tags for an endpoint."""
    tags: set[str] = set()

    # From explicit tags
    tags.update(t.lower() for t in endpoint.tags)

    # From path segments
    tags.update(_infer_tags_from_path(endpoint.path))

    # From summary
    tags.update(_infer_tags_from_summary(endpoint.summary))

    # From request body schema name
    if endpoint.request_body and endpoint.request_body.schema_ref:
        schema_words = re.findall(r"[A-Z][a-z]+", endpoint.request_body.schema_ref)
        tags.update(w.lower() for w in schema_words if w.lower() not in _TAG_STOP_WORDS)

    # From response schema name
    if response_schema:
        clean = response_schema.rstrip("[]")
        schema_words = re.findall(r"[A-Z][a-z]+", clean)
        tags.update(w.lower() for w in schema_words if w.lower() not in _TAG_STOP_WORDS)

    # Add action type as tag
    action = _infer_action_type(endpoint)
    if action != ActionType.UNKNOWN:
        tags.add(action.value)

    return sorted(tags)


# ---------------------------------------------------------------------------
# Domain group & action type inference
# ---------------------------------------------------------------------------


def _infer_domain_group(path: str) -> str:
    """Infer a domain group from the first meaningful path segment."""
    segments = [s for s in path.strip("/").split("/") if s and not s.startswith("{")]
    if not segments:
        return "general"
    first = segments[0].lower()
    # Handle hyphenated prefixes like "demarcation-paths" → "demarcation"
    return first.split("-")[0] if "-" in first else first


def _infer_action_type(endpoint: EndpointInfo) -> ActionType:
    """Infer the action type from HTTP method and summary."""
    method = endpoint.method.upper()
    summary_lower = endpoint.summary.lower()

    if method == "DELETE":
        return ActionType.DELETE
    if method == "PUT" or method == "PATCH":
        return ActionType.UPDATE
    if method == "POST":
        if any(w in summary_lower for w in ("create", "store", "add", "convert")):
            return ActionType.CREATE
        # Some POSTs are query-like (e.g.,, POST /loa)
        return ActionType.CREATE
    if method == "GET":
        if any(w in summary_lower for w in ("list", "get all", "fetch all")):
            return ActionType.LIST
        if any(w in summary_lower for w in ("fetch", "get", "retrieve", "find")):
            return ActionType.RETRIEVE
        return ActionType.RETRIEVE

    return ActionType.UNKNOWN


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------


def _clean_schema_display_name(name: str) -> str:
    """Clean module-qualified schema names.

    'intranet_api__schema__finance__read__Contract' → 'Contract (finance, read)'
    'DemarcationDetails' → 'DemarcationDetails'
    """
    if "__" in name:
        parts = name.split("__")
        # Last part is the actual name, middle parts are qualifiers
        actual_name = parts[-1]
        qualifiers = [p for p in parts[1:-1] if p not in ("schema",)]
        if qualifiers:
            return f"{actual_name} ({', '.join(qualifiers)})"
        return actual_name
    return name


def _classify_schema_complexity(schema: SchemaInfo) -> SchemaComplexity:
    """Classify schema complexity."""
    if schema.enum_values:
        return SchemaComplexity.ENUM
    if schema.name in ("HTTPValidationError", "ValidationError", "Response"):
        return SchemaComplexity.UTILITY
    has_refs = any(f.ref or f.items_ref for f in schema.fields)
    field_count = len(schema.fields)
    if field_count <= _INLINE_SCHEMA_MAX_FIELDS and not has_refs:
        return SchemaComplexity.SIMPLE
    if field_count > 15 or (has_refs and field_count > 10):
        return SchemaComplexity.COMPLEX
    return SchemaComplexity.MEDIUM


def _resolve_response_schema(endpoint: EndpointInfo) -> str:
    """Determine the response schema string for the 200 response."""
    for resp in endpoint.responses:
        if resp.status_code in ("200", "201"):
            if resp.items_ref:
                return f"{resp.items_ref}[]"
            if resp.schema_ref:
                return resp.schema_ref
            break
    return ""


# ---------------------------------------------------------------------------
# Auth description helper
# ---------------------------------------------------------------------------


def _describe_auth_schemes(schemes: list[SecuritySchemeInfo]) -> str:
    """Generate a human-readable auth description."""
    if not schemes:
        return "None"
    parts: list[str] = []
    for s in schemes:
        if s.scheme_type == "apiKey":
            parts.append(f"API Key via {s.location} ({s.param_name})")
        elif s.scheme_type == "http":
            parts.append(f"HTTP {s.param_name}")
        else:
            parts.append(f"{s.scheme_type}: {s.name}")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Text generation: natural-language chunk text for embedding
# ---------------------------------------------------------------------------


def _generate_api_summary_text(spec: OpenApiSpec, domain_groups: dict[str, list[str]], endpoint_count: int) -> str:
    """Generate the natural-language text for the API summary chunk."""
    auth_desc = _describe_auth_schemes(spec.security_schemes)

    lines = [
        f"{spec.title} (version {spec.version})",
    ]
    if spec.description:
        lines.append(spec.description)
    lines.append(f"Base URL: {spec.base_url or 'not specified'}")
    lines.append(f"Authentication: {auth_desc}")
    lines.append(f"OpenAPI Specification: {spec.spec_version}")
    lines.append("")
    lines.append(f"This API provides {endpoint_count} endpoints organized in the following areas:")

    for group, paths in sorted(domain_groups.items()):
        lines.append(f"- {group}: {', '.join(paths)}")

    return "\n".join(lines)


def _format_parameter(param: ParameterInfo) -> str:
    """Format a single parameter for the chunk text."""
    type_str = param.param_type
    if param.format:
        type_str = f"{type_str}/{param.format}"
    req = "required" if param.required else "optional"
    parts = [f"  - {param.name} ({type_str}, {req}, {param.location})"]
    if param.default is not None:
        parts[0] += f" [default: {param.default}]"
    if param.enum_values:
        parts[0] += f" [values: {', '.join(param.enum_values)}]"
    if param.title and param.title.lower().replace(" ", "_") != param.name:
        parts.append(f"    Title: {param.title}")
    return "\n".join(parts)


def _format_schema_fields_inline(schema: SchemaInfo) -> list[str]:
    """Format schema fields for inline inclusion in endpoint chunk text."""
    lines: list[str] = []
    for f in schema.fields:
        type_str = f.field_type
        if f.format:
            type_str = f"{type_str}/{f.format}"
        if f.ref:
            type_str = f.ref
        if f.items_ref:
            type_str = f"array of {f.items_ref}"
        if f.union_types:
            type_str = "|".join(f.union_types)
        req = "required" if f.required else "optional"
        line = f"    - {f.name} ({type_str}, {req})"
        if f.default is not None:
            line += f" [default: {f.default}]"
        lines.append(line)
    return lines


def _format_response(resp: ResponseInfo) -> str:
    """Format a single response for the chunk text."""
    line = f"Response {resp.status_code}: {resp.description}"
    if resp.items_ref:
        line += f"\n  Returns: array of {resp.items_ref}"
    elif resp.schema_ref:
        line += f"\n  Returns: {resp.schema_ref}"
    return line


def _generate_endpoint_text(
    spec: OpenApiSpec,
    endpoint: EndpointInfo,
    domain_group: str,
    action_type: ActionType,
    response_schema: str,
    schema_map: dict[str, SchemaInfo],
) -> str:
    """Generate the natural-language text for an endpoint chunk."""
    lines = [
        f"{spec.title} (v{spec.version}) — {endpoint.method} {endpoint.path}",
        f"Summary: {endpoint.summary}",
    ]

    if endpoint.description:
        lines.append(f"Description: {endpoint.description}")

    lines.append(f"Operation ID: {endpoint.operation_id}")
    lines.append(f"Domain: {domain_group}")
    lines.append(f"Action: {action_type.value}")

    # Parameters
    if endpoint.parameters:
        lines.append("")
        lines.append("Parameters:")
        for param in endpoint.parameters:
            lines.append(_format_parameter(param))

    # Request body
    if endpoint.request_body:
        rb = endpoint.request_body
        lines.append("")
        req = "required" if rb.required else "optional"
        lines.append(f"Request Body ({req}):")
        lines.append(f"  Content-Type: {rb.media_type}")
        lines.append(f"  Schema: {_clean_schema_display_name(rb.schema_ref)}")

        # Inline the schema fields if available
        if rb.schema_ref in schema_map:
            schema = schema_map[rb.schema_ref]
            lines.append("  Fields:")
            lines.extend(_format_schema_fields_inline(schema))

        if rb.additional_media_types:
            lines.append(f"  Also accepts: {', '.join(rb.additional_media_types)}")

    # Responses
    if endpoint.responses:
        lines.append("")
        for resp in endpoint.responses:
            lines.append(_format_response(resp))

            # Inline key fields for 200 response schema if it's simple
            if resp.status_code in ("200", "201"):
                ref_name = resp.items_ref or resp.schema_ref
                if ref_name and ref_name in schema_map:
                    schema = schema_map[ref_name]
                    complexity = _classify_schema_complexity(schema)
                    if complexity in (SchemaComplexity.SIMPLE, SchemaComplexity.MEDIUM) and schema.fields:
                        key_fields = ", ".join(f"{f.name} ({f.field_type})" for f in schema.fields[:8])
                        lines.append(f"  Key fields: {key_fields}")

    # Auth
    auth_desc = _describe_auth_schemes(spec.security_schemes)
    lines.append(f"\nAuthentication: {auth_desc}")

    return "\n".join(lines)


def _generate_schema_text(
    spec: OpenApiSpec,
    schema: SchemaInfo,
    complexity: SchemaComplexity,
) -> str:
    """Generate the natural-language text for a schema chunk."""
    display_name = _clean_schema_display_name(schema.name)

    lines = [
        f"{spec.title} (v{spec.version}) — Schema: {display_name}",
        f"Type: {schema.schema_type}",
    ]

    if schema.description:
        lines.append(f"Description: {schema.description}")

    lines.append(f"Complexity: {complexity.value}")

    # Enum values
    if schema.enum_values:
        lines.append("")
        lines.append(f"Allowed values: {', '.join(repr(v) for v in schema.enum_values)}")

    # Object fields
    if schema.fields:
        required_count = len(schema.required_fields)
        lines.append("")
        lines.append(f"Fields ({len(schema.fields)}, {required_count} required):")
        for f in schema.fields:
            type_str = f.field_type
            if f.format:
                type_str = f"{type_str}/{f.format}"
            if f.ref:
                type_str = f.ref
            if f.items_ref:
                type_str = f"array of {f.items_ref}"
            if f.union_types:
                type_str = "|".join(f.union_types)

            req = "required" if f.required else "optional"
            line = f"  - {f.name} ({type_str}, {req})"
            if f.default is not None:
                line += f" [default: {f.default}]"
            if f.ref:
                line += f" → references {f.ref}"
            if f.items_ref:
                line += f" → references {f.items_ref}"
            if f.enum_values:
                line += f" [values: {', '.join(f.enum_values)}]"
            if f.pattern:
                line += f" [pattern: {f.pattern}]"
            lines.append(line)

    # Referenced by
    if schema.referenced_by:
        lines.append("")
        lines.append("Used by endpoints:")
        for ref in schema.referenced_by:
            method, path = ref.split(":", 1)
            lines.append(f"  - {method.upper()} {path}")

    return "\n".join(lines)


def _infer_tags_for_schema(schema: SchemaInfo) -> list[str]:
    """Generate enrichment tags for a schema."""
    tags: set[str] = set()

    # From the schema name (split CamelCase)
    words = re.findall(r"[A-Z][a-z]+", _clean_schema_display_name(schema.name))
    tags.update(w.lower() for w in words if w.lower() not in _TAG_STOP_WORDS)

    # From module-qualified parts
    if "__" in schema.name:
        parts = schema.name.split("__")
        tags.update(p.lower() for p in parts if p.lower() not in ("schema",) and len(p) > 2)

    # From field names
    for f in schema.fields[:10]:  # Limit to avoid tag explosion
        tags.update(w.lower() for w in f.name.split("_") if len(w) > 2 and w.lower() not in _TAG_STOP_WORDS)

    return sorted(tags)


# ---------------------------------------------------------------------------
# Main mapping function
# ---------------------------------------------------------------------------


def map_spec_to_chunks(
    spec: OpenApiSpec,
    inline_schema_max_fields: int = _INLINE_SCHEMA_MAX_FIELDS,
) -> tuple[ApiSummaryChunk, list[EndpointChunk], list[SchemaChunk]]:
    """Transform a parsed OpenApiSpec into Qdrant-ready chunks."""
    global _INLINE_SCHEMA_MAX_FIELDS
    _INLINE_SCHEMA_MAX_FIELDS = inline_schema_max_fields

    schema_map = {s.name: s for s in spec.schemas}
    api_name = spec.api_name_slug

    # --- Domain groups ---
    domain_groups: dict[str, list[str]] = defaultdict(list)
    for ep in spec.endpoints:
        group = _infer_domain_group(ep.path)
        methods_on_path = [e.method for e in spec.endpoints if e.path == ep.path]
        if len(methods_on_path) > 1:
            entry = f"{ep.path} ({ep.method})"
        else:
            entry = ep.path
        if entry not in domain_groups[group]:
            domain_groups[group].append(entry)

    # --- API Summary Chunk ---
    summary_text = _generate_api_summary_text(spec, dict(domain_groups), len(spec.endpoints))
    summary_hash = _sha256(summary_text)

    api_summary = ApiSummaryChunk(
        source_type=SourceType.OPENAPI,
        source_name=api_name,
        chunk_id=f"{api_name}:summary",
        chunk_type=ChunkType.API_SUMMARY,
        text=summary_text,
        content_hash=summary_hash,
        payload=ApiSummaryPayload(
            source_type=SourceType.OPENAPI,
            source_name=api_name,
            chunk_id=f"{api_name}:summary",
            api_name=api_name,
            api_title=spec.title,
            api_version=spec.version,
            api_description=spec.description,
            spec_version=spec.spec_version,
            api_base_url=spec.base_url,
            auth_schemes=[s.name for s in spec.security_schemes],
            endpoint_count=len(spec.endpoints),
            domain_groups=sorted(domain_groups.keys()),
            tags=sorted(
                {t for group_paths in domain_groups.values() for p in group_paths for t in _infer_tags_from_path(p)}
            ),
            version_is_placeholder=spec.version in ("", "0.0.0"),
            content_hash=summary_hash,
        ),
    )

    # --- Endpoint Chunks ---
    endpoint_chunks: list[EndpointChunk] = []
    for ep in spec.endpoints:
        domain_group = _infer_domain_group(ep.path)
        action_type = _infer_action_type(ep)
        response_schema = _resolve_response_schema(ep)

        text = _generate_endpoint_text(spec, ep, domain_group, action_type, response_schema, schema_map)
        content_hash = _sha256(text)
        tags = _infer_tags_for_endpoint(ep, response_schema)

        chunk_id = f"{api_name}:{ep.method.lower()}:{ep.path}"

        endpoint_chunks.append(
            EndpointChunk(
                source_type=SourceType.OPENAPI,
                source_name=api_name,
                chunk_id=chunk_id,
                chunk_type=ChunkType.ENDPOINT,
                text=text,
                content_hash=content_hash,
                payload=EndpointPayload(
                    source_type=SourceType.OPENAPI,
                    source_name=api_name,
                    chunk_id=chunk_id,
                    api_name=api_name,
                    api_version=spec.version,
                    path=ep.path,
                    method=ep.method,
                    summary=ep.summary,
                    operation_id=ep.operation_id,
                    domain_group=domain_group,
                    action_type=action_type,
                    tags=tags,
                    parameters_raw=ep.parameters_raw or None,
                    request_body_raw=ep.request_body_raw or None,
                    response_raw=ep.responses_raw or None,
                    security=[s.name for s in spec.security_schemes] if ep.security else [],
                    has_request_body=ep.request_body is not None,
                    param_count=len(ep.parameters),
                    response_schema=response_schema,
                    content_hash=content_hash,
                ),
            )
        )

    # --- Schema Chunks (only medium, complex, and enum) ---
    schema_chunks: list[SchemaChunk] = []
    for schema in spec.schemas:
        complexity = _classify_schema_complexity(schema)

        # Skip simple and utility schemas — they're inlined into endpoint chunks
        if complexity in (SchemaComplexity.SIMPLE, SchemaComplexity.UTILITY):
            continue

        display_name = _clean_schema_display_name(schema.name)
        text = _generate_schema_text(spec, schema, complexity)
        content_hash = _sha256(text)
        tags = _infer_tags_for_schema(schema)

        chunk_id = f"{api_name}:schema:{schema.name}"

        schema_chunks.append(
            SchemaChunk(
                source_type=SourceType.OPENAPI,
                source_name=api_name,
                chunk_id=chunk_id,
                chunk_type=ChunkType.SCHEMA,
                text=text,
                content_hash=content_hash,
                payload=SchemaPayload(
                    source_type=SourceType.OPENAPI,
                    source_name=api_name,
                    chunk_id=chunk_id,
                    api_name=api_name,
                    api_version=spec.version,
                    schema_name=schema.name,
                    schema_display_name=display_name,
                    schema_type=schema.schema_type,
                    complexity=complexity,
                    field_count=len(schema.fields),
                    required_field_count=len(schema.required_fields),
                    referenced_by_endpoints=schema.referenced_by,
                    references_schemas=schema.references,
                    tags=tags,
                    content_hash=content_hash,
                ),
            )
        )

    logger.info(
        "Mapped %s: 1 summary + %d endpoint + %d schema chunks",
        api_name,
        len(endpoint_chunks),
        len(schema_chunks),
    )

    return api_summary, endpoint_chunks, schema_chunks
