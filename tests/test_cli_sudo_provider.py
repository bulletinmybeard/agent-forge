from agentforge.tools.cli_sudo_provider import CliSudoProvider


def test_prompts_once_then_caches(monkeypatch):
    calls = []
    p = CliSudoProvider(getpass_fn=lambda prompt: calls.append(prompt) or "pw", isatty=lambda: True)
    assert p.get("localhost") == "pw"
    assert p.get("localhost") == "pw"  # cached, no second prompt
    assert len(calls) == 1


def test_non_tty_returns_none():
    p = CliSudoProvider(getpass_fn=lambda prompt: "pw", isatty=lambda: False)
    assert p.get("localhost") is None


def test_invalidate_forces_reprompt():
    calls = []
    p = CliSudoProvider(getpass_fn=lambda prompt: calls.append(1) or "pw", isatty=lambda: True)
    p.get("localhost")
    p.invalidate("localhost")
    p.get("localhost")
    assert len(calls) == 2
