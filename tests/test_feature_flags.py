import pytest
from fastapi import HTTPException

from app.config import settings


def test_canvas_settings_default_enabled():
    assert settings.canvas.enabled is True


def test_botty_settings_default_enabled():
    assert settings.botty.enabled is True


def test_prompt_lab_settings_default_enabled():
    assert settings.prompt_lab.enabled is True


def test_prompt_lab_disabled_returns_503(monkeypatch):
    from web.server import api

    monkeypatch.setattr(api.af_settings.prompt_lab, "enabled", False)

    with pytest.raises(HTTPException) as exc_info:
        api._require_prompt_lab()

    assert exc_info.value.status_code == 503
