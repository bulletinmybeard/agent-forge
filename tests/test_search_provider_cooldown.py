"""Web search provider chain: cooldown, enabled flag, per-provider settings."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import redis

import agentforge.tools.search_providers as sp
import agentforge.tools.web_search as ws
from agentforge.tools.search_providers import ollama, settings, tavily
from agentforge.tools.search_providers.base import (
    FetchResult,
    NoProviderAvailable,
    ProviderError,
    RateLimitError,
    SearchResult,
    parse_retry_after,
)
from agentforge.tools.search_providers.cooldown import ProviderCooldown, cooldown_ttl, cooldown_until


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, tuple[str, int | None]] = {}

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = (value, ex)

    def ttl(self, key: str) -> int:
        if key not in self.store:
            return -2
        return self.store[key][1] or -1


class DeadRedis:
    def set(self, *args, **kwargs):
        raise redis.ConnectionError("down")

    def ttl(self, *args, **kwargs):
        raise redis.ConnectionError("down")


class StubProvider:
    def __init__(self, name: str, exc: BaseException | None = None, available: bool = True) -> None:
        self.name = name
        self.exc = exc
        self.available = available
        self.calls: list[int] = []

    def is_available(self) -> bool:
        return self.available

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        self.calls.append(max_results)
        if self.exc is not None:
            raise self.exc
        return [SearchResult(title=self.name, url=f"https://{self.name}.test", content="")]

    def fetch(self, url: str) -> FetchResult:
        self.calls.append(0)
        if self.exc is not None:
            raise self.exc
        return FetchResult(title=self.name, url=url, content="x" * 50)


@pytest.fixture
def fake_redis(monkeypatch) -> FakeRedis:
    client = FakeRedis()
    monkeypatch.setattr(sp, "get_provider_cooldown", lambda: ProviderCooldown(client=client))
    return client


@pytest.fixture
def ws_cfg(monkeypatch) -> dict:
    cfg: dict = {}
    monkeypatch.setattr(settings, "_web_search_cfg", lambda: cfg)
    monkeypatch.setattr(settings, "_warned", set())
    return cfg


def _walk(chain):
    return sp._walk(chain, "search", lambda p: p.search("q", 5))


# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------


def test_ttl_plan_limit_runs_until_first_of_next_month():
    now = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)
    ttl = cooldown_ttl(RateLimitError("x", status_code=432), now=now)
    assert ttl == int((datetime(2026, 10, 1, tzinfo=UTC) - now).total_seconds())
    assert cooldown_until(ttl, now=now) == "2026-10-01 00:00 UTC"


def test_ttl_plan_limit_december_rolls_into_january():
    now = datetime(2026, 12, 31, 23, 0, 0, tzinfo=UTC)
    assert cooldown_ttl(RateLimitError("x", status_code=432), now=now) == 3600


def test_ttl_429_uses_retry_after():
    assert cooldown_ttl(RateLimitError("x", status_code=429, retry_after=17)) == 17


def test_ttl_429_without_retry_after_defaults_to_60s():
    assert cooldown_ttl(RateLimitError("x", status_code=429)) == 60


def test_ttl_unknown_status_defaults_to_60s():
    assert cooldown_ttl(RateLimitError("x", status_code=433)) == 60
    assert cooldown_ttl(RateLimitError("x")) == 60


@pytest.mark.parametrize(
    ("header", "expected"),
    [("30", 30), (" 5 ", 5), (None, None), ("", None), ("Wed, 21 Oct 2026 07:28:00 GMT", None), ("-3", None)],
)
def test_parse_retry_after(header, expected):
    assert parse_retry_after(header) == expected


# ---------------------------------------------------------------------------
# Fallback walk + cooldown
# ---------------------------------------------------------------------------


def test_rate_limit_marks_cooldown_and_falls_back(fake_redis):
    tav = StubProvider("tavily", RateLimitError("plan", status_code=432))

    results = _walk([tav, StubProvider("ollama")])

    assert results[0].title == "ollama"
    assert fake_redis.store["websearch:cooldown:tavily"][1] > 60


def test_cooling_provider_is_skipped(fake_redis):
    fake_redis.set("websearch:cooldown:tavily", "HTTP 432", ex=3600)
    tav = StubProvider("tavily")

    results = _walk([tav, StubProvider("ollama")])

    assert tav.calls == []
    assert results[0].title == "ollama"


def test_generic_provider_error_does_not_cool_down(fake_redis):
    _walk([StubProvider("tavily", ProviderError("500")), StubProvider("ollama")])
    assert fake_redis.store == {}


def test_only_provider_cooling_raises_without_request(fake_redis):
    fake_redis.set("websearch:cooldown:tavily", "HTTP 432", ex=3600)
    tav = StubProvider("tavily")

    with pytest.raises(NoProviderAvailable) as info:
        _walk([tav])

    assert tav.calls == []
    assert str(info.value).startswith("No alternative web search provider available (tavily: rate limited until ")


def test_only_provider_runs_out_raises_no_provider(fake_redis):
    with pytest.raises(NoProviderAvailable) as info:
        _walk([StubProvider("tavily", RateLimitError("plan", status_code=432))])
    assert info.value.reasons["tavily"].startswith("rate limited until ")
    assert info.value.reasons["tavily"].endswith("-01 00:00 UTC")


def test_no_provider_lists_every_reason(fake_redis):
    fake_redis.set("websearch:cooldown:tavily", "HTTP 432", ex=3600)
    chain = [
        StubProvider("tavily"),
        StubProvider("ollama", ProviderError("boom")),
        StubProvider("brave", available=False),
    ]

    with pytest.raises(NoProviderAvailable) as info:
        _walk(chain)

    assert set(info.value.reasons) == {"tavily", "ollama", "brave"}
    assert info.value.reasons["ollama"] == "boom"
    assert info.value.reasons["brave"] == "no API key"


def test_empty_chain_raises_no_provider(fake_redis):
    with pytest.raises(NoProviderAvailable, match="no enabled provider configured"):
        _walk([])


def test_redis_down_fails_open(monkeypatch):
    monkeypatch.setattr(sp, "get_provider_cooldown", lambda: ProviderCooldown(client=DeadRedis()))
    tav = StubProvider("tavily", RateLimitError("plan", status_code=432))

    assert _walk([tav, StubProvider("ollama")])[0].title == "ollama"
    assert _walk([tav, StubProvider("ollama")])[0].title == "ollama"
    assert len(tav.calls) == 2


# ---------------------------------------------------------------------------
# enabled flag + per-provider settings
# ---------------------------------------------------------------------------


def test_disabled_provider_dropped_from_chain(ws_cfg):
    ws_cfg.update({"tavily": {"enabled": True}, "ollama": {"enabled": False}})

    chain = sp._build_chain(primary="tavily", fallbacks=["ollama"], registry=sp._SEARCH_PROVIDERS, kind="search")

    assert [p.name for p in chain] == ["tavily"]


def test_enabled_defaults_to_true(ws_cfg):
    ws_cfg.update({"tavily": {"api_key": "x"}})
    assert settings.is_enabled("tavily") is True
    assert settings.is_enabled("ollama") is True


@pytest.mark.parametrize("value", [False, "false", "off", "no", "0"])
def test_enabled_false_values(ws_cfg, value):
    ws_cfg.update({"brave": {"enabled": value}})
    assert settings.is_enabled("brave") is False


def test_provider_block_wins_over_legacy_root(ws_cfg):
    ws_cfg.update({"max_results": 3, "fetch_max_chars": 1000, "tavily": {"max_results": 9, "fetch_max_chars": 2000}})
    assert settings.max_results_for("tavily") == 9
    assert settings.fetch_max_chars_for("tavily") == 2000


def test_legacy_root_used_when_provider_block_missing(ws_cfg):
    ws_cfg.update({"max_results": 3, "fetch_max_chars": 1000})
    assert settings.max_results_for("ollama") == 3
    assert settings.fetch_max_chars_for("ollama") == 1000


def test_defaults_and_cap(ws_cfg):
    assert settings.max_results_for("ollama") == 5
    assert settings.fetch_max_chars_for("ollama") == 8000
    assert settings.fetch_max_chars_for(None) == 8000
    ws_cfg.update({"brave": {"max_results": 50}})
    assert settings.max_results_for("brave") == 20


def test_ollama_key_from_provider_block_then_legacy_root(ws_cfg, monkeypatch):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    ws_cfg.update({"api_key": "legacy"})
    assert ollama._get_api_key() == "legacy"
    ws_cfg.update({"ollama": {"api_key": "new"}})
    assert ollama._get_api_key() == "new"


def test_legacy_root_api_key_not_used_for_other_providers(ws_cfg):
    ws_cfg.update({"api_key": "legacy"})
    assert settings.provider_setting("tavily", "api_key", "") == ""


def test_search_uses_each_providers_max_results(fake_redis, ws_cfg, monkeypatch):
    ws_cfg.update({"tavily": {"max_results": 3}, "ollama": {"max_results": 10}})
    tav = StubProvider("tavily", RateLimitError("plan", status_code=432))
    oll = StubProvider("ollama")
    monkeypatch.setattr(sp, "_resolve_search_chain", lambda: [tav, oll])

    sp.search_with_fallback("q", 0)

    assert tav.calls == [3]
    assert oll.calls == [10]


def test_explicit_max_results_wins(fake_redis, ws_cfg, monkeypatch):
    ws_cfg.update({"ollama": {"max_results": 10}})
    oll = StubProvider("ollama")
    monkeypatch.setattr(sp, "_resolve_search_chain", lambda: [oll])

    sp.search_with_fallback("q", 7)

    assert oll.calls == [7]


def test_fetch_stamps_provider_and_truncates_per_provider(fake_redis, ws_cfg, monkeypatch):
    ws_cfg.update({"ollama": {"fetch_max_chars": 10}})
    monkeypatch.setattr(sp, "_resolve_fetch_chain", lambda: [StubProvider("ollama")])
    monkeypatch.setattr(ws, "fetch_with_fallback", sp.fetch_with_fallback)

    assert sp.fetch_with_fallback("https://x.test").provider == "ollama"
    assert "truncated" in ws.web_fetch("https://x.test")


# ---------------------------------------------------------------------------
# Route description (@search routing card)
# ---------------------------------------------------------------------------


def test_route_primary_available(fake_redis, monkeypatch):
    monkeypatch.setattr(sp, "_resolve_search_chain", lambda: [StubProvider("tavily"), StubProvider("ollama")])
    assert sp.describe_search_route() == "web search via Tavily"


def test_route_fallback_when_primary_cooling(fake_redis, monkeypatch):
    fake_redis.set("websearch:cooldown:tavily", "HTTP 432", ex=3600)
    monkeypatch.setattr(sp, "_resolve_search_chain", lambda: [StubProvider("tavily"), StubProvider("ollama")])

    route = sp.describe_search_route()

    assert route.startswith("web search via Ollama Cloud (fallback — tavily: rate limited until ")
    assert route.endswith(" UTC)")


def test_route_fallback_when_primary_has_no_key(fake_redis, monkeypatch):
    chain = [StubProvider("tavily", available=False), StubProvider("ollama")]
    monkeypatch.setattr(sp, "_resolve_search_chain", lambda: chain)
    assert sp.describe_search_route() == "web search via Ollama Cloud (fallback — tavily: no API key)"


def test_route_nothing_available(fake_redis, monkeypatch):
    fake_redis.set("websearch:cooldown:tavily", "HTTP 432", ex=3600)
    monkeypatch.setattr(sp, "_resolve_search_chain", lambda: [StubProvider("tavily")])
    assert sp.describe_search_route().startswith("no web search provider available (tavily: rate limited until ")


def test_route_empty_chain(fake_redis, monkeypatch):
    monkeypatch.setattr(sp, "_resolve_search_chain", lambda: [])
    assert sp.describe_search_route() == "no web search provider available (no enabled provider configured)"


# ---------------------------------------------------------------------------
# Tool-facing messages
# ---------------------------------------------------------------------------


def test_web_search_reports_no_provider_without_key_hint(monkeypatch):
    def _raise(query, max_results):
        raise NoProviderAvailable("search", {"tavily": "rate limited until 2026-10-01 00:00 UTC"})

    monkeypatch.setattr(ws, "search_with_fallback", _raise)

    out = ws.web_search("q")

    assert out == (
        "Search error: No alternative web search provider available (tavily: rate limited until 2026-10-01 00:00 UTC)"
    )


def test_web_search_keeps_key_hint_when_keys_missing(monkeypatch):
    def _raise(query, max_results):
        raise NoProviderAvailable("search", {"tavily": "no API key"})

    monkeypatch.setattr(ws, "search_with_fallback", _raise)

    assert "TAVILY_API_KEY" in ws.web_search("q")


# ---------------------------------------------------------------------------
# Provider status mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 432, 433])
def test_tavily_rate_limit_carries_status_and_retry_after(monkeypatch, status):
    resp = httpx.Response(status, text="nope", headers={"Retry-After": "12"})
    monkeypatch.setattr(tavily.httpx, "post", lambda *a, **kw: resp)

    with pytest.raises(RateLimitError) as info:
        tavily._post("https://api.tavily.com/search", {}, "tvly-x")

    assert info.value.status_code == status
    assert info.value.retry_after == 12
