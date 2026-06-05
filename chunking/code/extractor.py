"""Python AST-based code extractor.

Walks a project directory, parses Python files, and extracts classes, functions,
and module docstrings into intermediate dataclasses (types.py).

Requires: Python 3.10+ (stdlib only).
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

from .types import ClassInfo, FunctionInfo, MethodInfo, ModuleInfo, ProjectMeta

logger = logging.getLogger(__name__)

# ── Directories and files to skip ────────────────────────────────────────────

SKIP_DIRS = frozenset(
    {
        "__pycache__",
        ".git",
        ".venv",
        "venv",
        "env",
        "node_modules",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        "dist",
        "build",
        "egg-info",
        "migrations",  # Django migrations are auto-generated
    }
)

SKIP_FILES = frozenset({"__init__.py"})

# ── Django/framework-specific tag detection ──────────────────────────────────

_BASE_CLASS_TAGS: dict[str, str] = {
    "models.Model": "model",
    "ModelSerializer": "serializer",
    "Serializer": "serializer",
    "APIView": "view",
    "ViewSet": "viewset",
    "ModelViewSet": "viewset",
    "GenericAPIView": "view",
    "ListAPIView": "view",
    "CreateAPIView": "view",
    "RetrieveAPIView": "view",
    "UpdateAPIView": "view",
    "DestroyAPIView": "view",
    "MiddlewareMixin": "middleware",
    "BaseMiddleware": "middleware",
    "TestCase": "test",
    "APITestCase": "test",
    "Form": "form",
    "ModelForm": "form",
    "Admin": "admin",
    "ModelAdmin": "admin",
    "Command": "management_command",
    "BaseCommand": "management_command",
    "AppConfig": "app_config",
    "Migration": "migration",
    "FilterSet": "filter",
    "Permission": "permission",
    "BasePermission": "permission",
    "Signal": "signal",
    "Celery": "task",
    "shared_task": "task",
}

_DECORATOR_TAGS: dict[str, str] = {
    "app.route": "route",
    "router.get": "route",
    "router.post": "route",
    "router.put": "route",
    "router.patch": "route",
    "router.delete": "route",
    "api_view": "view",
    "action": "viewset_action",
    "property": "property",
    "staticmethod": "static_method",
    "classmethod": "class_method",
    "receiver": "signal_handler",
    "shared_task": "task",
    "celery_app.task": "task",
    "login_required": "auth",
    "permission_required": "auth",
    "cache_page": "cached",
    "transaction.atomic": "transactional",
}


# ── Helpers ──────────────────────────────────────────────────────────────────


def _format_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Format a function/method signature as a readable string."""
    args = node.args
    parts: list[str] = []

    for i, arg in enumerate(args.args):
        param = arg.arg
        annotation = ast.unparse(arg.annotation) if arg.annotation else None

        default_offset = len(args.args) - len(args.defaults)
        has_default = i >= default_offset

        if annotation:
            param = f"{arg.arg}: {annotation}"
            if has_default:
                default = ast.unparse(args.defaults[i - default_offset])
                param += f" = {default}"
        elif has_default:
            default = ast.unparse(args.defaults[i - default_offset])
            param += f"={default}"

        if param not in ("self", "cls"):
            parts.append(param)

    if args.vararg:
        va = f"*{args.vararg.arg}"
        if args.vararg.annotation:
            va += f": {ast.unparse(args.vararg.annotation)}"
        parts.append(va)

    if args.kwarg:
        kw = f"**{args.kwarg.arg}"
        if args.kwarg.annotation:
            kw += f": {ast.unparse(args.kwarg.annotation)}"
        parts.append(kw)

    sig = f"({', '.join(parts)})"
    if node.returns:
        sig += f" -> {ast.unparse(node.returns)}"
    return sig


def _format_decorators(node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Extract decorator strings from a class or function."""
    return [f"@{ast.unparse(dec)}" for dec in node.decorator_list]


def _detect_class_tag(node: ast.ClassDef) -> str:
    """Detect the tag for a class based on its base classes and decorators."""
    for base in node.bases:
        base_name = ast.unparse(base)
        for pattern, tag in _BASE_CLASS_TAGS.items():
            if pattern in base_name:
                return tag
    for dec in node.decorator_list:
        dec_str = ast.unparse(dec)
        for pattern, tag in _DECORATOR_TAGS.items():
            if pattern in dec_str:
                return tag
    return "class"


def _detect_function_tag(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Detect the tag for a function based on its decorators."""
    for dec in node.decorator_list:
        dec_str = ast.unparse(dec)
        for pattern, tag in _DECORATOR_TAGS.items():
            if pattern in dec_str:
                return tag
    return "function"


def _detect_framework(classes: list[ClassInfo]) -> str:
    """Best-effort framework detection from class tags."""
    django_tags = {"model", "serializer", "view", "viewset", "admin", "management_command", "form", "middleware"}
    fastapi_tags = {"route"}
    tag_set = {c.tag for c in classes}
    if tag_set & django_tags:
        return "django"
    if tag_set & fastapi_tags:
        return "fastapi"
    return ""


# ── Single-file extraction ───────────────────────────────────────────────────


def extract_from_file(
    file_path: Path,
    base_path: Path,
) -> tuple[list[ClassInfo], list[FunctionInfo], ModuleInfo | None]:
    """Extract all code metadata from a single Python file."""
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(file_path))
    except (SyntaxError, ValueError) as exc:
        logger.warning("Skipping %s: %s", file_path, exc)
        return [], [], None

    relative_path = str(file_path.relative_to(base_path))
    classes: list[ClassInfo] = []
    functions: list[FunctionInfo] = []
    module_info: ModuleInfo | None = None

    # Module-level docstring
    module_doc = ast.get_docstring(tree)
    if module_doc:
        module_info = ModuleInfo(
            name=file_path.stem,
            file_path=relative_path,
            docstring=module_doc.strip(),
        )

    # Walk the AST
    for node in ast.walk(tree):
        # ── Classes ──
        if isinstance(node, ast.ClassDef):
            methods: list[MethodInfo] = []
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # Include public methods + dunder essentials
                    if not item.name.startswith("_") or item.name in ("__init__", "__str__", "__repr__"):
                        method_doc = ast.get_docstring(item)
                        methods.append(
                            MethodInfo(
                                name=item.name,
                                signature=_format_signature(item),
                                decorators=_format_decorators(item),
                                docstring=method_doc.strip() if method_doc else None,
                                line_number=item.lineno,
                                is_async=isinstance(item, ast.AsyncFunctionDef),
                            )
                        )

            class_doc = ast.get_docstring(node)
            classes.append(
                ClassInfo(
                    name=node.name,
                    tag=_detect_class_tag(node),
                    file_path=relative_path,
                    line_number=node.lineno,
                    bases=[ast.unparse(b) for b in node.bases],
                    decorators=_format_decorators(node),
                    docstring=class_doc.strip() if class_doc else None,
                    methods=methods,
                )
            )

        # ── Top-level functions (skip methods) ──
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parent_is_class = False
            for parent in ast.walk(tree):
                if isinstance(parent, ast.ClassDef):
                    for child in parent.body:
                        if child is node:
                            parent_is_class = True
                            break
            if parent_is_class:
                continue

            func_doc = ast.get_docstring(node)
            functions.append(
                FunctionInfo(
                    name=node.name,
                    tag=_detect_function_tag(node),
                    file_path=relative_path,
                    line_number=node.lineno,
                    signature=_format_signature(node),
                    decorators=_format_decorators(node),
                    docstring=func_doc.strip() if func_doc else None,
                    is_async=isinstance(node, ast.AsyncFunctionDef),
                )
            )

    return classes, functions, module_info


# ── Project-level extraction ─────────────────────────────────────────────────


def extract_project(project_path: Path, project_name: str) -> ProjectMeta:
    """Walk a project directory and extract metadata from all Python files."""
    all_classes: list[ClassInfo] = []
    all_functions: list[FunctionInfo] = []
    all_modules: list[ModuleInfo] = []

    py_files = sorted(project_path.rglob("*.py"))
    processed = 0
    skipped = 0

    for py_file in py_files:
        parts = py_file.relative_to(project_path).parts
        if any(part in SKIP_DIRS for part in parts):
            skipped += 1
            continue

        if py_file.name in SKIP_FILES:
            if py_file.stat().st_size < 100:
                skipped += 1
                continue

        classes, functions, module_info = extract_from_file(py_file, project_path)
        all_classes.extend(classes)
        all_functions.extend(functions)
        if module_info:
            all_modules.append(module_info)
        processed += 1

    framework = _detect_framework(all_classes)

    logger.info(
        "Extracted from %s: %d files processed, %d skipped → %d classes, %d functions, %d modules",
        project_name,
        processed,
        skipped,
        len(all_classes),
        len(all_functions),
        len(all_modules),
    )

    return ProjectMeta(
        project_name=project_name,
        language="python",
        framework=framework,
        file_count=processed,
        classes=all_classes,
        functions=all_functions,
        modules=all_modules,
    )
