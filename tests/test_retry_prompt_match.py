"""Retry must recognize the bubble text, not only the stored execution text."""

from web.server import ws_endpoint


def test_exact_and_prefix_stripped_match():
    stored = "Check my cloud for one of the movies"
    assert ws_endpoint._retry_prompt_matches(stored, stored, "film")
    assert ws_endpoint._retry_prompt_matches(stored, f"@film {stored}", "film")
    assert not ws_endpoint._retry_prompt_matches(stored, "@film a different prompt", "film")


def test_normalized_hashtag_matches_the_bubble(monkeypatch):
    def fake_norm(query, mode):
        if mode == "film":
            return query.replace("#myservice", "My Service")
        return query

    monkeypatch.setattr(ws_endpoint, "_normalize_cloud_hashtags", fake_norm)
    stored = "Check my My Service cloud for one of the movies"
    sent = "@film Check my #myservice cloud for one of the movies"
    assert ws_endpoint._retry_prompt_matches(stored, sent, "film")
    assert not ws_endpoint._retry_prompt_matches("unrelated", sent, "film")
