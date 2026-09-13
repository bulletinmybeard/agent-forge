"""@review style flags, gather preamble, and classic report assembly."""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from agentforge.review.gather import gather_review_context
from agentforge.review.loop import extract_agent_loop_output
from agentforge.review.output import (
    parse_review_output_path,
    resolve_review_output_file,
    usable_branch_name,
    write_review_output,
)
from agentforge.review.report import build_classic_report, specialist_extra_rules
from agentforge.review.style import parse_review_query, resolve_review_max_workers, style_reason


def test_default_style_is_single():
    q = parse_review_query("@review Review changes in /tmp/proj")
    assert q.style == "single"
    assert q.target == "/tmp/proj"
    assert q.instruction == "Review changes in"


def test_flags_work_after_prefix_strip():
    q = parse_review_query("--deep /opt/api check error handling")
    assert q.style == "deep"
    assert q.target == "/opt/api"
    assert q.instruction == "check error handling"


def test_boolean_flags_set_style_and_are_stripped():
    q = parse_review_query("@review --deep /opt/api check error handling")
    assert q.style == "deep"
    assert q.target == "/opt/api"
    assert q.instruction == "check error handling"
    assert "--deep" not in q.instruction


def test_style_equals_flag():
    q = parse_review_query("@review --style=classic look at unpushed commits")
    assert q.style == "classic"
    assert q.target == ""
    assert q.instruction == "look at unpushed commits"


def test_style_space_separated_flag():
    q = parse_review_query("@review --style single /home/u/repo")
    assert q.style == "single"
    assert q.target == "/home/u/repo"


def test_last_style_flag_wins():
    q = parse_review_query("@review --classic --deep --style=single /tmp/x")
    assert q.style == "single"
    assert q.target == "/tmp/x"


def test_unknown_style_value_keeps_default():
    q = parse_review_query("@review --style=turbo /tmp/x", default_style="classic")
    assert q.style == "classic"
    assert "--style=turbo" not in q.instruction


def test_invalid_default_falls_back_to_single():
    q = parse_review_query("@review /tmp/x", default_style="nope")
    assert q.style == "single"


def test_tilde_path_expands():
    q = parse_review_query("@review --classic ~/src/app nits")
    assert q.target.endswith("/src/app")
    assert q.style == "classic"
    assert q.instruction == "nits"


def test_review_target_prefers_repo_over_downloads_save_path():
    q = parse_review_query(
        "second opinion on branch `feat-healthcheck` (/opt/api) versus master and store the response in ~/Downloads"
    )
    assert q.target == "/opt/api"
    assert q.branch == "feat-healthcheck"


def test_parenthesized_repo_path_strips_trailing_paren():
    q = parse_review_query("@review branch `feat-healthcheck` (/opt/api) versus master")
    assert q.target == "/opt/api"
    assert q.branch == "feat-healthcheck"


def test_backticked_path_strips_tick():
    q = parse_review_query("@review `/tmp/proj`")
    assert q.target == "/tmp/proj"


def test_empty_instruction_gets_default_copy():
    q = parse_review_query("@review --single /tmp/p")
    assert q.instruction == "Review all current and unpushed changes"


def test_specialist_extra_rules_only_for_deep():
    assert specialist_extra_rules("classic", max_findings=5) == ""
    assert specialist_extra_rules("single", max_findings=5) == ""
    deep = specialist_extra_rules("deep", max_findings=5)
    assert "at most 5" in deep.lower()
    assert "zero findings" in deep.lower()


def test_classic_report_concatenates_in_specialist_order():
    sub_agents = [
        ("error_handling", "Error Handling", "error_handling.md", "Silent failures"),
        ("type_design", "Type Design", "type_design.md", "Type safety"),
    ]
    report = build_classic_report(
        target="/repo",
        elapsed_s=12.3,
        sub_agents=sub_agents,
        sub_results={
            "error_handling": {"text": "finding A", "tool_calls": [{}, {}]},
            "type_design": {"text": "finding B", "tool_calls": [{}]},
        },
        errors={},
    )
    assert report.index("## Error Handling") < report.index("## Type Design")
    assert "finding A" in report
    assert "finding B" in report
    assert "**Sub-agents**: 2/2" in report
    assert "**Tool calls**: 3" in report


def test_classic_report_includes_failed_specialist():
    sub_agents = [
        ("error_handling", "Error Handling", "error_handling.md", "Silent failures"),
    ]
    report = build_classic_report(
        target="",
        elapsed_s=1.0,
        sub_agents=sub_agents,
        sub_results={},
        errors={"error_handling": "timeout"},
    )
    assert "Sub-agent failed: timeout" in report


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "app.py").write_text("x = 1\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "init")
    return repo


def test_gather_includes_branch_diff_and_file_contents(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "app.py").write_text("x = 2\n")
    gathered = gather_review_context(str(repo), max_bytes=50_000)
    assert "app.py" in gathered.changed_files
    assert "x = 2" in gathered.file_contents["app.py"]
    assert "x = 2" in gathered.diff or "+x = 2" in gathered.diff
    assert gathered.branch
    assert "app.py" in gathered.preamble
    assert "do NOT re-run" in gathered.preamble


def test_gather_skips_binary_files(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02\x03")
    gathered = gather_review_context(str(repo), max_bytes=50_000)
    assert "blob.bin" in gathered.changed_files
    assert "blob.bin" not in gathered.file_contents
    assert any(path == "blob.bin" for path, _reason in gathered.skipped_files)


def test_style_reason_covers_all_styles():
    assert "large-context" in style_reason("single")
    assert "merge" in style_reason("deep")
    assert "sub-agents" in style_reason("classic")
    assert style_reason("nope") == style_reason("single")


def test_resolve_review_max_workers_is_positive():
    assert resolve_review_max_workers(4) >= 1
    assert resolve_review_max_workers(0) >= 1


def test_review_prompts_exist():
    import agentforge

    prompt_dir = Path(agentforge.__file__).resolve().parent / "prompts" / "review"
    whole = (prompt_dir / "whole_review.md").read_text()
    agg = (prompt_dir / "aggregator.md").read_text()
    assert "Verdict" in whole
    assert "Do not invent issues" in whole
    assert "{instruction}" in agg
    assert "{findings}" in agg
    assert "{max_findings}" in agg


def test_review_settings_defaults():
    from app.config import settings

    assert settings.review.style == "single"
    assert settings.review.max_findings == 5
    assert settings.review.reviewer_profile
    assert settings.review.specialist_profile


def test_parse_output_path_from_store_in_downloads():
    q = "second opinion on /opt/api versus master and store the response/review results in ~/Downloads"
    assert parse_review_output_path(q, target="/opt/api") == "~/Downloads"


def test_parse_output_path_save_to_file():
    assert parse_review_output_path("save the review to ~/Downloads/review-feat.md") == "~/Downloads/review-feat.md"


def test_parse_output_path_ignores_review_target():
    assert parse_review_output_path("Review changes in /opt/api") is None
    assert parse_review_output_path("@review --deep /opt/api") is None


def test_parse_output_path_ignores_ticket_attachment():
    q = "Here is the ticket: '/tmp/tickets/PROJ-1.pdf'"
    assert parse_review_output_path(q) is None


def test_resolve_output_file_directory_gets_dated_name(tmp_path: Path):
    dest = resolve_review_output_file(
        str(tmp_path),
        branch="feat-healthcheck",
        when=datetime(2026, 9, 10, 14, 32, 59),
    )
    assert dest == tmp_path / "review-feat-healthcheck-2026-09-10-14-32.md"


def test_resolve_output_file_keeps_explicit_markdown_path(tmp_path: Path):
    wanted = tmp_path / "notes.md"
    assert resolve_review_output_file(str(wanted), branch="x") == wanted


def test_usable_branch_rejects_git_fatal():
    fatal = "fatal: not a git repository (or any of the parent directories): .git"
    assert usable_branch_name(fatal, "feat-healthcheck") == "feat-healthcheck"
    assert usable_branch_name(fatal, "") == ""
    assert usable_branch_name("main", "") == "main"
    assert usable_branch_name("(empty)", "feature/foo") == "feature/foo"


def test_resolve_keeps_tilde_under_container_home(monkeypatch):
    monkeypatch.setenv("HOME", "/root")
    dest = resolve_review_output_file(
        "~/Downloads",
        branch="feat-healthcheck",
        when=datetime(2026, 9, 10, 14, 32, 0),
    )
    assert str(dest) == "~/Downloads/review-feat-healthcheck-2026-09-10-14-32.md"


def test_write_refuses_container_root_in_process(monkeypatch):
    monkeypatch.setenv("HOME", "/root")
    monkeypatch.setattr("agentforge.tools.routing.dispatch_mode", lambda: "in_process")
    wrote: list[str] = []
    monkeypatch.setattr(
        "agentforge.tools.filesystem.write_file",
        lambda path, content, unique=None: wrote.append(path) or f"Wrote to {path}",
    )
    note = write_review_output(Path("~/Downloads/review.md"), "# Review\n")
    assert wrote == []
    assert "Could not write" in note
    assert "~/Downloads/review.md" in note


def test_write_review_output_creates_file(tmp_path: Path):
    dest = tmp_path / "review.md"
    note = write_review_output(dest, "# Review\n\nReady\n")
    assert dest.is_file()
    assert dest.read_text() == "# Review\n\nReady\n"
    assert str(dest) in note


def test_extract_agent_loop_output_reads_ctx_metadata_not_missing_iterations():
    it = SimpleNamespace(
        tool_calls=[{"name": "read_file", "arguments": {"path": "a.py"}}],
        tool_results=[{"name": "read_file", "result": "x = 1\n"}],
    )
    ctx = SimpleNamespace(
        result="# Review\nReady",
        metadata={"agent_iterations": [it], "token_usage": {"prompt_tokens": 3, "completion_tokens": 7}},
    )
    text, calls, tokens = extract_agent_loop_output(ctx)
    assert text == "# Review\nReady"
    assert calls == [{"name": "read_file", "args": {"path": "a.py"}, "result": "x = 1\n"}]
    assert tokens == {"prompt_tokens": 3, "completion_tokens": 7}


def test_extract_agent_loop_output_empty_without_metadata():
    ctx = SimpleNamespace(result="ok", metadata={})
    text, calls, tokens = extract_agent_loop_output(ctx)
    assert text == "ok"
    assert calls == []
    assert tokens == {}


def test_worker_socket_broadcasts_session_title_and_review_progress():
    from web.server.queue.jobs_common import HttpCallbackSocket

    assert "session.title" in HttpCallbackSocket._BROADCAST_TYPES
    assert "review.progress" in HttpCallbackSocket._BROADCAST_TYPES
    assert "file.diff" in HttpCallbackSocket._BROADCAST_TYPES


def test_gather_respects_byte_cap(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "big.py").write_text("n = 1\n" * 200)
    gathered = gather_review_context(str(repo), max_bytes=40)
    assert "big.py" in gathered.changed_files
    assert "big.py" not in gathered.file_contents
    assert any(path == "big.py" for path, _reason in gathered.skipped_files)
