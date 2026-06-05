"""Intermediate dataclasses for code extraction.

These are the parsed representations that sit between the raw AST extraction
and the final Qdrant chunk models. The extractor produces these, and the
mapper converts them into chunk models with natural-language text.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MethodInfo:
    """A single method within a class."""

    name: str
    signature: str = ""
    decorators: list[str] = field(default_factory=list)
    docstring: str | None = None
    line_number: int = 0
    is_async: bool = False


@dataclass
class ClassInfo:
    """A Python class extracted from source code."""

    name: str
    tag: str = "class"  # model, serializer, view, viewset, etc.
    file_path: str = ""
    line_number: int = 0
    bases: list[str] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    docstring: str | None = None
    methods: list[MethodInfo] = field(default_factory=list)


@dataclass
class FunctionInfo:
    """A top-level function extracted from source code."""

    name: str
    tag: str = "function"  # function, route, task, signal_handler, etc.
    file_path: str = ""
    line_number: int = 0
    signature: str = ""
    decorators: list[str] = field(default_factory=list)
    docstring: str | None = None
    is_async: bool = False


@dataclass
class ModuleInfo:
    """A module-level docstring."""

    name: str  # filename stem
    file_path: str = ""
    docstring: str = ""


@dataclass
class ProjectMeta:
    """Top-level project metadata produced by the extractor."""

    project_name: str
    language: str = "python"
    framework: str = ""
    file_count: int = 0
    classes: list[ClassInfo] = field(default_factory=list)
    functions: list[FunctionInfo] = field(default_factory=list)
    modules: list[ModuleInfo] = field(default_factory=list)
