"""write_file unique flag: prefix on conflict (default) vs in-place overwrite."""

from pathlib import Path

from agentforge.tools.filesystem import (
    apply_write_file_unique,
    clear_dir_remap,
    wants_in_place_write,
    write_file,
)


def test_unique_true_versions_occupied_parent(tmp_path: Path):
    parent = tmp_path / "profiles" / "providers"
    parent.mkdir(parents=True)
    target = parent / "ollama.yaml"
    target.write_text("old\n", encoding="utf-8")
    (parent / "other.yaml").write_text("sibling\n", encoding="utf-8")
    clear_dir_remap()

    result = write_file(str(target), "new profile\n", unique=True)

    assert target.read_text(encoding="utf-8") == "old\n"
    versioned = tmp_path / "profiles" / "providers_1" / "ollama.yaml"
    assert versioned.is_file()
    assert versioned.read_text(encoding="utf-8") == "new profile\n"
    assert str(versioned) in result


def test_unique_false_overwrites_in_place(tmp_path: Path):
    parent = tmp_path / "profiles" / "providers"
    parent.mkdir(parents=True)
    target = parent / "ollama.yaml"
    target.write_text("old\n", encoding="utf-8")
    (parent / "other.yaml").write_text("sibling\n", encoding="utf-8")
    clear_dir_remap()

    result = write_file(str(target), "new profile\n", unique=False)

    assert target.read_text(encoding="utf-8") == "new profile\n"
    assert (parent / "other.yaml").read_text(encoding="utf-8") == "sibling\n"
    assert not (tmp_path / "profiles" / "providers_1").exists()
    assert str(target) in result


def test_unique_false_creates_beside_existing(tmp_path: Path):
    parent = tmp_path / "profiles" / "providers"
    parent.mkdir(parents=True)
    (parent / "ollama.yaml").write_text("old\n", encoding="utf-8")
    clear_dir_remap()

    new = parent / "foo.yaml"
    write_file(str(new), "foo\n", unique=False)

    assert new.read_text(encoding="utf-8") == "foo\n"
    assert not (tmp_path / "profiles" / "providers_1").exists()


def test_unique_coerces_string_false(tmp_path: Path):
    target = tmp_path / "a.yaml"
    target.write_text("old\n", encoding="utf-8")
    clear_dir_remap()
    write_file(str(target), "new\n", unique="false")  # type: ignore[arg-type]
    assert target.read_text(encoding="utf-8") == "new\n"


def test_wants_in_place_for_add_profile_prompt(tmp_path: Path):
    target = tmp_path / "profiles" / "providers" / "ollama.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("old\n", encoding="utf-8")
    query = (
        "Editor context:\n"
        f"- path: {target}\n"
        "- file: ollama.yaml\n"
        "\n"
        "Add the new model profile before `ollama-glm-5-3-flash` "
        "and call it `ollama-glm-5-3`"
    )
    assert wants_in_place_write(query, str(target)) is True
    args = apply_write_file_unique({"path": str(target), "content": "x"}, query)
    assert args["unique"] is False


def test_wants_in_place_skips_when_unique_explicit(tmp_path: Path):
    target = tmp_path / "ollama.yaml"
    target.write_text("old\n", encoding="utf-8")
    query = f"Update {target.name} please"
    args = apply_write_file_unique(
        {"path": str(target), "content": "x", "unique": True},
        query,
    )
    assert args["unique"] is True


def test_wants_in_place_false_for_report_without_edit_verb(tmp_path: Path):
    target = tmp_path / "report.md"
    target.write_text("old\n", encoding="utf-8")
    query = f"Write a summary to {target}"
    assert wants_in_place_write(query, str(target)) is False
    args = apply_write_file_unique({"path": str(target), "content": "x"}, query)
    assert "unique" not in args


def test_inferred_unique_false_overwrites(tmp_path: Path):
    parent = tmp_path / "profiles" / "providers"
    parent.mkdir(parents=True)
    target = parent / "ollama.yaml"
    target.write_text("old\n", encoding="utf-8")
    (parent / "other.yaml").write_text("sibling\n", encoding="utf-8")
    query = f"Add a profile to {target} before ollama-glm-5-3-flash"
    args = apply_write_file_unique({"path": str(target), "content": "new\n"}, query)
    clear_dir_remap()
    result = write_file(str(target), "new\n", unique=args["unique"])
    assert target.read_text(encoding="utf-8") == "new\n"
    assert not (tmp_path / "profiles" / "providers_1").exists()
    assert str(target) in result
