"""CLI for AgentForge SQLite migrations (Django ``manage.py migrate`` analogue).

Usage::

    python -m web.server.database.cli upgrade-all
    python -m web.server.database.cli upgrade --database chat
    python -m web.server.database.cli upgrade --database prompt_lab
    python -m web.server.database.cli current --database chat
    python -m web.server.database.cli applied --database chat
    python -m web.server.database.cli history --database chat
    python -m web.server.database.cli revision -m "add foo" --database chat --autogenerate
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from web.server.database import migrate
from web.server.database.migrate import DatabaseName


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentforge-db",
        description="AgentForge SQLite migrations (Alembic) — chat + prompt_lab",
    )

    def _add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--db",
            dest="db_path",
            default=None,
            help="SQLite path override for the selected --database",
        )
        p.add_argument(
            "--database",
            choices=["chat", "prompt_lab"],
            default="chat",
            help="Which database (default: chat). Ignored by upgrade-all.",
        )

    sub = parser.add_subparsers(dest="command", required=True)

    p_all = sub.add_parser("upgrade-all", help="Upgrade chat + prompt_lab to head")
    p_all.add_argument(
        "--db",
        dest="db_path",
        default=None,
        help="Optional override for the chat SQLite path only",
    )

    p_up = sub.add_parser("upgrade", help="Apply migrations for --database to head")
    _add_common(p_up)

    p_down = sub.add_parser("downgrade", help="Roll back --database")
    _add_common(p_down)
    p_down.add_argument("revision", help="Target revision (e.g. -1, base)")

    p_cur = sub.add_parser("current", help="Show applied Alembic revision")
    _add_common(p_cur)

    p_app = sub.add_parser("applied", help="List schema_migrations rows (filename + revision)")
    _add_common(p_app)

    p_hist = sub.add_parser("history", help="List revision ids base → head")
    p_hist.add_argument(
        "--database",
        choices=["chat", "prompt_lab"],
        default="chat",
    )

    p_rev = sub.add_parser("revision", help="Create a new migration file")
    _add_common(p_rev)
    p_rev.add_argument("-m", "--message", required=True)
    p_rev.add_argument("--autogenerate", action="store_true")

    args = parser.parse_args(argv)
    root = str(_project_root())
    if root not in sys.path:
        sys.path.insert(0, root)

    database: DatabaseName = getattr(args, "database", "chat")  # type: ignore[assignment]
    db_path_arg = getattr(args, "db_path", None)

    if args.command == "upgrade-all":
        results = migrate.upgrade_all(chat_db=db_path_arg)
        for name, rev in results.items():
            print(f"{name}: {rev or '(empty)'}")
        return 0

    db_path = migrate.resolve_db_path(database, db_path_arg)

    if args.command == "upgrade":
        migrate.upgrade(db_path, database=database)
        print(f"{database}: {migrate.current(db_path, database=database) or '(empty)'}")
        return 0

    if args.command == "downgrade":
        migrate.downgrade(db_path, args.revision, database=database)
        print(f"{database}: {migrate.current(db_path, database=database) or '(empty)'}")
        return 0

    if args.command == "current":
        print(migrate.current(db_path, database=database) or "(no revision applied)")
        return 0

    if args.command == "applied":
        rows = migrate.list_applied(db_path, database=database)
        if not rows:
            print("(no schema_migrations rows)")
            return 0
        for row in rows:
            print(f"{row['applied_at']}  {row['revision']}  {row['filename']}")
        return 0

    if args.command == "history":
        for rev in migrate.history(database=database):
            print(rev)
        return 0

    if args.command == "revision":
        migrate.make_revision(
            args.message,
            database=database,
            autogenerate=args.autogenerate,
            db_path=db_path if args.autogenerate else None,
        )
        print(f"Revision created under migrations for database={database}")
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
