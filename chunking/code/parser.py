"""Parser for pre-extracted code JSON (from test-scripts/extract_python.py).

Provides an alternative entry point: instead of running the AST extractor
directly, load a previously exported JSON file and convert it into
intermediate dataclasses (types.py).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .types import ClassInfo, FunctionInfo, MethodInfo, ModuleInfo, ProjectMeta

logger = logging.getLogger(__name__)


def _parse_method(raw: dict) -> MethodInfo:
    return MethodInfo(
        name=raw["name"],
        signature=raw.get("signature", ""),
        decorators=raw.get("decorators", []),
        docstring=raw.get("docstring"),
        line_number=raw.get("line", 0),
        is_async=raw.get("is_async", False),
    )


def _parse_class(raw: dict) -> ClassInfo:
    methods = [_parse_method(m) for m in raw.get("methods", [])]
    return ClassInfo(
        name=raw["name"],
        tag=raw.get("tag", "class"),
        file_path=raw.get("file", ""),
        line_number=raw.get("line", 0),
        bases=raw.get("bases", []),
        decorators=raw.get("decorators", []),
        docstring=raw.get("docstring"),
        methods=methods,
    )


def _parse_function(raw: dict) -> FunctionInfo:
    return FunctionInfo(
        name=raw["name"],
        tag=raw.get("tag", "function"),
        file_path=raw.get("file", ""),
        line_number=raw.get("line", 0),
        signature=raw.get("signature", ""),
        decorators=raw.get("decorators", []),
        docstring=raw.get("docstring"),
        is_async=raw.get("is_async", False),
    )


def _parse_module(raw: dict) -> ModuleInfo:
    return ModuleInfo(
        name=raw.get("name", ""),
        file_path=raw.get("file", ""),
        docstring=raw.get("docstring", ""),
    )


def parse_extraction_json(
    json_path: Path,
    project_name: str,
) -> ProjectMeta:
    """Load a JSON file produced by extract_python.py and convert to ProjectMeta."""
    with open(json_path, encoding="utf-8") as f:
        raw_chunks: list[dict] = json.load(f)

    classes: list[ClassInfo] = []
    functions: list[FunctionInfo] = []
    modules: list[ModuleInfo] = []
    files_seen: set[str] = set()

    for chunk in raw_chunks:
        chunk_type = chunk.get("type", "")

        if chunk_type == "class":
            classes.append(_parse_class(chunk))
        elif chunk_type == "function":
            functions.append(_parse_function(chunk))
        elif chunk_type == "module_docstring":
            modules.append(_parse_module(chunk))

        # Track unique files
        if "file" in chunk:
            files_seen.add(chunk["file"])

    # Detect framework from class tags
    django_tags = {"model", "serializer", "view", "viewset", "admin", "management_command"}
    tag_set = {c.tag for c in classes}
    framework = "django" if tag_set & django_tags else ""

    logger.info(
        "Parsed %s: %d raw chunks → %d classes, %d functions, %d modules from %d files",
        json_path.name,
        len(raw_chunks),
        len(classes),
        len(functions),
        len(modules),
        len(files_seen),
    )

    return ProjectMeta(
        project_name=project_name,
        language="python",
        framework=framework,
        file_count=len(files_seen),
        classes=classes,
        functions=functions,
        modules=modules,
    )
