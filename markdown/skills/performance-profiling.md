# Performance Profiling Skill

You have been given this skill because the user's query involves performance analysis, profiling, optimization, or bottleneck investigation. Follow these guidelines when advising on performance-related topics.

## Database Performance

1. **N+1 Query Detection**:
   - Symptom: One query followed by many similar queries in a loop
   - Example: Load user, then load address for each user in Python (should use JOIN)
   - Detection: Enable query logging, count identical query patterns
   - Fix: Use JOIN, eager loading, or batch queries (`WHERE id IN (...)`)

2. **Missing or Ineffective Indexes**:
   - Use `EXPLAIN ANALYZE` on slow queries to check for sequential scans
   - Check index usage: `SELECT * FROM pg_stat_user_indexes WHERE idx_scan = 0`
   - Create indexes on: Foreign keys, WHERE clause columns, ORDER BY fields
   - Avoid over-indexing: Each index slows writes, increases storage

3. **Full Table Scans**:
   - Identify with `EXPLAIN ANALYZE` showing "Seq Scan"
   - Root cause: No WHERE clause, OR conditions without indexes, LIKE '%pattern%'
   - Fix: Add indexes, refactor queries, use full-text search if needed
   - Cost impact: O(n) → O(log n) with index

4. **Connection Pooling**:
   - Check connection pool size: `max_connections` in config
   - Monitor active connections: `SELECT count(*) FROM pg_stat_activity`
   - Connection exhaustion: Queries queue and timeout
   - Fix: Increase pool size, reduce query duration, use connection pooling (pgbouncer)

5. **Query Optimization**:
   - Identify slow queries: `pg_stat_statements` extension, slow query log
   - Check: Join order (index on foreign keys), WHERE clause selectivity
   - Analyze: `EXPLAIN ANALYZE` output, look for high "Rows" vs "actual rows"
   - Measure improvement before/after with `ANALYZE` command

## Application Performance

1. **CPU Profiling**:
   - Tool: `py-spy` (sampling profiler, 1% overhead)
   - Command: `py-spy record -o profile.svg -- python app.py`
   - Flame graph interpretation: Wide = hot, tall = deep call stack
   - Targets: Loops without early termination, inefficient algorithms, regex compilation

2. **Memory Leaks**:
   - Tool: `memory_profiler` for line-by-line memory usage
   - Symptom: Memory grows over time, doesn't release after operation ends
   - Check: Circular references, cached data without eviction, global state
   - Fix: Use weak references, implement cache TTL, profile with `tracemalloc`

3. **Garbage Collection Pressure**:
   - Monitor: `gc.get_stats()` in Python, GC pause times
   - Symptom: Frequent GC pauses causing latency spikes
   - Cause: Large object allocation, high allocation rate
   - Fix: Reduce object creation, use object pools, tune GC thresholds (`gc.set_threshold`)

4. **Async Bottlenecks**:
   - Check: Are async tasks actually running concurrently or blocked?
   - Tool: `asyncio` debugging with `asyncio.run(..., debug=True)`
   - Common issue: Blocking I/O in async context (`requests` → use `aiohttp`)
   - Profile: Use `py-spy` on async code to find blocking operations

5. **Algorithm Efficiency**:
   - Measure: O(n) vs O(n²) difference at scale (1000→10000 items)
   - Example: Linear search vs hash lookup, bubble sort vs merge sort
   - Tool: Time benchmarks with `timeit` or pytest-benchmark
   - Target: Hot paths handling large datasets

## Network Performance

1. **DNS Resolution**:
   - Overhead: 10-100ms per lookup (single-threaded)
   - Check: How many DNS lookups per request? Use `dig` or DNS profiler
   - Fix: Cache results, use connection pooling, batch operations
   - Tool: `strace -e trace=network` to see all DNS calls

2. **Connection Reuse**:
   - HTTP: Use persistent connections (Connection: keep-alive)
   - Database: Use connection pooling (not creating new connection per query)
   - Check: Number of TIME_WAIT connections with `netstat -an | grep TIME_WAIT`
   - Fix: Keep-alive timeouts, connection pooling, reduce open/close cycles

3. **Payload Size**:
   - Measure: Total bytes sent/received per operation
   - Reduce: JSON compression with gzip, select only needed fields, pagination
   - Tool: Browser DevTools network tab, check Content-Length headers
   - Target: APIs should compress responses > 1KB

4. **Compression**:
   - Enable: `Accept-Encoding: gzip, deflate` in HTTP headers
   - Verify: `Content-Encoding: gzip` in response
   - Overhead: Compression adds CPU, saves bandwidth (good for > 1KB)
   - Trade-off: Fast network (no compression), slow network (compress everything)

## Frontend Performance

1. **Bundle Size**:
   - Measure: Size of JS bundles before/after
   - Tool: `webpack-bundle-analyzer`, `esbuild --analyze`
   - Target: Main bundle < 200KB, async chunks < 100KB
   - Reduce: Code splitting, tree-shaking, unused dependency removal

2. **Render Blocking**:
   - JavaScript blocking DOM parsing: Defer or async <script> tags
   - CSS blocking render: Put <style> in <head>, minify
   - Large layouts: Measure Time to Interactive (TTI)
   - Fix: Move scripts to end of body, async CSS loading, critical CSS inlining

3. **Layout Thrashing**:
   - Symptom: Multiple reflows/repaints in tight loop
   - Example: Read offsetHeight, modify DOM, read offsetWidth (3 reflows)
   - Fix: Batch DOM reads, then writes; use `requestAnimationFrame` for visual updates
   - Tool: Chrome DevTools "Rendering" tab to see reflow events

4. **Image Optimization**:
   - Measure: Total image bytes, HTTP requests
   - Compress: WebP format, responsive images (srcset), lazy loading
   - Tool: ImageOptim, TinyPNG, or buildtime optimization
   - Target: No image > 500KB, lazy-load below fold

5. **Third-party scripts**:
   - Audit: All external <script> tags (analytics, ads, tracking)
   - Measure: Impact on page load with DevTools (disable + measure improvement)
   - Defer: Use async, defer attributes; delay non-critical scripts
   - Monitor: Web Vitals (LCP, FID, CLS)

## Infrastructure Performance

1. **Container Resource Limits**:
   - Check: CPU limit, memory limit in compose/Kubernetes
   - Symptom: Throttling (CPU), OOMKilled (memory)
   - Set: Memory limit slightly above peak usage (not too tight)
   - Monitor: `docker stats`, Kubernetes metrics

2. **Disk I/O**:
   - Monitor: I/O utilization with `iostat -x 1`
   - Bottleneck: Slow disk for databases, cache writes
   - Fix: Use SSDs, increase cache, batch writes, enable compression
   - Measurement: I/O wait time (iowait %) should be < 5%

3. **Swap Usage**:
   - Problem: Swap is 10x slower than RAM
   - Check: `free -h | grep Swap`, or monitor OOMKilled events
   - Fix: Increase available RAM, reduce memory footprint
   - Warning: If swap active, performance degradation is severe

4. **CPU Hotspots**:
   - Monitor: CPU usage per core with `mpstat -P ALL 1`
   - Check: Is it single-threaded bottleneck or truly high CPU?
   - Profile: Use `py-spy` on live process to find hot functions
   - Fix: Parallelization, algorithm improvement, or defer work (async)

5. **Load balancing**:
   - Check: Are requests evenly distributed across servers?
   - Tool: Monitor request counts per instance in load balancer logs
   - Fix: Sticky sessions, consistent hashing, or proper balancing algorithm

## Profiling Tools Summary

| Tool | Use Case | Overhead |
|------|----------|----------|
| `py-spy` | CPU hotspots in production | 1% |
| `cProfile` | CPU profiling (detailed) | 10-50% |
| `memory_profiler` | Line-by-line memory usage | 2-5x |
| `asyncio` debug | Async deadlocks, blocking | varies |
| `pg_stat_statements` | Slow SQL queries | < 1% |
| `EXPLAIN ANALYZE` | Query plan analysis | runs query |
| Browser DevTools | Frontend performance | built-in |

## Response Format

When profiling a performance issue, structure your response as:

1. **Issue Summary** — What is slow, measured baseline (e.g.,, "API endpoint takes 5s")
2. **Root Cause Analysis**:
   - Top bottleneck (e.g.,, "N+1 queries loading 1000 users")
   - Impact estimate (e.g.,, "2 seconds of 5 seconds total")
   - Profiling evidence (tool output, flame graph)

3. **Optimization Plan** — Ordered by impact-to-effort ratio:
   - High impact, low effort (quick wins)
   - Medium impact, medium effort
   - Low impact or high effort (defer or skip)

4. **For each optimization**:
   - What to change (specific query, code location)
   - Expected improvement (before: 5s → after: 2s)
   - Effort estimate (hours/days)
   - Verification method (benchmark, profiling)

5. **Measurement strategy** — How to validate improvements are real
6. **Post-optimization** — Monitor targets (e.g.,, "p95 latency < 1s")

## Quick Profiling Checklist

- [ ] Database queries are indexed and efficient (EXPLAIN ANALYZE)
- [ ] No N+1 queries or inefficient loops
- [ ] Connection pooling active, not exhausted
- [ ] No obvious memory leaks or GC pressure
- [ ] Async operations are actually concurrent
- [ ] Network payloads compressed, reasonable size
- [ ] Frontend bundles code-split and lazy-loaded
- [ ] Container resources not constrained (CPU throttle, memory OOM)
- [ ] Disk I/O reasonable (iowait < 5%)
- [ ] No swap usage under load
