from pathlib import Path

from agentforge.config import get_config, load_merged_yaml, reset_config


def test_load_merged_yaml_includes_ai_profiles():
    root = Path(__file__).resolve().parents[1]
    raw = load_merged_yaml(root / "config.yaml")
    assert "ai" in raw
    assert isinstance(raw["ai"].get("profiles"), dict)


def test_app_config_uses_same_merge_as_framework():
    root = Path(__file__).resolve().parents[1]
    from app.config import _yaml

    framework_raw = load_merged_yaml(root / "config.yaml")
    assert _yaml.get("ai", {}).get("profiles") == framework_raw.get("ai", {}).get("profiles")


def test_config_manager_raw_matches_load_merged_yaml():
    root = Path(__file__).resolve().parents[1]
    reset_config()
    cfg = get_config(root / "config.yaml")
    assert cfg.raw.get("ai", {}).get("profiles") == load_merged_yaml(root / "config.yaml").get("ai", {}).get("profiles")
    reset_config()
