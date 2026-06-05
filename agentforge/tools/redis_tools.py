"""Redis inspection tools - read-only introspection of the Redis server.

Provides server info, key scanning, value reading, DB size analysis,
and memory diagnostics.  All actions are strictly read-only - no SET,
DEL, FLUSHDB, or any write commands.

Redis URL is read from the ``REDIS_URL`` environment variable
(same source as ``tool_cache.py`` and ``pipeline_tools.py``):
  - Docker (agentforge-web): REDIS_URL=redis://redis:6379
  - Native worker: defaults to redis://localhost:6379

Usage::

    from agentforge.tools import ToolRegistry
    from agentforge.tools.redis_tools import register_redis_tools

    registry = ToolRegistry()
    register_redis_tools(registry)
"""

from __future__ import annotations

import logging
import os
import time
from collections import Counter
from typing import TYPE_CHECKING

from .registry import tool

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = logging.getLogger(__name__)

_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")


# ---------------------------------------------------------------------------
# Lazy connection
# ---------------------------------------------------------------------------


def _get_client():
    """Create a sync Redis client with fast timeouts."""
    try:
        import redis
    except ImportError:
        raise RuntimeError("redis package is not installed. Run: pip install 'redis[hiredis]>=5.0.0'")

    client = redis.from_url(
        _REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=5,
    )
    # Quick connectivity check
    client.ping()
    return client


def _human_size(size_bytes: int | float) -> str:
    """Convert bytes to a human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}" if unit != "B" else f"{int(size_bytes)} B"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def _format_ttl(ttl_seconds: int) -> str:
    """Format TTL to a human string."""
    if ttl_seconds == -1:
        return "persistent"
    if ttl_seconds == -2:
        return "(expired)"
    if ttl_seconds < 60:
        return f"{ttl_seconds}s"
    if ttl_seconds < 3600:
        m, s = divmod(ttl_seconds, 60)
        return f"{m}m {s:02d}s"
    h, rem = divmod(ttl_seconds, 3600)
    m = rem // 60
    return f"{h}h {m:02d}m"


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

_VALID_ACTIONS = ("info", "keys", "get", "dbsize", "memory")


def _action_info() -> str:
    """Server summary: version, uptime, memory, keyspace, persistence."""
    client = _get_client()

    server = client.info("server")
    memory = client.info("memory")
    keyspace = client.info("keyspace")
    persistence = client.info("persistence")
    clients_info = client.info("clients")

    # Version & uptime
    version = server.get("redis_version", "?")
    uptime_s = server.get("uptime_in_seconds", 0)
    days, rem = divmod(int(uptime_s), 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    uptime_str = ""
    if days:
        uptime_str += f"{days}d "
    if hours:
        uptime_str += f"{hours}h "
    uptime_str += f"{mins}m"

    # Clients
    connected = clients_info.get("connected_clients", "?")
    max_clients = server.get("maxclients", "?")

    # Memory
    used = memory.get("used_memory", 0)
    peak = memory.get("used_memory_peak", 0)
    rss = memory.get("used_memory_rss", 0)
    frag = memory.get("mem_fragmentation_ratio", 0)
    policy = server.get("maxmemory_policy", memory.get("maxmemory_policy", "?"))

    frag_warn = ""
    if isinstance(frag, (int, float)) and frag > 1.5:
        frag_warn = " (!) high (>1.5)"

    # Keyspace
    ks_lines = []
    for db_name, db_info in sorted(keyspace.items()):
        if isinstance(db_info, dict):
            keys_count = db_info.get("keys", 0)
            expires = db_info.get("expires", 0)
            ks_lines.append(f"{db_name}: {keys_count:,} keys ({expires:,} with TTL)")
        else:
            ks_lines.append(f"{db_name}: {db_info}")

    if not ks_lines:
        ks_lines.append("(empty - no keys)")

    # Persistence
    rdb_last = persistence.get("rdb_last_save_time", 0)
    rdb_status = persistence.get("rdb_last_bgsave_status", "?")
    aof_enabled = persistence.get("aof_enabled", 0)

    rdb_ago = ""
    if rdb_last:
        elapsed = int(time.time() - rdb_last)
        if elapsed < 3600:
            rdb_ago = f"last save {elapsed // 60}m ago"
        elif elapsed < 86400:
            rdb_ago = f"last save {elapsed // 3600}h ago"
        else:
            rdb_ago = f"last save {elapsed // 86400}d ago"

    lines = [
        "Redis Server Info\n",
        f"Version:    {version}",
        f"Uptime:     {uptime_str}",
        f"Clients:    {connected} connected (max: {max_clients})",
        "",
        "Memory",
        f"Used:       {_human_size(used)} (peak: {_human_size(peak)})",
        f"RSS:        {_human_size(rss)}",
        f"Frag ratio: {frag}{frag_warn}",
        f"Policy:     {policy}",
        "",
        "Keyspace",
    ]
    lines.extend(ks_lines)
    lines.append("")
    lines.append("Persistence")
    lines.append(f"RDB:        {rdb_ago}, {rdb_status}")
    lines.append(f"AOF:        {'enabled' if aof_enabled else 'disabled'}")

    return "\n".join(lines)


def _action_keys(pattern: str, count: int) -> str:
    """Scan keys matching pattern with type, TTL, and size."""
    client = _get_client()
    count = max(1, min(count, 100))

    matched = []
    cursor = 0

    while len(matched) < count:
        cursor, batch = client.scan(cursor=cursor, match=pattern, count=200)
        matched.extend(batch)
        if cursor == 0:
            break

    # Deduplicate (SCAN can return duplicates)
    matched = list(dict.fromkeys(matched))[:count]

    if not matched:
        return f'No keys matching "{pattern}".'

    # Gather metadata for each key
    rows = []
    for key in matched:
        try:
            ktype = client.type(key)
        except Exception:
            ktype = "?"

        try:
            ttl = client.ttl(key)
            ttl_str = _format_ttl(ttl)
        except Exception:
            ttl_str = "?"

        try:
            mem = client.memory_usage(key)
            size_str = _human_size(mem) if mem else "?"
        except Exception:
            size_str = "(n/a)"

        rows.append((key, ktype, ttl_str, size_str, mem or 0))

    # Sort by size descending
    rows.sort(key=lambda r: r[4], reverse=True)

    # Count total matches (approximate from scan)
    total_cursor = 0
    total_count = 0
    while True:
        total_cursor, batch = client.scan(cursor=total_cursor, match=pattern, count=500)
        total_count += len(batch)
        if total_cursor == 0 or total_count > 10_000:
            break

    total_label = f"{total_count:,}" if total_count <= 10_000 else "10,000+"

    lines = [f'Keys matching "{pattern}" (showing {len(rows)} of {total_label})\n']

    # Header
    max_key_len = max(len(r[0]) for r in rows)
    max_key_len = min(max_key_len, 50)
    lines.append(f"{'key':<{max_key_len}}  {'type':<8} {'TTL':<12} {'size':>10}")
    lines.append(f"{'-' * max_key_len}  {'-' * 8} {'-' * 12} {'-' * 10}")

    total_size = 0
    for key, ktype, ttl_str, size_str, mem in rows:
        display_key = key if len(key) <= 50 else key[:47] + "..."
        lines.append(f"{display_key:<{max_key_len}}  {ktype:<8} {ttl_str:<12} {size_str:>10}")
        total_size += mem

    if total_size:
        lines.append(f"\nTotal matched: {total_label} keys (~{_human_size(total_size)} shown)")

    return "\n".join(lines)


def _action_get(key: str) -> str:
    """Read a specific key with type-aware display."""
    client = _get_client()

    if not client.exists(key):
        return f'Error: key "{key}" does not exist'

    ktype = client.type(key)
    ttl = client.ttl(key)
    ttl_str = _format_ttl(ttl)

    try:
        mem = client.memory_usage(key)
        size_str = _human_size(mem) if mem else "?"
    except Exception:
        mem = None
        size_str = "?"

    encoding = "?"
    try:
        encoding = client.object("encoding", key) or "?"
    except Exception:
        pass

    lines = [
        f"Key: {key}\n",
        f"Type:     {ktype}",
        f"TTL:      {ttl_str}",
        f"Size:     {size_str}",
        f"Encoding: {encoding}",
        "",
        "Value",
    ]

    max_display = 10_000

    try:
        if ktype == "string":
            val = client.get(key) or ""
            if len(val) > max_display:
                lines.append(val[:max_display])
                lines.append(f"\n[truncated at {max_display:,} chars - full value is {len(val):,} chars]")
            else:
                lines.append(val)

        elif ktype == "list":
            length = client.llen(key)
            lines.append(f"Length: {length:,}")
            if length > 0:
                # Show first 5 and last 5
                head = client.lrange(key, 0, 4)
                lines.append("\nFirst items:")
                for i, item in enumerate(head):
                    preview = item[:200] if len(item) > 200 else item
                    lines.append(f"  [{i}] {preview}")
                if length > 10:
                    tail = client.lrange(key, -5, -1)
                    lines.append(f"\nLast items (of {length:,}):")
                    for i, item in enumerate(tail):
                        idx = length - 5 + i
                        preview = item[:200] if len(item) > 200 else item
                        lines.append(f"  [{idx}] {preview}")
                elif length > 5:
                    tail = client.lrange(key, 5, -1)
                    lines.append("\nRemaining:")
                    for i, item in enumerate(tail, 5):
                        preview = item[:200] if len(item) > 200 else item
                        lines.append(f"  [{i}] {preview}")

        elif ktype == "hash":
            hlen = client.hlen(key)
            lines.append(f"Fields: {hlen:,}")
            if hlen <= 50:
                all_fields = client.hgetall(key)
                for fname, fval in sorted(all_fields.items()):
                    preview = fval[:200] if len(fval) > 200 else fval
                    lines.append(f"  {fname}: {preview}")
            else:
                # Show first 50 via hscan
                cursor, batch = client.hscan(key, cursor=0, count=50)
                for fname, fval in sorted(batch.items()):
                    preview = fval[:200] if len(fval) > 200 else fval
                    lines.append(f"  {fname}: {preview}")
                lines.append(f"\n  ... and {hlen - len(batch)} more fields")

        elif ktype == "set":
            card = client.scard(key)
            lines.append(f"Cardinality: {card:,}")
            members = client.srandmember(key, min(card, 10))
            if members:
                lines.append("\nSample members:")
                for m in members:
                    preview = m[:200] if len(m) > 200 else m
                    lines.append(f"  - {preview}")
                if card > 10:
                    lines.append(f"  ... and {card - 10:,} more")

        elif ktype == "zset":
            card = client.zcard(key)
            lines.append(f"Cardinality: {card:,}")
            top = client.zrevrange(key, 0, 9, withscores=True)
            if top:
                lines.append("\nTop 10 by score:")
                for member, score in top:
                    preview = member[:200] if len(member) > 200 else member
                    lines.append(f"  {score:>10.2f}  {preview}")
                if card > 10:
                    lines.append(f"  ... and {card - 10:,} more")

        elif ktype == "stream":
            slen = client.xlen(key)
            lines.append(f"Length: {slen:,}")
            entries = client.xrevrange(key, count=5)
            if entries:
                lines.append("\nLast 5 entries:")
                for entry_id, fields in entries:
                    lines.append(f"  {entry_id}:")
                    for k, v in fields.items():
                        preview = v[:100] if len(v) > 100 else v
                        lines.append(f"    {k}: {preview}")

        else:
            lines.append(f"(unsupported type: {ktype})")

    except Exception as exc:
        lines.append(f"(error reading value: {exc})")

    return "\n".join(lines)


def _action_dbsize() -> str:
    """Total key count + prefix breakdown."""
    client = _get_client()

    total = client.dbsize()

    lines = [
        "Redis DB Size\n",
        f"Total keys: {total:,}",
    ]

    if total == 0:
        return "\n".join(lines)

    # Prefix analysis (scan up to 10K keys)
    prefix_counter: Counter = Counter()
    cursor = 0
    scanned = 0

    while scanned < 10_000:
        cursor, batch = client.scan(cursor=cursor, match="*", count=500)
        for key in batch:
            # Prefix = part before first ':'
            colon_idx = key.find(":")
            if colon_idx > 0:
                prefix = key[: colon_idx + 1]
            else:
                prefix = "(no prefix)"
            prefix_counter[prefix] += 1
            scanned += 1
        if cursor == 0:
            break

    if prefix_counter:
        lines.append("\nPrefix Breakdown")
        max_prefix = max(len(p) for p in prefix_counter)
        max_prefix = min(max_prefix, 30)
        lines.append(f"{'prefix':<{max_prefix}}  {'count':>6}  {'%':>6}")
        lines.append(f"{'-' * max_prefix}  {'-' * 6}  {'-' * 6}")

        for prefix, cnt in prefix_counter.most_common(20):
            pct = (cnt / scanned) * 100 if scanned else 0
            display_prefix = prefix if len(prefix) <= 30 else prefix[:27] + "..."
            lines.append(f"{display_prefix:<{max_prefix}}  {cnt:>6,}  {pct:>5.1f}%")

        if len(prefix_counter) > 20:
            lines.append(f"  ... and {len(prefix_counter) - 20} more prefixes")

    return "\n".join(lines)


def _action_memory() -> str:
    """Detailed memory breakdown with warnings."""
    client = _get_client()

    mem = client.info("memory")

    used = mem.get("used_memory", 0)
    used_human = mem.get("used_memory_human", _human_size(used))
    peak = mem.get("used_memory_peak", 0)
    peak_human = mem.get("used_memory_peak_human", _human_size(peak))
    rss = mem.get("used_memory_rss", 0)
    rss_human = mem.get("used_memory_rss_human", _human_size(rss))
    frag = mem.get("mem_fragmentation_ratio", 0)
    maxmem = mem.get("maxmemory", 0)
    maxmem_human = mem.get("maxmemory_human", _human_size(maxmem)) if maxmem else "unlimited"
    allocator = mem.get("mem_allocator", "?")
    overhead = mem.get("used_memory_overhead", 0)
    dataset = mem.get("used_memory_dataset", 0)

    lines = [
        "Redis Memory Analysis\n",
        f"Used memory:     {used_human}",
        f"Peak memory:     {peak_human}",
        f"RSS:             {rss_human}",
        f"Fragmentation:   {frag}",
        f"Max memory:      {maxmem_human}",
        f"Allocator:       {allocator}",
        "",
        f"Overhead:        {_human_size(overhead)}",
        f"Dataset:         {_human_size(dataset)}",
    ]

    # Warnings
    warnings = []
    if isinstance(frag, (int, float)) and frag > 1.5:
        warnings.append(f"(!) High fragmentation ({frag:.2f}x) - consider restarting Redis to reclaim memory")
    if maxmem and used and (used / maxmem) > 0.80:
        pct = (used / maxmem) * 100
        warnings.append(f"(!) Memory pressure: {pct:.0f}% of maxmemory used")

    # MEMORY DOCTOR (Redis 4.0+)
    try:
        doctor = client.execute_command("MEMORY", "DOCTOR")
        if doctor and doctor != "Sam, I have no memory problems":
            warnings.append(f"MEMORY DOCTOR: {doctor}")
    except Exception:
        pass

    if warnings:
        lines.append("\nWarnings")
        for w in warnings:
            lines.append(f"  {w}")

    # Top keys by memory (sample)
    try:
        total = client.dbsize()
        if total > 0:
            lines.append(f"\nLargest Keys (sampled from {total:,} keys)")

            key_sizes = []
            cursor = 0
            sampled = 0

            while sampled < 1_000:
                cursor, batch = client.scan(cursor=cursor, match="*", count=200)
                for key in batch:
                    try:
                        m = client.memory_usage(key)
                        if m:
                            key_sizes.append((key, m))
                    except Exception:
                        pass
                    sampled += 1
                if cursor == 0:
                    break

            key_sizes.sort(key=lambda x: x[1], reverse=True)
            for key, size in key_sizes[:10]:
                display_key = key if len(key) <= 50 else key[:47] + "..."
                lines.append(f"  {_human_size(size):>10}  {display_key}")
    except Exception as exc:
        lines.append(f"\n(could not sample key sizes: {exc})")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool entry point
# ---------------------------------------------------------------------------


@tool
def redis_inspect(action: str, key: str = "", pattern: str = "*", count: int = 25) -> str:
    """Inspect Redis state. Read-only - no mutations.

    action: one of: info, keys, get, dbsize, memory
    key: specific key to inspect (required for get action)
    pattern: glob pattern for keys action (default "*")
    count: max keys to return for keys action (default 25, max 100)
    """
    action = action.strip().lower()

    if action not in _VALID_ACTIONS:
        return f'Error: unknown action "{action}". Valid: {", ".join(_VALID_ACTIONS)}'

    if action == "get" and not key:
        return 'Error: "key" is required for action "get"'

    try:
        if action == "info":
            return _action_info()
        elif action == "keys":
            return _action_keys(pattern, count)
        elif action == "get":
            return _action_get(key)
        elif action == "dbsize":
            return _action_dbsize()
        elif action == "memory":
            return _action_memory()
        else:
            return f'Error: unknown action "{action}"'
    except Exception as exc:
        exc_str = str(exc).lower()
        if "connect" in exc_str or "refused" in exc_str or "timeout" in exc_str:
            return f"Error: cannot connect to Redis at {_REDIS_URL} - {exc}"
        return f"Error: {exc}"


# ---------------------------------------------------------------------------
# Bulk registration
# ---------------------------------------------------------------------------


def register_redis_tools(registry: ToolRegistry) -> int:
    """Register Redis inspection tools. Returns count."""
    registry.register_category_hint(
        "Redis",
        "Redis tools inspect the Redis server used for task queue (SAQ), "
        "tool result cache, and pipeline result storage. Read-only - "
        "no SET, DEL, or any write operations.",
    )
    tools = [redis_inspect]
    for func in tools:
        registry.register(func, category="Redis")
    return len(tools)
