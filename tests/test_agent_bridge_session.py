"""Live tool callbacks stay on the run that is executing the tool."""

import contextvars
from concurrent.futures import ThreadPoolExecutor

from agentforge.tools.registry import ToolRegistry, submit_with_context
from web.server.agent_bridge import AgentBridge
from web.server.confirm import ConfirmationBroker


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    def ping() -> str:
        return "pong"

    registry.register(ping, name="ping")
    return registry


def _bridge(sink: list) -> AgentBridge:
    return AgentBridge(sink.append, ConfirmationBroker(), loop=None)  # type: ignore[arg-type]


def _names(sink: list, msg_type: str) -> list[str]:
    return [msg["name"] for msg in sink if msg.get("type") == msg_type]


def test_second_run_does_not_steal_tool_calls():
    registry = _registry()
    sent_a: list = []
    sent_b: list = []
    ctx_a = contextvars.Context()
    ctx_b = contextvars.Context()
    ctx_a.run(_bridge(sent_a).setup_registry, registry)
    ctx_b.run(_bridge(sent_b).setup_registry, registry)

    ctx_a.run(registry.execute, "ping")
    ctx_b.run(registry.execute, "ping")
    registry.execute("ping")
    ctx_a.run(registry.notify_tools_complete)

    assert _names(sent_a, "tool.call") == ["ping"]
    assert _names(sent_b, "tool.call") == ["ping"]
    assert [msg["type"] for msg in sent_a if msg.get("type") == "tool.calls.flush"] == ["tool.calls.flush"]
    assert not any(msg.get("type") == "tool.calls.flush" for msg in sent_b)


def test_pool_thread_keeps_the_submitting_run():
    registry = _registry()
    sent_a: list = []
    sent_b: list = []
    ctx_a = contextvars.Context()
    ctx_b = contextvars.Context()
    ctx_a.run(_bridge(sent_a).setup_registry, registry)
    ctx_b.run(_bridge(sent_b).setup_registry, registry)

    def fire_a() -> None:
        with ThreadPoolExecutor(max_workers=1) as pool:
            submit_with_context(pool, registry.execute, "ping").result()

    def fire_bare() -> None:
        registry.execute("ping")

    ctx_a.run(fire_a)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(fire_bare).result()

    assert _names(sent_a, "tool.call") == ["ping"]
    assert sent_b == []
