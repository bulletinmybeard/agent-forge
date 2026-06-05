# Observability & Monitoring Skill

You have been given this skill because the user's query involves monitoring, observability, alerting, dashboards, incident correlation, or system visibility. Follow these guidelines when advising on observability topics.

## Three Pillars of Observability

1. **Metrics** — Numeric measurements over time (gauge, counter, histogram).
   - Use for: Performance trends, resource usage, throughput, error rates
   - Examples: CPU%, response time (ms), requests/sec, error count
   - Cardinality risk: Avoid unbounded tags (user IDs, request paths)

2. **Logs** — Discrete events with context.
   - Use for: Debugging, audit trails, error details, business events
   - Examples: Application errors, state changes, user actions
   - Structure: JSON logs with request ID for correlation

3. **Traces** — Request journey across services.
   - Use for: Latency analysis, dependency mapping, bottleneck identification
   - Examples: API request → embedding service → Qdrant → response
   - Sampling: Sample 10% of normal requests, 100% of errors

## Health Check Patterns

**Liveness probe** (is service alive?):
```
GET /health/live → 200 if running, 5xx if should restart
Response time: <100ms
Checks: Process alive, basic connectivity
```

**Readiness probe** (can service handle traffic?):
```
GET /health/ready → 200 if ready, 503 if not
Response time: <500ms
Checks: DB connected, cache available, dependencies healthy
```

**Deep health check** (comprehensive diagnostics):
```
GET /health/deep → Detailed status of all systems
Includes:
  - Database: connection pool status, query latency
  - Cache: hit rate, evictions
  - External services: response times, error rates
  - Disk: available space, inode count
Response time: 1-2 seconds (not used for routing)
```

## Alerting: SLO/SLI/SLA

**SLI (Service Level Indicator)** — Measurable metric:
- "API endpoint responds in <200ms for 99.9% of requests"
- "Search queries return results within 5 seconds"

**SLO (Service Level Objective)** — Target SLI:
- "Maintain 99.9% availability"
- "Keep error rate below 0.1%"

**SLA (Service Level Agreement)** — Legal commitment:
- "We guarantee 99.9% uptime or you get 10% credit"

**Error budget** — How much failure is acceptable:
```
Error budget = 100% - SLO
SLO 99.9% = 0.1% error budget
= 43 minutes downtime per month
= 8.6 seconds downtime per day

Once budget exhausted, trigger maintenance/freeze
```

**Alert fatigue prevention**:
1. Alert only on SLO violations, not warnings
2. Group related alerts (avoid 50 notifications for one outage)
3. Use runbooks: each alert links to mitigation steps
4. Gradual escalation: Team lead → on-call → manager
5. Deduplicate: same issue = one alert, not 10

## Web Monitoring

1. **Uptime checks** (external):
   ```
   Every 60 seconds from 3 geographies:
   GET https://api.service.com/health → 200 within 5s

   Alert if:
     - 2+ consecutive failures (120s window)
     - Response time > 5s (not just 5xx)
   ```

2. **Response time percentiles**:
   ```
   Track: p50 (median), p95, p99, p99.9
   Alert on: p99 > 2x baseline
   Example: p99 baseline 500ms → alert at 1s
   ```

3. **SSL certificate expiry**:
   ```bash
   # Check expiry
   openssl s_client -connect api.service.com:443 -servername api.service.com \
     | openssl x509 -noout -dates

   # Alert threshold: notify 30 days before expiry
   # Auto-renew: ACME with Let's Encrypt 7 days before expiry
   ```

4. **DNS propagation**:
   ```bash
   # Verify all nameservers return same IP
   nslookup api.service.com ns1.example.com
   nslookup api.service.com ns2.example.com

   # Alert: any nameserver differs by >5 min
   ```

## Change Detection

1. **Content diff** (HTML):
   ```bash
   curl -s https://example.com > current.html
   diff -u baseline.html current.html

   Alert if:
     - Major structure change (>20% different)
     - Expected element missing
     - New unexpected content
   ```

2. **Visual diff** (screenshots):
   - Screenshot comparison with pixelwise diff
   - Tool: Percy, Chromatic, or custom script
   - Threshold: >5% visual difference

3. **Structural change** (JSON/XML):
   ```bash
   # Extract schema
   jq 'keys' api_response.json > schema.json

   Alert if new keys appear or required keys vanish
   ```

4. **Data integrity checks**:
   ```sql
   -- Verify referential integrity
   SELECT COUNT(*) FROM page_snapshots
   WHERE page_id NOT IN (SELECT id FROM tracked_pages);

   -- Alert if orphaned records found
   ```

## Incident Correlation

1. **Timeline reconstruction**:
   ```
   T+0m00s  Metric spike detected → alert fires
   T+0m15s  Error rate increases → second alert
   T+0m30s  Log shows "connection refused"
   T+0m45s  Database pod restart log entry

   Root cause: Database OOM killer → pod restart → app connection errors
   ```

2. **Blast radius estimation**:
   ```
   Affected systems:
     - Search API: 5000 err/min (100% of traffic)
     - Indexer: 0 err/min (offline dependencies?)
     - Web UI: 0 err/min (cached data serving)

   Impact: Search broken, indexing paused, UI working
   Priority: P1 (core feature down)
   ```

3. **Dependency graph analysis**:
   ```
   User → Web UI → Search API → Qdrant
                 → Embedding Service
                 → Response Cache (Redis)

   If Cache fails: Response time ↑20%, error rate ↑2%
   If Qdrant fails: Error rate 100%
   ```

4. **Event correlation queries**:
   ```sql
   -- Find events within 5 minutes
   SELECT a.event_type, b.event_type, TIMESTAMPDIFF(MINUTE, a.ts, b.ts)
   FROM events a, events b
   WHERE ABS(TIMESTAMPDIFF(MINUTE, a.ts, b.ts)) <= 5
   ORDER BY TIMESTAMPDIFF(MINUTE, a.ts, b.ts);
   ```

## Dashboard Design

**RED method** (for request-driven services):
```
Rate:     Requests per second (success + failure)
Errors:   Error rate (5xx, timeouts, exceptions)
Duration: Latency (p50, p95, p99)

Query Example:
  - Rate: COUNT / time_window
  - Errors: COUNT(status >= 500) / COUNT(*)
  - Duration: HISTOGRAM_QUANTILE(0.99, latency_ms)
```

**USE method** (for resources: CPU, disk, network):
```
Utilization: % of resource in use (0-100%)
Saturation:  Queue length waiting for resource
Errors:      Error count

Example (CPU):
  - Utilization: load_average / cpu_count * 100%
  - Saturation: run_queue_length
  - Errors: context_switches (high = contention)
```

**Example dashboard layout**:
```
Top Row (Service Health):
  ├── Uptime % (green/red)
  ├── Error Rate (line chart, 24h)
  └── Request Rate (area chart)

Middle Row (Performance):
  ├── p50 Latency (gauge)
  ├── p99 Latency (gauge)
  └── Response Time Distribution (histogram)

Bottom Row (Resources):
  ├── CPU Usage (line, 4 panels: user/system/io/idle)
  ├── Memory Usage (line, total/used/cached)
  └── Disk I/O (bars: read/write bytes/ops)

Sidebar (Alerts):
  └── Critical alerts (red), Warnings (yellow)
```

## Response Format

When designing or reviewing monitoring, structure your response as:

1. **Current State** — What's being monitored now, what's missing
2. **Gaps** — What visibility is lost, what decisions can't be made
3. **Recommended Metrics** — Specific metric names, thresholds, alert conditions
4. **Dashboard Sketch** — Panel layout and key queries/visualizations
5. **Runbooks** — For each critical alert, 1-2 sentence mitigation steps
6. **Implementation** — Which tools/systems to use (Prometheus, Grafana, DataDog, etc.)
