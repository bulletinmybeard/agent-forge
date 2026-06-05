"""Code mapper — transforms extracted ProjectMeta into Qdrant-ready chunk models.

Produces four chunk types:
- CodeSummaryChunk: one per project (overview, class listing, framework info)
- CodeClassChunk: one per class (model, serializer, view, etc.)
- CodeFunctionChunk: one per top-level function
- CodeModuleChunk: one per module with a docstring
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter

from chunking.models import (
    CodeClassChunk,
    CodeClassPayload,
    CodeFunctionChunk,
    CodeFunctionPayload,
    CodeModuleChunk,
    CodeModulePayload,
    CodeSummaryChunk,
    CodeSummaryPayload,
    SourceType,
)

from .types import ClassInfo, FunctionInfo, ModuleInfo, ProjectMeta

logger = logging.getLogger(__name__)

_TAG_STOP_WORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "into",
        "that",
        "this",
        "are",
        "not",
        "use",
        "set",
        "get",
        "all",
        "self",
        "none",
        "true",
        "false",
    }
)

# Tag → human-readable label for text generation
_TAG_LABELS: dict[str, str] = {
    "model": "Django model",
    "serializer": "DRF serializer",
    "view": "DRF API view",
    "viewset": "DRF viewset",
    "admin": "Django admin",
    "form": "Django form",
    "middleware": "Django middleware",
    "management_command": "Django management command",
    "app_config": "Django app config",
    "filter": "Django filter",
    "permission": "DRF permission",
    "signal": "Django signal",
    "task": "Celery task",
    "test": "Test case",
    "route": "API route",
    "class": "Python class",
    "function": "Python function",
    "signal_handler": "Signal handler",
    "viewset_action": "Viewset action",
    "auth": "Auth-decorated function",
    "cached": "Cached function",
    "transactional": "Transactional function",
}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _infer_tags(name: str, tag: str, docstring: str | None, file_path: str) -> list[str]:
    """Infer search-enrichment tags from various metadata."""
    tags: set[str] = set()

    # Always include the semantic tag
    tags.add(tag)

    # Split class/function name on camelCase/PascalCase boundaries
    for part in re.sub(r"([a-z])([A-Z])", r"\1 \2", name).split():
        w = part.lower()
        if len(w) >= 3 and w not in _TAG_STOP_WORDS:
            tags.add(w)

    # Extract meaningful words from docstring
    if docstring:
        for word in re.split(r"[\s,;.()]+", docstring):
            w = word.lower().strip()
            if len(w) >= 4 and w not in _TAG_STOP_WORDS:
                tags.add(w)
                if len(tags) >= 20:
                    break

    # Extract app name from file path (e.g., "accounts/models.py" → "accounts")
    path_parts = file_path.replace("\\", "/").split("/")
    for part in path_parts[:-1]:  # skip filename
        if len(part) >= 3 and part not in ("src", "app", "apps", "lib", "core", "utils"):
            tags.add(part.lower())

    return sorted(tags)[:15]


# ── Text generators (embedding-critical) ─────────────────────────────────────


def _generate_class_text(cls: ClassInfo, source_name: str, project_name: str) -> str:
    """Generate natural-language text for a class chunk.

    Example output:
        Django model UserProfile in accounts/models.py
        Project: my-api
        Extends: AbstractUser
        Decorators: none
        Methods: get_full_name(), is_active_member(days: int) -> bool, deactivate()
        Docstring: "Extended user profile with organization membership tracking."
    """
    label = _TAG_LABELS.get(cls.tag, f"Python {cls.tag}")
    parts: list[str] = [
        f"{label} {cls.name} in {cls.file_path}",
        f"Project: {project_name}",
        f"Source: {source_name} (code)",
    ]

    if cls.bases:
        parts.append(f"Extends: {', '.join(cls.bases)}")

    if cls.decorators:
        parts.append(f"Decorators: {', '.join(cls.decorators)}")

    if cls.methods:
        method_sigs: list[str] = []
        for m in cls.methods:
            if m.name == "__init__":
                method_sigs.append(f"__init__{m.signature}")
            else:
                method_sigs.append(f"{m.name}{m.signature}")
        parts.append(f"Methods: {', '.join(method_sigs)}")

        # Include method docstrings for richer embedding context
        documented_methods = [m for m in cls.methods if m.docstring]
        if documented_methods:
            parts.append("")
            parts.append("Method details:")
            for m in documented_methods:
                parts.append(f"  {m.name}: {m.docstring}")

    if cls.docstring:
        parts.append(f"\nDocstring: {cls.docstring}")

    return "\n".join(parts)


def _generate_function_text(func: FunctionInfo, source_name: str, project_name: str) -> str:
    """Generate natural-language text for a function chunk.

    Example output:
        Function parse_circuit_reference in utils/parsers.py
        Project: my-api
        Tag: function
        Signature: (reference: str, strict: bool = True) -> CircuitRef
        Decorators: @lru_cache(maxsize=256)
        Docstring: "Parse a circuit reference string into structured components."
    """
    label = _TAG_LABELS.get(func.tag, "Python function")
    async_prefix = "Async " if func.is_async else ""
    parts: list[str] = [
        f"{async_prefix}{label} {func.name} in {func.file_path}",
        f"Project: {project_name}",
        f"Source: {source_name} (code)",
    ]

    if func.signature:
        parts.append(f"Signature: {func.name}{func.signature}")

    if func.decorators:
        parts.append(f"Decorators: {', '.join(func.decorators)}")

    if func.docstring:
        parts.append(f"\nDocstring: {func.docstring}")

    return "\n".join(parts)


def _generate_module_text(mod: ModuleInfo, source_name: str, project_name: str) -> str:
    """Generate natural-language text for a module docstring chunk."""
    parts: list[str] = [
        f"Module {mod.name} in {mod.file_path}",
        f"Project: {project_name}",
        f"Source: {source_name} (code)",
    ]
    if mod.docstring:
        parts.append(f"\n{mod.docstring}")
    return "\n".join(parts)


def _generate_summary_text(meta: ProjectMeta, source_name: str) -> str:
    """Generate the project summary text."""
    tag_counts = Counter(c.tag for c in meta.classes)
    parts: list[str] = [
        f"Code project: {meta.project_name}",
        f"Source: {source_name} (code)",
        f"Language: {meta.language}",
    ]
    if meta.framework:
        parts.append(f"Framework: {meta.framework}")
    parts.append(f"Files: {meta.file_count}")
    parts.append(f"Classes: {len(meta.classes)}")
    parts.append(f"Functions: {len(meta.functions)}")
    parts.append(f"Modules with docstrings: {len(meta.modules)}")

    if tag_counts:
        parts.append("\nClass distribution:")
        for tag, count in tag_counts.most_common():
            label = _TAG_LABELS.get(tag, tag)
            parts.append(f"  {label}: {count}")

    # List all class names grouped by tag
    if meta.classes:
        parts.append("\nAll classes:")
        by_tag: dict[str, list[str]] = {}
        for cls in meta.classes:
            by_tag.setdefault(cls.tag, []).append(cls.name)
        for tag in sorted(by_tag.keys()):
            label = _TAG_LABELS.get(tag, tag)
            names = ", ".join(sorted(by_tag[tag]))
            parts.append(f"  {label}: {names}")

    return "\n".join(parts)


# ── Main mapper ──────────────────────────────────────────────────────────────


def map_code_to_chunks(
    meta: ProjectMeta,
    source_name: str,
) -> tuple[CodeSummaryChunk, list[CodeClassChunk], list[CodeFunctionChunk], list[CodeModuleChunk]]:
    """Map extracted project metadata into chunk models."""
    project_name = meta.project_name
    class_chunks: list[CodeClassChunk] = []
    function_chunks: list[CodeFunctionChunk] = []
    module_chunks: list[CodeModuleChunk] = []

    # ── Class chunks ──
    for cls in meta.classes:
        text = _generate_class_text(cls, source_name, project_name)
        # Include file path to disambiguate same-named classes across files
        file_slug = cls.file_path.replace("/", ".").replace("\\", ".").removesuffix(".py")
        chunk_id = f"{source_name}:class:{file_slug}.{cls.name}"
        method_names = [m.name for m in cls.methods]

        class_chunks.append(
            CodeClassChunk(
                source_type=SourceType.CODE,
                source_name=source_name,
                chunk_id=chunk_id,
                text=text,
                content_hash=_sha256(text),
                payload=CodeClassPayload(
                    source_name=source_name,
                    chunk_id=chunk_id,
                    project_name=project_name,
                    class_name=cls.name,
                    tag=cls.tag,
                    file_path=cls.file_path,
                    line_number=cls.line_number,
                    bases=cls.bases,
                    decorators=cls.decorators,
                    method_count=len(cls.methods),
                    method_names=method_names,
                    has_docstring=bool(cls.docstring),
                    tags=_infer_tags(cls.name, cls.tag, cls.docstring, cls.file_path),
                    content_hash=_sha256(text),
                ),
            )
        )

    # ── Function chunks ──
    for func in meta.functions:
        text = _generate_function_text(func, source_name, project_name)
        # Include file path to disambiguate same-named functions (e.g., main, _sha256)
        file_slug = func.file_path.replace("/", ".").replace("\\", ".").removesuffix(".py")
        chunk_id = f"{source_name}:func:{file_slug}.{func.name}"

        function_chunks.append(
            CodeFunctionChunk(
                source_type=SourceType.CODE,
                source_name=source_name,
                chunk_id=chunk_id,
                text=text,
                content_hash=_sha256(text),
                payload=CodeFunctionPayload(
                    source_name=source_name,
                    chunk_id=chunk_id,
                    project_name=project_name,
                    function_name=func.name,
                    tag=func.tag,
                    file_path=func.file_path,
                    line_number=func.line_number,
                    signature=func.signature,
                    decorators=func.decorators,
                    is_async=func.is_async,
                    has_docstring=bool(func.docstring),
                    tags=_infer_tags(func.name, func.tag, func.docstring, func.file_path),
                    content_hash=_sha256(text),
                ),
            )
        )

    # ── Module chunks ──
    for mod in meta.modules:
        text = _generate_module_text(mod, source_name, project_name)
        # Use file path as identifier (more unique than module name)
        mod_slug = mod.file_path.replace("/", ".").replace("\\", ".").rstrip(".py")
        chunk_id = f"{source_name}:module:{mod_slug}"

        module_chunks.append(
            CodeModuleChunk(
                source_type=SourceType.CODE,
                source_name=source_name,
                chunk_id=chunk_id,
                text=text,
                content_hash=_sha256(text),
                payload=CodeModulePayload(
                    source_name=source_name,
                    chunk_id=chunk_id,
                    project_name=project_name,
                    module_name=mod.name,
                    file_path=mod.file_path,
                    has_docstring=True,
                    tags=_infer_tags(mod.name, "module", mod.docstring, mod.file_path),
                    content_hash=_sha256(text),
                ),
            )
        )

    # ── Summary chunk ──
    summary_text = _generate_summary_text(meta, source_name)
    tag_distribution = dict(Counter(c.tag for c in meta.classes))
    all_class_names = [c.name for c in meta.classes]

    summary_chunk = CodeSummaryChunk(
        source_type=SourceType.CODE,
        source_name=source_name,
        chunk_id=f"{source_name}:code-summary",
        text=summary_text,
        content_hash=_sha256(summary_text),
        payload=CodeSummaryPayload(
            source_name=source_name,
            chunk_id=f"{source_name}:code-summary",
            project_name=project_name,
            language=meta.language,
            framework=meta.framework,
            file_count=meta.file_count,
            class_count=len(meta.classes),
            function_count=len(meta.functions),
            class_names=all_class_names,
            tag_distribution=tag_distribution,
            tags=_infer_tags(project_name, meta.framework or "python", None, ""),
            content_hash=_sha256(summary_text),
        ),
    )

    logger.info(
        "Mapped %s: 1 summary + %d class + %d function + %d module chunks",
        source_name,
        len(class_chunks),
        len(function_chunks),
        len(module_chunks),
    )
    return summary_chunk, class_chunks, function_chunks, module_chunks
