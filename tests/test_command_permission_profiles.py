"""Named command permission profiles (YAML + user SQLite)."""

import pytest

from agentforge.config import reset_config
from agentforge.tools.command_permission_profiles import (
    apply_profile,
    delete_user_profile,
    get_profile,
    list_profiles,
    save_user_profile,
)
from agentforge.tools.command_policy_store import (
    get_effective_policy,
    get_runtime_override,
    reset_db,
    set_db,
)
from web.server.database import ChatDatabase


@pytest.fixture
def chat_db(tmp_path, monkeypatch):
    db_path = tmp_path / "web_chat.db"
    db = ChatDatabase(db_path)
    db.create_tables()
    reset_db()
    set_db(db)
    monkeypatch.setenv("AGENTFORGE_CHAT_DB", str(db_path))
    # Minimal config with two built-in profiles
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
ai:
  model: dummy
  profiles:
    dummy: { model: dummy, provider: ollama }
tools:
  command_permission_profiles:
    tight:
      description: lab tight
      shell:
        mode: allowlist
        allowed_commands: [ls, df]
        blocked_patterns: ['rm\\\\s+-rf']
      ssh:
        mode: confirm
    open:
      description: open confirm
      shell:
        mode: confirm
        blocked_patterns: []
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    reset_config()
    yield db
    reset_db()
    reset_config()


def test_list_includes_yaml_profiles(chat_db):
    ids = {p.id for p in list_profiles()}
    assert "tight" in ids
    assert "open" in ids
    tight = get_profile("tight")
    assert tight is not None
    assert tight.source == "yaml"
    assert tight.shell is not None
    assert tight.shell["mode"] == "allowlist"
    assert "ls" in tight.shell["allowed_commands"]


def test_apply_sets_runtime_override(chat_db):
    apply_profile("tight")
    ov = get_runtime_override("shell")
    assert ov is not None
    assert ov.mode == "allowlist"
    assert "ls" in ov.allowed_commands
    eff = get_effective_policy("shell")
    assert eff.mode == "allowlist"


def test_save_and_delete_user_profile(chat_db):
    save_user_profile(
        "my-lab",
        description="custom",
        shell={"mode": "denylist", "blocked_patterns": ["sudo"]},
    )
    p = get_profile("my-lab")
    assert p is not None
    assert p.source == "user"
    assert p.shell is not None
    assert p.shell["mode"] == "denylist"
    assert delete_user_profile("my-lab") is True
    assert get_profile("my-lab") is None


def test_cannot_delete_yaml_only_profile(chat_db):
    with pytest.raises(ValueError, match="built-in"):
        delete_user_profile("tight")


def test_save_from_current_overrides(chat_db):
    apply_profile("tight")
    save_user_profile("snapshot", description="from live", from_current_overrides=True)
    p = get_profile("snapshot")
    assert p is not None
    assert p.shell is not None
    assert p.shell["mode"] == "allowlist"
    assert "df" in p.shell["allowed_commands"]
