"""Small typing helpers shared across agentforge modules."""

from __future__ import annotations

from collections.abc import Callable
from types import FunctionType
from typing import Any, Literal, cast

PlaywrightWaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]

ToolCallable = FunctionType

_PLAYWRIGHT_WAITS = frozenset({"commit", "domcontentloaded", "load", "networkidle"})


def callable_name(func: Callable[..., Any]) -> str:
    """Return a tool/function name without assuming every Callable is a function."""
    return getattr(func, "__name__", type(func).__name__)


def as_playwright_wait(value: str, *, default: PlaywrightWaitUntil = "networkidle") -> PlaywrightWaitUntil:
    """Coerce user/config input to a Playwright ``wait_until`` literal."""
    if value in _PLAYWRIGHT_WAITS:
        return cast(PlaywrightWaitUntil, value)
    return default
