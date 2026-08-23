from dataclasses import replace

from agentforge.config import AIProfile
from agentforge.session_overrides import apply_session_overrides


def _base(**kw) -> AIProfile:
    p = AIProfile(
        name="agent",
        model="minimax-m3:cloud",
        provider="ollama",
        temperature=0.1,
        max_tokens=8000,
        parse_thinking=True,
    )
    return replace(p, **kw) if kw else p


def test_no_overrides_returns_same_profile():
    p = _base()
    assert apply_session_overrides(p, None) is p
    assert apply_session_overrides(p, {}) is p


def test_per_profile_model_and_temp():
    p = _base()
    out = apply_session_overrides(
        p,
        {
            "profiles": {
                "agent": {
                    "model": "deepseek-v4-flash:0731-cloud",
                    "temperature": 0.2,
                    "max_tokens": 16000,
                }
            }
        },
    )
    assert out is not p
    assert out.model == "deepseek-v4-flash:0731-cloud"
    assert out.temperature == 0.2
    assert out.max_tokens == 16000
    # Non-overridable fields preserved
    assert out.parse_thinking is True
    assert out.provider == "ollama"
    assert out.name == "agent"


def test_profile_key_lookup_when_name_differs():
    """Resolved profile name may differ from the role key used in the UI."""
    p = _base(name="ollama-devstral-small", model="minimax-m3:cloud")
    out = apply_session_overrides(
        p,
        {"profiles": {"default": {"model": "nemotron-3-ultra:cloud", "temperature": 0.3}}},
        profile_key="default",
    )
    assert out.model == "nemotron-3-ultra:cloud"
    assert out.temperature == 0.3


def test_unrelated_profile_key_ignored():
    p = _base()
    out = apply_session_overrides(
        p,
        {"profiles": {"fast": {"model": "kimi-k2.6:cloud", "temperature": 0.5}}},
    )
    assert out.model == p.model
    assert out.temperature == p.temperature


def test_flat_model_wins_over_profiles_map():
    p = _base()
    out = apply_session_overrides(
        p,
        {
            "profiles": {"agent": {"model": "deepseek-v4-flash:0731-cloud"}},
            "model": "kimi-k3:cloud",
            "temperature": 0.9,
        },
    )
    assert out.model == "kimi-k3:cloud"
    assert out.temperature == 0.9


def test_aiclient_accepts_session_overrides():
    from agentforge.client import AIClient
    from agentforge.config import get_config

    cfg = get_config()
    # Use a profile that must exist in this project's config
    try:
        base = cfg.get_profile("agent")
    except ValueError:
        base = cfg.get_profile("default")

    client = AIClient(
        profile=base.name,
        session_overrides={
            "profiles": {
                base.name: {
                    "model": "deepseek-v4-flash:0731-cloud",
                    "temperature": 0.15,
                }
            }
        },
    )
    assert client.profile.model == "deepseek-v4-flash:0731-cloud"
    assert client.profile.temperature == 0.15
    # Host / provider still from YAML resolution
    assert client.profile.provider
