from agentforge.client import (
    add_model_used,
    get_models_used,
    normalize_model_label,
    reset_models_used,
)


def test_profile_label_wins_over_model_id():
    from types import SimpleNamespace

    from agentforge.client import profile_display_label

    labeled = SimpleNamespace(label="glm-5.3-flash", model="glm-5.3-flash:cloud")
    assert profile_display_label(labeled) == "glm-5.3-flash"
    bare = SimpleNamespace(label="", model="deepseek/deepseek-v4-flash")
    assert profile_display_label(bare) == "deepseek-v4-flash"

    reset_models_used()
    add_model_used(profile_display_label(labeled))
    add_model_used(profile_display_label(labeled))
    assert get_models_used(profile_display_label(labeled)) == ["glm-5.3-flash"]


def test_normalize_strips_provider_prefix():
    assert normalize_model_label("deepseek/deepseek-v4-flash") == "deepseek-v4-flash"
    assert normalize_model_label("minimax-m3:cloud") == "minimax-m3:cloud"
    assert normalize_model_label(None) == ""


def test_chain_collapses_consecutive_duplicates():
    reset_models_used()
    add_model_used("minimax-m3:cloud")
    add_model_used("minimax-m3:cloud")
    add_model_used("deepseek-v4-flash:0731-cloud")
    add_model_used("deepseek-v4-flash:0731-cloud")
    add_model_used("mistral-large-3:675b-cloud")
    assert get_models_used() == [
        "minimax-m3:cloud",
        "deepseek-v4-flash:0731-cloud",
        "mistral-large-3:675b-cloud",
    ]


def test_get_models_used_extra_fills_gap():
    reset_models_used()
    # No calls recorded (e.g. context lost) — still surface the primary client
    assert get_models_used("kimi-k3:cloud") == ["kimi-k3:cloud"]
    reset_models_used()
    add_model_used("minimax-m3:cloud")
    # Primary already present — no dup
    assert get_models_used("minimax-m3:cloud") == ["minimax-m3:cloud"]
    # Different primary appended if missing
    assert get_models_used("nemotron-3-ultra:cloud") == [
        "minimax-m3:cloud",
        "nemotron-3-ultra:cloud",
    ]


def test_decide_result_records_model_on_the_caller():
    from agentforge.agent import record_tool_model

    reset_models_used()
    record_tool_model(
        "decide",
        '{"model": "clef", "answers": {"health": {"choice": "degraded"}}}',
    )
    # A failed call has no answer. Another tool's JSON is not a decision model.
    record_tool_model("decide", '{"error": "404 page not found", "status": 404}')
    record_tool_model("shell", '{"model": "nope"}')
    assert get_models_used() == ["clef"]


def test_uninitialised_tracking_is_noop():
    # Clear the ContextVar to the default (None)
    from agentforge import client as client_mod

    token = client_mod._models_used.set(None)
    try:
        add_model_used("should-not-appear")
        assert get_models_used() == []
        # extra still works as fallback for summary
        assert get_models_used("agent-model") == ["agent-model"]
    finally:
        client_mod._models_used.reset(token)
