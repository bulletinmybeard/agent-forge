# Infrastructure Health Skill

You have been given this skill because the user's query involves infrastructure diagnostics, system health monitoring, resource capacity, certificate management, or incident investigation.
Follow these guidelines when assessing and reporting infrastructure health.

## System Health: CPU, Memory, Disk, Network

**CPU diagnostics**:
```bash
# Current usage
top -b -n 1 | head -15
# or: watch -n 1 'ps aux | sort -k3,3nr | head -10'

# Load average (5-minute average)
uptime  # Load > CPU count = saturation

# Process causing high CPU
ps aux --sort=-%cpu | head -5
strace -p <pid> -c          # System call breakdown

# Alert thresholds:
# CPU > 85% for >5 min → investigate
# Load > 2 * cpu_count → critical
```

**Memory diagnostics**:
```bash
# Memory usage breakdown
free -h
# or: cat /proc/meminfo

# Process memory
ps aux --sort=-%mem | head -5
# RSS = resident (physical), VSZ = virtual

# Swap usage (if high = memory pressure)
swapon -s  # or: cat /proc/swaps

# OOM killer events
dmesg | grep "Out of memory"
journalctl | grep "OOM killer"

# Alert thresholds:
# Memory > 90% → investigate
# Swap active > 10% → urgent
# OOM killer events → critical
```

**Disk diagnostics**:
```bash
# Capacity
df -h          # % used per mount
du -sh /*      # Directory sizes

# I/O performance
iostat -x 1 5  # IOPS, utilization, average queue length
iotop           # Disk I/O by process

# Inodes (can fill up separately from blocks)
df -i           # Inode usage per filesystem
find / -type f | wc -l  # Total file count

# Alert thresholds:
# Used > 90% → investigate
# Used > 95% → critical
# Inodes > 85% → investigate
# IOPS at 100% → saturated
```

**Network diagnostics**:
```bash
# Interface stats
ip -s link show
# or: netstat -i

# Connection count
netstat -an | wc -l
# By state
netstat -an | grep ESTABLISHED | wc -l
netstat -an | grep TIME_WAIT | wc -l

# Packet loss / latency to key services
ping -c 5 database.internal
mtr --report service.internal

# Alert thresholds:
# Connections > 10000 → investigate connection pool
# Packet loss > 0.1% → network instability
# Latency > 2x baseline → degradation
```

## Docker Health: Container Status, Resource Usage, Restart Loops

**Container status**:
```bash
# Basic status
docker ps -a
docker inspect <container_id> | grep -A 5 '"State"'
# Looks for: Running, ExitCode, Restarting, OOMKilled

# Health check status (if HEALTHCHECK defined)
docker inspect --format='{{.State.Health.Status}}' <container>
# Returns: healthy, unhealthy, starting, none

# Container logs (last 50 lines, follow)
docker logs -n 50 -f <container>
docker logs --since 10m <container>  # Last 10 minutes

# Alert if:
# State = "Exited" or "Dead"
# Health = "unhealthy"
# Restart count > 3 in 5 min
```

**Resource usage**:
```bash
# Real-time container stats
docker stats <container>
# Shows: CPU%, MEM usage/limit, NET I/O, BLOCK I/O

# Historical resource limits
docker inspect --format='{{json .HostConfig.Memory}}' <container>
# 0 = unlimited (bad), set explicit limits

# Check if container is OOM killed
docker inspect <container> | grep OOMKilled
journalctl | grep "Docker:.*OOMKilled"

# Alert thresholds:
# CPU > 90% of limit
# Memory > 95% of limit
# Memory not limited → risk
```

**Restart loops**:
```bash
# Detect restart loop
docker inspect <container> | grep RestartCount

# View restart policy
docker inspect --format='{{json .HostConfig.RestartPolicy}}' <container>
# Example: {"Name":"always","MaximumRetryCount":0}

# Check logs immediately after restart
docker logs <container> | tail -20

# Common causes:
# - Command exited with non-zero
# - Out of memory
# - Missing dependencies
# - Port already in use

# Fix: Check logs, test command locally, verify dependencies
```

## Network Diagnostics: DNS, Port Connectivity, TLS, Latency

**DNS resolution**:
```bash
# Standard resolution
nslookup api.service.com
dig api.service.com

# Check all nameservers (should be consistent)
for ns in $(dig +short NS api.service.com); do
  echo "$ns:"; dig @$ns api.service.com +short
done

# TTL check (cache timing)
dig api.service.com | grep -A1 api.service.com

# Alert if:
# Nameservers differ
# TTL = 0 (every query expensive)
# Resolution > 100ms
```

**Port connectivity**:
```bash
# Basic: is port open?
curl -v http://service:8080/health
telnet service 8080

# More detailed with timeout
timeout 5 bash -c 'cat < /dev/null > /dev/tcp/service/8080'
# Exit 0 = open, 124 = timeout, other = closed

# Full port scan (local)
netstat -tlnp | grep LISTEN

# Check firewall rules
sudo iptables -L -n | grep ACCEPT
sudo firewall-cmd --list-ports

# Alert if:
# Critical ports not listening
# Unexpected open ports
# Firewall blocking expected traffic
```

**TLS/SSL certificate validation**:
```bash
# Expiry date
openssl s_client -connect api.service.com:443 -servername api.service.com \
  </dev/null 2>/dev/null | openssl x509 -noout -dates

# Full certificate details
openssl s_client -connect api.service.com:443 -servername api.service.com \
  </dev/null 2>/dev/null | openssl x509 -text -noout

# Certificate chain verification
openssl s_client -connect api.service.com:443 -showcerts </dev/null

# OCSP stapling (good for performance)
openssl s_client -connect api.service.com:443 -servername api.service.com \
  -tlsextdebug </dev/null 2>&1 | grep "OCSP"

# Alert thresholds:
# Expiry < 30 days → urgent (certificate renewal needed)
# Expiry < 7 days → critical
# Chain broken (missing intermediate) → critical
# OCSP response stale → warning
```

**Latency measurements**:
```bash
# Simple ICMP ping
ping -c 5 service.internal
# Look for: all 5 received, <1% loss, latency < 10ms

# TCP latency (more realistic than ICMP)
time curl -w 'time_connect=%{time_connect}\ntime_starttransfer=%{time_starttransfer}\ntime_total=%{time_total}\n' \
  -o /dev/null -s http://service:8080/health

# Full network path (hop-by-hop latency)
mtr --report --csv service.internal > latency_report.csv

# Alert on:
# Baseline latency × 2 (degradation)
# Jitter > baseline × 0.5 (instability)
# Packet loss > 0%
```

## Service Health: HTTP Endpoints, Database, Queues, Cache

**HTTP health endpoints**:
```bash
# Readiness (can handle traffic?)
curl -s http://localhost:8100/health/ready | jq .

# Expected response:
# {
#   "status": "ready",
#   "database": "connected",
#   "cache": "available",
#   "embeddings": "online"
# }

# Deep health
curl -s http://localhost:8100/health/deep | jq .
# Includes: response times, error rates, queue depths
```

**Database connection health**:
```bash
# Connection pool status
SELECT count(*) FROM pg_stat_activity;  -- Total connections
SELECT count(*) FROM pg_stat_activity WHERE state = 'active';
SELECT count(*) FROM pg_stat_activity WHERE state = 'idle in transaction';

# If idle-in-transaction is high = apps holding connections (bad)

# Query latency
SELECT query, mean_exec_time, calls FROM pg_stat_statements
ORDER BY mean_exec_time DESC LIMIT 10;

# Alert thresholds:
# Active connections > 80% of max_connections
# Idle-in-transaction > 10
# Query p99 latency > baseline × 2
```

**Queue depth (message backlog)**:
```bash
# For Redis-based queue
redis-cli LLEN queue_name
# Alert if > 1000 (backlog building)

# For database queue
SELECT COUNT(*) FROM queue_table WHERE status = 'pending';

# Alert if:
# Depth > 10× normal
# Depth increasing (consumer slower than producer)
```

**Cache hit rate**:
```bash
# Redis hit rate
INFO stats | grep keyspace_hits/keyspace_misses

# Calculation:
# hit_rate = hits / (hits + misses) * 100%

# Alert if:
# Hit rate < 50% (cache not effective)
# Hit rate drops 20% from baseline (eviction/invalidation issue)
```

## Certificate Management

**Automated renewal (recommended)**:
```bash
# Let's Encrypt via Certbot (renews every 60 days)
sudo certbot renew --quiet --no-eff-email

# Add to crontab (runs daily, renews if < 30 days to expiry)
0 3 * * * /usr/bin/certbot renew --quiet
```

**Manual renewal**:
```bash
# Check expiry
certbot certificates

# Renew specific cert
certbot renew --cert-name api.service.com

# Test renewal process
certbot renew --dry-run
```

**Chain validation**:
```bash
# Verify chain is complete (should output: OK)
openssl verify -CApath /etc/ssl/certs api_cert.pem

# If missing intermediate certificate, add it
cat intermediate.crt >> api_cert.pem
```

## Resource Forecasting

**Growth rate estimation**:
```
Historical data (last 90 days):
Database size: 50 GB → 55 GB → 62 GB
Trend: +6-7 GB per month
Forecast: 62 + (7 × 12) = 146 GB in 12 months

Storage limit: 500 GB
Runout date: In ~62 months (acceptable)

Action: No immediate concern, review quarterly
```

**Capacity planning signals**:
```
CPU:
  - p99 latency trending up despite stable request rate
  - CPU 75% at peak, was 50% 3 months ago
  → Add capacity or optimize code

Memory:
  - Memory usage grows 200 MB per day
  → Memory leak or unchecked growth; investigate
  → Upgrade if unable to fix in timeframe

Disk:
  - Currently 200 GB, growing 10 GB/month
  → Limit: 500 GB, runout in 30 months
  → Plan upgrade or archive old data in 12 months

Network:
  - Peak bandwidth: 2 Gbps, grew from 1 Gbps 6 months ago
  → Current capacity: 10 Gbps, adequate for 4 more years
  → No action needed
```

## Common Failure Modes

1. **Disk full** (100% capacity):
   ```bash
   # Symptoms: All writes fail, logs stop, system sluggish
   # Diagnosis:
   df -h | grep 100%
   du -sh /* | sort -h | tail -5

   # Quick fix: Delete logs or temp files
   rm /var/log/*.old
   rm -rf /tmp/*

   # Permanent: Expand disk or implement retention
   ```

2. **Connection pool exhaustion**:
   ```bash
   # Symptoms: "Timeout waiting for connection"
   # Diagnosis:
   SELECT count(*) FROM pg_stat_activity;
   # Result: equals max_connections

   # Causes: Slow queries holding connections, apps not releasing
   # Fix: Kill idle connections, scale app instances, optimize queries
   ```

3. **File descriptor limits**:
   ```bash
   # Check limit
   ulimit -n  # Per-process
   cat /proc/sys/fs/file-max  # System-wide

   # Symptoms: "Too many open files"
   # Fix:
   ulimit -n 65536  # Per-process
   # or in /etc/security/limits.conf for permanent

   # For Docker:
   # --ulimit nofile=65536:65536 in run command
   ```

4. **Zombie processes**:
   ```bash
   # Detect
   ps aux | grep defunct

   # Cause: Parent process didn't wait() on child
   # Fix: Kill parent process (zombie will be adopted by init)
   kill -9 <parent_pid>
   ```

5. **OOM killer**:
   ```bash
   # Symptoms: Process killed suddenly, memory pressure
   # Diagnosis:
   dmesg | tail -20 | grep oom-kill
   journalctl | grep OOMKilled

   # Fix: Add swap, increase memory limit, optimize application
   ```

## Response Format

When assessing infrastructure health, structure your response as:

1. **Status Summary** — Overall health: OK/WARN/CRIT with justification
2. **System Metrics** — CPU, memory, disk, network current state
3. **Service Status** — Database, cache, queues, health endpoints
4. **Alerts Triggered** — Which thresholds exceeded, severity
5. **Affected Components** — What's degraded or at risk
6. **Recommended Actions** — Priority-ordered fixes with estimated impact
7. **Forecast** — If current trend continues, when will limits be reached
