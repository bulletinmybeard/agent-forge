"""Mode allowlist: tools outside the subset never execute."""

from __future__ import annotations

from agentforge.tools.registry import ToolRegistry


def _ping() -> str:
    return "pong"


def _boom() -> str:
    raise AssertionError("disallowed tool ran")


def test_execute_refuses_name_outside_allowlist():
    reg = ToolRegistry()
    reg.register(_ping, name="ping")
    reg.register(_boom, name="boom")
    out = reg.execute("boom", {}, allowed_tools=["ping"])
    assert "not available" in str(out)
    assert "boom" in str(out)


def test_execute_allows_name_on_allowlist():
    reg = ToolRegistry()
    reg.register(_ping, name="ping")
    assert reg.execute("ping", {}, allowed_tools=["ping"]) == "pong"


def test_execute_unrestricted_when_allowed_is_none():
    reg = ToolRegistry()
    reg.register(_ping, name="ping")
    assert reg.execute("ping", {}, allowed_tools=None) == "pong"


def test_deny_helper():
    assert ToolRegistry.deny_if_not_allowed("shell", ["film_stats"]) is not None
    assert ToolRegistry.deny_if_not_allowed("film_stats", ["film_stats"]) is None
    assert ToolRegistry.deny_if_not_allowed("shell", None) is None
