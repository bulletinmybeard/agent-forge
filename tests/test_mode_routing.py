from web.server.mode_routing import (
    instruction_for_routing,
    is_supplied_text_transform,
    markdown_path_links,
    strip_mode_prefix,
)
from web.server.ws_endpoint import _classify_mode_heuristic, _match_mode_patterns


def test_strip_start_of_query_aliases():
    assert strip_mode_prefix("@agent list files") == ("list files", "agent")
    assert strip_mode_prefix("@search latest news") == ("latest news", "web_search")
    assert strip_mode_prefix("@coding fix imports") == ("fix imports", "coding")
    assert strip_mode_prefix("@film Recommend Alien") == ("Recommend Alien", "film")
    assert strip_mode_prefix("@movie Arrival") == ("Arrival", "film")
    assert strip_mode_prefix("@code refactor") == ("refactor", "coding")


def test_strip_rag_aliases_at_start():
    for prefix in ("@qdrant", "@docs", "@find"):
        cleaned, mode = strip_mode_prefix(f"{prefix} how does auth work?")
        assert mode == "search"
        assert cleaned == "how does auth work?"


def test_strip_rag_aliases_anywhere_in_query():
    cases = [
        ("find me @qdrant session compaction docs", "find me  session compaction docs"),
        ("before @docs after", "before  after"),
        ("use @find for this", "use  for this"),
        ("mixed @QDRANT case", "mixed  case"),
    ]
    for query, expected in cases:
        cleaned, mode = strip_mode_prefix(query)
        assert mode == "search", query
        assert cleaned == expected, query


def test_no_prefix_returns_original():
    assert strip_mode_prefix("plain question") == ("plain question", None)
    assert strip_mode_prefix("  spaced  ") == ("  spaced  ", None)


def test_reformat_of_pasted_paths_is_supplied_text():
    query = (
        "Reformat the local directory paths below to be Markdown links\n\n"
        "- /Users/ada/projects/alpha/NOTES.md\n"
        "- /Users/ada/projects/beta/NOTES.md\n"
    )
    assert is_supplied_text_transform(query)


def test_rewrite_of_one_file_stays_with_the_classifier():
    assert not is_supplied_text_transform("rewrite ~/notes.md")
    assert not is_supplied_text_transform("rewrite /Users/ada/projects/app/notes.md")


def test_read_then_reformat_stays_with_the_classifier():
    query = "Read /Users/ada/a.md and /Users/ada/b.md and reformat them as links"
    assert not is_supplied_text_transform(query)


def test_words_inside_the_pasted_note_do_not_block_a_translation():
    query = (
        "Translate the note below into German. Keep paths and numbers unchanged.\n\n"
        "The cron job writes /var/log/nightly.log and rotates /tmp/nightly.log.1 "
        "after 7 days. Last run exited 0.\n"
    )
    assert is_supplied_text_transform(query)


def test_mentioning_a_cron_job_is_not_a_schedule_request():
    note = "the cron job writes /var/log/nightly.log after the last run"
    assert _match_mode_patterns(note) is None
    assert _match_mode_patterns("set up a cron job for nightly cleanup") == "scheduler"


class _Runtime:
    agent_available = True
    custom_agents: dict = {}


def test_routing_uses_the_instruction_above_the_blank_line():
    query = (
        "Summarize the ticket below.\n\n"
        "Please schedule a daily backup at 2am and set up a cron job for nightly cleanup.\n"
    )
    assert instruction_for_routing(query) == "Summarize the ticket below."
    mode, confidence = _classify_mode_heuristic(query, _Runtime())
    assert mode == "chat"
    assert confidence == "low"


def test_a_single_mode_pattern_is_a_medium_hint():
    mode, confidence = _classify_mode_heuristic("set up a cron job for nightly cleanup", _Runtime())
    assert mode == "scheduler"
    assert confidence == "medium"


def test_a_read_request_above_the_paste_stays_with_the_classifier():
    query = "Read the files below and reformat them as links.\n\n- /Users/ada/a.md\n- /Users/ada/b.md\n"
    assert not is_supplied_text_transform(query)


def _link_prompt(paths: list[str], example: str) -> str:
    bullets = "\n".join(f"- {path}" for path in paths)
    return (
        "Reformat the list of directories below to be Markdown links "
        f"(e.g., `{example}`) as I want to use it in an Obsidian Markdown document:\n\n"
        f"{bullets}\n"
    )


def test_markdown_links_keep_absolute_paths():
    query = _link_prompt(
        [
            "/Users/ada/www/projects/alpha/NOTES.md",
            "/Users/ada/www/projects/beta/docs/GUIDE.md",
        ],
        "[alpha](/Users/ada/www/projects/alpha/NOTES.md)",
    )
    assert markdown_path_links(query) == (
        "```markdown\n"
        "- [alpha](/Users/ada/www/projects/alpha/NOTES.md)\n"
        "- [beta](/Users/ada/www/projects/beta/docs/GUIDE.md)\n"
        "```"
    )


def test_markdown_links_keep_backticks_and_tilde():
    query = _link_prompt(
        [
            "`~/www/projects/alpha/NOTES.md`",
            "`~/www/projects/beta/NOTES.md`",
        ],
        "[alpha](~/www/projects/alpha/NOTES.md)",
    )
    text = markdown_path_links(query)
    assert text is not None
    assert "[alpha](~/www/projects/alpha/NOTES.md)" in text
    assert "[beta](~/www/projects/beta/NOTES.md)" in text


def test_markdown_links_expand_a_grouped_docs_bullet():
    query = _link_prompt(
        [
            "`~/www/projects/alpha/NOTES.md`",
            "`~/www/projects/felix/docs/ (architecture.md, cli.md)`",
        ],
        "[alpha](~/www/projects/alpha/NOTES.md)",
    )
    text = markdown_path_links(query) or ""
    assert "[felix/architecture](~/www/projects/felix/docs/architecture.md)" in text
    assert "[felix/cli](~/www/projects/felix/docs/cli.md)" in text


def test_markdown_links_ignores_other_rewrites():
    assert markdown_path_links("Translate the paragraph below\n\nHello there.") is None
