"""Ollama native message.thinking + agent empty/plan handling.

Regression: thinking models put CoT in ``message.thinking``. Backend must
capture it without promoting intermediate traces to content (that ended the
agent loop early). AgentLoop nudges, then promotes thinking as last resort.
"""

from __future__ import annotations

import types
from typing import Any

from agentforge.agent import (
    AgentIteration,
    _best_answer_from_iterations,
    _coerce_final_text,
    _is_length_stop,
    _looks_like_leaked_reasoning,
    _looks_like_plan_fragment,
)
from agentforge.backends.ollama import OllamaBackend


def _backend(*, parse_thinking: bool = True) -> OllamaBackend:
    """Build a backend without opening a real Ollama client."""
    b = object.__new__(OllamaBackend)
    b._profile = types.SimpleNamespace(  # ty: ignore[invalid-assignment]
        parse_thinking=parse_thinking,
        model="deepseek-v4-flash:0731-cloud",
    )
    return b


def _raw(
    *,
    content: str | None = "",
    thinking: str | None = None,
    tool_calls: list[Any] | None = None,
) -> types.SimpleNamespace:
    msg = types.SimpleNamespace(content=content, thinking=thinking, tool_calls=tool_calls)
    return types.SimpleNamespace(
        message=msg,
        model="deepseek-v4-flash:0731-cloud",
        done_reason="stop",
        total_duration=None,
        prompt_eval_count=10,
        eval_count=20,
    )


def test_native_thinking_captured_not_promoted():
    """Thinking stays in the side channel; content unchanged when present."""
    resp = _backend()._wrap_response(_raw(content="The answer is 4.", thinking="Count the letters carefully."))
    assert resp.thinking == "Count the letters carefully."
    assert resp.content == "The answer is 4."
    assert resp.tool_calls is None


def test_empty_content_does_not_promote_thinking_in_backend():
    """AgentLoop owns promote-vs-nudge; backend must leave content empty."""
    writeup = "Full analysis that should only surface after agent decides."
    resp = _backend()._wrap_response(_raw(content="", thinking=writeup))
    assert resp.content == ""
    assert resp.thinking == writeup
    assert resp.tool_calls is None


def test_tool_turn_keeps_empty_content_and_thinking():
    tc = types.SimpleNamespace(function=types.SimpleNamespace(name="read_file", arguments={"path": "x.py"}))
    resp = _backend()._wrap_response(_raw(content="", thinking="I should read the file first.", tool_calls=[tc]))
    assert resp.content == ""
    assert resp.thinking == "I should read the file first."
    assert resp.tool_calls == [{"name": "read_file", "arguments": {"path": "x.py"}}]


def test_inline_think_tags_still_stripped():
    raw_content = "<think>internal plan</think>\n\nVisible answer."
    resp = _backend(parse_thinking=True)._wrap_response(_raw(content=raw_content))
    assert resp.thinking == "internal plan"
    assert resp.content == "Visible answer."


def test_inline_tags_only_leaves_empty_content_with_thinking():
    raw_content = "<think>Full analysis that should surface as the answer.</think>\n"
    resp = _backend(parse_thinking=True)._wrap_response(_raw(content=raw_content))
    assert (resp.content or "").strip() == ""
    assert resp.thinking is not None
    assert "Full analysis" in resp.thinking


def test_native_thinking_merged_with_tags():
    raw_content = "<think>tag trace</think>\nDone."
    resp = _backend()._wrap_response(_raw(content=raw_content, thinking="native trace"))
    assert resp.content == "Done."
    assert resp.thinking is not None
    assert "native trace" in resp.thinking
    assert "tag trace" in resp.thinking


def test_plan_fragment_heuristic():
    assert _looks_like_plan_fragment(
        "Now the `app/config.py` and `web/server/api.py` pieces — they look like other tier-resolution sites."
    )
    assert _looks_like_plan_fragment("Let me read the remaining call sites.")
    assert not _looks_like_plan_fragment(
        "## Layering\n\nOllama is the base map. Cloud providers overlay "
        "per-tier entries. Resolution happens in `_compute_active_map` "
        "and `_resolve_profile` at agentforge/config.py:520-600.\n\n"
        "### Risk\nSharing reasoner and general hides capability gaps."
    )


def test_best_answer_prefers_content_then_thinking():
    iters = [
        AgentIteration(iteration=1, response="", thought="plan step 1"),
        AgentIteration(iteration=2, response="", thought="deeper analysis of _wrap_response"),
        AgentIteration(iteration=3, response="  ", thought=""),
    ]
    assert "deeper analysis" in _best_answer_from_iterations(iters)
    iters[-1].response = "Final visible answer with enough substance to win"
    assert "Final visible answer" in _best_answer_from_iterations(iters)
    assert _best_answer_from_iterations([], ctx_thinking="from ctx") == "from ctx"


def test_best_answer_skips_plan_fragments():
    iters = [
        AgentIteration(
            iteration=1,
            response="",
            thought=(
                "Here is the full analysis of provider_override_map layering: "
                "Ollama is the base, cloud providers update on top, get_profile "
                "is the single funnel at config.py."
            ),
        ),
        AgentIteration(
            iteration=2,
            response="Now the app/config.py pieces — they look like other sites.",
            thought="Let me read more files next.",
        ),
    ]
    best = _best_answer_from_iterations(iters)
    assert "full analysis" in best
    assert not best.startswith("Now the")


def test_coerce_final_prefers_content_over_thinking():
    assert _coerce_final_text("Visible answer.", "private CoT only") == "Visible answer."


def test_coerce_final_falls_back_to_thinking():
    assert _coerce_final_text("", "CoT that becomes the answer") == "CoT that becomes the answer"


def test_coerce_final_swaps_plan_fragment_for_better_leftover():
    iters = [
        AgentIteration(
            iteration=1,
            thought=(
                "Complete write-up: empty content does not terminate in the "
                "backend; AgentLoop nudges then promotes thinking or force-finals."
            ),
        ),
    ]
    text = _coerce_final_text(
        "Now I'll summarize the findings.",
        "Let me write the answer.",
        iterations=iters,
    )
    assert "Complete write-up" in text


def test_length_stop_reasons():
    assert _is_length_stop("length")
    assert _is_length_stop("max_tokens")
    assert not _is_length_stop("stop")
    assert not _is_length_stop(None)


def test_leaked_reasoning_opener():
    dump = (
        "The user wants me to add a new model profile ollama-glm-5-3 "
        "before ollama-glm-5-3-flash in the YAML file.\n"
        "The file content I read — I need to figure out the line numbers."
    )
    assert _looks_like_leaked_reasoning(dump)


def test_leaked_reasoning_hand_numbered_reconstruction():
    lines = [f"{i}:     provider: ollama" for i in range(1, 21)]
    assert _looks_like_leaked_reasoning("\n".join(lines))


def test_leaked_reasoning_ignores_structured_edit():
    text = (
        "The user wants a new profile.\n"
        "<<<EDIT start_line=161 end_line=0 mode=insert_before>>>\n"
        "```yaml\n  ollama-glm-5-3:\n    model: x\n```\n"
        "<<<END>>>"
    )
    assert not _looks_like_leaked_reasoning(text)


def _params_backend(*, parse_thinking: bool, extra_body: dict | None = None) -> OllamaBackend:
    b = object.__new__(OllamaBackend)
    b._profile = types.SimpleNamespace(  # ty: ignore[invalid-assignment]
        parse_thinking=parse_thinking,
        model="glm-5.3-flash:cloud",
        temperature=0.2,
        max_tokens=16000,
        top_p=None,
        top_k=None,
        repeat_penalty=None,
        stop=None,
        keep_alive=None,
        extra_body=extra_body or {},
    )
    return b


def test_parse_thinking_sets_think_true():
    params = _params_backend(parse_thinking=True)._build_chat_params([], False, None, None, None)
    assert params["think"] is True


def test_extra_body_can_override_think():
    params = _params_backend(parse_thinking=True, extra_body={"think": False})._build_chat_params(
        [], False, None, None, None
    )
    assert params["think"] is False


def test_no_think_when_parse_thinking_off():
    params = _params_backend(parse_thinking=False)._build_chat_params([], False, None, None, None)
    assert "think" not in params


def test_reasoning_effort_is_not_forwarded_to_chat():
    params = _params_backend(
        parse_thinking=True,
        extra_body={"think": True, "reasoning_effort": "max"},
    )._build_chat_params([], False, None, None, None)
    assert "reasoning_effort" not in params
    assert params["think"] == "high"


def test_think_max_coerced_to_high():
    params = _params_backend(
        parse_thinking=True,
        extra_body={"think": "max"},
    )._build_chat_params([], False, None, None, None)
    assert params["think"] == "high"
    assert "reasoning_effort" not in params
