"""Coding driver assign-merge + unified-diff extraction."""

from __future__ import annotations

from agentforge.coding.driver import Plan, PlanStep, _bind_assign, run_plan
from agentforge.coding.retry import _surviving_are_new_code
from agentforge.tools.coding_tools import _confine_to_cwd, _extract_unified_diff


def test_bind_assign_concatenates_hit_lists():
    first = [{"file": "a.py", "line": 1, "text": "class Foo"}]
    second = [{"file": "a.py", "line": 10, "text": "def __init__"}]
    merged = _bind_assign(first, second)
    assert [h["line"] for h in merged] == [1, 10]


def test_bind_assign_dedups_same_file_line():
    first = [{"file": "a.py", "line": 10, "text": "x"}]
    second = [{"file": "a.py", "line": 10, "text": "x again"}]
    merged = _bind_assign(first, second)
    assert len(merged) == 1


def test_run_plan_merges_consecutive_find_assigns():
    calls = {"n": 0}

    def fake_find(*, pattern: str, path: str, glob: str = "") -> list[dict]:
        calls["n"] += 1
        return [{"file": path, "line": calls["n"], "text": pattern}]

    plan = Plan(
        steps=[
            PlanStep(tool="code_find", args={"pattern": "A", "path": "f.py"}, assign="hits"),
            PlanStep(tool="code_find", args={"pattern": "B", "path": "f.py"}, assign="hits"),
        ]
    )
    ctx = run_plan(plan, tool_overrides={"code_find": fake_find})
    assert [h["text"] for h in ctx["hits"]] == ["A", "B"]


def test_extract_diff_fence():
    raw = "noise\n```diff\n@@ -1,1 +1,1 @@\n-a\n+b\n```\n"
    assert "@@ -1,1 +1,1 @@" in _extract_unified_diff(raw)


def test_extract_unfenced_hunk():
    raw = "sure, here you go:\n@@ -74,2 +74,2 @@\n-    def __init__(self, token: str, user_agent: str) -> None:\n+    def __init__(self, token: str, user_agent: str, timeout: float | None = None) -> None:\n"
    assert "timeout: float | None = None" in _extract_unified_diff(raw)


def test_extract_empty_fence_is_empty():
    assert _extract_unified_diff("```diff\n```") == ""


def test_transform_uses_ctx_hits_when_planner_inlines_patterns():
    """Planner passed the three search strings as hits instead of $hits."""

    def fake_find(*, pattern: str, path: str, glob: str = "") -> list[dict]:
        return [{"file": path, "line": 74, "text": pattern}]

    captured: dict = {}

    def fake_transform(*, hits: list, instruction: str, profile: str = "coding") -> list:
        captured["hits"] = hits
        return [{"file": hits[0]["file"], "unified_diff": "ok"}]

    plan = Plan(
        steps=[
            PlanStep(
                tool="code_find",
                args={"pattern": "def __init__", "path": "github_api.py"},
                assign="hits",
            ),
            PlanStep(
                tool="code_transform",
                args={
                    "hits": ["def __init__", "_DEFAULT_TIMEOUT", "httpx.Client("],
                    "instruction": "add timeout",
                },
                assign="proposed",
            ),
        ]
    )
    ctx = run_plan(
        plan,
        tool_overrides={"code_find": fake_find, "code_transform": fake_transform},
    )
    assert captured["hits"][0]["file"] == "github_api.py"
    assert captured["hits"][0]["text"] == "def __init__"
    assert ctx["proposed"][0]["unified_diff"] == "ok"


def test_confine_allows_sibling_git_repo(tmp_path, monkeypatch):
    worker = tmp_path / "agent-forge"
    worker.mkdir()
    (worker / ".git").mkdir()
    monkeypatch.chdir(worker)

    other = tmp_path / "github-traffic-vault"
    other.mkdir()
    (other / ".git").mkdir()
    target = other / "github_api.py"
    target.write_text("x\n", encoding="utf-8")

    p, err = _confine_to_cwd(str(target))
    assert err is None
    assert p == target.resolve()


def test_surviving_sites_that_are_insertions_are_new_code():
    applied = [
        {
            "file": "github_api.py",
            "combined_diff": (
                "--- a/github_api.py\n+++ b/github_api.py\n"
                "@@ -70,1 +70,2 @@\n"
                "+def can_reach_github_api(token: str | None = None, timeout: float = 5.0) -> bool:\n"
                " class GitHubClient:\n"
            ),
        }
    ]
    surviving = [
        {
            "file": "github_api.py",
            "line": 71,
            "text": "def can_reach_github_api(token: str | None = None, timeout: float = 5.0) -> bool:",
        }
    ]
    assert _surviving_are_new_code(surviving, applied) is True


def test_confine_rejects_unrooted_file(tmp_path, monkeypatch):
    worker = tmp_path / "agent-forge"
    worker.mkdir()
    (worker / ".git").mkdir()
    monkeypatch.chdir(worker)

    stray = tmp_path / "no-repo" / "secrets.txt"
    stray.parent.mkdir()
    stray.write_text("x\n", encoding="utf-8")

    p, err = _confine_to_cwd(str(stray))
    assert p is None
    assert err is not None
    assert "outside allowed roots" in err
