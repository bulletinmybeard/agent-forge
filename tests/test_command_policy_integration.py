from unittest.mock import patch

from agentforge.tools.command_policy import CommandPolicy, PolicyVerdict
from agentforge.tools.registry import ToolRegistry
from agentforge.tools.ssh_tools import ssh


def test_registry_denies_before_confirm(monkeypatch):
    reg = ToolRegistry()
    prompts = []
    reg.set_confirm_handler(lambda p: prompts.append(p) or True)

    policy = CommandPolicy(mode="allowlist", allowed_commands=("ls",))

    def fake_shell(command: str) -> str:
        return "ok"

    reg.register(fake_shell, name="shell")

    with (
        patch("agentforge.tools.registry.get_effective_policy", return_value=policy),
        patch(
            "agentforge.tools.registry.evaluate",
            return_value=PolicyVerdict(action="deny", reason="blocked", source="policy_allowlist"),
        ),
    ):
        result = reg.execute("shell", {"command": "npm install"})

    assert "blocked" in result.lower() or "not allowed" in result.lower() or "policy" in result.lower()
    assert prompts == []  # confirm must NOT fire


def test_registry_allow_skips_command_guard():
    reg = ToolRegistry()
    prompts = []
    reg.set_confirm_handler(lambda p: prompts.append(p) or True)

    policy = CommandPolicy(mode="allowlist", allowed_commands=("ls",))

    def fake_shell(command: str) -> str:
        return "ok"

    reg.register(fake_shell, name="shell")

    with (
        patch("agentforge.tools.registry.get_effective_policy", return_value=policy),
        patch(
            "agentforge.tools.registry.evaluate",
            return_value=PolicyVerdict(action="allow", reason="", source="policy_allowlist"),
        ),
        patch.object(reg, "_classify_guard") as mock_guard,
    ):
        result = reg.execute("shell", {"command": "ls -la"})

    assert result == "ok"
    mock_guard.assert_not_called()
    assert prompts == []


def test_registry_confirm_runs_command_guard():
    reg = ToolRegistry()
    prompts = []
    reg.set_confirm_handler(lambda p: prompts.append(p) or True)

    policy = CommandPolicy(mode="confirm")

    def fake_shell(command: str) -> str:
        return "ok"

    reg.register(fake_shell, name="shell")

    guard_result = {
        "source": "fast-path",
        "destructive": False,
        "sudo_only": False,
        "auto_confirmed": False,
        "verdict": "safe",
    }

    with (
        patch("agentforge.tools.registry.get_effective_policy", return_value=policy),
        patch(
            "agentforge.tools.registry.evaluate",
            return_value=PolicyVerdict(action="confirm", reason="", source="policy_confirm"),
        ),
        patch.object(reg, "_classify_guard", return_value=guard_result) as mock_guard,
    ):
        result = reg.execute("shell", {"command": "git status"})

    assert result == "ok"
    mock_guard.assert_called_once()


def test_ssh_policy_denied():
    policy = CommandPolicy(mode="allowlist", allowed_commands=("ls",))

    with (
        patch("agentforge.tools.ssh_tools._validate_host", return_value=None),
        patch(
            "agentforge.tools.command_policy_store.get_effective_policy",
            return_value=policy,
        ),
    ):
        result = ssh("myserver", "npm install")

    assert "Error:" in result
    assert "not allowed" in result.lower() or "npm" in result.lower()
