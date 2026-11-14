"""Shell-host prompt facts and the Darwin `stat -c` rewrite.

The agent loop often runs in a Linux container while `shell` is dispatched to
a worker. These tests pin both sides of that split: the prompt must
describe the worker that runs the command, and GNU `stat -c %s` must become
BSD `stat -f '%z'` in the process that executes it.
"""

import json

import pytest

import agentforge.tools.shell as sh
import agentforge.tools.system as system


class FakeRedis:
    def __init__(self, initial=None):
        self.data = dict(initial or {})

    def set(self, key, value):
        self.data[key] = value
        return True

    def get(self, key):
        return self.data.get(key)

    def ping(self):
        return True


def teardown_function():
    system._redis_client_cached = None


def test_darwin_condensed_keeps_stat_hint(monkeypatch):
    monkeypatch.setattr(system.platform, "system", lambda: "Darwin")
    ctx = system.get_system_context()
    assert "stat -f '%z'" in ctx["summary"]
    assert "stat -f '%z'" in ctx["condensed_summary"]
    assert "not stat -c '%s'" in ctx["condensed_summary"]


def test_linux_condensed_has_no_bsd_hint(monkeypatch):
    monkeypatch.setattr(system.platform, "system", lambda: "Linux")
    ctx = system.get_system_context()
    assert ctx["os"] == "Linux"
    assert "stat -f" not in ctx["condensed_summary"]
    assert "BSD" not in ctx["summary"]


def test_in_process_uses_this_process(monkeypatch):
    monkeypatch.setattr(system, "dispatch_mode", lambda: "in_process")
    ctx = system.context_for_shell()
    assert "not in this process" not in ctx["summary"]
    assert ctx["os"] == system.platform.system()


def test_split_same_role_uses_this_process(monkeypatch):
    monkeypatch.setattr(system, "dispatch_mode", lambda: "split")
    monkeypatch.setattr(system, "get_role_for_tool", lambda name: "local")
    monkeypatch.setattr(system, "my_role", lambda: "local")
    ctx = system.context_for_shell()
    assert "not in this process" not in ctx["summary"]
    assert ctx["os"] == system.platform.system()


def test_split_uses_published_shell_host(monkeypatch):
    published = {
        "os": "Darwin",
        "os_label": "macOS (24.0.0)",
        "summary": "OS=macOS (24.0.0), arch=arm64. stat -f '%z' (NOT stat -c '%s').",
        "condensed_summary": "OS=macOS (24.0.0). BSD userland: stat -f '%z' (not stat -c '%s')",
        "sibling_projects": [],
    }
    monkeypatch.setattr(system, "dispatch_mode", lambda: "split")
    monkeypatch.setattr(system, "get_role_for_tool", lambda name: "local")
    monkeypatch.setattr(system, "my_role", lambda: "remote")
    monkeypatch.setattr(system, "load_host_context", lambda role: dict(published) if role == "local" else None)

    ctx = system.context_for_shell()
    assert "Shell and file tools run on the 'local' worker (macOS (24.0.0))" in ctx["summary"]
    assert "not in this process" in ctx["summary"]
    assert "stat -f '%z'" in ctx["summary"]
    assert "stat -c" in ctx["summary"]
    assert ctx["condensed_summary"].startswith("Shell/file tools: 'local' worker")
    assert "stat -f '%z'" in ctx["condensed_summary"]
    # The Linux process's own OS must not replace the published one.
    assert ctx["os"] == "Darwin"


def test_split_missing_publish_does_not_claim_this_os(monkeypatch):
    monkeypatch.setattr(system.platform, "system", lambda: "Linux")
    monkeypatch.setattr(system, "dispatch_mode", lambda: "split")
    monkeypatch.setattr(system, "get_role_for_tool", lambda name: "local")
    monkeypatch.setattr(system, "my_role", lambda: "remote")
    monkeypatch.setattr(system, "load_host_context", lambda role: None)

    ctx = system.context_for_shell()
    assert ctx["os"] == ""
    assert "not published" in ctx["summary"].lower() or "has not published" in ctx["summary"]
    assert "wc -c" in ctx["summary"]
    assert "Linux" not in ctx["summary"]
    assert "macOS" not in ctx["summary"]
    assert "wc -c" in ctx["condensed_summary"]


def test_publish_roundtrip(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(system, "_redis_client", lambda: fake)
    monkeypatch.setattr(system, "my_role", lambda: "local")

    assert system.publish_host_context() is True
    loaded = system.load_host_context("local")
    assert loaded is not None
    assert loaded["os"] == system.platform.system()
    assert loaded["summary"]
    assert isinstance(loaded["sibling_projects"], list)
    raw = fake.data[system.host_context_key("local")]
    assert json.loads(raw)["os"] == loaded["os"]


def test_publish_false_when_redis_down(monkeypatch):
    monkeypatch.setattr(system, "_redis_client", lambda: None)
    assert system.publish_host_context("local") is False
    assert system.load_host_context("local") is None


def test_load_rejects_bad_json(monkeypatch):
    fake = FakeRedis({system.host_context_key("local"): "not-json"})
    monkeypatch.setattr(system, "_redis_client", lambda: fake)
    assert system.load_host_context("local") is None


@pytest.mark.parametrize(
    "command,expected",
    [
        ("stat -c %s /etc/hosts", "stat -f '%z' /etc/hosts"),
        ("stat -c '%s' /etc/resolv.conf", "stat -f '%z' /etc/resolv.conf"),
        ('stat -c "%s" /etc/hosts', "stat -f '%z' /etc/hosts"),
        ("stat -c%s /etc/hosts", "stat -f '%z' /etc/hosts"),
        ("stat -L -c '%s' /etc/hosts", "stat -L -f '%z' /etc/hosts"),
        ("stat --format=%s /etc/hosts", "stat -f '%z' /etc/hosts"),
        ("stat --format='%s' /etc/hosts", "stat -f '%z' /etc/hosts"),
        (
            "stat -c %s /etc/hosts; stat -c '%s' /etc/resolv.conf",
            "stat -f '%z' /etc/hosts; stat -f '%z' /etc/resolv.conf",
        ),
    ],
)
def test_gnu_stat_size_rewritten_on_macos(monkeypatch, command, expected):
    monkeypatch.setattr(sh, "_IS_MACOS", True)
    rewritten, note = sh._rewrite_gnu_stat(command)
    assert rewritten == expected
    assert note


def test_gnu_stat_left_alone_on_linux(monkeypatch):
    monkeypatch.setattr(sh, "_IS_MACOS", False)
    command = "stat -c %s /etc/hosts"
    rewritten, note = sh._rewrite_gnu_stat(command)
    assert rewritten == command
    assert note == ""


def test_bsd_stat_and_other_formats_unchanged(monkeypatch):
    monkeypatch.setattr(sh, "_IS_MACOS", True)
    for command in ("stat -f '%z' /etc/hosts", "stat -c '%n' /etc/hosts", "ls -l /etc/hosts"):
        rewritten, note = sh._rewrite_gnu_stat(command)
        assert rewritten == command
        assert note == ""


def test_stat_does_not_take_the_llm_rewrite(monkeypatch):
    monkeypatch.setattr(sh, "_IS_MACOS", True)
    assert sh._needs_platform_fix("stat -f '%z' /etc/hosts") is False
    assert sh._needs_platform_fix("stat -c '%n' /etc/hosts") is False
    assert sh._needs_platform_fix("sed -i 's/a/b/' file") is True
