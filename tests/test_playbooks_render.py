"""Render: deterministic context building + Jinja2 template fill."""

from __future__ import annotations

import pytest

from agentforge.playbooks.models import Playbook, PlaybookCommand

pytest.importorskip("jinja2")

from agentforge.playbooks.render import build_context, render_playbook  # noqa: E402


def _playbook(template: str) -> Playbook:
    return Playbook(
        id="disk",
        description="Diagnose disk",
        commands=[
            PlaybookCommand(id="df", command="df -h", cwd="/tmp"),
            PlaybookCommand(id="docker_df", command="docker system df", cwd="/tmp"),
        ],
        template_text=template,
    )


def test_build_context_maps_output_by_id() -> None:
    pb = _playbook("x")
    # matched by literal command string, not id
    ctx = build_context(pb, {"df -h": "FS 90%", "docker system df": "12GB"})
    assert ctx["commands"]["df"]["output"] == "FS 90%"
    assert ctx["commands"]["df"]["ok"] is True
    assert ctx["commands"]["docker_df"]["output"] == "12GB"


def test_missing_output_is_empty_and_not_ok() -> None:
    pb = _playbook("x")
    ctx = build_context(pb, {"df -h": "FS 90%"})
    assert ctx["commands"]["docker_df"]["output"] == ""
    assert ctx["commands"]["docker_df"]["ok"] is False


def test_error_output_marks_not_ok() -> None:
    pb = _playbook("x")
    ctx = build_context(pb, {"df -h": "Error: boom", "docker system df": "ok"})
    assert ctx["commands"]["df"]["ok"] is False
    assert ctx["commands"]["docker_df"]["ok"] is True


def test_render_fills_placeholders() -> None:
    pb = _playbook("## Disk\n{{ commands.df.output }}\n{{ commands.docker_df.output }}")
    ctx = build_context(pb, {"df -h": "FS 90%", "docker system df": "12GB"})
    out = render_playbook(pb, ctx)
    assert "FS 90%" in out
    assert "12GB" in out


def test_render_conditional_section() -> None:
    tmpl = "{% if commands.docker_df.ok %}DOCKER:{{ commands.docker_df.output }}{% endif %}END"
    pb = _playbook(tmpl)

    ctx_ok = build_context(pb, {"df -h": "x", "docker system df": "12GB"})
    assert "DOCKER:12GB" in render_playbook(pb, ctx_ok)

    ctx_missing = build_context(pb, {"df -h": "x"})
    rendered = render_playbook(pb, ctx_missing)
    assert "DOCKER" not in rendered
    assert rendered.endswith("END")


def test_unknown_placeholder_renders_empty_not_error() -> None:
    pb = _playbook("A{{ commands.nonexistent.output }}B")
    ctx = build_context(pb, {"df -h": "x", "docker system df": "y"})
    assert render_playbook(pb, ctx) == "AB"
