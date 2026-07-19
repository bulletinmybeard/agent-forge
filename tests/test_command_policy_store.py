import pytest

from agentforge.config import reset_config
from agentforge.tools.command_policy import CommandPolicy
from agentforge.tools.command_policy_store import (
    clear_runtime_override,
    get_effective_policy,
    get_runtime_override,
    reset_db,
    set_db,
    set_runtime_override,
)
from web.server.database import ChatDatabase


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    db = ChatDatabase(db_path)
    db.create_tables()
    reset_db()
    set_db(db)
    monkeypatch.setenv("AGENTFORGE_CHAT_DB", str(db_path))
    yield db_path
    clear_runtime_override(None)
    reset_db()
    reset_config()


def test_runtime_override_overrides_yaml_mode(tmp_db, monkeypatch):
    set_runtime_override("shell", CommandPolicy(mode="allowlist", allowed_commands=("ls",)))
    p = get_effective_policy("shell")
    assert p.mode == "allowlist"
    assert p.allowed_commands == ("ls",)
    clear_runtime_override("shell")


def test_get_runtime_override_none_when_empty(tmp_db):
    assert get_runtime_override("shell") is None


def test_set_and_get_runtime_override(tmp_db):
    policy = CommandPolicy(
        mode="denylist",
        blocked_patterns=(r"rm\s+-rf",),
    )
    set_runtime_override("ssh", policy)
    loaded = get_runtime_override("ssh")
    assert loaded == policy


def test_clear_runtime_override_single_tool(tmp_db):
    set_runtime_override("shell", CommandPolicy(mode="allowlist", allowed_commands=("ls",)))
    set_runtime_override("ssh", CommandPolicy(mode="confirm"))
    clear_runtime_override("shell")
    assert get_runtime_override("shell") is None
    assert get_runtime_override("ssh") is not None
    clear_runtime_override("ssh")


def test_clear_runtime_override_all(tmp_db):
    set_runtime_override("shell", CommandPolicy(mode="allowlist", allowed_commands=("ls",)))
    set_runtime_override("ssh", CommandPolicy(mode="confirm"))
    clear_runtime_override(None)
    assert get_runtime_override("shell") is None
    assert get_runtime_override("ssh") is None


def test_effective_policy_override_is_full_document(tmp_db, monkeypatch):
    """Empty override lists clear the YAML baseline (full-document semantics)."""
    yaml_policy = CommandPolicy(
        mode="confirm",
        allowed_commands=("git", "ls"),
        blocked_patterns=(r"sudo",),
    )
    monkeypatch.setattr(
        "agentforge.tools.command_policy_store.load_yaml_policy",
        lambda _tool: yaml_policy,
    )
    set_runtime_override(
        "shell",
        CommandPolicy(mode="allowlist", allowed_commands=(), blocked_patterns=()),
    )
    effective = get_effective_policy("shell")
    assert effective.mode == "allowlist"
    assert effective.allowed_commands == ()
    assert effective.blocked_patterns == ()
    clear_runtime_override("shell")
