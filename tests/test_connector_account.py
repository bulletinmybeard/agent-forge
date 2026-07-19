from agentforge.connectors._account import (
    build_account_aliases,
    build_account_prompt,
    build_account_slug_index,
    build_account_tools,
    group_by_account,
)


def _c(cid, label, ctype, account):
    return {"id": cid, "label": label, "connector_type": ctype, "account_identifier": account, "status": "active"}


def test_group_by_account_skips_empty_identifier():
    conns = [
        _c("a1", "alice-mail", "gmail", "alice@example.com"),
        _c("a2", "alice-drive", "google_drive", "alice@example.com"),
        _c("b1", "bob-mail", "gmail", "bob@example.org"),
        _c("g1", "gitlab-com", "gitlab", ""),  # no account > standalone, skipped
    ]
    groups = group_by_account(conns)
    assert set(groups) == {"alice@example.com", "bob@example.org"}
    assert len(groups["alice@example.com"]) == 2


def test_build_account_tools_unions_and_dedupes():
    members = [_c("a1", "x", "gmail", "e"), _c("a2", "y", "google_drive", "e")]
    tool_names = {"a1": ["gmail_search_a1", "gmail_read_a1"], "a2": ["drive_list_a2"]}
    tools = build_account_tools(members, tool_names, ["write_file", "gmail_search_a1"])
    assert tools == ["gmail_search_a1", "gmail_read_a1", "drive_list_a2", "write_file"]


def test_build_account_aliases_covers_account_and_member_labels():
    members = [
        _c("a1", "alice-mail", "gmail", "alice@example.com"),
        _c("a2", "alice-drive", "google_drive", "alice@example.com"),
    ]
    aliases = build_account_aliases("alice@example.com", members)
    assert aliases == ["@alice-example-com", "@alice-mail", "@alice-drive"]


def test_build_account_slug_index_maps_labels_and_account():
    conns = [
        _c("a1", "alice-mail", "gmail", "alice@example.com"),
        _c("a2", "alice-drive", "google_drive", "alice@example.com"),
    ]
    idx = build_account_slug_index(conns)
    assert idx["alice-mail"] == "alice@example.com"
    assert idx["alice-drive"] == "alice@example.com"
    assert idx["alice-example-com"] == "alice@example.com"


def test_umbrella_prompt_names_account_and_products():
    p = build_account_prompt("alice@example.com", ["Gmail", "Google Drive"])
    assert "alice@example.com" in p
    assert "Gmail, Google Drive" in p
    assert "verbatim" in p.lower()
