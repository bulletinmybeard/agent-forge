# Log Analysis Patterns Skill

You have been given this skill because the user's query involves log analysis, troubleshooting, log parsing, or investigating issues via logs. Follow these guidelines when analyzing logs and identifying patterns.

## Common Log Formats

### Syslog Format

Standard format used by Linux/Unix system logs and many services:

```
<priority> timestamp hostname service[pid]: message
Mar 22 10:15:42 web-prod app[1234]: Database connection error
```

**Parsing pattern**:
- Priority: `<number>` (facility*8 + severity)
- Timestamp: Month Day HH:MM:SS (no year)
- Hostname: Service name or IP
- Service[PID]: Application name and process ID
- Message: Free-form text

### JSON Structured Logs

Recommended format for parseable, queryable logs:

```json
{
  "timestamp": "2026-03-22T10:15:42.123Z",
  "level": "ERROR",
  "logger": "agentforge.providers.configurable_provider",
  "message": "Failed to extract product price",
  "error": "KeyError: 'offers'",
  "traceback": "Traceback (most recent call last)...",
  "request_id": "req-abc123",
  "context": {
    "provider": "jumbo",
    "url": "https://www.jumbo.com/product",
    "retry_count": 3
  }
}
```

**Advantages**: Easily grep, filter, aggregate by any field

### Apache/Nginx Access Logs

Standard HTTP access format:

```
192.168.1.100 - user [22/Mar/2026:10:15:42 +0000] "GET /search HTTP/1.1" 200 5234 "-" "Mozilla/5.0"
```

**Fields**:
1. Client IP
2. Ident (usually -)
3. User (usually -)
4. Timestamp: `[DD/Mon/YYYY:HH:MM:SS +TZOFF]`
5. Request: `"METHOD /path HTTP/VERSION"`
6. Status code (200, 404, 500)
7. Response size (bytes)
8. Referrer
9. User agent

**Parsing**: Use standard regex or log parsing tools (logstash, fluentd)

### Docker Container Logs

Docker logs mix stdout/stderr with timestamps:

```
2026-03-22T10:15:42.123456789Z [INFO] agentforge started on port 8100
2026-03-22T10:15:43.234567890Z [ERROR] Connection to Qdrant failed (retrying...)
```

**Accessing**: `docker logs <container>` or `docker compose logs -f`

### Python Application Logs

Common patterns from logging module:

```
2026-03-22 10:15:42,123 - agentforge.providers - ERROR - Failed to extract offers
2026-03-22 10:15:43,234 - agentforge.database - DEBUG - Executing query: SELECT * FROM...
```

**Parsing**: `timestamp - logger_name - level - message`

## Pattern Matching

### Common Regex Patterns

1. **Timestamps**:
   - ISO 8601: `\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}`
   - Syslog: `[A-Za-z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}`
   - Apache: `\[\d{2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2}`

2. **IP Addresses**:
   - IPv4: `(?:\d{1,3}\.){3}\d{1,3}`
   - IPv6: `(?:[0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}`

3. **HTTP Status Codes**:
   - 2xx Success: `2\d{2}`
   - 4xx Client error: `4\d{2}`
   - 5xx Server error: `5\d{2}`
   - Specific: `(200|301|404|500|503)`

4. **Error Indicators**:
   - Exception type: `(?:Error|Exception|Failed|exception):?\s+\w+`
   - Stack trace: `at\s+[a-z_]+\.[a-z_]+\s*\(`
   - Traceback: `Traceback \(most recent call last\)`

5. **Database Queries**:
   - Slow query: `Query took (\d+)ms`
   - Connection: `(?:Connect|Disconnect|connection)\s+(?:from|to)\s+\d+\.\d+\.\d+\.\d+`

6. **Common Error Messages**:
   - Timeout: `(?:timeout|timed out|TIMEOUT)`
   - Authentication: `(?:403|401|Unauthorized|Forbidden|auth)`
   - Not found: `(?:404|NotFound|not found)`
   - Connection refused: `(?:refused|ECONNREFUSED)`

## Correlation and Tracing

### Request ID Tracing

1. **Log all steps with request ID**:
   - Example: `req-abc123` appears in request, authentication, database query, response logs
   - Allows tracking single user action across multiple services

2. **Implementation**:
   - Pass request ID in HTTP headers: `X-Request-ID: req-abc123`
   - Include in all log messages: `context={"request_id": "req-abc123"}`
   - Useful for asynchronous operations (queue job with request ID)

3. **Example correlation**:
   ```
   2026-03-22 10:15:42 req-abc123 [INFO] API request: GET /search
   2026-03-22 10:15:42 req-abc123 [DEBUG] Database query: SELECT products...
   2026-03-22 10:15:43 req-abc123 [DEBUG] Embedding query via Ollama
   2026-03-22 10:15:44 req-abc123 [INFO] Response sent: 200 OK
   ```

### Timestamp Alignment

1. **Synchronize system clocks** across services (NTP):
   - Clock skew causes ordering confusion
   - Check: `ntpq -p` to verify NTP is synced

2. **Use consistent timestamp format**:
   - All logs: ISO 8601 with milliseconds (2026-03-22T10:15:42.123Z)
   - Allows proper ordering even across services

3. **Reconstructing sequence of events**:
   - Grep for time range: `grep "2026-03-22 10:15:4[0-5]" logfile`
   - Sort by timestamp if mixed: `sort -k 1,2`
   - Verify clock skew between services

## Error Classification

### Stack Trace Grouping

1. **Extract error type and message**:
   ```
   Exception type: KeyError
   Message: 'offers'
   Location: /app/src/providers/jsonld_extractor.py:124
   ```

2. **Group similar errors**:
   - Same exception type + message = same issue
   - Different locations but same root cause (e.g.,, "Database connection refused")

3. **Identify error frequency**:
   - Count occurrences: `grep "KeyError: 'offers'" logs/* | wc -l`
   - Time-based: `grep -c "2026-03-22 10:15" logs/*` (errors per minute)

### Error Rate Calculation

```bash
# Total errors in time period
total_errors=$(grep "ERROR\|Exception" logfile | wc -l)

# Total log entries in time period
total_logs=$(wc -l < logfile)

# Error rate as percentage
error_rate=$((total_errors * 100 / total_logs))

# Example: 50 errors in 10,000 logs = 0.5% error rate
```

### First Occurrence Detection

1. **Find when error first appeared**:
   ```bash
   grep -n "ERROR: Database connection refused" logfile | head -1
   # Output: 1234:2026-03-22 10:15:42 ERROR: Database connection refused
   # Error started at line 1234, timestamp 10:15:42
   ```

2. **Determine impact timeline**:
   - Start time: First occurrence
   - End time: Last error
   - Duration: Calculate time span
   - Frequency: Errors per second/minute during incident

## Performance Signals

### Slow Query Detection

1. **Identify slow database queries**:
   ```
   2026-03-22 10:15:42 [WARN] SLOW QUERY: 2450ms SELECT * FROM tracked_pages...
   ```

2. **Pattern matching**:
   - Grep: `grep "SLOW QUERY" logfile`
   - Extract time: `grep -oP 'SLOW QUERY: \K\d+' logfile`
   - Sort by duration: `grep "SLOW QUERY" logfile | sort -rn`

3. **Root causes**:
   - Missing index: Same query slow every time
   - Data size: Progressively slower as data grows (O(n) algorithm)
   - Lock contention: Slow only during concurrent access

### Timeout Patterns

1. **Identify timeout errors**:
   ```
   2026-03-22 10:15:42 [ERROR] HTTP request timeout after 30s: GET /search
   2026-03-22 10:15:43 [ERROR] Database query timeout after 60s
   ```

2. **Frequency analysis**:
   - Constant timeouts: Service overloaded or hanging
   - Intermittent: Resource contention or burst traffic
   - Growing timeouts: Degrading performance

3. **Upstream impact**:
   - When API times out, clients retry
   - Retries cause more load
   - Risk of cascading failure

### Connection Pool Exhaustion

1. **Signature logs**:
   ```
   [WARN] Database connection pool exhausted (10/10 active)
   [ERROR] Unable to acquire connection after 30s (queue full)
   ```

2. **Analysis**:
   - Count active connections over time
   - Identify which queries are holding connections
   - Check if connections are being released properly

3. **Remediation**:
   - Increase pool size
   - Reduce query duration (optimize slow queries)
   - Add connection timeout to kill hanging queries

## Alert-Worthy Patterns

### Error Spikes

1. **Detect sudden increase in errors**:
   - Baseline: Last hour average is 2 errors/minute
   - Alert: Current minute has 50 errors = 25x spike

2. **Implementation**:
   ```bash
   # Last 10 minutes error count
   recent=$(grep "2026-03-22 10:1[56]" logfile | grep ERROR | wc -l)
   # Last 10 minutes before that
   baseline=$(grep "2026-03-22 10:1[34]" logfile | grep ERROR | wc -l)
   # Alert if recent > baseline * 5
   ```

### Cascading Failures

1. **Pattern**:
   - Service A fails
   - Service B can't reach Service A, queues requests
   - Service B runs out of memory
   - Service C can't reach Service B
   - Entire system down

2. **Detection logs**:
   ```
   Service A: [ERROR] Failed to start
   Service B: [WARN] Service A unavailable, queuing requests
   Service B: [ERROR] Queue full, rejecting new requests
   Service B: [ERROR] Out of memory
   Service C: [ERROR] Connection to Service B refused
   ```

3. **Mitigation**:
   - Circuit breaker: Stop calling failing service immediately
   - Health checks: Detect failures early
   - Graceful degradation: Continue with reduced functionality

### Disk and Memory Warnings

1. **Disk usage**:
   ```
   [WARN] Disk usage 85% (/app/data)
   [ERROR] Disk usage 99%, cannot write logs
   ```

2. **Memory warnings**:
   ```
   [WARN] Memory usage 80%, consider cleanup
   [ERROR] Out of memory, OOMKilled
   ```

3. **Response strategy**:
   - Increase disk/memory allocation
   - Remove old logs/data
   - Optimize memory footprint

## Response Format

When analyzing logs, structure your response as:

1. **Time Range** — What period logs cover, timezone
2. **Summary** — Key findings in 1-2 sentences
3. **Timeline** — Chronological sequence of events:
   - Timestamp: Event and any error/warning
   - Duration: How long the issue lasted

4. **Affected Services** — Which components/APIs impacted
5. **Error Classification**:
   - Error types and counts
   - Root cause hypothesis based on logs
   - Evidence supporting the hypothesis

6. **Contributing Factors** — What led to the issue:
   - Performance degradation
   - Cascading failures
   - Resource exhaustion

7. **Recommendations** — How to prevent in future:
   - Monitoring/alerting thresholds
   - Code or config changes
   - Capacity planning

## Quick Log Analysis Checklist

- [ ] Timestamps synchronized across services (NTP)
- [ ] Request IDs included for correlation
- [ ] Error messages include context (which service, which operation)
- [ ] Slow query logs enabled (with duration threshold)
- [ ] Connection pool metrics logged
- [ ] Memory/disk usage warnings configured
- [ ] Circuit breaker/timeout configs logged
- [ ] Log rotation prevents disk exhaustion
- [ ] Error rates monitored (alert on spikes)
- [ ] Critical paths traced with debug logging
