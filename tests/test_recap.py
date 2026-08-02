"""Recap windowing + the flatten helper shared with session compaction."""

from web.server.database import ChatDatabase
from web.server.summarise import (
    RECAP_MESSAGE_TYPE,
    flatten_messages_for_summary,
    select_recap_window,
    strip_markdown,
)


def _db(tmp_path):
    db = ChatDatabase(tmp_path / "recap.db")
    db.create_tables()
    db.create_session("s1", source="web")
    return db


def _exchange(db, user: str, assistant: str, **kw):
    db.add_message("s1", role="user", msg_type="query", content=user, **kw)
    db.add_message("s1", role="assistant", msg_type="result", content=assistant, **kw)


# -- flatten -------------------------------------------------------------------


def test_flatten_renders_roles_and_tools(tmp_path):
    db = _db(tmp_path)
    _exchange(db, "how do I rebase", "run git rebase")
    db.add_message("s1", role="assistant", msg_type="tool_calls", content=None, tool_calls=[{"name": "shell"}])

    text = flatten_messages_for_summary(db.get_messages("s1"))

    assert text == "User: how do I rebase\nAssistant: run git rebase\nTools called: shell"


def test_flatten_truncates_long_results_and_caps_entries(tmp_path):
    db = _db(tmp_path)
    db.add_message("s1", role="assistant", msg_type="result", content="x" * 2500)
    for i in range(5):
        db.add_message("s1", role="user", msg_type="query", content=f"q{i}")

    full = flatten_messages_for_summary(db.get_messages("s1"))
    assert full.startswith("Assistant: " + "x" * 2000 + "...")

    capped = flatten_messages_for_summary(db.get_messages("s1"), max_entries=2)
    assert capped == "User: q3\nUser: q4"


def test_flatten_empty_when_nothing_summarisable(tmp_path):
    db = _db(tmp_path)
    db.add_message("s1", role="system", msg_type="config", content="mode=agent")

    assert flatten_messages_for_summary(db.get_messages("s1")) == ""


# -- markdown stripping --------------------------------------------------------


def test_strips_backticks_from_identifiers():
    raw = (
        "We brainstormed humorous git branch names including `wibbly-wobbly-git-owy`, "
        "`schrodingers-commit`, and `glorbnax-deliberations`."
    )

    assert strip_markdown(raw) == (
        "We brainstormed humorous git branch names including wibbly-wobbly-git-owy, "
        "schrodingers-commit, and glorbnax-deliberations."
    )


def test_strips_bold_italics_links_and_headings():
    assert strip_markdown("We shipped **the thing** and *tidied* up.") == "We shipped the thing and tidied up."
    assert strip_markdown("See [the docs](https://example.com) for it.") == "See the docs for it."
    assert strip_markdown("## Recap\nWe did a thing.") == "Recap We did a thing."


def test_strips_list_markers_and_folds_to_one_paragraph():
    raw = "- We fixed the parser\n- We shipped it\n\n  and redeployed."

    assert strip_markdown(raw) == "We fixed the parser We shipped it and redeployed."


def test_strips_code_fences():
    raw = "We ran the migration:\n```bash\npoetry run alembic upgrade head\n```\nand it worked."

    assert strip_markdown(raw) == "We ran the migration: poetry run alembic upgrade head and it worked."


def test_underscores_in_identifiers_survive():
    """Stripping `_` as emphasis would corrupt the names a recap exists to preserve."""
    raw = "We added `min_new_messages` and `ends_at` to the config."

    assert strip_markdown(raw) == "We added min_new_messages and ends_at to the config."


def test_strip_markdown_handles_empty():
    assert strip_markdown("") == ""
    assert strip_markdown("   \n  ") == ""


# -- windowing -----------------------------------------------------------------


def test_first_recap_covers_whole_session(tmp_path):
    db = _db(tmp_path)
    for i in range(3):
        _exchange(db, f"q{i}", f"a{i}")

    fresh, watermark, previous = select_recap_window(db.get_messages("s1"))

    assert watermark == 0
    assert previous is None
    assert len(fresh) == 6


def test_second_recap_covers_only_messages_after_the_first(tmp_path):
    db = _db(tmp_path)
    # 20 prompts + 20 responses, then a recap, then 3 more exchanges.
    for i in range(20):
        _exchange(db, f"q{i}", f"a{i}")
    db.add_message("s1", role="system", msg_type=RECAP_MESSAGE_TYPE, content="We did the first 20.", is_volatile=True)
    for i in range(3):
        _exchange(db, f"later{i}", f"answer{i}")

    fresh, watermark, previous = select_recap_window(db.get_messages("s1"))

    assert watermark == 41  # 40 messages + the recap itself
    assert previous == "We did the first 20."
    assert len(fresh) == 6
    assert [m.content for m in fresh if m.type == "query"] == ["later0", "later1", "later2"]


def test_window_excludes_volatile_and_incognito(tmp_path):
    db = _db(tmp_path)
    _exchange(db, "keep me", "kept")
    _exchange(db, "secret", "hidden", is_incognito=True)
    db.add_message("s1", role="assistant", msg_type="result", content="ephemeral", is_volatile=True)

    fresh, _watermark, _previous = select_recap_window(db.get_messages("s1"))

    contents = [m.content for m in fresh]
    assert contents == ["keep me", "kept"]


def test_window_empty_when_only_a_recap_follows(tmp_path):
    db = _db(tmp_path)
    _exchange(db, "q", "a")
    db.add_message("s1", role="system", msg_type=RECAP_MESSAGE_TYPE, content="We did a thing.", is_volatile=True)

    fresh, _watermark, previous = select_recap_window(db.get_messages("s1"))

    assert fresh == []
    assert previous == "We did a thing."


def test_only_the_latest_recap_sets_the_watermark(tmp_path):
    db = _db(tmp_path)
    _exchange(db, "q0", "a0")
    db.add_message("s1", role="system", msg_type=RECAP_MESSAGE_TYPE, content="first", is_volatile=True)
    _exchange(db, "q1", "a1")
    db.add_message("s1", role="system", msg_type=RECAP_MESSAGE_TYPE, content="second", is_volatile=True)
    _exchange(db, "q2", "a2")

    fresh, watermark, previous = select_recap_window(db.get_messages("s1"))

    assert previous == "second"
    assert watermark == 6
    assert [m.content for m in fresh] == ["q2", "a2"]
