"""ProfileRouter — capability × intensity (no live LLM)."""

from __future__ import annotations

import types
from pathlib import Path

from agentforge.config import get_config, reset_config
from agentforge.router import (
    ProfileRouter,
    capability_for_mode,
    intensity_profile_name,
    valid_profiles,
)


class _FakeClient:
    def __init__(self, content: str, thinking: str | None = None) -> None:
        self._content = content
        self._thinking = thinking

    def chat(self, messages, **kwargs):  # noqa: ANN001, ARG002
        return types.SimpleNamespace(content=self._content, thinking=self._thinking)


def test_intensity_profile_name_default_is_family():
    assert intensity_profile_name("coder", "default") == "coder"
    assert intensity_profile_name("coder", "") == "coder"
    assert intensity_profile_name("coder", "light") == "coder-light"
    assert intensity_profile_name("coder", "heavy") == "coder-heavy"


def test_intensity_profile_name_vision_is_singleton():
    assert intensity_profile_name("visual", "heavy") == "vision"
    assert intensity_profile_name("vision", "light") == "vision"
    assert intensity_profile_name("embed", "light") == "embed"


def test_valid_profiles_coder_family():
    assert valid_profiles("coder") == {"coder", "coder-light", "coder-heavy"}


def test_valid_profiles_agent_includes_vision_escape():
    assert "vision" in valid_profiles("agent")
    assert "agent-light" in valid_profiles("agent")
    assert "agent-heavy" in valid_profiles("agent")


def test_capability_for_mode():
    assert capability_for_mode("coding") == "coder"
    assert capability_for_mode("agent") == "agent"
    assert capability_for_mode("discover") == "agent"
    assert capability_for_mode("chat") == "general"


def test_select_locked_capability_reads_intensity():
    router = ProfileRouter(_FakeClient('{"intensity": "heavy", "reason": "invent a helper"}'), fallback="coder")
    result = router.select("add a cloud reachability helper", capability="coder")
    assert result.profile == "coder-heavy"
    assert "helper" in result.reason


def test_select_accepts_composed_profile_name():
    router = ProfileRouter(_FakeClient('{"profile": "coder-light", "reason": "kwargs"}'), fallback="coder")
    result = router.select("add a size kwarg to the class", capability="coder")
    assert result.profile == "coder-light"


def test_select_unknown_falls_back_to_family():
    router = ProfileRouter(_FakeClient('{"intensity": "ultra", "reason": "nope"}'), fallback="coder")
    result = router.select("something", capability="coder")
    assert result.profile == "coder"
    assert result.reason  # keep model reason or fallback marker


def test_select_non_json_falls_back():
    router = ProfileRouter(_FakeClient("not json"), fallback="agent")
    result = router.select("list files", capability="agent")
    assert result.profile == "agent"
    assert "parse" in result.reason.lower() or result.reason.startswith("(")


def test_select_json_embedded_in_prose():
    router = ProfileRouter(
        _FakeClient('Sure.\n{"intensity": "heavy", "reason": "invent a helper"}\n'),
        fallback="coder",
    )
    result = router.select("add a reachability helper", capability="coder")
    assert result.profile == "coder-heavy"


def test_select_keyword_fallback_light():
    router = ProfileRouter(_FakeClient("this is a light one-shot listing"), fallback="agent")
    result = router.select("show the last 8 git commits", capability="agent")
    assert result.profile == "agent-light"


def test_select_json_in_thinking_side_channel():
    router = ProfileRouter(
        _FakeClient("", thinking='{"intensity": "light", "reason": "kwargs only"}'),
        fallback="coder",
    )
    result = router.select("add a timeout arg", capability="coder")
    assert result.profile == "coder-light"


def test_ollama_coder_family_resolves():
    root = Path(__file__).resolve().parents[1]
    reset_config()
    cfg = get_config(root / "config.yaml")
    light = cfg.get_profile("coder-light")
    default = cfg.get_profile("coder")
    heavy = cfg.get_profile("coder-heavy")
    assert "kimi-k2.7-code" in (light.model or "")
    assert "glm-5.2" in (default.model or "")
    assert "glm-5.3-flash" in (heavy.model or "")
    reset_config()
