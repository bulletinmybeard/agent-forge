"""Service facade: match threshold gating + render orchestration (no I/O)."""

from __future__ import annotations

import pytest

from agentforge.playbooks import service
from agentforge.playbooks.models import Playbook, PlaybookCommand, PlaybookLibrary


class _FakeIndex:
    def __init__(self, result):
        self._result = result

    def search_one(self, query: str):
        return self._result


def _library() -> PlaybookLibrary:
    pb = Playbook(
        id="disk",
        description="Diagnose disk",
        commands=[PlaybookCommand(id="df", command="df -h")],
        template_text="## Disk\n{{ commands.df.output }}",
        score_threshold=0.6,
    )
    return PlaybookLibrary(playbooks={"disk": pb}, default_threshold=0.55)


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(service, "_library", _library())
    monkeypatch.setattr(service, "_enabled", lambda: True)

    def _install(result):
        monkeypatch.setattr(service, "_index", _FakeIndex(result))

    return _install


def test_match_above_threshold(wired):
    wired(("disk", 0.72))
    matched = service.match("why is my disk full")
    assert matched is not None
    pb, score = matched
    assert pb.id == "disk"
    assert score == 0.72


def test_match_below_threshold_returns_none(wired):
    wired(("disk", 0.50))  # below the playbook's 0.6 override
    assert service.match("something") is None


def test_match_no_hit_returns_none(wired):
    wired(None)
    assert service.match("something") is None


def test_match_disabled_returns_none(monkeypatch, wired):
    wired(("disk", 0.99))
    monkeypatch.setattr(service, "_enabled", lambda: False)
    assert service.match("something") is None


def test_match_retrieval_error_returns_none(monkeypatch):
    monkeypatch.setattr(service, "_library", _library())
    monkeypatch.setattr(service, "_enabled", lambda: True)

    class _Boom:
        def search_one(self, query):
            raise RuntimeError("qdrant down")

    monkeypatch.setattr(service, "_index", _Boom())
    assert service.match("something") is None


def test_render_from_finding():
    pytest.importorskip("jinja2")

    class _Finding:
        raw_outputs = [{"command": "df -h", "output": "FS 90%"}]

    pb = Playbook(
        id="disk",
        description="Diagnose disk",
        commands=[PlaybookCommand(id="df", command="df -h")],
        template_text="## Disk\n{{ commands.df.output }}",
    )
    out = service.render(pb, _Finding())
    assert "FS 90%" in out
    assert "## Disk" in out
