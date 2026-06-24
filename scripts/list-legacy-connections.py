#!/usr/bin/env python3
"""Read-only audit of legacy per-product Google connector rows in the chat DB.

Legacy types (pre-unified Google OAuth): gmail, google_drive, bigquery, youtube.
New connections use connector_type ``google``. See docs/connectors.md.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

LEGACY_TYPES = ("gmail", "google_drive", "bigquery", "youtube")
DEFAULT_DB = "data/web_chat.db"


def _resolve_db_path(config_path: Path) -> Path:
    db_rel = DEFAULT_DB
    if config_path.is_file():
        try:
            import yaml
        except ImportError:
            print("PyYAML required to read config (--db-path to skip)", file=sys.stderr)
            sys.exit(1)
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
        db_rel = (cfg.get("web") or {}).get("database_path", db_rel)
    return config_path.parent / db_rel


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="config.yaml path (default: ./config.yaml)",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="SQLite chat DB path (overrides web.database_path from config)",
    )
    args = parser.parse_args(argv)

    db_path = args.db_path or _resolve_db_path(args.config.resolve())
    if not db_path.is_file():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 1

    placeholders = ",".join("?" for _ in LEGACY_TYPES)
    sql = f"""
        SELECT id, connector_type, label, account_identifier, status, created_at
        FROM connections
        WHERE connector_type IN ({placeholders})
        ORDER BY connector_type, label
    """

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, LEGACY_TYPES).fetchall()

    if not rows:
        print(f"No legacy Google connections in {db_path}")
        return 0

    print(f"Legacy Google connections in {db_path} ({len(rows)} row(s)):\n")
    for row in rows:
        acct = row["account_identifier"] or "(no account)"
        print(
            f"  {row['id']}  {row['connector_type']:14}  {row['label']:24}  "
            f"{acct}  [{row['status']}]  created={row['created_at']}"
        )
    print("\nMigrate each row to connector_type 'google' — see docs/connectors.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
