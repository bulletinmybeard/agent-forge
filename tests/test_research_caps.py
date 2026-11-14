"""Research output caps stay on the research clients."""

from agentforge.config import AIProfile
from web.server import ws_endpoint


class _Cfg:
    def __init__(self, research: dict | None = None, profile: AIProfile | None = None):
        self._research = research
        self._profile = profile

    def get(self, key: str):
        assert key == "research"
        return self._research

    def get_profile(self, name: str) -> AIProfile:
        assert self._profile is not None
        assert name == self._profile.name
        return self._profile


def test_research_settings_reads_generous_caps(monkeypatch):
    monkeypatch.setattr(
        ws_endpoint,
        "get_config",
        lambda: _Cfg({"max_tool_output": 1_000_000, "max_tokens": 131072}),
    )
    cfg = ws_endpoint._research_settings()
    assert cfg["max_tool_output"] == 1_000_000
    assert cfg["max_tokens"] == 131072


def test_research_settings_non_positive_falls_back(monkeypatch):
    monkeypatch.setattr(
        ws_endpoint,
        "get_config",
        lambda: _Cfg({"max_tool_output": 0, "max_tokens": -1}),
    )
    cfg = ws_endpoint._research_settings()
    assert cfg["max_tool_output"] == 5000
    assert cfg["max_tokens"] == 131072


def test_research_client_clones_the_output_cap(monkeypatch):
    shared = AIProfile(name="cloud-heavy", model="example-model:cloud", max_tokens=16000)
    monkeypatch.setattr(ws_endpoint, "get_config", lambda: _Cfg(profile=shared))
    client = ws_endpoint._research_client("cloud-heavy", 131072)
    assert client.profile is not shared
    assert client.profile.max_tokens == 131072
    assert shared.max_tokens == 16000
    assert client.profile.model == "example-model:cloud"
