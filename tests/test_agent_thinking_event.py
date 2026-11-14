"""agent.thinking names the model request that is in flight."""

from web.server.protocol import agent_thinking


def test_agent_thinking_includes_model_request_fields():
    msg = agent_thinking(
        2,
        "calling_model",
        1.234,
        model="qwen3.5",
        profile="",
        provider="",
        messages=4,
        purpose="final",
    )
    assert msg["type"] == "agent.thinking"
    assert msg["model"] == "qwen3.5"
    assert msg["messages"] == 4
    assert msg["purpose"] == "final"
    assert msg["elapsed"] == 1.23
    assert "profile" not in msg
    assert "provider" not in msg
