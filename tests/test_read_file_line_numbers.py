"""read_file prefixes 1-based ``N │`` line numbers."""

from pathlib import Path

from agentforge.tools.filesystem import prefix_line_numbers, read_file


def test_prefix_line_numbers_basic():
    out = prefix_line_numbers("alpha\nbeta", start_line=1)
    assert out.splitlines()[0].endswith("│ alpha")
    assert "     1 │ alpha" in out
    assert "     2 │ beta" in out


def test_prefix_line_numbers_offset():
    out = prefix_line_numbers("flash:\n  abstract: true", start_line=165)
    assert "   165 │ flash:" in out
    assert "   166 │   abstract: true" in out


def test_prefix_skips_empty():
    assert prefix_line_numbers("") == ""


def test_read_file_numbers_lines(tmp_path: Path):
    p = tmp_path / "ollama.yaml"
    p.write_text("profiles:\n  ollama-glm-5-3-flash:\n    model: x\n", encoding="utf-8")
    out = read_file(str(p))
    assert "     1 │ profiles:" in out
    assert "     2 │   ollama-glm-5-3-flash:" in out
    assert "     3 │     model: x" in out


def test_read_file_offset_uses_real_line_numbers(tmp_path: Path):
    p = tmp_path / "n.py"
    p.write_text("\n".join(f"line-{i}" for i in range(1, 21)) + "\n", encoding="utf-8")
    out = read_file(str(p), offset=10, limit=3)
    assert "    10 │ line-10" in out
    assert "    12 │ line-12" in out
    assert "line-9" not in out
    assert "[Lines 10–" in out


def test_read_file_error_is_not_numbered():
    out = read_file("/no/such/file/anywhere.yaml")
    assert out.startswith("Error:")
    assert "│" not in out
