"""Alembic multi-database migration helpers.

Databases:

* **chat** — main SQLite (sessions, tools, monitor, connectors, **canvas**).
* **prompt_lab** — separate SQLite for Prompt Lab history.

Public API:

* ``upgrade(db_path, database=...)`` / ``upgrade_all()``
* ``downgrade``, ``current``, ``history``, ``list_applied``
* ``make_revision``

After every successful upgrade/stamp, applied revisions are logged in the
``schema_migrations`` table (Laravel-style): ``revision``, ``filename``,
``applied_at``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import yaml
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)

DatabaseName = Literal["chat", "prompt_lab"]

_PACKAGE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _PACKAGE_DIR.parents[3]


@dataclass(frozen=True)
class DatabaseSpec:
    name: DatabaseName
    alembic_ini: Path
    migrations_dir: Path
    legacy_marker_table: str
    default_rel_path: str


SPECS: dict[DatabaseName, DatabaseSpec] = {
    "chat": DatabaseSpec(
        name="chat",
        alembic_ini=_PACKAGE_DIR / "alembic.ini",
        migrations_dir=_PACKAGE_DIR / "migrations",
        legacy_marker_table="chat_sessions",
        default_rel_path="data/web_chat.db",
    ),
    "prompt_lab": DatabaseSpec(
        name="prompt_lab",
        alembic_ini=_PACKAGE_DIR.parent / "prompt_lab" / "database" / "alembic.ini",
        migrations_dir=_PACKAGE_DIR.parent / "prompt_lab" / "database" / "migrations",
        legacy_marker_table="prompt_lab_runs",
        default_rel_path="data/prompt_lab.db",
    ),
}

_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    revision TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


def _sqlite_url(db_path: str | Path) -> str:
    path = Path(db_path).expanduser().resolve()
    return f"sqlite:///{path}"


def _alembic_config(spec: DatabaseSpec, db_path: str | Path) -> Config:
    cfg = Config(str(spec.alembic_ini))
    cfg.set_main_option("script_location", str(spec.migrations_dir))
    cfg.set_main_option("sqlalchemy.url", _sqlite_url(db_path))
    cfg.set_main_option("path_separator", "os")
    cfg.set_main_option("prepend_sys_path", str(_PROJECT_ROOT))
    return cfg


def _script_directory(spec: DatabaseSpec) -> ScriptDirectory:
    cfg = Config(str(spec.alembic_ini))
    cfg.set_main_option("script_location", str(spec.migrations_dir))
    cfg.set_main_option("path_separator", "os")
    return ScriptDirectory.from_config(cfg)


def _has_legacy_schema(spec: DatabaseSpec, db_path: str | Path) -> bool:
    path = Path(db_path).expanduser()
    if not path.exists() or path.stat().st_size == 0:
        return False
    engine = create_engine(_sqlite_url(path), poolclass=NullPool)
    try:
        tables = set(inspect(engine).get_table_names())
        if spec.legacy_marker_table not in tables:
            return False
        if "alembic_version" in tables:
            with engine.connect() as conn:
                row = conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).fetchone()
                if row and row[0]:
                    return False
        return True
    finally:
        engine.dispose()


def _head_revision(spec: DatabaseSpec) -> str:
    script = _script_directory(spec)
    heads = script.get_heads()
    if not heads:
        raise RuntimeError(f"No Alembic heads for database={spec.name!r}")
    if len(heads) > 1:
        raise RuntimeError(f"Multiple Alembic heads for {spec.name!r} (merge required): {heads}")
    return heads[0]


def _revision_filename(script: ScriptDirectory, revision: str) -> str:
    sc = script.get_revision(revision)
    if sc is None or not sc.path:
        return f"{revision}.py"
    return Path(sc.path).name


def _ensure_schema_migrations_table(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(_SCHEMA_MIGRATIONS_DDL))


def _record_applied_migrations(spec: DatabaseSpec, db_path: str | Path) -> None:
    """Sync ``schema_migrations`` with the current Alembic lineage (filename + revision)."""
    path = Path(db_path).expanduser()
    if not path.exists():
        return
    engine = create_engine(_sqlite_url(path), poolclass=NullPool)
    script = _script_directory(spec)
    try:
        _ensure_schema_migrations_table(engine)
        with engine.connect() as conn:
            context = MigrationContext.configure(conn)
            current = context.get_current_revision()
        if current is None:
            return

        # All revisions from base up to *current* (inclusive).
        applied: list[str] = []
        for sc in script.walk_revisions(base="base", head=current):
            applied.append(sc.revision)
        applied.reverse()

        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
        with engine.begin() as conn:
            for rev in applied:
                filename = _revision_filename(script, rev)
                conn.execute(
                    text(
                        "INSERT OR IGNORE INTO schema_migrations (revision, filename, applied_at) "
                        "VALUES (:revision, :filename, :applied_at)"
                    ),
                    {"revision": rev, "filename": filename, "applied_at": now},
                )
        logger.info(
            "schema_migrations synced for %s (%s): %d revision(s)",
            spec.name,
            path,
            len(applied),
        )
    finally:
        engine.dispose()


def upgrade(
    db_path: str | Path,
    *,
    database: DatabaseName = "chat",
    revision: str = "head",
) -> None:
    """Bring *database* at *db_path* up to *revision* (default: latest head)."""
    spec = SPECS[database]
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = _alembic_config(spec, path)

    if _has_legacy_schema(spec, path):
        head = _head_revision(spec)
        logger.info(
            "Legacy %s DB at %s — stamping Alembic head %s (no DDL)",
            database,
            path,
            head,
        )
        command.stamp(cfg, head)
        command.upgrade(cfg, revision)
    else:
        logger.info("Alembic upgrade %s → %s on %s", database, revision, path)
        command.upgrade(cfg, revision)

    _record_applied_migrations(spec, path)


def downgrade(
    db_path: str | Path,
    revision: str,
    *,
    database: DatabaseName = "chat",
) -> None:
    spec = SPECS[database]
    cfg = _alembic_config(spec, db_path)
    logger.info("Alembic downgrade %s → %s on %s", database, revision, db_path)
    command.downgrade(cfg, revision)
    # Rebuild log from remaining lineage (remove rows past current).
    _prune_schema_migrations_log(spec, db_path)


def _prune_schema_migrations_log(spec: DatabaseSpec, db_path: str | Path) -> None:
    path = Path(db_path).expanduser()
    if not path.exists():
        return
    engine = create_engine(_sqlite_url(path), poolclass=NullPool)
    script = _script_directory(spec)
    try:
        tables = set(inspect(engine).get_table_names())
        if "schema_migrations" not in tables:
            return
        with engine.connect() as conn:
            context = MigrationContext.configure(conn)
            current = context.get_current_revision()
        keep: set[str] = set()
        if current is not None:
            for sc in script.walk_revisions(base="base", head=current):
                keep.add(sc.revision)
        with engine.begin() as conn:
            if keep:
                # Delete rows not in keep
                rows = conn.execute(text("SELECT revision FROM schema_migrations")).fetchall()
                for (rev,) in rows:
                    if rev not in keep:
                        conn.execute(
                            text("DELETE FROM schema_migrations WHERE revision = :r"),
                            {"r": rev},
                        )
            else:
                conn.execute(text("DELETE FROM schema_migrations"))
    finally:
        engine.dispose()


def current(db_path: str | Path, *, database: DatabaseName = "chat") -> str | None:
    path = Path(db_path).expanduser()
    if not path.exists():
        return None
    engine = create_engine(_sqlite_url(path), poolclass=NullPool)
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()


def history(*, database: DatabaseName = "chat") -> list[str]:
    script = _script_directory(SPECS[database])
    revs: list[str] = []
    for sc in script.walk_revisions():
        revs.append(sc.revision)
    revs.reverse()
    return revs


def list_applied(db_path: str | Path, *, database: DatabaseName = "chat") -> list[dict]:
    """Return rows from ``schema_migrations`` ordered by application time."""
    path = Path(db_path).expanduser()
    if not path.exists():
        return []
    engine = create_engine(_sqlite_url(path), poolclass=NullPool)
    try:
        tables = set(inspect(engine).get_table_names())
        if "schema_migrations" not in tables:
            return []
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT revision, filename, applied_at FROM schema_migrations ORDER BY id ASC")
            ).fetchall()
        return [{"revision": r[0], "filename": r[1], "applied_at": r[2]} for r in rows]
    finally:
        engine.dispose()


def make_revision(
    message: str,
    *,
    database: DatabaseName = "chat",
    autogenerate: bool = False,
    db_path: str | Path | None = None,
) -> None:
    if autogenerate and db_path is None:
        raise ValueError("autogenerate requires db_path")
    path = Path(db_path).expanduser() if db_path else Path("/tmp/agentforge_alembic_dummy.db")
    cfg = _alembic_config(SPECS[database], path)
    command.revision(cfg, message=message, autogenerate=autogenerate)


def resolve_db_path(database: DatabaseName, explicit: str | Path | None = None) -> Path:
    """Resolve SQLite path from CLI arg, env, or config.yaml."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    env_key = {
        "chat": "AGENTFORGE_CHAT_DB",
        "prompt_lab": "AGENTFORGE_PROMPT_LAB_DB",
    }[database]
    env = os.environ.get(env_key)
    if env:
        return Path(env).expanduser().resolve()

    rel = SPECS[database].default_rel_path
    try:
        cfg_path = _PROJECT_ROOT / "config.yaml"
        if cfg_path.exists() and database == "chat":
            with open(cfg_path) as fh:
                cfg = yaml.safe_load(fh) or {}
            rel = cfg.get("web", {}).get("database_path", rel)
    except Exception:
        pass
    return (_PROJECT_ROOT / rel).resolve()


def upgrade_all(
    *,
    chat_db: str | Path | None = None,
    prompt_lab_db: str | Path | None = None,
    include_prompt_lab: bool = True,
) -> dict[str, str | None]:
    """Upgrade every known database. Returns map of database → current revision."""
    results: dict[str, str | None] = {}
    chat_path = resolve_db_path("chat", chat_db)
    upgrade(chat_path, database="chat")
    results["chat"] = current(chat_path, database="chat")

    if include_prompt_lab:
        pl_path = resolve_db_path("prompt_lab", prompt_lab_db)
        upgrade(pl_path, database="prompt_lab")
        results["prompt_lab"] = current(pl_path, database="prompt_lab")
    return results
