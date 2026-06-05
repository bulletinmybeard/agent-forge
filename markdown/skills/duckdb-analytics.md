# DuckDB Analytics Skill

You have been given this skill because the user's query involves DuckDB database operations, analytics queries, or data processing tasks. Follow these guidelines when advising on DuckDB usage patterns and optimization.

## DuckDB Strengths

1. **Columnar storage** — DuckDB is OLAP-optimized (analytical queries), not OLTP:
   - Vectorized execution (processes data in chunks, not row-by-row)
   - Compression-friendly (similar values stored together)
   - Cache-efficient (working set stays in L1/L2 CPU cache)
   - Use for: Analytics, time-series, aggregations, reporting
   - Avoid for: High-frequency updates, transactional workloads
2. **Zero-copy reads** — Memory views instead of copying:
   - Reading data from Parquet doesn't require deserialization
   - Result sets are views into memory (no materialization cost)
3. **Parallel execution** — Automatic parallelization across CPU cores:
   - Each query automatically distributed to available threads
   - No manual sharding or partitioning required
4. **SQL compatibility** — Extended SQL dialect compatible with PostgreSQL:
   - Standard ANSI SQL operations
   - Plus DuckDB extensions: LIST, STRUCT types, QUALIFY, ASOF joins

## Parquet Integration

1. **Reading Parquet** — Zero-copy, predicate pushdown:
   ```sql
   -- Predicate pushdown: only reads matching row groups
   SELECT price, timestamp FROM 'snapshots.parquet'
   WHERE timestamp > '2025-01-01';
   ```
2. **Writing Parquet** — Optimized columnar format:
   ```sql
   COPY (
       SELECT product_id, price, timestamp
       FROM snapshots
       WHERE timestamp > now() - interval 30 day
   ) TO 'snapshots.parquet' (FORMAT PARQUET);
   ```
3. **Partition pruning** — Automatically skips partitions:
   ```sql
   -- If file is partitioned by date, DuckDB skips unused partitions
   SELECT COUNT(*) FROM read_parquet('data/snapshots/year=2025/**/*.parquet')
   WHERE month = 3;
   ```
4. **Schema inference** — Automatic type detection:
   ```sql
   -- DuckDB infers schema without specifying columns
   SELECT * FROM read_parquet('file.parquet') LIMIT 1;
   ```

## Advanced SQL Features

1. **Window functions** — Analytical queries over subsets of rows:
   ```sql
   -- Moving average price over 7 days
   SELECT
       product_id,
       price,
       timestamp,
       AVG(price) OVER (
           PARTITION BY product_id
           ORDER BY timestamp
           ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
       ) AS avg_price_7d
   FROM snapshots
   ORDER BY product_id, timestamp;
   ```
2. **QUALIFY clause** — Filter after window functions (cleaner than subqueries):
   ```sql
   -- Only keep snapshots where price is within 10% of 7-day moving average
   SELECT product_id, price, timestamp
   FROM snapshots
   QUALIFY ABS(price - AVG(price) OVER (
       PARTITION BY product_id
       ORDER BY timestamp
       ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
   )) / AVG(price) OVER (...) < 0.10;
   ```
3. **PIVOT/UNPIVOT** — Reshape data:
   ```sql
   -- PIVOT: Convert rows to columns (retailers as columns)
   SELECT * FROM (
       SELECT retailer, price, product_id FROM prices
   ) PIVOT (MAX(price) FOR retailer IN ('jumbo', 'ah', 'bol'))
   AS pivoted;
   ```
4. **ASOF join** — Time-series join (latest matching row):
   ```sql
   -- Join product snapshots with price updates (latest before snapshot)
   SELECT s.product_id, s.timestamp, u.new_price
   FROM snapshots s
   ASOF LEFT JOIN price_updates u
       ON s.product_id = u.product_id
       AND s.timestamp >= u.timestamp;
   ```
5. **LIST/STRUCT types** — Nested data structures:
   ```sql
   -- Aggregate prices into list per product
   SELECT
       product_id,
       LIST(STRUCT_PACK(timestamp, price)) AS price_history
   FROM snapshots
   GROUP BY product_id;
   ```

## Analytics Patterns

1. **Time-series aggregation** — Price trends by retailer:
   ```sql
   SELECT
       product_id,
       retailer,
       DATE_TRUNC('week', timestamp) AS week,
       MIN(price) AS min_price,
       AVG(price) AS avg_price,
       MAX(price) AS max_price,
       COUNT(*) AS samples
   FROM snapshots
   WHERE timestamp > now() - interval 90 day
   GROUP BY product_id, retailer, DATE_TRUNC('week', timestamp)
   ORDER BY product_id, retailer, week;
   ```
2. **Moving averages** — Smooth price volatility:
   ```sql
   SELECT
       product_id,
       timestamp,
       price,
       AVG(price) OVER (
           PARTITION BY product_id
           ORDER BY timestamp
           ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
       ) AS ma_7,
       AVG(price) OVER (
           PARTITION BY product_id
           ORDER BY timestamp
           ROWS BETWEEN 29 PRECEDING AND CURRENT ROW
       ) AS ma_30
   FROM snapshots;
   ```
3. **Percentile calculations** — Find price distribution:
   ```sql
   SELECT
       product_id,
       retailer,
       QUANTILE_CONT(price, 0.25) AS p25,
       QUANTILE_CONT(price, 0.50) AS median,
       QUANTILE_CONT(price, 0.75) AS p75,
       QUANTILE_CONT(price, 0.95) AS p95
   FROM snapshots
   GROUP BY product_id, retailer;
   ```

## Performance Optimization

1. **Memory limits** — Control RAM usage for large datasets:
   ```python
   # Python: set memory limit to 8GB
   import duckdb
   conn = duckdb.connect(':memory:')
   conn.execute("SET memory_limit='8GB'")
   ```
2. **Parallel execution** — Configure thread count:
   ```python
   conn = duckdb.connect(':memory:')
   conn.execute("SET threads=8")  # Use 8 CPU cores
   ```
3. **External sorting** — Handle datasets larger than RAM:
   - DuckDB automatically spills to disk when needed
   - No manual configuration required
   - Performance degrades gracefully (uses temp disk space)
4. **Index selection** — DuckDB has limited indexing:
   - No manual CREATE INDEX (not needed for OLAP)
   - Parquet predicate pushdown provides filtering
   - For frequent filters on small columns, pre-sort Parquet

## Concurrency & Connection Management

1. **Single-writer limitation** — Critical constraint:
   - Only one connection can write at a time
   - Multiple readers are fine (concurrent)
   - Use read-only replicas for horizontal scaling
2. **Read-only replicas pattern** — Master-replica architecture:
   ```python
   # Master (write-only)
   master_db = duckdb.connect('data/main.duckdb')

   # Replicas (read-only, synced from master)
   replica_db = duckdb.connect('data/main.duckdb', read_only=True)

   # Write on master, read from replicas
   master_db.execute("INSERT INTO table VALUES (...)")
   # Sync via filesystem (Parquet export)
   replica_db.execute("SELECT * FROM 'data/snapshots.parquet'")
   ```
3. **Connection management** — Use context managers:
   ```python
   import duckdb

   with duckdb.connect('db.duckdb') as db:
       result = db.execute("SELECT COUNT(*) FROM table").fetch_all()
   # Connection automatically closed
   ```

## Data Import Patterns

1. **CSV ingestion** — Fast import with type inference:
   ```sql
   CREATE TABLE products AS
   SELECT * FROM read_csv('products.csv');  -- Auto schema inference
   ```
2. **JSON import** — Nested structure support:
   ```sql
   CREATE TABLE events AS
   SELECT * FROM read_json('events.jsonl');  -- Supports JSONL
   ```
3. **Parquet import** — Columnar to columnar (fastest):
   ```sql
   CREATE TABLE snapshots AS
   SELECT * FROM read_parquet('snapshots.parquet');
   ```
4. **Type casting** — Handle mismatched types:
   ```sql
   SELECT
       product_id::INTEGER,
       price::DECIMAL(10, 2),
       available::BOOLEAN,
       created_at::TIMESTAMP
   FROM raw_data;
   ```

## Python & Pandas Interop

1. **DuckDB from Pandas** — Zero-copy conversion:
   ```python
   import pandas as pd
   import duckdb

   df = pd.DataFrame({'price': [10.5, 20.3], 'quantity': [5, 3]})
   result = duckdb.query('SELECT AVG(price) FROM df').to_df()
   ```
2. **Pandas from DuckDB** — Efficient batch export:
   ```python
   conn = duckdb.connect('db.duckdb')
   df = conn.execute("SELECT * FROM table WHERE date > ?", [cutoff_date]).df()
   ```
3. **SQLAlchemy integration** — ORM support:
   ```python
   from sqlalchemy import create_engine

   engine = create_engine('duckdb:///db.duckdb')
   df = pd.read_sql("SELECT * FROM table", engine)
   ```

## Common Pitfalls

1. **WAL mode issues** — Write-ahead log requires explicit management:
   ```python
   # DuckDB uses WAL by default (safe)
   # Disable only if needed:
   conn.execute("PRAGMA disable_checkpoint_on_shutdown")
   ```
2. **Concurrent write attempts** — Will hang or error:
   - Only one writer at a time
   - Queue writes or use master-replica pattern
   - Monitor for stuck transactions: `PRAGMA database_list`
3. **Large result sets** — Don't materialize entire table in memory:
   ```python
   # Bad: loads 1M rows into Python list
   rows = conn.execute("SELECT * FROM huge_table").fetch_all()

   # Good: streaming cursor
   cursor = conn.execute("SELECT * FROM huge_table")
   for batch in iter(lambda: cursor.fetch(10000), []):
       process_batch(batch)
   ```
4. **Missing data types** — Explicit casting prevents errors:
   ```sql
   -- Bad: assumes type conversion
   SELECT price + discount FROM prices;

   -- Good: explicit casting
   SELECT price::DECIMAL + discount::DECIMAL AS total FROM prices;
   ```

## Response Format

When advising on DuckDB usage, structure your response as:
1. **Approach** — Which DuckDB feature(s) to use and why
2. **Implementation** — SQL query or Python code with examples
3. **Performance notes** — Expected runtime, memory usage, parallelization
4. **Complete solution** — If complex, provide full schema and optimized queries
5. **Execution plan** — Show EXPLAIN output if optimization relevant
