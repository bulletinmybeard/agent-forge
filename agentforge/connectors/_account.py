from __future__ import annotations

import re


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")


def account_slug(account_identifier: str) -> str:
    return slugify(account_identifier)


def label_slug(label: str) -> str:
    return slugify(label)


def group_by_account(connections: list[dict]) -> dict[str, list[dict]]:
    """Group connections by non-empty ``account_identifier``.
    Connections without a group are skipped."""
    groups: dict[str, list[dict]] = {}
    for c in connections:
        acct = (c.get("account_identifier") or "").strip()
        if not acct:
            continue
        groups.setdefault(acct, []).append(c)
    return groups


def build_account_tools(members: list[dict], tool_names: dict[str, list[str]], extra: list[str]) -> list[str]:
    """Union (order-preserving, de-duplicated) of all members' scoped tool names + the extras."""
    tools: list[str] = []
    for c in members:
        for name in tool_names.get(c["id"], []):
            if name not in tools:
                tools.append(name)
    for name in extra:
        if name not in tools:
            tools.append(name)
    return tools


def build_account_aliases(account_identifier: str, members: list[dict]) -> list[str]:
    """Mode @-aliases the account agent owns: account-email slug + every member label slug."""
    aliases = [f"@{account_slug(account_identifier)}"]
    for c in members:
        alias = f"@{label_slug(c['label'])}"
        if alias not in aliases:
            aliases.append(alias)
    return aliases


def build_account_slug_index(connections: list[dict]) -> dict[str, str]:
    """label-slug and account-email-slug > account_identifier, for grouped connections."""
    index: dict[str, str] = {}
    for c in connections:
        acct = (c.get("account_identifier") or "").strip()
        if not acct:
            continue
        index[label_slug(c["label"])] = acct
        index.setdefault(account_slug(acct), acct)
    return index


def build_account_prompt(account_identifier: str, product_names: list[str]) -> str:
    products = ", ".join(product_names) if product_names else "the connected services"
    return (
        f"You assist the connected account {account_identifier}. "
        f"Available tools cover: {products}. "
        "Pick the right tool(s) for each request, and you may combine them in one task. "
        "Emit structured tool calls only, never inline text. "
        "When a tool returns a list, copy it into your reply verbatim."
    )
