from app.config import settings


def test_canvas_settings_default_enabled():
    assert settings.canvas.enabled is True


def test_botty_settings_default_enabled():
    assert settings.botty.enabled is True
