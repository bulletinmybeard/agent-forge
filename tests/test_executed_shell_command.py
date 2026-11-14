"""The Tools card is drawn before shell rewrites a command.

These tests cover the value that travels back from the worker so the card
can be corrected to the command that actually ran.
"""

import io
import json

from agentforge.parallel import ParallelAgentRunner
from agentforge.tools import shell as sh
from agentforge.tools.shell import (
    _EXECUTED_RESULT_KEY,
    pack_tool_result,
    publish_executed_shell_command,
    shell,
    take_executed_shell_command,
    unpack_tool_result,
)


def teardown_function():
    take_executed_shell_command()


def test_take_returns_published_command_once():
    publish_executed_shell_command("stat -f '%z' /etc/hosts")
    assert take_executed_shell_command() == "stat -f '%z' /etc/hosts"
    assert take_executed_shell_command() is None


def test_pack_roundtrip_dict():
    packed = pack_tool_result("$ stat -f '%z' /etc/hosts\n\n4218", "stat -f '%z' /etc/hosts")
    output, executed = unpack_tool_result(packed)
    assert executed == "stat -f '%z' /etc/hosts"
    assert output.startswith("$ stat -f '%z'")
    assert "4218" in output


def test_pack_plain_when_unchanged():
    assert pack_tool_result("ok", None) == "ok"
    assert unpack_tool_result("ok") == ("ok", None)


def test_unpack_json_string_wrapper():
    payload = json.dumps({"result": "4218", _EXECUTED_RESULT_KEY: "stat -f '%z' /etc/hosts"})
    output, executed = unpack_tool_result(payload)
    assert output == "4218"
    assert executed == "stat -f '%z' /etc/hosts"


def test_unpack_leaves_unrelated_json():
    payload = json.dumps({"result": "4218", "bytes": 22})
    assert unpack_tool_result(payload) == (payload, None)


class _Proc:
    pid = 42

    def __init__(self, out: str):
        self.stdout = io.StringIO(out)
        self.stderr = io.StringIO("")
        self.stdin = None

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


def _install_popen(monkeypatch, out: str = "4218\n"):
    monkeypatch.setattr(sh, "_IS_MACOS", True)
    monkeypatch.setattr(sh, "_fix_platform_compat", lambda command: (command, ""))
    monkeypatch.setattr(sh, "_validate_command", lambda command: None)
    monkeypatch.setattr(sh.subprocess, "Popen", lambda *args, **kwargs: _Proc(out))


def test_shell_records_rewritten_command(monkeypatch):
    _install_popen(monkeypatch)
    output = shell("stat -c %s /etc/hosts")
    assert take_executed_shell_command() == "stat -f '%z' /etc/hosts"
    assert output.startswith("$ stat -f '%z' /etc/hosts")
    assert "4218" in output


def test_shell_records_nothing_when_command_is_unchanged(monkeypatch):
    _install_popen(monkeypatch, out="hi\n")
    output = shell("echo hi")
    assert take_executed_shell_command() is None
    assert output.startswith("$ echo hi")


def test_parallel_reports_the_command_that_ran():
    class _Registry:
        def execute_with_role(self, name, args):
            assert name == "shell"
            command = args["command"]
            if command.startswith("stat -c"):
                executed = "stat -f '%z' /etc/hosts"
                publish_executed_shell_command(executed)
                return f"$ {executed}\n\n4218"
            return f"$ {command}\n\nhi"

    events = []
    runner = ParallelAgentRunner(client=None, registry=_Registry(), max_workers=1)
    ctx = runner.execute(
        {
            "groups": [
                {
                    "label": "sizes",
                    "commands": [
                        {"command": "stat -c %s /etc/hosts", "cwd": "/tmp"},
                        {"command": "echo hi", "cwd": "/tmp"},
                    ],
                }
            ]
        },
        on_group_event=lambda *parts: events.append(parts),
    )

    revised = [event for event in events if event[2] == "command_revised"]
    assert len(revised) == 1
    _idx, _label, _kind, data = revised[0]
    assert data["command"] == "stat -c %s /etc/hosts"
    assert data["executed"] == "stat -f '%z' /etc/hosts"
    assert data["cwd"] == "/tmp"

    outputs = ctx.metadata["parallel_results"][0].outputs
    assert outputs[0]["command"] == "stat -f '%z' /etc/hosts"
    assert outputs[1]["command"] == "echo hi"
    assert "stat -c %s" not in (ctx.result or "")
