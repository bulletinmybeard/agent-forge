# SQL Optimization Skill

You have been given this skill because the user's query involves SQL query optimization, database performance, indexing strategy, or query analysis. Follow these guidelines when advising on SQL optimization topics.

## Query Analysis: EXPLAIN and Cost Estimation

1. **EXPLAIN (plan without execution)**:
   ```sql
   EXPLAIN SELECT * FROM tracked_pages WHERE provider = 'jumbo';
   ```
   Shows estimated costs and node types.

2. **EXPLAIN ANALYZE (plan with actual execution)**:
   ```sql
   EXPLAIN ANALYZE SELECT * FROM tracked_pages WHERE provider = 'jumbo';
   ```
   Compares estimated vs actual rows, execution time, memory usage.

3. **Reading EXPLAIN output**:
   - **Seq Scan** — Full table scan (slow, O(n))
   - **Index Scan** — Uses index (fast, O(log n) or O(1))
   - **Hash Join** — Loads smaller table into hash table, probes with larger
   - **Nested Loop** — Outer table loop over inner table (slowest for large sets)
   - **Merge Join** — Both tables pre-sorted, efficient for large sorted sets
   - **Cost** — Estimated disk reads; lower is better
   - **Rows** — Estimated vs actual row count (mismatch = outdated statistics)

4. **Cost interpretation** — First number is startup cost, second is total cost.
   Higher cost = more expensive operation.

## Index Strategy

**B-tree indexes (default):**
```sql
CREATE INDEX idx_provider ON tracked_pages(provider);
```
Best for equality and range queries. Good for `WHERE`, `ORDER BY`, `JOIN ON`.

**Hash indexes (PostgreSQL):**
```sql
CREATE INDEX idx_provider_hash ON tracked_pages USING hash(provider);
```
Only for equality; faster than B-tree for exact matches on high cardinality data.

**Composite indexes (multi-column):**
```sql
CREATE INDEX idx_provider_url ON tracked_pages(provider, url);
```
Order matters: equality columns first, then range columns, then sort columns.
Covers: `WHERE provider = ? AND url LIKE ?`

**Partial indexes (conditional):**
```sql
CREATE INDEX idx_active_pages ON tracked_pages(provider)
WHERE status = 'active';
```
Reduces index size by indexing only rows matching the WHERE condition.

**Covering indexes (include non-key columns):**
```sql
CREATE INDEX idx_provider_cover ON tracked_pages(provider)
INCLUDE (url, title, price);
```
PostgreSQL 11+. Query can be satisfied entirely from index (index-only scan).

**GIN/GiST indexes (complex types):**
```sql
CREATE INDEX idx_metadata_gin ON page_snapshots USING gin(metadata);
```
For JSON, arrays, or full-text search. GIN is more precise, GiST more flexible.

**Anti-pattern: too many indexes**
- Each index slows writes (INSERT, UPDATE, DELETE must update all indexes)
- Maintenance cost: VACUUM, ANALYZE required
- Memory usage: indexes kept in buffer pool
- Rule: one index per 1-2 GB of data

## Common Anti-Patterns

1. **SELECT \*** — Forces database to read all columns even if only 3 needed.
   ```sql
   -- Bad
   SELECT * FROM tracked_pages WHERE provider = 'jumbo';

   -- Good
   SELECT url, title, price FROM tracked_pages WHERE provider = 'jumbo';
   ```

2. **Implicit type casting** — Converts index values, forcing full scan.
   ```sql
   -- Bad: price is DECIMAL, implicit cast to TEXT
   SELECT * FROM page_snapshots WHERE price = '19.99';

   -- Good: explicit type matching
   SELECT * FROM page_snapshots WHERE price = 19.99::DECIMAL;
   ```

3. **Functions on indexed columns** — Index can't be used.
   ```sql
   -- Bad: LOWER() on indexed column
   SELECT * FROM tracked_pages WHERE LOWER(url) = 'jumbo';

   -- Good: store lowercase in index
   CREATE INDEX idx_provider_lower ON tracked_pages(LOWER(provider));
   -- Or use CITEXT type for case-insensitive storage
   ```

4. **OR conditions** — May prevent index usage (use UNION instead).
   ```sql
   -- Potentially bad
   SELECT * FROM tracked_pages WHERE provider = 'jumbo' OR provider = 'ah';

   -- Better: union
   SELECT * FROM tracked_pages WHERE provider = 'jumbo'
   UNION ALL
   SELECT * FROM tracked_pages WHERE provider = 'ah';
   ```

5. **NOT IN with subqueries** — Uses nested loop.
   ```sql
   -- Bad
   SELECT * FROM tracked_pages WHERE provider NOT IN (SELECT name FROM blocked);

   -- Good: use NOT EXISTS
   SELECT * FROM tracked_pages p WHERE NOT EXISTS
     (SELECT 1 FROM blocked b WHERE b.name = p.provider);
   ```

## Join Optimization

1. **Join order** — Database chooses, but hint with optimizer if needed.
   - Smaller result sets join first
   - Index on join column is essential

2. **Join types**:
   - **Inner Join** — Only matching rows (smallest result set)
   - **Left Join** — All rows from left table, matching from right
   - **Cross Join** — Cartesian product (avoid unless needed)

3. **Nested loop joins** (for small inner sets):
   ```sql
   SELECT p.url, s.price
   FROM tracked_pages p
   INNER JOIN page_snapshots s ON p.id = s.page_id
   WHERE p.provider = 'jumbo';
   ```
   Add index: `CREATE INDEX idx_snapshot_page_id ON page_snapshots(page_id);`

4. **Hash joins** (for large sets):
   PostgreSQL chooses automatically. Ensure work_mem is sufficient.

## PostgreSQL-Specific Optimization

1. **CTEs (Common Table Expressions)**:
   ```sql
   -- MATERIALIZED: forces execution (useful for expensive subqueries)
   WITH active_pages AS MATERIALIZED (
     SELECT * FROM tracked_pages WHERE status = 'active'
   )
   SELECT COUNT(*) FROM active_pages;

   -- NOT MATERIALIZED: inlines (default in PostgreSQL 12+)
   WITH recent_snapshots AS (
     SELECT * FROM page_snapshots
     WHERE created_at > NOW() - INTERVAL '7 days'
   )
   SELECT * FROM recent_snapshots WHERE price > 100;
   ```

2. **Window functions** (efficient aggregation):
   ```sql
   SELECT url, price,
     AVG(price) OVER (PARTITION BY provider) AS avg_price,
     ROW_NUMBER() OVER (PARTITION BY provider ORDER BY price DESC) AS rank
   FROM page_snapshots;
   ```

3. **pg_stat_statements** (query analysis):
   ```sql
   CREATE EXTENSION pg_stat_statements;
   SELECT query, calls, mean_exec_time
   FROM pg_stat_statements
   ORDER BY mean_exec_time DESC LIMIT 10;
   ```

4. **VACUUM and ANALYZE**:
   ```sql
   VACUUM ANALYZE tracked_pages;  -- Update table statistics
   REINDEX INDEX idx_provider;    -- Rebuild fragmented index
   ```

## DuckDB-Specific Optimization

1. **Columnar scan pushdown** — DuckDB scans only needed columns.
   ```sql
   -- Efficient: reads only url, price columns
   SELECT url, price FROM tracked_pages WHERE provider = 'jumbo';
   ```

2. **Parquet file pushdown** (if data in Parquet):
   ```sql
   SELECT COUNT(*) FROM 'data/snapshots/*.parquet'
   WHERE provider = 'jumbo';
   ```
   DuckDB pushes filter to Parquet reader, skips blocks.

3. **PRAGMA settings for performance**:
   ```sql
   PRAGMA threads = 4;           -- Parallel execution
   PRAGMA memory_limit = '4GB';  -- Max memory for queries
   PRAGMA max_memory = 8589934592;
   ```

4. **Aggregation optimization**:
   ```sql
   -- Efficient: DuckDB uses columnar aggregation
   SELECT provider, COUNT(*), AVG(price)
   FROM page_snapshots
   GROUP BY provider;
   ```

## Write Optimization

1. **Batch inserts** (vs single-row inserts):
   ```sql
   -- Good: single statement
   INSERT INTO page_snapshots (page_id, price, created_at)
   VALUES
     (1, 19.99, NOW()),
     (2, 24.99, NOW()),
     (3, 29.99, NOW());
   ```

2. **COPY command** (fastest for bulk loads):
   ```sql
   COPY page_snapshots (page_id, price, created_at)
   FROM stdin WITH (FORMAT csv);
   1,19.99,2026-03-22
   2,24.99,2026-03-22
   \.
   ```

3. **Upsert patterns** (insert or update):
   ```sql
   -- PostgreSQL
   INSERT INTO tracked_pages (url, provider)
   VALUES ('https://jumbo.com/...', 'jumbo')
   ON CONFLICT (url) DO UPDATE SET
     provider = EXCLUDED.provider,
     updated_at = NOW();
   ```

4. **Disable indexes during bulk load**:
   ```sql
   ALTER TABLE page_snapshots DISABLE TRIGGER ALL;
   -- bulk insert here
   ALTER TABLE page_snapshots ENABLE TRIGGER ALL;
   REINDEX TABLE page_snapshots;
   ```

## Response Format

When optimizing a query, structure your response as:

1. **Current Query** — The slow query with EXPLAIN output
2. **Analysis** — What's slow: full scans, joins, aggregation?
3. **Optimization** — Suggested indexes, rewritten query, or both
4. **Before/After Comparison** — EXPLAIN ANALYZE results showing improvement
5. **Trade-offs** — Index size, write cost, maintenance overhead
