from agentforge.tools.sudo_cache import SudoCredentialCache


def test_get_returns_none_when_absent():
    c = SudoCredentialCache(ttl_seconds=300)
    assert c.get("localhost") is None


def test_set_then_get_returns_value():
    now = [100.0]
    c = SudoCredentialCache(ttl_seconds=300, clock=lambda: now[0])
    c.set("localhost", "pw")
    assert c.get("localhost") == "pw"


def test_expires_after_ttl():
    now = [100.0]
    c = SudoCredentialCache(ttl_seconds=300, clock=lambda: now[0])
    c.set("localhost", "pw")
    now[0] = 100.0 + 301
    assert c.get("localhost") is None


def test_get_slides_the_ttl():
    now = [100.0]
    c = SudoCredentialCache(ttl_seconds=300, clock=lambda: now[0])
    c.set("localhost", "pw")
    now[0] = 100.0 + 200
    assert c.get("localhost") == "pw"  # refreshes last_used
    now[0] = 100.0 + 200 + 299
    assert c.get("localhost") == "pw"  # still alive due to slide


def test_invalidate_drops_entry():
    c = SudoCredentialCache(ttl_seconds=300, clock=lambda: 100.0)
    c.set("localhost", "pw")
    c.invalidate("localhost")
    assert c.get("localhost") is None


def test_clear_drops_all():
    c = SudoCredentialCache(ttl_seconds=300, clock=lambda: 100.0)
    c.set("a", "1")
    c.set("b", "2")
    c.clear()
    assert c.get("a") is None and c.get("b") is None
