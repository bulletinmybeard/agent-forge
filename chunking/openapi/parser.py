"""OpenAPI JSON parser.

Reads an OpenAPI 3.x JSON file and extracts structured data into
intermediate dataclasses (types.py). Handles encoding quirks (BOM),
$ref resolution, and the structural patterns observed in the Intranet API spec.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from chunking.openapi.types import (
    EndpointInfo,
    OpenApiSpec,
    ParameterInfo,
    RequestBodyInfo,
    ResponseInfo,
    SchemaFieldInfo,
    SchemaInfo,
    SecuritySchemeInfo,
)

logger = logging.getLogger(__name__)

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}


def _load_json(filepath: Path) -> dict:
    """Load a JSON file, handling UTF-8 BOM encoding."""
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            with open(filepath, encoding=encoding) as f:
                return json.load(f)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    raise ValueError(f"Failed to parse JSON from {filepath}")


def _derive_api_name_slug(filepath: Path, title: str) -> str:
    """Derive an api_name_slug from the filename or info.title.

    openapi-intranet-api.json → "intranet-api"
    If title is available and filename is generic, prefer title.
    """
    stem = filepath.stem  # "openapi-intranet-api"
    # Strip common prefixes
    slug = re.sub(r"^openapi-?", "", stem, flags=re.IGNORECASE)
    if slug:
        return slug.lower()
    # Fallback to title
    if title:
        return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return "unknown-api"


def _resolve_ref(ref: str) -> str:
    """Extract schema name from a $ref string.

    '#/components/schemas/DemarcationDetails' → 'DemarcationDetails'
    """
    if ref and ref.startswith("#/components/schemas/"):
        return ref.split("/")[-1]
    return ref or ""


def _extract_type_info(schema: dict) -> tuple[str, str, list[str], list[str]]:
    """Extract type, format, union types, and enum values from a schema dict.

    Returns: (type_str, format_str, union_types, enum_values)
    """
    schema_type = schema.get("type", "")
    format_str = schema.get("format", "")
    enum_values = schema.get("enum", [])
    union_types: list[str] = []

    # Handle anyOf / oneOf union types
    for union_key in ("anyOf", "oneOf"):
        if union_key in schema:
            for variant in schema[union_key]:
                if "$ref" in variant:
                    union_types.append(_resolve_ref(variant["$ref"]))
                elif "type" in variant:
                    union_types.append(variant["type"])
            if not schema_type and union_types:
                schema_type = "|".join(union_types)

    # Handle $ref at top level
    if "$ref" in schema:
        schema_type = _resolve_ref(schema["$ref"])

    return schema_type, format_str, union_types, [str(v) for v in enum_values]


def _parse_parameter(param: dict) -> ParameterInfo:
    """Parse a single parameter object."""
    schema = param.get("schema", {})
    param_type, format_str, _, enum_values = _extract_type_info(schema)

    return ParameterInfo(
        name=param.get("name", ""),
        location=param.get("in", ""),
        required=param.get("required", False),
        param_type=param_type or "string",
        title=schema.get("title", ""),
        format=format_str,
        default=str(schema["default"]) if "default" in schema else None,
        enum_values=enum_values,
        pattern=schema.get("pattern", ""),
        minimum=schema.get("minimum"),
        maximum=schema.get("maximum"),
    )


def _parse_request_body(request_body: dict) -> RequestBodyInfo | None:
    """Parse a requestBody object."""
    if not request_body:
        return None

    content = request_body.get("content", {})
    if not content:
        return None

    # Prefer application/json
    media_types = list(content.keys())
    primary = "application/json" if "application/json" in media_types else media_types[0]
    additional = [mt for mt in media_types if mt != primary]

    schema = content.get(primary, {}).get("schema", {})
    schema_ref = _resolve_ref(schema.get("$ref", ""))

    return RequestBodyInfo(
        required=request_body.get("required", False),
        media_type=primary,
        schema_ref=schema_ref,
        additional_media_types=additional,
    )


def _parse_responses(responses: dict) -> list[ResponseInfo]:
    """Parse the responses object into a list of ResponseInfo."""
    result: list[ResponseInfo] = []

    for status_code, response in responses.items():
        schema_ref = ""
        schema_type = ""
        items_ref = ""

        content = response.get("content", {})
        if content:
            # Prefer application/json
            media = content.get("application/json", {})
            schema = media.get("schema", {})

            if "$ref" in schema:
                schema_ref = _resolve_ref(schema["$ref"])
                schema_type = "object"
            elif "type" in schema:
                schema_type = schema["type"]
                if schema_type == "array" and "items" in schema:
                    items = schema["items"]
                    if "$ref" in items:
                        items_ref = _resolve_ref(items["$ref"])

        result.append(
            ResponseInfo(
                status_code=str(status_code),
                description=response.get("description", ""),
                schema_ref=schema_ref,
                schema_type=schema_type,
                items_ref=items_ref,
            )
        )

    return result


def _parse_security(security: list[dict]) -> list[str]:
    """Extract security scheme names from the endpoint security list."""
    names: list[str] = []
    for entry in security:
        names.extend(entry.keys())
    return names


def _parse_endpoint(path: str, method: str, operation: dict) -> EndpointInfo:
    """Parse a single endpoint operation."""
    parameters = [_parse_parameter(p) for p in operation.get("parameters", [])]
    request_body = _parse_request_body(operation.get("requestBody", {}))
    responses = _parse_responses(operation.get("responses", {}))
    security = _parse_security(operation.get("security", []))

    return EndpointInfo(
        path=path,
        method=method.upper(),
        summary=operation.get("summary", ""),
        description=operation.get("description", ""),
        operation_id=operation.get("operationId", ""),
        tags=operation.get("tags", []),
        parameters=parameters,
        request_body=request_body,
        responses=responses,
        security=security,
        parameters_raw=json.dumps(operation.get("parameters", []), indent=2) if operation.get("parameters") else "",
        request_body_raw=json.dumps(operation.get("requestBody", {}), indent=2) if operation.get("requestBody") else "",
        responses_raw=json.dumps(operation.get("responses", {}), indent=2) if operation.get("responses") else "",
    )


def _parse_schema_field(name: str, field_def: dict, required_fields: list[str]) -> SchemaFieldInfo:
    """Parse a single field from a schema's properties."""
    field_type, format_str, union_types, enum_values = _extract_type_info(field_def)

    ref = ""
    items_ref = ""

    if "$ref" in field_def:
        ref = _resolve_ref(field_def["$ref"])
        field_type = ref
    elif field_type == "array" and "items" in field_def:
        items = field_def["items"]
        if "$ref" in items:
            items_ref = _resolve_ref(items["$ref"])

    return SchemaFieldInfo(
        name=name,
        field_type=field_type or "object",
        title=field_def.get("title", ""),
        format=format_str,
        required=name in required_fields,
        default=str(field_def["default"]) if "default" in field_def else None,
        ref=ref,
        items_ref=items_ref,
        enum_values=enum_values,
        pattern=field_def.get("pattern", ""),
        minimum=field_def.get("minimum"),
        maximum=field_def.get("maximum"),
        union_types=union_types,
    )


def _parse_schema(name: str, schema_def: dict) -> SchemaInfo:
    """Parse a single schema from components.schemas."""
    required_fields = schema_def.get("required", [])
    properties = schema_def.get("properties", {})

    fields = [_parse_schema_field(fname, fdef, required_fields) for fname, fdef in properties.items()]

    # Track which schemas this schema references
    references: list[str] = []
    for f in fields:
        if f.ref:
            references.append(f.ref)
        if f.items_ref:
            references.append(f.items_ref)

    return SchemaInfo(
        name=name,
        title=schema_def.get("title", ""),
        description=schema_def.get("description", ""),
        schema_type=schema_def.get("type", "object"),
        fields=fields,
        required_fields=required_fields,
        enum_values=[str(v) for v in schema_def.get("enum", [])],
        references=references,
    )


def _parse_security_schemes(components: dict) -> list[SecuritySchemeInfo]:
    """Parse security schemes from components.securitySchemes."""
    schemes: list[SecuritySchemeInfo] = []
    for name, scheme_def in components.get("securitySchemes", {}).items():
        schemes.append(
            SecuritySchemeInfo(
                name=name,
                scheme_type=scheme_def.get("type", ""),
                location=scheme_def.get("in", ""),
                param_name=scheme_def.get("name", ""),
            )
        )
    return schemes


def parse_openapi_file(filepath: Path) -> OpenApiSpec:
    """Parse an OpenAPI JSON file into a structured OpenApiSpec."""
    logger.info("Parsing OpenAPI file: %s", filepath)
    raw = _load_json(filepath)

    info = raw.get("info", {})
    title = info.get("title", "")
    version = info.get("version", "")
    description = info.get("description", "")

    # Servers
    servers = raw.get("servers", [])
    base_url = servers[0].get("url", "") if servers else ""

    # Security schemes
    components = raw.get("components", {})
    security_schemes = _parse_security_schemes(components)

    # Endpoints
    endpoints: list[EndpointInfo] = []
    for path, path_item in raw.get("paths", {}).items():
        for method in HTTP_METHODS:
            if method in path_item:
                endpoint = _parse_endpoint(path, method, path_item[method])
                endpoints.append(endpoint)

    # Schemas
    schemas: list[SchemaInfo] = []
    for schema_name, schema_def in components.get("schemas", {}).items():
        schema = _parse_schema(schema_name, schema_def)
        schemas.append(schema)

    # Cross-reference: which endpoints reference which schemas
    _build_schema_references(endpoints, schemas)

    api_name_slug = _derive_api_name_slug(filepath, title)

    spec = OpenApiSpec(
        spec_version=raw.get("openapi", ""),
        title=title,
        version=version,
        description=description,
        base_url=base_url,
        security_schemes=security_schemes,
        endpoints=endpoints,
        schemas=schemas,
        source_filename=filepath.name,
        api_name_slug=api_name_slug,
    )

    logger.info(
        "Parsed %s: %d endpoints, %d schemas",
        api_name_slug,
        len(endpoints),
        len(schemas),
    )

    return spec


def _build_schema_references(endpoints: list[EndpointInfo], schemas: list[SchemaInfo]) -> None:
    """Cross-reference endpoints and schemas: populate schema.referenced_by."""
    schema_map = {s.name: s for s in schemas}

    for ep in endpoints:
        ref_key = f"{ep.method.lower()}:{ep.path}"

        # Check request body schema
        if ep.request_body and ep.request_body.schema_ref:
            schema = schema_map.get(ep.request_body.schema_ref)
            if schema:
                schema.referenced_by.append(ref_key)

        # Check response schemas
        for resp in ep.responses:
            for ref_name in (resp.schema_ref, resp.items_ref):
                if ref_name:
                    schema = schema_map.get(ref_name)
                    if schema:
                        schema.referenced_by.append(ref_key)
