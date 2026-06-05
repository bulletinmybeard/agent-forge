# Python Best Practices Skill

You have been given this skill because the user's query involves Python code design, development patterns, or project structure. Follow these guidelines when advising on Python development.

## Type Hints

1. **Modern syntax (3.10+)** — Use `X | None` instead of `Optional[X]`. Avoid importing
   from `typing` when built-in generics are available:
   ```python
   # Good (3.10+)
   def fetch(url: str) -> dict[str, Any] | None:
       pass

   # Avoid
   from typing import Optional, Dict, Any
   def fetch(url: str) -> Optional[Dict[str, Any]]:
       pass
   ```
2. **TypeAlias** — For complex types, define aliases at module level:
   ```python
   from typing import TypeAlias
   JSONValue: TypeAlias = dict[str, Any] | list[Any] | str | int | float | bool | None
   ```
3. **Generic collections** — Use `list[T]`, `dict[K, V]`, `tuple[T, ...]`, not capitalized
   `List`, `Dict`, etc.
4. **Callable types** — Prefer `collections.abc.Callable` over `typing.Callable`:
   ```python
   from collections.abc import Callable
   def register_hook(cb: Callable[[str], None]) -> None:
       pass
   ```

## Async Patterns

1. **asyncio best practices**:
   - Use `asyncio.TaskGroup` (3.11+) instead of `gather()` for cleaner exception handling
   - Never `await` in list comprehensions; use async comprehensions instead
   - Always use `asyncio.Semaphore` to limit concurrent tasks
2. **Avoid blocking calls in async code**:
   ```python
   # Bad: time.sleep blocks event loop
   await asyncio.sleep(1)  # Good: non-blocking

   # Bad: requests.get blocks
   async with aiohttp.ClientSession() as session:
       async with session.get(url) as resp:  # Good: async HTTP
   ```
3. **Context managers for cleanup** — Use `async with` for resource management:
   ```python
   async with asyncio.TaskGroup() as tg:
       tg.create_task(task1())
       tg.create_task(task2())
   # All tasks completed or first exception raised
   ```
4. **Cancel gracefully** — Handle `asyncio.CancelledError` in task code to clean up
   resources before exiting.

## Error Handling

1. **Custom exceptions** — Define at module level, inherit from appropriate base:
   ```python
   class ConfigurationError(ValueError):
       """Raised when configuration is invalid."""
       pass

   class RetryableError(Exception):
       """Raised for transient errors that should trigger retry."""
       pass
   ```
2. **Exception chaining** — Use `raise NewException(...) from e` to preserve context:
   ```python
   try:
       result = fetch_data()
   except ConnectionError as e:
       raise DataFetchError(f"Failed to fetch: {url}") from e
   ```
3. **Contextlib helpers**:
   - `contextlib.suppress(ValueError)` — Silently ignore exceptions
   - `contextlib.ExitStack` — Dynamically manage multiple contexts
4. **Be specific** — Catch `Exception` only at top level (main, task entry). Catch
   specific exceptions in business logic.

## Project Structure

1. **src layout** — Preferred structure for installable packages:
   ```
   project/
   ├── src/
   │   └── mypackage/
   │       ├── __init__.py
   │       ├── core.py
   │       └── utils/
   │           └── __init__.py
   ├── tests/
   ├── pyproject.toml
   └── README.md
   ```
2. **__init__.py patterns**:
   - Empty for namespace packages
   - Minimal exports for public API: `__all__ = ["PublicClass", "public_function"]`
   - Never `from .module import *` in __init__.py
3. **Circular imports** — Prevent with:
   - Import at function scope (not module scope) where needed
   - Restructure to move shared code to a third module
   - Use type hints in `TYPE_CHECKING` block:
     ```python
     from typing import TYPE_CHECKING
     if TYPE_CHECKING:
         from module import ClassName
     ```

## Dependency Management

1. **Poetry** — Preferred for most projects (lock files, virtual envs, publishing):
   - `poetry add package` — Add dependency
   - `poetry lock` — Lock versions without installing
   - `poetry install` — Install with lock file
2. **Version pinning strategy**:
   - Runtime deps: `^1.2.3` (caret = 1.x, >=1.2.3, <2.0)
   - Dev/test deps: Usually caret is fine
   - Lock file: Always commit for reproducibility
3. **Dependency tree** — Use `poetry show --tree` to spot duplicates and conflicts.
4. **Avoid** — `requirements.txt` with `==` pins for source projects (use Poetry instead).

## Code Quality

1. **Ruff configuration** — Modern linter (replaces flake8, isort, black):
   ```toml
   [tool.ruff]
   line-length = 120
   target-version = "py312"
   select = ["E", "F", "I", "N", "W"]  # Error, Pyflakes, isort, naming, warnings
   ```
2. **mypy strict mode** — Enable to catch type issues early:
   ```toml
   [tool.mypy]
   strict = true
   ```
3. **Dataclasses vs Pydantic**:
   - Dataclasses: Lightweight, built-in, serialization via dataclasses.asdict()
   - Pydantic: Full validation, serialization, OpenAPI integration. Use for user input.
4. **Docstrings** — Google style for functions:
   ```python
   def fetch_user(user_id: int) -> dict:
       """Fetch user by ID.

       Args:
           user_id: The unique user identifier.

       Returns:
           Dictionary with user data.

       Raises:
           UserNotFoundError: If user_id does not exist.
       """
   ```

## Performance

1. **Generators over lists** — When iterating once:
   ```python
   # Good: memory efficient
   def read_large_file(path):
       with open(path) as f:
           for line in f:
               yield line.strip()

   # Avoid: loads entire file into memory
   with open(path) as f:
       lines = [line.strip() for line in f]
   ```
2. **__slots__** — For classes with many instances, reduce memory:
   ```python
   class Point:
       __slots__ = ['x', 'y']
       def __init__(self, x, y):
           self.x = x
           self.y = y
   ```
3. **functools.lru_cache** — Memoize expensive pure functions:
   ```python
   from functools import lru_cache
   @lru_cache(maxsize=128)
   def expensive_computation(n: int) -> int:
       return sum(i for i in range(n))
   ```
4. **Comprehensions** — Generally faster than loops:
   ```python
   result = [x * 2 for x in items if x > 0]  # Preferred
   ```

## Testing

1. **Pytest fixtures** — DRY up test setup:
   ```python
   @pytest.fixture
   def mock_db():
       return MockDatabase()

   def test_user_creation(mock_db):
       assert mock_db.get_user(1) is None
   ```
2. **Parametrize** — Test multiple inputs without duplication:
   ```python
   @pytest.mark.parametrize("input,expected", [
       ("valid", True),
       ("invalid", False),
   ])
   def test_validator(input, expected):
       assert validate(input) == expected
   ```
3. **conftest.py organization** — Share fixtures across test files (place in test directory root).
4. **Mock vs monkeypatch** — Use monkeypatch for simple replacements, mock for complex behavior.

## Logging

1. **Structlog or logging module** — Use per-module loggers:
   ```python
   import logging
   logger = logging.getLogger(__name__)
   ```
2. **Avoid f-strings in log calls** — Use lazy formatting:
   ```python
   # Good: message not formatted if log level is filtered
   logger.debug("User %s logged in", user_id)

   # Avoid: string always formatted
   logger.debug(f"User {user_id} logged in")
   ```
3. **Log levels**:
   - `DEBUG`: Internal operations, extraction details, state transitions
   - `INFO`: User-facing progress, summary results (rarely in CLI tools)
   - `WARNING`: Data quality issues, non-fatal errors
   - `ERROR`: Operation failures, exceptions
4. **Context** — Include relevant IDs in logs: request_id, user_id, session_id for traceability.

## Response Format

When reviewing Python code or advising on design, structure your response as:
1. **Summary** — What the code does in 1-2 sentences
2. **Issues** — Type issues, anti-patterns, performance problems (ordered by severity)
3. **Before/After examples** — Show anti-pattern and corrected version side-by-side
4. **Recommendations** — Concrete improvements with code snippets
5. **Complete refactored code** — If significant changes needed, provide the full improved version
