"""Chunk writer for code chunks — writes JSON files to disk.

Output structure:
    chunks/code/{source_name}/v{version}/
        _summary.json
        classes/
            {tag}__{class_name}.json
        functions/
            {function_name}.json
        modules/
            {module_slug}.json
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from chunking.models import (
    CodeClassChunk,
    CodeFunctionChunk,
    CodeModuleChunk,
    CodeSummaryChunk,
)

logger = logging.getLogger(__name__)


def _slugify(name: str) -> str:
    """Convert a name to a filesystem-safe slug.

    "UserProfile" → "UserProfile"
    "parse_circuit_ref" → "parse_circuit_ref"
    """
    return re.sub(r"[^a-zA-Z0-9_.-]", "-", name).strip("-")


def _file_path_to_slug(file_path: str) -> str:
    """Convert a relative file path to a short slug for filenames.

    "accounts/models.py" → "accounts.models"
    "core/utils/parsers.py" → "core.utils.parsers"
    """
    slug = file_path.replace("/", ".").replace("\\", ".")
    slug = re.sub(r"\.py$", "", slug)
    return _slugify(slug)


def write_code_chunks(
    output_dir: str | Path,
    source_name: str,
    version: str,
    summary: CodeSummaryChunk,
    classes: list[CodeClassChunk],
    functions: list[CodeFunctionChunk],
    modules: list[CodeModuleChunk],
) -> Path:
    """Write code chunks to disk as JSON files."""
    version_safe = re.sub(r"[^a-zA-Z0-9._-]", "_", version)
    if not version_safe.startswith("v"):
        version_safe = f"v{version_safe}"

    result_dir = Path(output_dir) / "code" / source_name / version_safe
    classes_dir = result_dir / "classes"
    functions_dir = result_dir / "functions"
    modules_dir = result_dir / "modules"

    classes_dir.mkdir(parents=True, exist_ok=True)
    functions_dir.mkdir(parents=True, exist_ok=True)
    modules_dir.mkdir(parents=True, exist_ok=True)

    # Write summary
    summary_path = result_dir / "_summary.json"
    summary_path.write_text(summary.model_dump_json(indent=2))
    logger.info("Wrote summary: %s", summary_path)

    # Write class chunks: {tag}__{file_slug}__{class_name}.json
    for chunk in classes:
        tag = chunk.payload.tag
        file_slug = _file_path_to_slug(chunk.payload.file_path)
        name = _slugify(chunk.payload.class_name)
        path = classes_dir / f"{tag}__{file_slug}__{name}.json"
        path.write_text(chunk.model_dump_json(indent=2))
    logger.info("Wrote %d class chunks to %s", len(classes), classes_dir)

    # Write function chunks: {file_slug}__{function_name}.json
    for chunk in functions:
        file_slug = _file_path_to_slug(chunk.payload.file_path)
        name = _slugify(chunk.payload.function_name)
        path = functions_dir / f"{file_slug}__{name}.json"
        path.write_text(chunk.model_dump_json(indent=2))
    logger.info("Wrote %d function chunks to %s", len(functions), functions_dir)

    # Write module chunks: {module_slug}.json
    for chunk in modules:
        # Use file path as slug for uniqueness
        slug = chunk.payload.file_path.replace("/", "__").replace("\\", "__")
        slug = re.sub(r"\.py$", "", slug)
        slug = _slugify(slug)
        path = modules_dir / f"{slug}.json"
        path.write_text(chunk.model_dump_json(indent=2))
    logger.info("Wrote %d module chunks to %s", len(modules), modules_dir)

    total = 1 + len(classes) + len(functions) + len(modules)
    logger.info("Total: %d chunks written to %s", total, result_dir)
    return result_dir
