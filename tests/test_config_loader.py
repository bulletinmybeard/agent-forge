from pathlib import Path

from agentforge.config import _config_with_example_fallback, get_config, load_merged_yaml, reset_config


def test_load_merged_yaml_includes_ai_profiles():
    root = Path(__file__).resolve().parents[1]
    raw = load_merged_yaml(root / "config.yaml")
    assert "ai" in raw
    assert isinstance(raw["ai"].get("profiles"), dict)
    # CI / fresh clones only have *.example.yaml (real configs are gitignored).
    assert raw["ai"].get("model")


def test_app_config_uses_same_merge_as_framework():
    root = Path(__file__).resolve().parents[1]
    from app.config import _yaml

    framework_raw = load_merged_yaml(root / "config.yaml")
    assert _yaml.get("ai", {}).get("profiles") == framework_raw.get("ai", {}).get("profiles")


def test_custom_agents_example_fallback(tmp_path):
    root = Path(__file__).resolve().parents[1]
    example_src = root / "custom_agents.example.yaml"
    assert example_src.exists()
    (tmp_path / "custom_agents.example.yaml").write_text(example_src.read_text())
    resolved = _config_with_example_fallback(tmp_path / "custom_agents.yaml")
    assert resolved == tmp_path / "custom_agents.example.yaml"


def test_config_manager_raw_matches_load_merged_yaml():
    root = Path(__file__).resolve().parents[1]
    reset_config()
    cfg = get_config(root / "config.yaml")
    assert cfg.raw.get("ai", {}).get("profiles") == load_merged_yaml(root / "config.yaml").get("ai", {}).get("profiles")
    reset_config()
