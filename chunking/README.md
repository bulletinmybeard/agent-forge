# Chunking and indexing into Qdrant

This package turns your sources into JSON chunks, which the `agentforge-api` app embeds and stores in Qdrant.
The mappers run offline (no service needed). The indexing and search routes live on `agentforge-api` (`app/main.py`, port `8100`), which is LAN-only.

The flow is two steps:

1. Chunk a source into JSON files with one of the mappers below (or write the JSON by hand).
2. Index those files into Qdrant via the `/indexer/*` endpoints, then query with `/search/*` or the `@docs` chat mode.

## Mappers

Each mapper is installed as a console script (and also runnable as `python -m chunking.<name>.cli`).
The first five share no service dependencies, only `pydantic` + `pyyaml`, which the package already pulls in.
The `db` mapper is the exception: it needs SQLAlchemy + a driver, installed via the `db` extra (see below).

| Command                     | Input                                               | `source_type` | Chunk dirs                           |
| --------------------------- | --------------------------------------------------- | ------------- | ------------------------------------ |
| `agentforge-chunk-openapi`  | An OpenAPI JSON spec (or `--all` over a directory)  | `openapi`     | `endpoints/`, `schemas/`             |
| `agentforge-chunk-sql`      | A [tbls](https://github.com/k1LoW/tbls) schema JSON | `sql-schema`  | `tables/`, `_relationships.json`     |
| `agentforge-chunk-db`       | A live database, introspected via SQLAlchemy        | `sql-schema`  | `tables/`, `_relationships.json`     |
| `agentforge-chunk-code`     | A Python/Django project root (walked via AST)       | `code`        | `classes/`, `functions/`, `modules/` |
| `agentforge-chunk-docs`     | A CLI tool's man pages (or its `--help` tree)       | `docs`        | `commands/`                          |
| `agentforge-chunk-document` | A directory of Markdown files                       | `document`    | `sections/`                          |

`agentforge-chunk-sql` and `agentforge-chunk-db` produce the same `sql-schema` chunks. The first reads a [tbls](https://github.com/k1LoW/tbls) JSON dump, the second connects to the running database itself.

Every mapper writes to `<output-dir>/<source_type>/<source_name>/v<version>/`.
The default output dir is `data/chunks`, which keeps chunks under `./data` so the compose stack's `./data -> /app/data` mount makes them visible to the indexer.
Override it with `--output-dir` or the `AGENTFORGE_CHUNKS_DIR` env var.

Common flags:

- `--source-name`: the slug used in the Qdrant payload and the on-disk path. Required for `code` and `document`, derived otherwise (OpenAPI from the spec title, SQL from the filename, docs from the tool name).
- `--version`: defaults to today's date.
- `--output-dir`: base chunks directory (default `data/chunks`).

Examples:

```bash
# OpenAPI: one spec file
agentforge-chunk-openapi data/openapi-schemas/my-api.json

# SQL: a tbls-generated schema JSON
agentforge-chunk-sql data/sql-schemas/sales-db.json --source-name sales-db

# Code: this repo's own framework package
agentforge-chunk-code ./agentforge --source-name agentforge-framework

# CLI docs: index git's man pages
agentforge-chunk-docs git

# Documents: a Markdown tree
agentforge-chunk-document --input ./docs --source-name agentforge-docs
```

### Live database extraction (`agentforge-chunk-db`)

This one connects to a running database, introspects its schema via SQLAlchemy, and writes the same `sql-schema` chunks as the tbls path. It replaces the manual tbls step.

Install the extra and its driver:

```bash
pip install -e ".[db]"   # sqlalchemy + pymysql + psycopg2
```

Connections are read from the `databases:` block in `config.yaml` (the same file the service uses, so credentials live in one place). Point at a different file with `AGENTFORGE_CHUNKING_CONFIG`.

```yaml
# config.yaml
databases:
  my-db:
    url: "mysql+pymysql://user:pass@localhost:3306/mydb"
    source_name: "my-db"
  warehouse:
    url: "postgresql+psycopg2://user:pass@localhost:5432/warehouse"
    source_name: "warehouse"
    schema: "public" # optional, PG schema to scope to
```

Then:

```bash
agentforge-chunk-db list                       # show configured connections (passwords masked)
agentforge-chunk-db export my-db            # chunk one database
agentforge-chunk-db export my-db --version 2026-05-31
agentforge-chunk-db export-all                  # chunk every configured database
```

## Chunk layout on disk

Chunks are JSON files under a versioned tree:

```
data/chunks/<source_type>/<source_name>/v<version>/
```

For example `data/chunks/openapi/my-api/v1.2.3/`.
The root directory is set by `indexer.chunks_dir` (default `/app/chunks` in the container, see below).
Inside a version directory the indexer discovers:

- `_summary.json`: overall source summary
- `_relationships.json`: relationship chunks (SQL schemas)
- subdirectories per chunk kind: `endpoints/`, `schemas/`, `tables/`, `classes/`, `functions/`, `modules/`, `sections/`, `commands/`

Each chunk file is a JSON object.
The fields the indexer relies on:

| Field          | Purpose                                                  |
| -------------- | -------------------------------------------------------- |
| `chunk_id`     | Stable unique id. Hashed into the Qdrant point id.       |
| `text`         | The content that gets embedded and stored.               |
| `content_hash` | Hash of `text`. Drives the unchanged-skip on re-index.   |
| `payload`      | Metadata dict (see below). Stored verbatim on the point. |

The payload carries the filterable metadata.
These keys get a Qdrant payload index (`app/services/vector_service.py`):

```
source_type, source_name, chunk_type, api_name, domain_group, document_name
```

Other payload fields are stored but not indexed: `file_path`, `line_number`, `method`, `path`, `summary`, `signature`, `table_name`, `section_title`, and so on, depending on the source type.

## Writing chunks by hand

The indexer is generic, so you can skip the mappers and produce chunk JSON yourself (from a script or by hand) as long as it lands in the tree above.
Of the fields in the table above, `text` and `chunk_id` are required (a chunk with empty `text` is skipped). `content_hash` and `payload` are optional.

Files are only discovered inside the fixed per-kind subdirectories (`endpoints/`, `schemas/`, `tables/`, `commands/`, `classes/`, `functions/`, `modules/`, `sections/`), plus `_summary.json` and `_relationships.json` at the version root.
For generic docs, `sections/` is the natural home.

A minimal chunk file at `data/chunks/docs/my-notes/v1/sections/intro.json` (strict JSON, the indexer reads it with `json.load`, so no comments or trailing commas):

```json
{
  "chunk_id": "my-notes:intro",
  "text": "AgentForge runs as a Docker Compose stack. ...",
  "content_hash": "any-stable-hash-of-text",
  "payload": {
    "source_type": "docs",
    "source_name": "my-notes",
    "document_name": "intro",
    "chunk_type": "section"
  }
}
```

## Make the chunks visible to the container

`indexer.chunks_dir` defaults to `/app/chunks`, which the compose stack does not mount.
The stack bind-mounts `./data -> /app/data`, so point `chunks_dir` at that path and keep the files under `./data/chunks` (which is the mappers' default output):

```yaml
# config.yaml
indexer:
  chunks_dir: /app/data/chunks
```

Your host tree `./data/chunks/docs/my-notes/v1/sections/*.json` is then visible inside the container.

## Index it

The indexer endpoints live on `agentforge-api` (`:8100`, LAN-only), and embedding needs a reachable backend (the local Ollama by default).

```bash
# Confirm the source was discovered
curl http://localhost:8100/indexer/sources

# Index one source (latest version, auto-detected source_type)
curl -X POST "http://localhost:8100/indexer/index/my-notes"

# ...or pin the version + type, replacing existing points first
curl -X POST "http://localhost:8100/indexer/index/my-notes?version=v1&source_type=docs&clean=true"

# Index every discovered source at once
curl -X POST "http://localhost:8100/indexer/index-all"

# Check what landed
curl http://localhost:8100/indexer/collection
```

A successful run returns `total_chunks`, `indexed`, `unchanged`, `deduped`, and `errors`.
Re-running is cheap: chunks whose `content_hash` is unchanged are skipped, and near-duplicates are dropped by semantic dedup (below).

## Worked example: index AgentForge's own OpenAPI

AgentForge can index its own REST API, which makes the endpoints searchable from the chat UI.
The web app serves its spec at `:8200/openapi.json` (the WebSocket routes are not in it, only REST).
Save the spec to a file and let the OpenAPI mapper chunk it:

```bash
# 1. Save the web app's spec
mkdir -p data/openapi-schemas
curl -s http://localhost:8200/openapi.json -o data/openapi-schemas/agentforge-rest.json

# 2. Chunk it (writes data/chunks/openapi/<slug>/v<version>/)
agentforge-chunk-openapi data/openapi-schemas/agentforge-rest.json

# 3. See the discovered source name (the slug comes from the spec title)
curl http://localhost:8100/indexer/sources

# 4. Index it, using the name from step 3
curl -X POST "http://localhost:8100/indexer/index/<source_name>?source_type=openapi&clean=true"

# 5. Ask about an endpoint over RAG
curl -X POST http://localhost:8100/search/answer \
  -H 'content-type: application/json' \
  -d '{"query": "how do I delete a chat session?"}'
```

In the chat UI, `@docs` then searches these endpoints, for example `@docs how do I delete a chat session?`.
Re-run the chunk + index steps after the API changes. Drop `clean=true` to let `content_hash` skip the operations that did not change.

## The index pipeline

`IndexerService.index_api()` (`app/services/indexer_service.py`) runs two phases.

Phase 1, load and skip unchanged:

1. Discover chunk files for the source/version.
2. Compute a deterministic point id per chunk: MD5 of `chunk_id`, formatted as a UUID. Same `chunk_id` always maps to the same point, so re-indexing replaces in place rather than duplicating.
3. Fetch existing `content_hash` values for those point ids in one Qdrant `retrieve()` call.
4. Skip any chunk whose `content_hash` matches the stored one. These never get embedded. They count as `unchanged`.

Phase 2, embed, dedup, upsert (per batch, size `indexer.batch_size`):

5. Embed the batch via the embedding backend.
6. Run semantic dedup against existing points (see below). Near-duplicates are dropped and counted as `deduped`.
7. Upsert the survivors. The stored payload is the chunk payload plus `text` and an `indexed_at` timestamp.

The call returns a stats dict: `total_chunks`, `indexed`, `unchanged`, `deduped`, `errors`, plus `api_name`, `version`, `timestamp`.

## Deduplication and drift

Three layers, all in `app/services/dedup_service.py` and configured under `dedup`:

1. Content-hash skip (Phase 1). No config, active whenever a chunk carries a `content_hash`. Identical content never re-embeds.
2. Semantic dedup (Phase 2). After embedding, each new vector is searched against existing points. If the nearest existing point scores at or above `dedup.similarity_threshold` (default `0.95` cosine), the new chunk is dropped. Toggle with `dedup.enabled` (default `true`).
3. Drift detection (on demand). `detect_drift()` compares `docs`/`document` chunks against `code` chunks. A doc whose nearest code chunk scores below `dedup.drift_threshold` (default `0.70`) is flagged as likely stale.

## Endpoints

All on `agentforge-api` (`:8100`).
Browse them at `http://<host>:8100/docs`.

Indexer (`app/routes/indexer.py`):

| Method | Path                             | Purpose                                                                                    |
| ------ | -------------------------------- | ------------------------------------------------------------------------------------------ |
| GET    | `/indexer/sources`               | List discovered sources.                                                                   |
| GET    | `/indexer/documents`             | List unique document names.                                                                |
| POST   | `/indexer/index/{api_name}`      | Index one source. Query: `version`, `clean`, `source_type`, `batch_size`, `embed_timeout`. |
| POST   | `/indexer/index-all`             | Index every discovered source. Query: `clean`.                                             |
| GET    | `/indexer/collection`            | Collection metadata (point count, status).                                                 |
| DELETE | `/indexer/collection/{api_name}` | Delete all points for a source.                                                            |
| GET    | `/indexer/dedup/report`          | Scan for near-duplicate pairs. Query: `source_name`, `source_type`, `limit`, `threshold`.  |
| GET    | `/indexer/dedup/drift`           | Doc-vs-code drift report. Query: `source_name`, `limit`, `threshold`.                      |

Search (`app/routes/search.py`):

| Method | Path             | Purpose                                       |
| ------ | ---------------- | --------------------------------------------- |
| POST   | `/search`        | Raw vector search.                            |
| POST   | `/search/smart`  | Intent-aware search plus re-rank.             |
| POST   | `/search/answer` | Full RAG: refine, search, generate an answer. |

The search body (`SearchRequest`) takes `query` (required) plus optional `limit`, `score_threshold`, `source_type`, `source_name`/`source_names`, `chunk_type`, `domain_group`, `document_name`, `brief`, and `session_id`.

Examples:

```bash
# Index one source at a specific version, replacing prior points
curl -X POST "http://localhost:8100/indexer/index/my-api?version=1.2.3&clean=true"

# Ask a RAG question scoped to one source
curl -X POST http://localhost:8100/search/answer \
  -H 'content-type: application/json' \
  -d '{"query": "how do I authenticate?", "source_name": "my-api", "limit": 8}'

# QA: find near-duplicate chunks and drifted docs
curl "http://localhost:8100/indexer/dedup/report?source_type=docs&limit=200"
curl "http://localhost:8100/indexer/dedup/drift?limit=200"
```

## Config keys

From `config.yaml` (`app/config.py`).
Each has an env-var override.

```yaml
qdrant:
  host: localhost # QDRANT_HOST
  port: 6333 # QDRANT_PORT
  collection_name: agentforge_kb # QDRANT_COLLECTION_NAME

embedding:
  dimension: 4096 # EMBEDDING_DIMENSION
  distance_metric: Cosine # EMBEDDING_DISTANCE_METRIC

indexer:
  chunks_dir: /app/chunks # INDEXER_CHUNKS_DIR
  batch_size: 50 # INDEXER_BATCH_SIZE

dedup:
  enabled: true # DEDUP_ENABLED
  similarity_threshold: 0.95 # DEDUP_SIMILARITY_THRESHOLD
  drift_threshold: 0.70 # DEDUP_DRIFT_THRESHOLD

search:
  score_floor: 0.50 # SEARCH_SCORE_FLOOR
  overfetch_factor: 3 # SEARCH_OVERFETCH_FACTOR
  relevance_threshold: 0.60 # SEARCH_RELEVANCE_THRESHOLD
```

The mappers also read two env vars: `AGENTFORGE_CHUNKS_DIR` (default `data/chunks`) and `AGENTFORGE_LOG_LEVEL` (default `INFO`).
`embedding.dimension` must match the model that produced the vectors.
Change it and you must re-index into a fresh collection.

## See also

- [docs/README.md](../docs/README.md): the full docs index.
- [docs/api.md](../docs/api.md): the `/indexer/*` + `/search/*` HTTP endpoints this package feeds.
