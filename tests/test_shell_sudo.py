import contextvars

import agentforge.tools.shell as sh


class FakeProvider:
    def __init__(self, secret):
        self.secret = secret
        self.invalidated = []

    def get(self, label):
        return self.secret

    def invalidate(self, label):
        self.invalidated.append(label)


def teardown_function():
    sh.set_sudo_secret_provider(None)


def test_request_returns_none_without_provider():
    sh.set_sudo_secret_provider(None)
    assert sh._request_sudo_password("localhost") is None


def test_request_uses_provider():
    sh.set_sudo_secret_provider(FakeProvider("hunter2"))
    assert sh._request_sudo_password("localhost") == "hunter2"


def test_get_sudo_password_is_gone():
    assert not hasattr(sh, "_get_sudo_password")


def test_request_returns_none_on_provider_error():
    # Fail-closed: a provider/transport error must not run a sudo command unconfirmed.
    class ErrorProvider:
        def get(self, label):
            raise RuntimeError("network down")

        def invalidate(self, label):
            pass

    sh.set_sudo_secret_provider(ErrorProvider())
    assert sh._request_sudo_password("localhost") is None


def test_invalidate_calls_provider():
    provider = FakeProvider("pw")
    sh.set_sudo_secret_provider(provider)
    sh._invalidate_sudo_password("localhost")
    assert provider.invalidated == ["localhost"]


def test_ctx_provider_overrides_global_then_resets():
    # The context-isolated provider (worker path) wins over the module global
    # (in-process path); resetting falls back to the global. This is what keeps
    # concurrent worker tool jobs from clobbering each other's provider.
    sh.set_sudo_secret_provider(FakeProvider("global-pw"))
    assert sh._request_sudo_password("localhost") == "global-pw"

    token = sh.set_sudo_secret_provider_ctx(FakeProvider("ctx-pw"))
    assert sh._request_sudo_password("localhost") == "ctx-pw"

    sh.reset_sudo_secret_provider_ctx(token)
    assert sh._request_sudo_password("localhost") == "global-pw"


def test_ctx_provider_isolated_per_context():
    sh.set_sudo_secret_provider(None)

    def _in_context():
        sh.set_sudo_secret_provider_ctx(FakeProvider("job-a"))
        return sh._request_sudo_password("localhost")

    # Each copied context gets its own value; the outer context is unaffected.
    assert contextvars.copy_context().run(_in_context) == "job-a"
    assert sh._request_sudo_password("localhost") is None
