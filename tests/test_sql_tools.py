"""SQL query tools register with the agent toolset."""

from agentforge.tools import register_configured_sql_tools
from agentforge.tools.registry import ToolRegistry
from agentforge.tools.sql_schema_tool import (
    _MISSING_ACTIONS_NOTE,
    _schema_markdown,
    register_sql_schema_tools,
    sql_save_schema,
)
from agentforge.tools.sql_tools import register_sql_tools

_SQL_TOOLS = {"execute_sql", "db_query_plan", "sql_extract_schema", "sql_save_schema"}

_SAMPLE_SCHEMA = {
    "dialect": "mysql",
    "table_count": 1,
    "total_columns": 2,
    "view_count": 0,
    "tables": {
        "orders": {
            "row_count": 3,
            "primary_keys": ["id"],
            "columns": [
                {"name": "id", "type": "int", "nullable": False, "extra": "auto_increment"},
                {"name": "user_id", "type": "int", "nullable": True},
            ],
            "foreign_keys": [
                {
                    "column": "user_id",
                    "ref_table": "users",
                    "ref_column": "id",
                    "constraint": "fk_orders_user",
                }
            ],
            "indexes": [{"name": "idx_user", "unique": False, "columns": ["user_id"]}],
        }
    },
    "views": [],
}


def test_sql_tools_register_under_database():
    registry = ToolRegistry()
    count = register_sql_tools(registry) + register_sql_schema_tools(registry)
    assert count == 4
    assert set(registry.list_tools()) == _SQL_TOOLS
    assert registry._tool_categories["sql_extract_schema"] == "Database"
    hints = registry.get_model_hints()
    assert "### Database" in hints
    assert "sql_extract_schema" in hints


def test_sql_register_is_idempotent():
    registry = ToolRegistry()
    register_sql_tools(registry)
    register_sql_schema_tools(registry)
    assert register_sql_tools(registry) == 0
    assert register_sql_schema_tools(registry) == 0
    assert set(registry.list_tools()) == _SQL_TOOLS


def test_configured_sql_tools_follow_sql_databases(monkeypatch):
    registry = ToolRegistry()
    monkeypatch.setattr("agentforge.tools._sql_databases_configured", lambda: False)
    assert register_configured_sql_tools(registry) == 0
    assert registry.list_tools() == []

    monkeypatch.setattr("agentforge.tools._sql_databases_configured", lambda: True)
    assert register_configured_sql_tools(registry) == 4
    assert set(registry.list_tools()) == _SQL_TOOLS


def test_schema_markdown_lists_keys_and_columns():
    text = _schema_markdown("demodb", _SAMPLE_SCHEMA)
    assert "## orders" in text
    assert "`id`" in text
    assert "int" in text
    assert "`orders` (`user_id`) → `users` (`id`)" in text
    assert "fk_orders_user" in text
    assert "optional, many" in text
    assert _MISSING_ACTIONS_NOTE in text
    assert "idx_user" in text


def test_sql_save_schema_writes_markdown(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agentforge.tools.sql_schema_tool._load_schema",
        lambda database, force_refresh=False: (_SAMPLE_SCHEMA, None, True),
    )
    dest = tmp_path / "out"
    result = sql_save_schema("demodb", str(dest))
    written = (dest / "demodb-schema.md").resolve()
    assert written.is_file()
    assert "orders" in written.read_text(encoding="utf-8")
    assert "Do not repeat the schema" in result
    assert str(written) in result
    assert "force_refresh=true" in result


def _col(name, col_type="int", nullable=True):
    return {"name": name, "type": col_type, "nullable": nullable}


def _table(columns, primary_keys=None, foreign_keys=None, indexes=None):
    return {
        "columns": columns,
        "primary_keys": primary_keys or [],
        "foreign_keys": foreign_keys or [],
        "indexes": indexes or [],
    }


def _relations_body(text: str) -> str:
    _head, _sep, rest = text.partition("## Relations\n")
    body, _sep, _tables = rest.partition("\n## ")
    return body


def test_schema_markdown_groups_composite_foreign_keys():
    schema = {
        "dialect": "mysql",
        "table_count": 4,
        "total_columns": 8,
        "view_count": 0,
        "views": [],
        "tables": {
            "sub": _table(
                [
                    _col("pa", nullable=False),
                    _col("pb", nullable=False),
                    _col("pc", nullable=False),
                ],
                ["pa", "pb", "pc"],
            ),
            "line": _table(
                [
                    _col("a", nullable=False),
                    _col("b", nullable=False),
                    _col("c", nullable=False),
                    _col("d", nullable=False),
                ],
                ["a", "b", "c", "d"],
                [
                    {
                        "column": "c",
                        "ref_table": "sub",
                        "ref_column": "pc",
                        "constraint": "fk_line",
                        "position": 3,
                        "update_rule": "CASCADE",
                        "delete_rule": "RESTRICT",
                    },
                    {
                        "column": "a",
                        "ref_table": "sub",
                        "ref_column": "pa",
                        "constraint": "fk_line",
                        "position": 1,
                        "update_rule": "CASCADE",
                        "delete_rule": "RESTRICT",
                    },
                    {
                        "column": "b",
                        "ref_table": "sub",
                        "ref_column": "pb",
                        "constraint": "fk_line",
                        "position": 2,
                        "update_rule": "CASCADE",
                        "delete_rule": "RESTRICT",
                    },
                ],
            ),
            "order": _table([_col("order_id", nullable=False)], ["order_id"]),
            "context": _table(
                [_col("order_id", nullable=False)],
                ["order_id"],
                [
                    {
                        "column": "order_id",
                        "ref_table": "order",
                        "ref_column": "order_id",
                        "constraint": "fk_ctx",
                        "position": 1,
                        "update_rule": "RESTRICT",
                        "delete_rule": "CASCADE",
                    }
                ],
            ),
            "note": _table(
                [_col("id", nullable=False), _col("user_id", nullable=False)],
                ["id"],
                [
                    {
                        "column": "user_id",
                        "ref_table": "users",
                        "ref_column": "id",
                        "constraint": "fk_note_user",
                        "update_rule": "CASCADE",
                        "delete_rule": "CASCADE",
                    }
                ],
                [{"name": "uq_user", "unique": True, "columns": ["user_id"]}],
            ),
            "users": _table([_col("id", nullable=False)], ["id"]),
        },
    }
    text = _schema_markdown("demodb", schema)
    assert text.count("| `fk_line` |") == 1
    assert "`line` (`a`, `b`, `c`) → `sub` (`pa`, `pb`, `pc`) ON UPDATE CASCADE ON DELETE RESTRICT" in text
    assert "(`c`, `a`, `b`)" not in text
    assert "| `fk_ctx` | `context` (`order_id`) | `order` (`order_id`) | RESTRICT | CASCADE | required, at most one |" in text
    assert "| `fk_note_user` | `note` (`user_id`) | `users` (`id`) | CASCADE | CASCADE | required, at most one |" in text
    assert "| `fk_line` | `line` (`a`, `b`, `c`) | `sub` (`pa`, `pb`, `pc`) | CASCADE | RESTRICT | required, many |" in text
    bullet = "`fk_line`: `line` (`a`, `b`, `c`) → `sub` (`pa`, `pb`, `pc`) ON UPDATE CASCADE ON DELETE RESTRICT"
    assert text.count(bullet) == 2
    assert _MISSING_ACTIONS_NOTE not in text


def test_schema_markdown_name_matches_and_unmatched_ids():
    tables = {
        "users": _table([_col("id", nullable=False)], ["id"]),
        "orders": _table(
            [_col("id", nullable=False), _col("user_id")],
            ["id"],
            [
                {
                    "column": "user_id",
                    "ref_table": "users",
                    "ref_column": "id",
                    "constraint": "fk_orders_user",
                    "update_rule": "RESTRICT",
                    "delete_rule": "CASCADE",
                }
            ],
        ),
        "area": _table([_col("id", nullable=False)], ["id"]),
        "area_site": _table([_col("area", nullable=False), _col("site", nullable=False)]),
        "parent": _table([_col("id", nullable=False)], ["id"]),
        "pages": _table(
            [_col("id", nullable=False), _col("parent_id", nullable=False)],
            ["id"],
        ),
        "ports": _table([_col("id", nullable=False)], ["id"]),
        "hosts": _table([_col("id", nullable=False), _col("port_id")], ["id"]),
        "rack": _table([_col("id", nullable=False)], ["id"]),
        "hardware": _table(
            [_col("id", nullable=False), _col("part_of", nullable=False), _col("rack")],
            ["id", "part_of"],
        ),
        "reports": _table([_col("id", nullable=False), _col("hardware_id")], ["id"]),
        "vlan": _table([_col("number", nullable=False)], ["number"]),
        "peering": _table([_col("id", nullable=False), _col("vlan")], ["id"]),
        "community": _table([_col("name")]),
        "snmpcommands": _table([_col("id", nullable=False), _col("community")], ["id"]),
        "relation": _table([_col("id", nullable=False)], ["id"]),
        "relation_address": _table([_col("id", nullable=False)], ["id"]),
        "hidden_relations": _table([_col("relation_id", nullable=False)], ["relation_id"]),
        "datacenters": _table(
            [_col("id", nullable=False), _col("cp_relation_address_id")],
            ["id"],
        ),
        "sales_order": _table(
            [
                _col("sales_order_id", nullable=False),
                _col("sales_order_eu_account_relation_id"),
            ],
            ["sales_order_id"],
        ),
        "ticket": _table([_col("ticket", nullable=False)], ["ticket"]),
        "ticket_data": _table([_col("id", nullable=False), _col("ticket")], ["id"]),
        "demarcation_point": _table(
            [_col("id", nullable=False), _col("demarcation_point_port_id", "mediumint")],
            ["id"],
        ),
        "TrafficHistory": _table([_col("id", nullable=False)], ["id"]),
        "traffic_history_extra_data": _table(
            [_col("id", nullable=False), _col("traffic_history_id", nullable=False)],
            ["id"],
        ),
        "allocations": _table(
            [_col("id", nullable=False), _col("contract_id", "mediumint")],
            ["id"],
        ),
        "site": _table([_col("id", nullable=False)], ["id"]),
    }
    schema = {
        "dialect": "mysql",
        "table_count": len(tables),
        "total_columns": 40,
        "view_count": 1,
        "tables": tables,
        "views": [{"name": "site", "definition": "select 1"}],
    }
    text = _schema_markdown("demodb", schema)
    body = _relations_body(text)
    name_part, _sep, unmatched_part = body.partition("### `_id` columns")

    assert "| likely | `area_site`.`area` | `area` (`id`) |" in name_part
    assert "| likely | `pages`.`parent_id` | `pages` (`id`) |" in name_part
    assert "`pages`.`parent_id` | `parent`" not in name_part
    assert "| likely | `hosts`.`port_id` | `ports` (`id`) |" in name_part
    assert "plural of `port`" in name_part
    assert "| likely | `hardware`.`rack` | `rack` (`id`) |" in name_part
    assert "| likely | `hidden_relations`.`relation_id` | `relation` (`id`) |" in name_part
    assert "name ends with `relation_id`" in name_part
    assert "| likely | `datacenters`.`cp_relation_address_id` | `relation_address` (`id`) |" in name_part
    assert (
        "| likely | `sales_order`.`sales_order_eu_account_relation_id` | `relation` (`id`) |"
        in name_part
    )
    assert "| likely | `ticket_data`.`ticket` | `ticket` (`ticket`) |" in name_part
    assert (
        "| partial | `reports`.`hardware_id` | `hardware` (`id`, `part_of`) |" in name_part
    )
    assert "this column alone does not identify a row" in name_part
    assert "| partial | `peering`.`vlan` | `vlan` (`number`) |" in name_part
    assert "| partial | `snmpcommands`.`community` | `community` (none) |" in name_part
    assert "demarcation_point_port_id" not in name_part
    assert "traffic_history_id" not in name_part
    assert "`orders`.`user_id`" not in name_part
    assert "`area_site`.`site`" not in name_part
    assert "`sales_order`.`sales_order_id`" not in name_part
    assert "`TrafficHistory`" not in name_part

    assert "| `demarcation_point` | `demarcation_point_port_id` | mediumint | yes |" in unmatched_part
    assert "| `traffic_history_extra_data` | `traffic_history_id` | int | no |" in unmatched_part
    assert "| `allocations` | `contract_id` | mediumint | yes |" in unmatched_part
    assert "relation_id" not in unmatched_part
    assert "user_id" not in unmatched_part

    bullet = (
        "`fk_orders_user`: `orders` (`user_id`) → `users` (`id`) "
        "ON UPDATE RESTRICT ON DELETE CASCADE"
    )
    assert text.count(bullet) == 2
    assert "Name matches (not enforced):" in text
    assert _MISSING_ACTIONS_NOTE not in text
