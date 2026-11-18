"""sql_extract_schema — full database schema extraction with Redis caching.

Queries ``information_schema`` (or ``pg_catalog``) to gather the complete schema
of a configured database: tables, columns with types/nullability/defaults,
primary keys, foreign keys, indexes, and views.  The full schema is stored
in Redis as JSON for follow-up queries; a compact human-readable summary
is returned to the LLM so it fits comfortably in the context window.

Cache lifecycle is managed via the Memory Settings.
A logged-in user reads and writes only their own keys. A cache written
before that split is adopted by the admin user on read.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .filesystem import write_file
from .registry import tool

logger = logging.getLogger(__name__)

_SCHEMA_CACHE_PREFIX = "schema_cache"
_CACHE_DISABLED_KEY = "schema_cache:_disabled"


def _query_all(engine, query: str) -> list[dict]:
    """Execute a query and return all rows as dicts (no row limit)."""
    from sqlalchemy import text

    with engine.connect() as conn:
        result = conn.execute(text(query))
        keys = list(result.keys())
        return [dict(zip(keys, row, strict=False)) for row in result.fetchall()]


def _normalise_keys(rows: list[dict]) -> list[dict]:
    """Normalise dict keys to UPPER_CASE for cross-dialect compatibility."""
    out = []
    for row in rows:
        out.append({str(k).upper(): v for k, v in row.items()})
    return out


def _assemble_schema(
    cols: list[dict],
    fks: list[dict],
    indexes: list[dict],
    views: list[dict],
    row_counts: list[dict],
    dialect: str,
) -> dict:
    """Assemble raw query results into a structured schema dict."""
    cols = _normalise_keys(cols)
    fks = _normalise_keys(fks)
    indexes = _normalise_keys(indexes)
    views = _normalise_keys(views)
    row_counts = _normalise_keys(row_counts)

    tables: dict[str, dict] = {}
    for col in cols:
        tname = col.get("TABLE_NAME") or ""
        cname = col.get("COLUMN_NAME") or ""
        if not tname or not cname:
            continue
        t = tables.setdefault(
            tname,
            {
                "columns": [],
                "primary_keys": [],
                "foreign_keys": [],
                "indexes": [],
                "row_count": None,
            },
        )
        entry = {
            "name": cname,
            "type": col.get("COLUMN_TYPE") or "",
            "nullable": str(col.get("IS_NULLABLE", "")).upper() == "YES",
        }
        default = col.get("COLUMN_DEFAULT")
        if default is not None:
            entry["default"] = str(default)
        extra = col.get("EXTRA") or ""
        if extra:
            entry["extra"] = str(extra)
        t["columns"].append(entry)
        if str(col.get("COLUMN_KEY", "")).upper() == "PRI" and cname not in t["primary_keys"]:
            t["primary_keys"].append(cname)

    for fk in fks:
        tname = fk.get("TABLE_NAME") or ""
        if tname not in tables:
            continue
        tables[tname]["foreign_keys"].append(_fk_entry(fk))

    idx_map: dict[str, dict[str, dict]] = defaultdict(dict)
    for idx in indexes:
        tname = idx.get("TABLE_NAME") or ""
        iname = idx.get("INDEX_NAME") or ""
        if not tname or not iname:
            continue
        slot = idx_map[tname].setdefault(
            iname,
            {
                "name": iname,
                "unique": int(idx.get("NON_UNIQUE") or 1) == 0,
                "columns": [],
            },
        )
        cname = idx.get("COLUMN_NAME") or ""
        if cname and cname not in slot["columns"]:
            slot["columns"].append(cname)
    for tname, idxs in idx_map.items():
        if tname in tables:
            tables[tname]["indexes"] = list(idxs.values())

    for rc in row_counts:
        tname = rc.get("TABLE_NAME") or ""
        if tname in tables:
            try:
                tables[tname]["row_count"] = int(rc.get("TABLE_ROWS") or 0)
            except (TypeError, ValueError):
                tables[tname]["row_count"] = None

    view_list = []
    for v in views:
        view_list.append(
            {
                "name": v.get("VIEW_NAME") or v.get("TABLE_NAME") or "",
                "definition": (v.get("VIEW_DEFINITION") or "")[:500],
            }
        )

    total_columns = sum(len(t["columns"]) for t in tables.values())
    return {
        "dialect": dialect,
        "table_count": len(tables),
        "total_columns": total_columns,
        "view_count": len(view_list),
        "tables": tables,
        "views": view_list,
    }


def _fk_entry(fk: dict) -> dict:
    """One foreign-key column, plus constraint ordinal and referential actions."""
    entry = {
        "column": fk.get("COLUMN_NAME") or "",
        "ref_table": fk.get("REFERENCED_TABLE_NAME") or "",
        "ref_column": fk.get("REFERENCED_COLUMN_NAME") or "",
        "constraint": fk.get("CONSTRAINT_NAME") or "",
    }
    pos = fk.get("ORDINAL_POSITION")
    if pos is not None and pos != "":
        try:
            entry["position"] = int(pos)
        except (TypeError, ValueError):
            pass
    update_rule = fk.get("UPDATE_RULE")
    delete_rule = fk.get("DELETE_RULE")
    if update_rule:
        entry["update_rule"] = str(update_rule)
    if delete_rule:
        entry["delete_rule"] = str(delete_rule)
    return entry


def _extract_mysql_schema(engine) -> dict:
    """Extract full schema from a MySQL database."""
    cols = _query_all(
        engine,
        """
        SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE,
               COLUMN_KEY, COLUMN_DEFAULT, EXTRA
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
        ORDER BY TABLE_NAME, ORDINAL_POSITION
    """,
    )
    fks = _query_all(
        engine,
        """
        SELECT kcu.TABLE_NAME, kcu.COLUMN_NAME,
               kcu.REFERENCED_TABLE_NAME, kcu.REFERENCED_COLUMN_NAME,
               kcu.CONSTRAINT_NAME, kcu.ORDINAL_POSITION,
               rc.UPDATE_RULE, rc.DELETE_RULE
        FROM information_schema.KEY_COLUMN_USAGE kcu
        JOIN information_schema.REFERENTIAL_CONSTRAINTS rc
          ON rc.CONSTRAINT_SCHEMA = kcu.CONSTRAINT_SCHEMA
         AND rc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
         AND rc.TABLE_NAME = kcu.TABLE_NAME
        WHERE kcu.TABLE_SCHEMA = DATABASE()
          AND kcu.REFERENCED_TABLE_NAME IS NOT NULL
        ORDER BY kcu.TABLE_NAME, kcu.CONSTRAINT_NAME, kcu.ORDINAL_POSITION
    """,
    )
    indexes = _query_all(
        engine,
        """
        SELECT TABLE_NAME, INDEX_NAME, COLUMN_NAME,
               NON_UNIQUE, SEQ_IN_INDEX
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND INDEX_NAME != 'PRIMARY'
        ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX
    """,
    )
    views = _query_all(
        engine,
        """
        SELECT TABLE_NAME AS VIEW_NAME, VIEW_DEFINITION
        FROM information_schema.VIEWS
        WHERE TABLE_SCHEMA = DATABASE()
        ORDER BY TABLE_NAME
    """,
    )
    row_counts = _query_all(
        engine,
        """
        SELECT TABLE_NAME, TABLE_ROWS
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_TYPE = 'BASE TABLE'
    """,
    )
    return _assemble_schema(cols, fks, indexes, views, row_counts, "mysql")


def _extract_postgres_schema(engine) -> dict:
    """Extract full schema from a PostgreSQL database."""
    identity_clause = "WHEN FALSE THEN 'auto_increment'"
    try:
        ver_rows = _query_all(engine, "SHOW server_version_num")
        ver = int(list(ver_rows[0].values())[0]) if ver_rows else 0
        if ver >= 100000:
            identity_clause = "WHEN c.is_identity = 'YES' THEN 'auto_increment'"
    except Exception:
        pass

    cols = _query_all(
        engine,
        f"""
        SELECT c.table_name AS TABLE_NAME,
               c.column_name AS COLUMN_NAME,
               CASE
                   WHEN c.data_type = 'USER-DEFINED' THEN c.udt_name
                   WHEN c.data_type = 'character varying' THEN
                       CASE WHEN c.character_maximum_length IS NOT NULL
                            THEN 'varchar(' || c.character_maximum_length || ')'
                            ELSE 'varchar' END
                   WHEN c.data_type = 'character' THEN
                       CASE WHEN c.character_maximum_length IS NOT NULL
                            THEN 'char(' || c.character_maximum_length || ')'
                            ELSE 'char' END
                   WHEN c.data_type = 'numeric' THEN
                       CASE WHEN c.numeric_precision IS NOT NULL
                            THEN 'numeric(' || c.numeric_precision || ',' || COALESCE(c.numeric_scale, 0) || ')'
                            ELSE 'numeric' END
                   ELSE c.data_type
               END AS COLUMN_TYPE,
               c.is_nullable AS IS_NULLABLE,
               CASE WHEN pk.column_name IS NOT NULL THEN 'PRI' ELSE '' END AS COLUMN_KEY,
               c.column_default AS COLUMN_DEFAULT,
               CASE
                   WHEN c.column_default LIKE 'nextval(%%' THEN 'auto_increment'
                   {identity_clause}
                   ELSE ''
               END AS EXTRA
        FROM information_schema.columns c
        LEFT JOIN (
            SELECT kcu.table_name, kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.constraint_type = 'PRIMARY KEY'
              AND tc.table_schema = 'public'
        ) pk ON c.table_name = pk.table_name AND c.column_name = pk.column_name
        WHERE c.table_schema = 'public'
        ORDER BY c.table_name, c.ordinal_position
    """,
    )
    fks = _query_all(
        engine,
        """
        SELECT src.relname AS TABLE_NAME,
               att.attname AS COLUMN_NAME,
               ref.relname AS REFERENCED_TABLE_NAME,
               fatt.attname AS REFERENCED_COLUMN_NAME,
               con.conname AS CONSTRAINT_NAME,
               ord.ordinality AS ORDINAL_POSITION,
               CASE con.confupdtype
                   WHEN 'a' THEN 'NO ACTION'
                   WHEN 'r' THEN 'RESTRICT'
                   WHEN 'c' THEN 'CASCADE'
                   WHEN 'n' THEN 'SET NULL'
                   WHEN 'd' THEN 'SET DEFAULT'
               END AS UPDATE_RULE,
               CASE con.confdeltype
                   WHEN 'a' THEN 'NO ACTION'
                   WHEN 'r' THEN 'RESTRICT'
                   WHEN 'c' THEN 'CASCADE'
                   WHEN 'n' THEN 'SET NULL'
                   WHEN 'd' THEN 'SET DEFAULT'
               END AS DELETE_RULE
        FROM pg_constraint con
        JOIN pg_class src ON src.oid = con.conrelid
        JOIN pg_namespace ns ON ns.oid = src.relnamespace
        JOIN pg_class ref ON ref.oid = con.confrelid
        -- conkey/confkey stay paired. information_schema has no ordinal here,
        -- so a join to constraint_column_usage cross-matches composite keys.
        JOIN LATERAL unnest(con.conkey, con.confkey)
            WITH ORDINALITY AS ord(attnum, refattnum, ordinality) ON TRUE
        JOIN pg_attribute att
          ON att.attrelid = src.oid AND att.attnum = ord.attnum
        JOIN pg_attribute fatt
          ON fatt.attrelid = ref.oid AND fatt.attnum = ord.refattnum
        WHERE con.contype = 'f'
          AND ns.nspname = 'public'
        ORDER BY src.relname, con.conname, ord.ordinality
    """,
    )
    indexes = _query_all(
        engine,
        """
        SELECT t.relname AS TABLE_NAME,
               i.relname AS INDEX_NAME,
               a.attname AS COLUMN_NAME,
               CASE WHEN ix.indisunique THEN 0 ELSE 1 END AS NON_UNIQUE,
               array_position(ix.indkey, a.attnum) AS SEQ_IN_INDEX
        FROM pg_index ix
        JOIN pg_class t ON t.oid = ix.indrelid
        JOIN pg_class i ON i.oid = ix.indexrelid
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(ix.indkey)
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE n.nspname = 'public'
          AND NOT ix.indisprimary
        ORDER BY t.relname, i.relname
    """,
    )
    views = _query_all(
        engine,
        """
        SELECT table_name AS VIEW_NAME,
               view_definition AS VIEW_DEFINITION
        FROM information_schema.views
        WHERE table_schema = 'public'
        ORDER BY table_name
    """,
    )
    row_counts = _query_all(
        engine,
        """
        SELECT relname AS TABLE_NAME,
               reltuples::bigint AS TABLE_ROWS
        FROM pg_class
        JOIN pg_namespace ON pg_namespace.oid = relnamespace
        WHERE nspname = 'public' AND relkind = 'r'
    """,
    )
    return _assemble_schema(cols, fks, indexes, views, row_counts, "postgres")


def _shorten_type(ctype: str) -> str:
    """Abbreviate common SQL types to save space in compact output."""
    t = (ctype or "").lower().strip()
    if t.startswith("varchar("):
        return "vc" + t[8:].rstrip(")")
    if t.startswith("character varying("):
        return "vc" + t[18:].rstrip(")")
    if t.startswith("decimal(") or t.startswith("numeric("):
        return "dec" + t[t.index("(") :]
    if t.startswith("tinyint("):
        return "ti" + t[8:].rstrip(")")
    mapping = {
        "timestamp": "ts",
        "timestamp without time zone": "ts",
        "timestamp with time zone": "tstz",
        "datetime": "dt",
        "bigint": "big",
        "integer": "int",
        "int": "int",
        "text": "txt",
        "float": "fl",
        "double": "dbl",
        "double precision": "dbl",
        "boolean": "bool",
        "bool": "bool",
    }
    return mapping.get(t, t)


def _compact_schema(schema: dict) -> str:
    """Render the full schema as an ultra-compact text summary for the LLM."""
    lines: list[str] = []
    dialect = schema.get("dialect") or "unknown"
    tcount = schema.get("table_count") or 0
    ccount = schema.get("total_columns") or 0
    lines.append(f"Database schema ({dialect}) — {tcount} tables, {ccount} columns")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    if ccount <= 200:
        mode = "full"
        lines.append("Format: name*:type (* = NOT NULL, AI = auto_increment)")
    elif ccount <= 500:
        mode = "abbrev"
        lines.append(
            "Types: vc=varchar, dec=decimal, dt=datetime, ts=timestamp, big=bigint, ti=tinyint, txt=text, fl=float"
        )
    else:
        mode = "names"
        lines.append("Note: column names only (use execute_sql on a specific table for types)")

    for tname, tinfo in (schema.get("tables") or {}).items():
        row_count = tinfo.get("row_count")
        row_label = f" (~{row_count})" if row_count is not None else ""
        pks = tinfo.get("primary_keys") or []
        pk_part = f" PK:{','.join(pks)}" if pks else ""
        lines.append(f"\n{tname}{row_label}{pk_part}")

        col_parts = []
        for col in tinfo.get("columns") or []:
            name = col.get("name") or ""
            ctype = col.get("type") or ""
            nn = "" if col.get("nullable", True) else "*"
            ai = " AI" if "auto_increment" in str(col.get("extra", "")).lower() else ""
            if mode == "names":
                col_parts.append(name)
            elif mode == "abbrev":
                col_parts.append(f"{name}{nn}:{_shorten_type(ctype)}{ai}")
            else:
                col_parts.append(f"{name}{nn}:{ctype}{ai}")
        if col_parts:
            # wrap long column lists
            current = "  "
            for i, cp in enumerate(col_parts):
                addition = cp if i == 0 else f", {cp}"
                if len(current) + len(addition) > 100 and current.strip():
                    lines.append(current)
                    current = f"  {cp}"
                else:
                    current += addition
            if current.strip():
                lines.append(current)

        extras = []
        fk_parts = []
        for fk in tinfo.get("foreign_keys") or []:
            fk_parts.append(f"{fk.get('column')}→{fk.get('ref_table')}.{fk.get('ref_column')}")
        if fk_parts:
            extras.append("FK " + " | ".join(fk_parts))
        idx_parts = []
        for idx in tinfo.get("indexes") or []:
            cols = ",".join(idx.get("columns") or [])
            label = idx.get("name") or "idx"
            if idx.get("unique"):
                label = f"UQ {label}"
            idx_parts.append(f"{label}({cols})")
        if idx_parts:
            extras.append("IDX " + " | ".join(idx_parts))
        if extras:
            lines.append("  " + " | ".join(extras))

    views = schema.get("views") or []
    if views:
        lines.append("\nViews: " + ", ".join(v.get("name") or "" for v in views if v.get("name")))

    return "\n".join(lines).strip()


def _get_redis():
    """Get the shared Redis connection (from ResultStore or fresh)."""
    try:
        from web.server.result_store import get_result_store

        store = get_result_store()
        if store is not None and getattr(store, "_redis", None) is not None:
            return store._redis
    except Exception:
        pass
    try:
        import redis

        url = os.environ.get("REDIS_URL", "redis://localhost:6379")
        client = redis.from_url(url)
        client.ping()
        return client
    except Exception:
        return None


def _cache_owner() -> str | None:
    """Logged-in user, or None when auth is off and the cache stays shared."""
    try:
        from web.server.identity import current_user_id

        return current_user_id()
    except Exception:
        return None


def _admin_user_id() -> str:
    try:
        from web.server.identity import ADMIN_USER_ID

        return ADMIN_USER_ID
    except Exception:
        return "00000000-0000-0000-0000-000000000001"


def _key_text(key: object) -> str:
    if isinstance(key, bytes):
        return key.decode()
    return str(key)


def _cache_key(database: str, owner: str | None = None) -> str:
    """Redis key for one database. ``owner`` defaults to the logged-in user.

    No user (auth off) keeps the old ``schema_cache:{database}`` key.
    """
    if owner is None:
        owner = _cache_owner()
    if owner:
        return f"{_SCHEMA_CACHE_PREFIX}:{owner}:{database}"
    return f"{_SCHEMA_CACHE_PREFIX}:{database}"


def _disabled_key(owner: str | None = None) -> str:
    if owner is None:
        owner = _cache_owner()
    if owner:
        return f"{_SCHEMA_CACHE_PREFIX}:{owner}:_disabled"
    return _CACHE_DISABLED_KEY


def _parse_cache_key(key: str) -> tuple[str | None, str] | None:
    """Return ``(owner, database)`` for a schema payload. None for flag keys.

    A key with no user segment is the pre-split cache. ``owner`` is None then.
    """
    prefix = _SCHEMA_CACHE_PREFIX + ":"
    if not key.startswith(prefix):
        return None
    rest = key[len(prefix) :]
    if rest == "_disabled" or rest.endswith(":_disabled"):
        return None
    owner, sep, name = rest.partition(":")
    if not sep:
        if not rest or rest.startswith("_"):
            return None
        return None, rest
    if not name or name.startswith("_"):
        return None
    return owner, name


def _visible_cache(key_owner: str | None, actor: str | None) -> bool:
    if actor is None:
        return True
    if key_owner == actor:
        return True
    # The unscoped key is the admin's cache from before this split.
    return key_owner is None and actor == _admin_user_id()


def _decode_schema(raw: object) -> dict | None:
    if isinstance(raw, bytes):
        raw = raw.decode()
    if not isinstance(raw, str) or not raw:
        return None
    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        return None
    return loaded


def _read_schema(r, database: str, owner: str | None) -> dict | None:
    return _decode_schema(r.get(_cache_key(database, owner)))


def _adopt_legacy(r, database: str, actor: str) -> dict | None:
    """Move ``schema_cache:{database}`` onto the admin key. Other users never see it."""
    if actor != _admin_user_id():
        return None
    legacy_key = f"{_SCHEMA_CACHE_PREFIX}:{database}"
    schema = _decode_schema(r.get(legacy_key))
    if schema is None:
        return None
    r.set(_cache_key(database, actor), json.dumps(schema, default=str))
    r.delete(legacy_key)
    return schema


def _get_cached_schema(database: str) -> dict | None:
    """Load one schema. A logged-in user only sees their own cache."""
    r = _get_redis()
    if r is None:
        return None
    try:
        actor = _cache_owner()
        schema = _read_schema(r, database, actor)
        if schema is None and actor:
            schema = _adopt_legacy(r, database, actor)
        if schema is not None:
            logger.debug("Schema cache HIT for %s", database)
        return schema
    except Exception:
        logger.debug("Schema cache read failed for %s", database)
        return None


def _set_cached_schema(database: str, schema: dict) -> None:
    """Store schema under the current user's key. No expiry."""
    r = _get_redis()
    if r is None:
        return
    try:
        payload = dict(schema)
        payload["_cached_at"] = datetime.now(timezone.utc).isoformat()
        r.set(_cache_key(database), json.dumps(payload, default=str))
        logger.debug("Schema cached for %s (no TTL)", database)
    except Exception:
        logger.debug("Schema cache write failed for %s", database)


def get_all_cached_schemas() -> dict[str, dict]:
    """Cached schemas the current user can see.

    Auth off returns every key. The admin also adopts leftover unscoped keys.
    """
    r = _get_redis()
    if r is None:
        return {}
    out: dict[str, dict] = {}
    try:
        actor = _cache_owner()
        for key in r.keys(f"{_SCHEMA_CACHE_PREFIX}:*"):
            parsed = _parse_cache_key(_key_text(key))
            if parsed is None:
                continue
            key_owner, name = parsed
            if not _visible_cache(key_owner, actor):
                continue
            if key_owner is None and actor:
                schema = _adopt_legacy(r, name, actor)
            else:
                schema = _decode_schema(r.get(key))
            if schema is not None:
                out[name] = schema
    except Exception:
        logger.debug("Failed to list cached schemas")
    return out


def get_cached_schema(database: str) -> dict | None:
    """Public accessor for a single cached schema (for REST API)."""
    return _get_cached_schema(database)


def get_cached_schema_metadata(database: str) -> dict | None:
    """Return cache metadata without the full schema payload."""
    schema = _get_cached_schema(database)
    if not schema:
        return None
    return {
        "cached_at": schema.get("_cached_at"),
        "table_count": schema.get("table_count", 0),
        "total_columns": schema.get("total_columns", 0),
        "view_count": schema.get("view_count", 0),
    }


def clear_cached_schema(database: str) -> bool:
    """Delete the current user's cached schema. Returns True on success."""
    r = _get_redis()
    if r is None:
        return False
    try:
        actor = _cache_owner()
        r.delete(_cache_key(database, actor))
        if actor == _admin_user_id():
            r.delete(f"{_SCHEMA_CACHE_PREFIX}:{database}")
        logger.info("Cleared schema cache for %s", database)
        return True
    except Exception:
        logger.debug("Failed to clear schema cache for %s", database)
        return False


def clear_all_cached_schemas() -> int:
    """Delete cached schemas for the current user. The disable flag stays.

    Auth off clears every user's schema keys.
    """
    r = _get_redis()
    if r is None:
        return 0
    try:
        actor = _cache_owner()
        keys = []
        for key in r.keys(f"{_SCHEMA_CACHE_PREFIX}:*"):
            parsed = _parse_cache_key(_key_text(key))
            if parsed is None:
                continue
            key_owner, _name = parsed
            if actor is None or key_owner == actor or (key_owner is None and actor == _admin_user_id()):
                keys.append(key)
        if not keys:
            return 0
        removed = int(r.delete(*keys))
        logger.info("Cleared %d cached schema(s)", removed)
        return removed
    except Exception:
        logger.debug("Failed to clear all schema caches")
        return 0


def clear_schema_cache_for_user(user_id: str) -> None:
    """Drop one user's schema keys. Used when that user is deleted."""
    r = _get_redis()
    if r is None or not user_id:
        return
    try:
        prefix = f"{_SCHEMA_CACHE_PREFIX}:{user_id}:"
        keys = list(r.keys(f"{prefix}*"))
        if keys:
            r.delete(*keys)
    except Exception:
        logger.debug("Failed to clear schema cache for user %s", user_id)


def _flag_on(r, key: str) -> bool:
    val = r.get(key)
    return val in (b"1", "1", 1)


def is_cache_disabled() -> bool:
    """True when this user set 'always fetch fresh'.

    The admin also honours the old unscoped flag until it is cleared.
    """
    r = _get_redis()
    if r is None:
        return False
    try:
        actor = _cache_owner()
        if _flag_on(r, _disabled_key(actor)):
            return True
        if actor == _admin_user_id():
            return _flag_on(r, _CACHE_DISABLED_KEY)
        return False
    except Exception:
        return False


def set_cache_disabled(disabled: bool) -> None:
    """Set or clear 'always fetch fresh' for the current user."""
    r = _get_redis()
    if r is None:
        return
    try:
        actor = _cache_owner()
        key = _disabled_key(actor)
        if disabled:
            r.set(key, "1")
        else:
            r.delete(key)
        if actor == _admin_user_id():
            if disabled:
                r.set(_CACHE_DISABLED_KEY, "1")
            else:
                r.delete(_CACHE_DISABLED_KEY)
        logger.info("Schema cache disabled=%s", disabled)
    except Exception:
        logger.debug("Failed to set cache-disabled flag")


@tool(
    locality="local",
    hint=(
        "Use sql_extract_schema to get the COMPLETE schema of a database before "
        "writing any queries. Returns every table, column, primary key, foreign "
        "key, and index. For large databases (500+ columns), column types are "
        "omitted from the summary — use execute_sql with information_schema on a "
        "SPECIFIC table to get column types when needed. Schemas are cached in "
        "Redis with no expiry — pre-scanned via the Memory Settings modal and "
        "only refreshed on manual re-scan or when the user disables caching. "
        "ALWAYS call this first in @sql mode instead of querying information_schema "
        "manually — it returns all 1000+ columns at once without truncation."
    ),
)
def _load_schema(database: str, force_refresh: bool = False) -> tuple[dict | None, str | None, bool]:
    """Return ``(schema, error, from_cache)``. ``error`` is set when schema is None."""
    try:
        from app.services.db_service import db_service
    except ImportError:
        return None, "ERROR: Database service not available.", False

    if database not in db_service.available_databases:
        available = ", ".join(db_service.available_databases) or "(none configured)"
        return (
            None,
            f"ERROR: Database '{database}' is not configured.\nAvailable databases: {available}",
            False,
        )

    if not force_refresh and not is_cache_disabled():
        cached = _get_cached_schema(database)
        if cached:
            return cached, None, True

    try:
        engine_type = db_service.get_engine_type(database)
    except Exception:
        engine_type = None
    engines = getattr(db_service, "_engines", {}) or {}
    engine = engines.get(database)
    if engine is None:
        getter = getattr(db_service, "get_engine", None)
        if callable(getter):
            try:
                engine = getter(database)
            except Exception:
                engine = None
    if engine is None:
        return None, f"ERROR: No engine available for '{database}'", False

    if not engine_type:
        engine_type = str(getattr(engine, "dialect", None) and engine.dialect.name or "mysql")

    logger.info("sql_extract_schema(%s): extracting full schema (%s)", database, engine_type)
    try:
        if engine_type in ("mysql", "mariadb"):
            schema = _extract_mysql_schema(engine)
        elif engine_type in ("postgres", "postgresql"):
            schema = _extract_postgres_schema(engine)
        else:
            return (
                None,
                f"ERROR: Unsupported engine type '{engine_type}'. Supported: mysql, postgres.",
                False,
            )
    except Exception as e:
        logger.exception("Schema extraction failed for %s", database)
        return None, f"ERROR: Schema extraction failed: {e}", False

    _set_cached_schema(database, schema)
    return schema, None, False


def sql_extract_schema(database: str, force_refresh: bool = False) -> str:
    """Extract the complete schema of a database — tables, columns, types, keys, indexes, views.

    database: Logical database name matching a configured database
    force_refresh: Set to true to bypass cache and re-extract (default: false)
    """
    schema, error, from_cache = _load_schema(database, force_refresh)
    if error or schema is None:
        return error or "ERROR: Schema extraction failed."
    compact = _compact_schema(schema)
    logger.info(
        "sql_extract_schema(%s): %d tables, %d columns, compact=%d chars%s",
        database,
        schema.get("table_count", 0),
        schema.get("total_columns", 0),
        len(compact),
        " cached" if from_cache else "",
    )
    if from_cache:
        return f"[cached] {compact}"
    return compact


def _md_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


_MISSING_ACTIONS_NOTE = (
    "ON UPDATE / ON DELETE were not in this schema cache. Pass force_refresh=true to sql_save_schema to load them."
)


def _base_tables(schema: dict) -> dict[str, dict]:
    """Tables only. Views also show up in information_schema.COLUMNS."""
    view_names = {v.get("name") for v in (schema.get("views") or []) if v.get("name")}
    return {name: info for name, info in (schema.get("tables") or {}).items() if name not in view_names}


def _group_foreign_keys(fks: list[dict]) -> list[dict]:
    """Collapse per-column FK rows into one entry per constraint, in key order."""
    groups: dict[str, dict[str, str]] = {}
    pair_lists: dict[str, list[tuple[int, int, str, str]]] = {}
    order: list[str] = []
    for i, fk in enumerate(fks):
        constraint = str(fk.get("constraint") or "")
        key = constraint or f"#{i}"
        slot = groups.get(key)
        if slot is None:
            slot = {
                "constraint": constraint,
                "ref_table": str(fk.get("ref_table") or ""),
                "update_rule": str(fk.get("update_rule") or ""),
                "delete_rule": str(fk.get("delete_rule") or ""),
            }
            groups[key] = slot
            pair_lists[key] = []
            order.append(key)
        if not slot["update_rule"]:
            slot["update_rule"] = str(fk.get("update_rule") or "")
        if not slot["delete_rule"]:
            slot["delete_rule"] = str(fk.get("delete_rule") or "")
        pos = fk.get("position")
        try:
            pos_n = int(pos) if pos is not None and pos != "" else 10**9
        except (TypeError, ValueError):
            pos_n = 10**9
        pair_lists[key].append((pos_n, i, str(fk.get("column") or ""), str(fk.get("ref_column") or "")))
    grouped: list[dict] = []
    for key in order:
        out: dict = dict(groups[key])
        pairs = sorted(pair_lists[key])
        out["columns"] = [col for _, _, col, _ in pairs]
        out["ref_columns"] = [ref for _, _, _, ref in pairs]
        grouped.append(out)
    return grouped


def _declared_foreign_keys(schema: dict) -> list[tuple[str, dict, dict]]:
    """(child table, child info, grouped constraint) for every declared FK."""
    found = []
    for tname, tinfo in (schema.get("tables") or {}).items():
        for group in _group_foreign_keys(tinfo.get("foreign_keys") or []):
            if group["columns"]:
                found.append((tname, tinfo, group))
    return found


def _referential_actions_missing(schema: dict) -> bool:
    for _tname, _tinfo, group in _declared_foreign_keys(schema):
        if not group.get("update_rule") or not group.get("delete_rule"):
            return True
    return False


def _backticked(names: list[str]) -> str:
    return ", ".join(f"`{name}`" for name in names)


def _pk_text(pks: list[str]) -> str:
    if not pks:
        return "(none)"
    return "(" + _backticked(pks) + ")"


def _action_text(group: dict) -> str:
    bits = []
    if group.get("update_rule"):
        bits.append(f"ON UPDATE {group['update_rule']}")
    if group.get("delete_rule"):
        bits.append(f"ON DELETE {group['delete_rule']}")
    return " ".join(bits)


def _fk_phrase(child: str, group: dict) -> str:
    """`child` (`cols`) → `parent` (`cols`), plus referential actions when known."""
    text = f"`{child}` ({_backticked(group['columns'])}) → `{group['ref_table']}` ({_backticked(group['ref_columns'])})"
    action = _action_text(group)
    if action:
        text = f"{text} {action}"
    return text


def _fk_bullet(child: str, group: dict) -> str:
    phrase = _fk_phrase(child, group)
    if group.get("constraint"):
        return f"- `{group['constraint']}`: {phrase}"
    return f"- {phrase}"


def _cardinality(table: dict, columns: list[str]) -> str:
    """required/optional from nullability; at most one when the FK columns are unique."""
    by_name = {c.get("name"): c for c in (table.get("columns") or [])}
    required = bool(columns) and all(not (by_name.get(name) or {}).get("nullable", True) for name in columns)
    fk_set = set(columns)
    pks = table.get("primary_keys") or []
    unique = bool(pks) and set(pks) <= fk_set
    if not unique:
        for idx in table.get("indexes") or []:
            icols = idx.get("columns") or []
            if idx.get("unique") and icols and set(icols) <= fk_set:
                unique = True
                break
    need = "required" if required else "optional"
    card = "at most one" if unique else "many"
    return f"{need}, {card}"


def _longest_id_suffix(column: str, names: list[str]) -> str | None:
    """Longest real table name T where the column is `{T}_id` or ends in `_{T}_id`."""
    if not column.endswith("_id"):
        return None
    best = ""
    for name in names:
        if not name:
            continue
        if column == f"{name}_id" or column.endswith(f"_{name}_id"):
            if len(name) > len(best):
                best = name
    return best or None


def _exact_table_name(column: str, tables: dict[str, dict]) -> str | None:
    if column in tables:
        return column
    lowered = column.lower()
    hits = [name for name in tables if name.lower() == lowered]
    if len(hits) == 1:
        return hits[0]
    return None


def _match_why(via: str, column: str, target: str, pks: list[str], kind: str) -> str:
    pk = _pk_text(pks)
    if via == "parent":
        return "column is `parent_id` and this table's primary key is (`id`); usual self-reference, not declared"
    if via == "plural":
        singular = column[:-3] if column.endswith("_id") else column
        reason = f"name is `{column}`; `{target}` is the plural of `{singular}`; target primary key is {pk}"
    elif via == "suffix":
        reason = f"name ends with `{target}_id`; target primary key is {pk}"
    else:
        reason = f"column name is the table `{target}`; target primary key is {pk}"
    if kind == "partial":
        reason += "; this column alone does not identify a row"
    return reason


def _is_likely(via: str, column: str, pks: list[str]) -> bool:
    if via == "parent":
        return True
    if via in ("suffix", "plural"):
        return pks == ["id"]
    return pks == ["id"] or pks == [column]


def _match_column(
    table_name: str,
    column: str,
    own_pks: list[str],
    tables: dict[str, dict],
) -> dict | None:
    """Name-only relation. Declared FK columns are filtered by the caller."""
    via = ""
    target = ""
    if column == "parent_id" and own_pks == ["id"]:
        via, target = "parent", table_name
    else:
        names = list(tables)
        suffix = _longest_id_suffix(column, names)
        if suffix:
            via, target = "suffix", suffix
        elif column.endswith("_id"):
            singular = column[:-3]
            plural = f"{singular}s"
            # Exact `{singular}_id` only. `demarcation_point_port_id` is not `port_id`.
            if singular and column == f"{singular}_id" and plural in tables:
                via, target = "plural", plural
        if not target:
            exact = _exact_table_name(column, tables)
            if exact and exact != table_name:
                via, target = "exact", exact
    if not target or not via:
        return None
    # The table's own primary key is not a relation to itself.
    if target == table_name and column in own_pks and via != "parent":
        return None
    pks = list((tables.get(target) or {}).get("primary_keys") or [])
    kind = "likely" if _is_likely(via, column, pks) else "partial"
    return {
        "table": table_name,
        "column": column,
        "target": target,
        "target_pks": pks,
        "kind": kind,
        "why": _match_why(via, column, target, pks, kind),
    }


def _name_matches(schema: dict) -> list[dict]:
    tables = _base_tables(schema)
    found = []
    for tname, tinfo in tables.items():
        declared = {fk.get("column") for fk in (tinfo.get("foreign_keys") or []) if fk.get("column")}
        pks = list(tinfo.get("primary_keys") or [])
        for col in tinfo.get("columns") or []:
            name = col.get("name") or ""
            if not name or name in declared:
                continue
            match = _match_column(tname, name, pks, tables)
            if match:
                found.append(match)
    found.sort(key=lambda row: (0 if row["kind"] == "likely" else 1, row["table"], row["column"]))
    return found


def _unmatched_id_columns(schema: dict, matched: set[tuple[str, str]]) -> list[dict]:
    """`_id` columns with no declared FK and no name match. Own PK columns are skipped."""
    rows = []
    for tname, tinfo in _base_tables(schema).items():
        declared = {fk.get("column") for fk in (tinfo.get("foreign_keys") or []) if fk.get("column")}
        pks = set(tinfo.get("primary_keys") or [])
        for col in tinfo.get("columns") or []:
            name = col.get("name") or ""
            if not name.endswith("_id"):
                continue
            if name in declared or name in pks or (tname, name) in matched:
                continue
            rows.append(
                {
                    "table": tname,
                    "column": name,
                    "type": col.get("type") or "",
                    "nullable": bool(col.get("nullable", True)),
                }
            )
    return rows


def _relation_lines(schema: dict, matches: list[dict], unmatched: list[dict]) -> list[str]:
    declared = _declared_foreign_keys(schema)
    lines = [
        "## Relations",
        "",
        "### Declared foreign keys",
        "",
    ]
    if not declared:
        lines.append("None.")
        lines.append("")
    else:
        lines.append(f"{len(declared)} foreign keys.")
        lines.append("")
        if _referential_actions_missing(schema):
            lines.append(_MISSING_ACTIONS_NOTE)
            lines.append("")
        lines.append("| Constraint | From | To | On update | On delete | Cardinality |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        ordered = sorted(declared, key=lambda item: (item[0], item[2].get("constraint") or ""))
        for tname, tinfo, group in ordered:
            lines.append(
                "| {constraint} | {src} | {dst} | {update} | {delete} | {card} |".format(
                    constraint=_md_cell(f"`{group['constraint']}`" if group.get("constraint") else ""),
                    src=_md_cell(f"`{tname}` ({_backticked(group['columns'])})"),
                    dst=_md_cell(f"`{group['ref_table']}` ({_backticked(group['ref_columns'])})"),
                    update=_md_cell(group.get("update_rule") or ""),
                    delete=_md_cell(group.get("delete_rule") or ""),
                    card=_md_cell(_cardinality(tinfo, group["columns"])),
                )
            )
        lines.append("")

    lines.extend(
        [
            "### Name-matched columns",
            "",
            "These columns are not part of a declared foreign key. The match is by name only.",
            "",
            "- **likely** — `{table}_id`, a `_{table}_id` suffix (longest real table name), "
            "`{singular}_id` when `{singular}s` exists, or the column is another table's name. "
            "The target is identified by `id` or by the same column. "
            "`parent_id` on a table whose primary key is `id` is a self-reference.",
            "- **partial** — the name matches a table, but that primary key is composite, "
            "named differently, or missing. The column alone is not a key.",
            "",
        ]
    )
    if not matches:
        lines.append("None.")
        lines.append("")
    else:
        lines.append("| Kind | Column | May correspond to | Why |")
        lines.append("| --- | --- | --- | --- |")
        for row in matches:
            lines.append(
                "| {kind} | {column} | {target} | {why} |".format(
                    kind=row["kind"],
                    column=_md_cell(f"`{row['table']}`.`{row['column']}`"),
                    target=_md_cell(f"`{row['target']}` {_pk_text(row['target_pks'])}"),
                    why=_md_cell(row["why"]),
                )
            )
        lines.append("")

    lines.extend(
        [
            "### `_id` columns with no declared foreign key and no name match",
            "",
            "A column that is part of its own table's primary key is not listed.",
            "",
        ]
    )
    if not unmatched:
        lines.append("None.")
        lines.append("")
    else:
        lines.append("| Table | Column | Type | Null |")
        lines.append("| --- | --- | --- | --- |")
        for row in unmatched:
            lines.append(
                "| {table} | {column} | {type} | {null} |".format(
                    table=_md_cell(f"`{row['table']}`"),
                    column=_md_cell(f"`{row['column']}`"),
                    type=_md_cell(row["type"]),
                    null="yes" if row["nullable"] else "no",
                )
            )
        lines.append("")
    return lines


def _schema_markdown(database: str, schema: dict) -> str:
    """Full schema as Markdown. Types stay even when the compact summary drops them."""
    dialect = schema.get("dialect") or "unknown"
    matches = _name_matches(schema)
    matched_keys = {(row["table"], row["column"]) for row in matches}
    unmatched = _unmatched_id_columns(schema, matched_keys)
    lines = [
        f"# {database} schema",
        "",
        f"- Dialect: {dialect}",
        f"- Tables: {schema.get('table_count') or 0}",
        f"- Columns: {schema.get('total_columns') or 0}",
        f"- Views: {schema.get('view_count') or 0}",
        "",
    ]
    lines.extend(_relation_lines(schema, matches, unmatched))

    incoming: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for tname, _tinfo, group in _declared_foreign_keys(schema):
        if group.get("ref_table"):
            incoming[group["ref_table"]].append((tname, group))

    tables = schema.get("tables") or {}
    matches_by: dict[tuple[str, str], dict] = {(row["table"], row["column"]): row for row in matches}
    for tname, tinfo in tables.items():
        lines.append(f"## {tname}")
        lines.append("")
        row_count = tinfo.get("row_count")
        if row_count is not None:
            lines.append(f"Approximate rows: {row_count}")
            lines.append("")
        pks = tinfo.get("primary_keys") or []
        lines.append("Primary key: " + (", ".join(f"`{pk}`" for pk in pks) if pks else "none"))
        lines.append("")
        lines.append("| Column | Type | Nullable | Default | Extra |")
        lines.append("| --- | --- | --- | --- | --- |")
        for col in tinfo.get("columns") or []:
            lines.append(
                "| {name} | {type} | {nullable} | {default} | {extra} |".format(
                    name=_md_cell(col.get("name")),
                    type=_md_cell(col.get("type")),
                    nullable="yes" if col.get("nullable", True) else "no",
                    default=_md_cell(col.get("default")),
                    extra=_md_cell(col.get("extra")),
                )
            )
        lines.append("")
        lines.append("Foreign keys:")
        lines.append("")
        groups = _group_foreign_keys(tinfo.get("foreign_keys") or [])
        if groups:
            for group in groups:
                lines.append(_fk_bullet(tname, group))
        else:
            lines.append("None.")
        lines.append("")
        lines.append("Referenced by:")
        lines.append("")
        refs = incoming.get(tname) or []
        if refs:
            for child, group in refs:
                lines.append(_fk_bullet(child, group))
        else:
            lines.append("None.")
        lines.append("")
        table_matches = []
        for col in tinfo.get("columns") or []:
            match = matches_by.get((tname, col.get("name") or ""))
            if match:
                table_matches.append(match)
        if table_matches:
            lines.append("Name matches (not enforced):")
            lines.append("")
            lines.append("| Column | Target | Kind | Why |")
            lines.append("| --- | --- | --- | --- |")
            for row in table_matches:
                lines.append(
                    "| {column} | {target} | {kind} | {why} |".format(
                        column=_md_cell(f"`{row['column']}`"),
                        target=_md_cell(f"`{row['target']}` {_pk_text(row['target_pks'])}"),
                        kind=row["kind"],
                        why=_md_cell(row["why"]),
                    )
                )
            lines.append("")
        indexes = tinfo.get("indexes") or []
        if indexes:
            lines.append("Indexes:")
            lines.append("")
            for idx in indexes:
                cols = ", ".join(f"`{c}`" for c in (idx.get("columns") or []))
                unique = " unique" if idx.get("unique") else ""
                lines.append(f"- `{idx.get('name')}`{unique} ({cols})")
            lines.append("")

    views = [v for v in (schema.get("views") or []) if v.get("name")]
    if views:
        lines.append("## Views")
        lines.append("")
        for view in views:
            lines.append(f"### {view.get('name')}")
            lines.append("")
            definition = (view.get("definition") or "").strip()
            if definition:
                lines.append("```sql")
                lines.append(definition)
                lines.append("```")
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


@tool(
    locality="local",
    hint=(
        "Use sql_save_schema when the user wants the database schema written to a "
        "file. It writes the Markdown itself. Do not rebuild the schema in the "
        "reply or pass it as write_file content — that hits the output token cap."
    ),
)
def sql_save_schema(database: str, path: str, force_refresh: bool = False) -> str:
    """Write the full schema to a Markdown file and return a short confirmation.

    Use this instead of write_file when the user wants tables, columns, primary
    keys, foreign keys (grouped, with referential actions), name-matched
    relations, and indexes saved. If path is a directory, the file is
    ``<database>-schema.md`` inside it.

    database: Logical database name matching a configured database
    path: File path, or a directory such as ~/Downloads
    force_refresh: Set to true to bypass cache and re-extract (default: false)
    """
    schema, error, from_cache = _load_schema(database, force_refresh)
    if error or schema is None:
        return error or "ERROR: Schema extraction failed."

    target = Path(path).expanduser()
    # A directory, a trailing slash, or a path with no suffix (such as ~/Downloads).
    if path.endswith(("/", os.sep)) or target.suffix == "" or (target.exists() and target.is_dir()):
        target = target / f"{database}-schema.md"
    markdown = _schema_markdown(database, schema)
    written = write_file(str(target), markdown)
    if written.lower().startswith("error"):
        return written
    cached = " (cached schema)" if from_cache else ""
    actions = ""
    if _referential_actions_missing(schema):
        actions = " " + _MISSING_ACTIONS_NOTE
    return (
        f"{written}{cached}. "
        f"{schema.get('table_count', 0)} tables, "
        f"{schema.get('total_columns', 0)} columns, "
        f"{schema.get('view_count', 0)} views. "
        "Do not repeat the schema in the reply."
        f"{actions}"
    )


def register_sql_schema_tools(registry) -> int:
    """Register schema tools. Skips names that are already registered."""
    count = 0
    for func in (sql_extract_schema, sql_save_schema):
        if func.__name__ in registry:
            continue
        registry.register(func, category="Database")
        count += 1
    return count
