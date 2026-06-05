from agentforge.tools.registry import ToolRegistry


def test_sudo_only_verdict_does_not_prompt():
    reg = ToolRegistry()
    prompts = []
    reg.set_confirm_handler(lambda p: prompts.append(p) or True)
    guard = {"destructive": False, "sudo_only": True, "auto_confirmed": False, "verdict": "sudo"}

    def fake_shell(command: str) -> str:
        return ""

    result = reg._check_confirm(fake_shell, {"command": "sudo systemctl restart nginx"}, guard)
    assert result is None
    assert prompts == []


def test_destructive_still_prompts():
    reg = ToolRegistry()
    prompts = []
    reg.set_confirm_handler(lambda p: prompts.append(p) or True)
    guard = {"destructive": True, "sudo_only": False, "auto_confirmed": False, "verdict": "destructive"}
    result = reg._check_confirm(lambda command: "", {"command": "rm -rf /x"}, guard)
    assert result is None
    assert len(prompts) == 1
