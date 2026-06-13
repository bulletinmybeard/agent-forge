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
        _c("a1", "walktheweb", "gmail", "walktheweb@gmail.com"),
        _c("a2", "walkthewebdrive", "google_drive", "walktheweb@gmail.com"),
        _c("b1", "hello", "gmail", "hello@rschu.me"),
        _c("g1", "gitlab-com", "gitlab", ""),  # no account > standalone, skipped
    ]
    groups = group_by_account(conns)
    assert set(groups) == {"walktheweb@gmail.com", "hello@rschu.me"}
    assert len(groups["walktheweb@gmail.com"]) == 2


def test_build_account_tools_unions_and_dedupes():
    members = [_c("a1", "x", "gmail", "e"), _c("a2", "y", "google_drive", "e")]
    tool_names = {"a1": ["gmail_search_a1", "gmail_read_a1"], "a2": ["drive_list_a2"]}
    tools = build_account_tools(members, tool_names, ["write_file", "gmail_search_a1"])
    assert tools == ["gmail_search_a1", "gmail_read_a1", "drive_list_a2", "write_file"]


def test_build_account_aliases_covers_account_and_member_labels():
    members = [
        _c("a1", "walktheweb", "gmail", "walktheweb@gmail.com"),
        _c("a2", "walkthewebdrive", "google_drive", "walktheweb@gmail.com"),
    ]
    aliases = build_account_aliases("walktheweb@gmail.com", members)
    assert aliases == ["@walktheweb-gmail-com", "@walktheweb", "@walkthewebdrive"]


def test_build_account_slug_index_maps_labels_and_account():
    conns = [
        _c("a1", "walktheweb", "gmail", "walktheweb@gmail.com"),
        _c("a2", "walkthewebdrive", "google_drive", "walktheweb@gmail.com"),
    ]
    idx = build_account_slug_index(conns)
    assert idx["walktheweb"] == "walktheweb@gmail.com"
    assert idx["walkthewebdrive"] == "walktheweb@gmail.com"
    assert idx["walktheweb-gmail-com"] == "walktheweb@gmail.com"


def test_umbrella_prompt_names_account_and_products():
    p = build_account_prompt("walktheweb@gmail.com", ["Gmail", "Google Drive"])
    assert "walktheweb@gmail.com" in p
    assert "Gmail, Google Drive" in p
    assert "verbatim" in p.lower()
