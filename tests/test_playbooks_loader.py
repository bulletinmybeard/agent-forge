"""Loader: playbooks.yaml parsing, template resolution, validation."""

from __future__ import annotations

from pathlib import Path

from agentforge.playbooks.loader import DEFAULT_THRESHOLD, load_library


def _write(root: Path, registry: str, templates: dict[str, str]) -> Path:
    (root / "markdown" / "playbooks").mkdir(parents=True, exist_ok=True)
    for name, body in templates.items():
        (root / "markdown" / "playbooks" / name).write_text(body, encoding="utf-8")
    reg = root / "playbooks.yaml"
    reg.write_text(registry, encoding="utf-8")
    return reg


def test_missing_registry_returns_empty(tmp_path: Path) -> None:
    lib = load_library(tmp_path / "nope.yaml")
    assert len(lib) == 0
    assert lib.default_threshold == DEFAULT_THRESHOLD


def test_loads_valid_playbook(tmp_path: Path) -> None:
    reg = _write(
        tmp_path,
        """
        score_threshold: 0.6
        playbooks:
          disk-pressure:
            description: Diagnose low disk space
            trigger_examples:
              - why is my disk full
              - running out of space
            commands:
              - {id: df, command: "df -h", cwd: /tmp}
              - {id: dirs, command: "du -sh *"}
            template_file: markdown/playbooks/disk.jinja
            score_threshold: 0.7
        """,
        {"disk.jinja": "## Disk\n{{ commands.df.output }}"},
    )
    lib = load_library(reg)

    assert lib.default_threshold == 0.6
    assert len(lib) == 1
    pb = lib.get("disk-pressure")
    assert pb is not None
    assert pb.description == "Diagnose low disk space"
    assert [c.id for c in pb.commands] == ["df", "dirs"]
    assert pb.commands[0].cwd == "/tmp"
    assert pb.commands[1].cwd == "/tmp"  # default
    assert pb.template_text.startswith("## Disk")
    assert pb.score_threshold == 0.7
    assert lib.threshold_for(pb) == 0.7


def test_index_text_is_description_plus_examples(tmp_path: Path) -> None:
    reg = _write(
        tmp_path,
        """
        playbooks:
          p:
            description: Alpha
            trigger_examples: [beta, gamma]
            commands: [{id: a, command: "echo a"}]
            template_file: markdown/playbooks/p.jinja
        """,
        {"p.jinja": "x"},
    )
    pb = load_library(reg).get("p")
    assert pb is not None
    assert pb.index_text() == "Alpha\nbeta\ngamma"


def test_invalid_entries_are_skipped(tmp_path: Path) -> None:
    reg = _write(
        tmp_path,
        """
        playbooks:
          no-desc:
            commands: [{id: a, command: "echo a"}]
            template_file: markdown/playbooks/x.jinja
          no-commands:
            description: has no commands
            template_file: markdown/playbooks/x.jinja
          missing-template:
            description: template does not exist
            commands: [{id: a, command: "echo a"}]
            template_file: markdown/playbooks/gone.jinja
          dup-cmd-ids:
            description: duplicate ids collapse
            commands:
              - {id: a, command: "echo 1"}
              - {id: a, command: "echo 2"}
            template_file: markdown/playbooks/x.jinja
        """,
        {"x.jinja": "x"},
    )
    lib = load_library(reg)
    assert set(lib.playbooks) == {"dup-cmd-ids"}
    dup = lib.get("dup-cmd-ids")
    assert dup is not None
    assert [c.command for c in dup.commands] == ["echo 1"]


def test_synthesis_mode(tmp_path: Path) -> None:
    reg = _write(
        tmp_path,
        """
        playbooks:
          info:
            synthesis: informational
            description: d
            commands: [{id: a, command: "echo a"}]
            template_file: markdown/playbooks/x.jinja
          default-diag:
            description: d
            commands: [{id: a, command: "echo a"}]
            template_file: markdown/playbooks/x.jinja
          bad:
            synthesis: nonsense
            description: d
            commands: [{id: a, command: "echo a"}]
            template_file: markdown/playbooks/x.jinja
        """,
        {"x.jinja": "x"},
    )
    lib = load_library(reg)
    info = lib.get("info")
    default_diag = lib.get("default-diag")
    bad = lib.get("bad")
    assert info is not None and info.synthesis == "informational"
    assert default_diag is not None and default_diag.synthesis == "diagnostic"  # default
    assert bad is not None and bad.synthesis == "diagnostic"  # unknown → fallback


def test_default_threshold_when_unset(tmp_path: Path) -> None:
    reg = _write(
        tmp_path,
        """
        playbooks:
          p:
            description: d
            commands: [{id: a, command: "echo a"}]
            template_file: markdown/playbooks/p.jinja
        """,
        {"p.jinja": "x"},
    )
    lib = load_library(reg)
    pb = lib.get("p")
    assert pb is not None
    assert lib.default_threshold == DEFAULT_THRESHOLD
    assert lib.threshold_for(pb) == DEFAULT_THRESHOLD
