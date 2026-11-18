from pathlib import Path

from agentforge.config import AIProfile, ConfigManager


def test_from_dict_reads_label_and_alias():
    labeled = AIProfile.from_dict(
        "ollama-glm",
        {"model": "glm-5.3-flash:cloud", "provider": "ollama", "label": "  glm-5.3-flash  "},
    )
    assert labeled.label == "glm-5.3-flash"
    assert labeled.model == "glm-5.3-flash:cloud"
    assert labeled.display_label == "glm-5.3-flash"

    aliased = AIProfile.from_dict(
        "ollama-inkling",
        {"model": "inkling-2-small:cloud", "model_label": "inkling-2-small"},
    )
    assert aliased.label == "inkling-2-small"

    bare = AIProfile.from_dict("ollama-clef", {"model": "clef:cloud"})
    assert bare.label == ""
    assert bare.display_label == "clef:cloud"

    # label wins when both keys are set
    both = AIProfile.from_dict(
        "ollama-both",
        {"model": "x:cloud", "label": "from-label", "model_label": "from-alias"},
    )
    assert both.label == "from-label"


def test_label_inherits_unless_child_overrides(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text(
        """
ai:
  model: fallback:cloud
  host: http://localhost:11434
  profiles:
    ollama-glm:
      abstract: true
      provider: ollama
      model: glm-5.3-flash:cloud
      label: glm-5.3-flash
    agent:
      profile: ollama-glm
      temperature: 0.2
    named:
      profile: ollama-glm
      label: custom
    aliased:
      abstract: true
      provider: ollama
      model: inkling-2-small:cloud
      model_label: inkling-2-small
""",
        encoding="utf-8",
    )
    cfg = ConfigManager(path)
    parent = cfg.get_profile("ollama-glm")
    child = cfg.get_profile("agent")
    named = cfg.get_profile("named")
    aliased = cfg.get_profile("aliased")

    assert parent.label == "glm-5.3-flash"
    assert child.model == "glm-5.3-flash:cloud"
    assert child.label == "glm-5.3-flash"
    assert child.temperature == 0.2
    assert named.label == "custom"
    assert named.model == "glm-5.3-flash:cloud"
    assert aliased.label == "inkling-2-small"
    assert aliased.display_label == "inkling-2-small"
