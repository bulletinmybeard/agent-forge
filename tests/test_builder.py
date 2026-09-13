"""@plan / @build intent, paths, and markdown store."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from agentforge.builder.intent import (
    is_approve_plan,
    is_cancel_plan,
    is_start_over_plan,
    parse_builder_query,
)
from agentforge.builder.store import (
    extract_section,
    merge_all_tasks,
    merge_overlapping_tasks,
    parse_frontmatter,
    parse_tasks,
    stamp_status,
    tasks_share_files,
    write_plan_file,
)
from web.server.builder import plan_path_from_message_list
from web.server.mode_routing import strip_mode_prefix


def test_parse_builder_query_extracts_repo_and_branch():
    q = parse_builder_query("@plan add a healthcheck in /opt/api on branch `feat-healthcheck`")
    assert q.phase == "plan"
    assert q.target == "/opt/api"
    assert q.branch == "feat-healthcheck"
    assert "add a healthcheck" in q.instruction


def test_builder_runner_module_imports():
    from web.server.builder import run_builder

    assert callable(run_builder)


def test_confirm_request_plan_kind():
    from web.server.protocol import confirm_request

    msg = confirm_request("cr_1", "Approve this plan?", kind="plan")
    assert msg["kind"] == "plan"
    assert "auto_accepted" not in msg


def test_mode_prefixes_plan_and_build():
    assert strip_mode_prefix("@plan add a healthcheck") == ("add a healthcheck", "plan")
    assert strip_mode_prefix("@build")[1] == "build"


def test_parse_build_prefix():
    q = parse_builder_query("@build the approved plan")
    assert q.phase == "build"
    assert q.instruction == "the approved plan"


def test_plan_path_from_message_list():
    msgs = [
        {"type": "result", "metadata": {"plan_path": "/tmp/a.md"}},
        {"type": "result", "content": "ok"},
    ]
    assert plan_path_from_message_list(msgs) == "/tmp/a.md"


def test_plan_path_from_orm_metadata_json():
    class _Orm:
        metadata = object()  # SQLAlchemy MetaData — not a dict
        metadata_json = '{"plan_path": "/tmp/from-json.md"}'
        content = None

    assert plan_path_from_message_list([_Orm()]) == "/tmp/from-json.md"


def test_plan_path_from_recap_text():
    body = "# Build recap\n\nPlan: `/tmp/plans/master-2026-09-13-13-48.md`\nTarget: `/repo`\n"
    assert plan_path_from_message_list([{"content": body}]) == "/tmp/plans/master-2026-09-13-13-48.md"


def test_approve_cancel_start_over():
    from agentforge.builder.intent import is_redo_build, is_undo_build

    assert is_approve_plan("approve the plan")
    assert is_approve_plan("Approve this plan and start building")
    assert is_approve_plan("I approve the plan")
    assert not is_approve_plan("I do not approve of this approach")
    assert is_cancel_plan("cancel the plan")
    assert is_start_over_plan("start over")
    assert not is_start_over_plan("start the server")
    assert is_undo_build("undo the build")
    assert is_undo_build("@build undo")
    assert is_undo_build("revert the changes")
    assert not is_undo_build("I do not want to undo the build today")
    assert is_redo_build("apply the changes again")
    assert is_redo_build("put them back")
    assert is_redo_build("@build redo")
    assert not is_redo_build("approve the plan")


def test_parse_frontmatter_and_tasks():
    md = """---
status: draft
target: /repo
branch: feat
created: 2026-09-11-12-14
---

# Plan

## Tasks

### T1 — Add alias field
- Files: `models.py`
- Acceptance: model accepts `location`

### T2 — Tests
- Files: `test_models.py`, `models.py`
- Acceptance: pytest passes
"""
    meta, body = parse_frontmatter(md)
    assert meta["status"] == "draft"
    assert meta["target"] == "/repo"
    tasks = parse_tasks(body)
    assert [t.id for t in tasks] == ["T1", "T2"]
    assert tasks[0].files == ["models.py"]
    assert tasks[1].files == ["test_models.py", "models.py"]
    assert tasks_share_files(tasks) is True
    merged = merge_overlapping_tasks(tasks)
    assert len(merged) == 1
    assert merged[0].id == "T1+T2"


def test_extract_findings_section():
    body = "# Plan\n\n## Findings\n- a.py:1 — foo\n- b.py:2 — bar\n\n## Tasks\n### T1 — x\n"
    assert "a.py:1" in extract_section(body, "Findings")
    assert "Tasks" not in extract_section(body, "Findings")


def test_merge_all_tasks_joins_disjoint_readmes():
    from agentforge.builder.store import PlanTask

    a = PlanTask(id="T1", title="root docs", files=["README.md"], body="root")
    b = PlanTask(id="T2", title="plugin docs", files=["plugins/x/README.md"], body="plugin")
    merged = merge_all_tasks([a, b])
    assert len(merged) == 1
    assert merged[0].id == "T1+T2"
    assert "__plan__" not in merged[0].files
    assert "README.md" in merged[0].files


def test_verify_task_is_title_only():
    from agentforge.builder.store import PlanTask
    from web.server.builder import _is_verify_task

    write = PlanTask(
        id="T1+T2",
        title="Document README; Add pointer",
        files=["README.md"],
        body="Write the section. Verify last; do not write during verify.",
    )
    verify = PlanTask(id="T3", title="Verify the doc", files=["README.md"], body="")
    assert _is_verify_task(write) is False
    assert _is_verify_task(verify) is True


def test_merge_leaves_disjoint_tasks_apart():
    from agentforge.builder.store import PlanTask

    a = PlanTask(id="T1", title="docs", files=["README.md"], body="write docs")
    b = PlanTask(id="T2", title="code", files=["app.py"], body="edit code")
    merged = merge_overlapping_tasks([a, b])
    assert [t.id for t in merged] == ["T1", "T2"]


def test_stamp_status_roundtrip():
    md = "---\nstatus: draft\ntarget: /r\n---\n\n# Plan\n"
    out = stamp_status(md, "approved")
    meta, _ = parse_frontmatter(out)
    assert meta["status"] == "approved"


def test_write_plan_file_creates_markdown(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = write_plan_file(
        "# Plan\n\nHello\n",
        slug="feat-healthcheck",
        when=datetime(2026, 9, 11, 12, 14),
        unique=False,
    )
    assert path.name == "feat-healthcheck-2026-09-11-12-14.md"
    assert path.is_file()
    assert "Hello" in path.read_text()
    assert path.parent.name == "plans"


def test_second_plan_stays_in_plans_not_plans_1(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    a = write_plan_file("# A\n", slug="x", when=datetime(2026, 9, 12, 23, 40), unique=True)
    b = write_plan_file("# B\n", slug="x", when=datetime(2026, 9, 12, 23, 40), unique=True)
    assert a.parent == b.parent
    assert a.parent.name == "plans"
    assert a.name != b.name
    assert b.name.endswith("_1.md") or "_1" in b.stem


def test_apply_bundle_undo_and_redo(tmp_path: Path, monkeypatch):
    import hashlib

    from agentforge.builder.apply import redo_bundle, save_apply_bundle, undo_bundle
    from agentforge.tools._file_snapshots import save_snapshot

    monkeypatch.setenv("AGENTFORGE_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "hello.txt"
    before = "old\n"
    after = "new\n"
    target.write_text(before)
    pre_hash = hashlib.sha256(before.encode()).hexdigest()
    save_snapshot(pre_hash=pre_hash, path=str(target), content=before, tool="write_file")
    target.write_text(after)
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n")
    bundle = save_apply_bundle(
        plan,
        {str(target): {"path": str(target), "pre_hash": pre_hash, "post_hash": "", "diff_text": ""}},
    )
    assert bundle is not None
    assert bundle.is_file()

    report = undo_bundle(plan)
    assert target.read_text() == before
    assert "Reverted" in report

    report = redo_bundle(plan)
    assert target.read_text() == after
    assert "Re-applied" in report


def test_apply_bundle_undo_deletes_created_file(tmp_path: Path, monkeypatch):
    import hashlib

    from agentforge.builder.apply import save_apply_bundle, undo_bundle
    from agentforge.tools._file_snapshots import save_snapshot

    monkeypatch.setenv("AGENTFORGE_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    created = tmp_path / "repo" / "new.txt"
    created.parent.mkdir()
    created.write_text("fresh\n")
    pre_hash = hashlib.sha256(b"").hexdigest()
    save_snapshot(pre_hash=pre_hash, path=str(created), content="", tool="write_file")
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n")
    save_apply_bundle(
        plan,
        {str(created): {"path": str(created), "pre_hash": pre_hash, "post_hash": "", "diff_text": ""}},
    )
    undo_bundle(plan)
    assert not created.exists()
