"""Dedicated clients stay on their agent when the alias is missing."""

from web.server.mode_routing import pinned_client_mode

_QUESTION = "How many times did I order groceries from Albert Heijn?"


def test_afemail_source_pins_a_bare_question():
    assert pinned_client_mode("afemail", None, None, _QUESTION) == "custom:afemail"


def test_afemail_source_is_case_insensitive():
    assert pinned_client_mode(" AFEMAIL ", None, None, _QUESTION) == "custom:afemail"


def test_afemail_alias_still_pins():
    assert (
        pinned_client_mode("afemail", None, "custom:afemail", f"@afemail {_QUESTION}")
        == "custom:afemail"
    )


def test_explicit_builtin_prefix_wins():
    assert pinned_client_mode("afemail", "web_search", None, f"@search {_QUESTION}") is None


def test_other_custom_alias_wins():
    assert (
        pinned_client_mode("afemail", None, "custom:haushaltsbuch", "@haushaltsbuch balance")
        is None
    )


def test_unknown_at_prefix_is_not_pinned():
    assert pinned_client_mode("afemail", None, None, "@not-a-mode hello") is None


def test_other_sources_are_not_pinned():
    assert pinned_client_mode("web", None, None, _QUESTION) is None
    assert pinned_client_mode(None, None, None, _QUESTION) is None
    assert pinned_client_mode("", None, None, _QUESTION) is None
