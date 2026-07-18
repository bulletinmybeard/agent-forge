from __future__ import annotations

import pytest

from agentforge.tools.command_policy import CommandPolicy, load_yaml_policy


class _FakeConfig:
    def __init__(self, raw: dict) -> None:
        self._raw = raw


@pytest.fixture
def fake_config(monkeypatch):
    holder: dict = {"raw": {"tools": {}}}

    def _get_config():
        return _FakeConfig(holder["raw"])

    # Patch the name bound in command_policy (top-level import).
    monkeypatch.setattr("agentforge.tools.command_policy.get_config", _get_config)
    return holder


def test_load_permissions_block(fake_config):
    fake_config["raw"] = {
        "tools": {
            "shell": {
                "permissions": {
                    "mode": "allowlist",
                    "allowed_commands": ["git", "ls"],
                    "allowed_patterns": [r"^npm\s+"],
                    "blocked_patterns": [r"rm\s+-rf"],
                }
            }
        }
    }
    policy = load_yaml_policy("shell")
    assert policy.mode == "allowlist"
    assert policy.allowed_commands == ("git", "ls")
    assert policy.allowed_patterns == (r"^npm\s+",)
    assert policy.blocked_patterns == (r"rm\s+-rf",)


def test_legacy_fallback_when_permissions_empty(fake_config):
    fake_config["raw"] = {
        "tools": {
            "shell": {
                "allowed_commands": ["npm", "node"],
                "blocked_patterns": [r"sudo\s+"],
                "permissions": {
                    "mode": "confirm",
                    "allowed_commands": [],
                    "blocked_patterns": [],
                },
            }
        }
    }
    policy = load_yaml_policy("shell")
    assert policy.mode == "confirm"
    assert policy.allowed_commands == ("npm", "node")
    assert policy.blocked_patterns == (r"sudo\s+",)


def test_legacy_only_without_permissions_key(fake_config):
    fake_config["raw"] = {
        "tools": {
            "ssh": {
                "allowed_commands": ["git"],
                "blocked_patterns": [r"git\s+push"],
            }
        }
    }
    policy = load_yaml_policy("ssh")
    assert policy.mode == "confirm"
    assert policy.allowed_commands == ("git",)
    assert policy.blocked_patterns == (r"git\s+push",)


def test_permissions_override_legacy_lists(fake_config):
    fake_config["raw"] = {
        "tools": {
            "shell": {
                "allowed_commands": ["legacy-cmd"],
                "blocked_patterns": [r"legacy-block"],
                "permissions": {
                    "mode": "denylist",
                    "allowed_commands": ["git"],
                    "blocked_patterns": [r"rm\s+"],
                },
            }
        }
    }
    policy = load_yaml_policy("shell")
    assert policy.mode == "denylist"
    assert policy.allowed_commands == ("git",)
    assert policy.blocked_patterns == (r"rm\s+",)


def test_config_error_returns_defaults(monkeypatch):
    def _boom():
        raise RuntimeError("no config")

    monkeypatch.setattr("agentforge.tools.command_policy.get_config", _boom)
    policy = load_yaml_policy("shell")
    assert policy == CommandPolicy()
