"""write_file preview confirm: pending card, home remap, timeout vs deny."""

from __future__ import annotations

from types import SimpleNamespace

from agentforge.agent import AgentLoop
from web.server.confirm import ConfirmDecision


class _Reg:
    def __init__(self, decision):
        self.decision = decision
        self.diffs: list[dict] = []
        self.prompt: str | None = None
        self.dispatched: list[tuple] = []

    def supports_preview_confirm(self) -> bool:
        return True

    def emit_file_diff(self, payload: dict) -> None:
        self.diffs.append(payload)

    def run_confirm(self, prompt: str):
        self.prompt = prompt
        return self.decision


def _loop(reg: _Reg) -> AgentLoop:
    agent = AgentLoop(client=SimpleNamespace(), registry=reg)  # type: ignore[arg-type]
    agent._dispatch_tool = lambda name, args, **kw: f"Wrote 12 chars to {args.get('path')}"  # type: ignore[method-assign]
    return agent


def test_remap_dispatcher_home_rewrites_container_root():
    remapped = AgentLoop._remap_dispatcher_home(
        {"path": "/root/Downloads/kogot-7-loadout.md", "content": "x"},
        home="/root",
    )
    assert remapped["path"] == "~/Downloads/kogot-7-loadout.md"
    assert remapped["content"] == "x"


def test_remap_dispatcher_home_leaves_tilde_and_foreign_home():
    assert AgentLoop._remap_dispatcher_home({"path": "~/Downloads/a.md"}, home="/root")["path"] == "~/Downloads/a.md"
    assert (
        AgentLoop._remap_dispatcher_home({"path": "/Users/alice/Downloads/a.md"}, home="/root")["path"]
        == "/Users/alice/Downloads/a.md"
    )


def test_preview_write_emits_proposed_with_tilde_path(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTFORGE_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    monkeypatch.setattr("os.path.expanduser", lambda p: "/root" if p == "~" else p)
    reg = _Reg(ConfirmDecision(confirmed=True))
    agent = _loop(reg)

    result = agent._preview_confirm_write(
        "write_file",
        {"path": "/root/Downloads/kogot-7-loadout.md", "content": "# loadout\n"},
    )

    assert result.startswith("Wrote 12 chars")
    assert [d["action"] for d in reg.diffs] == ["proposed", "written"]
    preview = reg.diffs[0]
    assert preview["path"] == "~/Downloads/kogot-7-loadout.md"
    assert preview["post_hash"] == ""
    receipt = reg.diffs[1]
    assert receipt["path"] == "~/Downloads/kogot-7-loadout.md"
    assert len(receipt["post_hash"]) == 64
    assert "~/Downloads/kogot-7-loadout.md" in (reg.prompt or "")
    assert "kogot-7-loadout.md" in (reg.prompt or "")


def test_post_hash_from_apply_line():
    assert AgentLoop._post_hash_from_apply("pre_hash=aa\npost_hash=bb\npath=/x") == "bb"
    assert AgentLoop._post_hash_from_apply("nope") == ""


def test_preview_write_timeout_does_not_write(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFORGE_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    reg = _Reg(ConfirmDecision(confirmed=False, timed_out=True))
    agent = _loop(reg)
    result = agent._preview_confirm_write(
        "write_file",
        {"path": "~/Downloads/kogot-7-loadout.md", "content": "# loadout\n"},
    )
    assert "timed out" in result.lower()
    assert "not saved" in result.lower()
    assert "cancelled by user" not in result.lower()
    assert [d["action"] for d in reg.diffs] == ["proposed"]


def test_preview_write_deny_is_cancelled_by_user(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFORGE_SNAPSHOT_DIR", str(tmp_path / "snaps"))
    reg = _Reg(ConfirmDecision(confirmed=False, timed_out=False))
    agent = _loop(reg)
    result = agent._preview_confirm_write(
        "write_file",
        {"path": "~/Downloads/kogot-7-loadout.md", "content": "# loadout\n"},
    )
    assert result == "Operation cancelled by user."
    assert [d["action"] for d in reg.diffs] == ["proposed"]
