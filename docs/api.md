# HTTP API

AgentForge is a headless backend, so the REST + WebSocket API is the whole surface.
It runs as two FastAPI apps with different exposure:

| App              | Module              | Port   | Exposure                                                      |
| ---------------- | ------------------- | ------ | ------------------------------------------------------------- |
| `agentforge-api` | `app/main.py`       | `8100` | RAG indexing + search + Knowledge Database. LAN-only.         |
| `agentforge-web` | `web/server/app.py` | `8200` | Chat WebSocket + REST + agent runners. The public entrypoint. |

## Live reference

Both apps auto-generate an interactive OpenAPI spec. That is the canonical, always-current reference:

- `agentforge-web`: `https://<host>/docs` (Swagger UI) and `/openapi.json`.
- `agentforge-api`: `http://<host>:8100/docs` and `:8100/openapi.json` (LAN).

The WebSocket endpoints (`/ws/chat`, `/ws/botty`) do not appear in OpenAPI. It has no representation for WebSocket operations.
They're documented under [The agent WebSocket](#the-agent-websocket-wschat) below.

This page is a hand-written orientation: it details the core developer surfaces and tabulates the rest.
Endpoints under `/internal/*` are a LAN-only worker bridge and are not part of the public API.

## Authentication

API-key auth is off by default (open). When `security.api_keys` is set (see [the auth section in the README](../README.md#authentication)), every HTTP and WebSocket request needs a key, except `GET /health`, `GET /api/health`, and the internal worker callbacks.

- HTTP: send `Authorization: Bearer agf_...` or `X-API-Key: agf_...`.
- WebSocket: browsers can't set headers, so pass the key as a `Sec-WebSocket-Protocol` subprotocol or a `?api_key=agf_...` query param.

---

# agentforge-api (:8100): RAG search + indexing

The knowledge-base API: index prepared chunks into Qdrant and query them, plus the
personal Knowledge Database for user-created entries.
LAN-only. Don't expose it to the internet.

## Search

| Method | Path             | Purpose                                                                     |
| ------ | ---------------- | --------------------------------------------------------------------------- |
| POST   | `/search`        | Raw vector search. Qdrant similarity scores, no LLM.                        |
| POST   | `/search/smart`  | Intent-aware: LLM query refinement, then search + re-rank.                  |
| POST   | `/search/answer` | Full RAG: refine -> embed -> search -> score-gate -> re-rank -> LLM answer. |

All three take the same JSON body (`SearchRequest`):

```jsonc
{
  "query": "how do I authenticate?", // required
  "limit": 8, // optional, default from config
  "score_threshold": 0.5, // optional cosine floor
  "source_type": "openapi", // optional filters ...
  "source_name": "my-api", // single source
  "source_names": ["a", "b"], // or several
  "chunk_type": "endpoints",
  "domain_group": "...",
  "document_name": "...",
  "session_id": "...", // optional, for logging
}
```

`/search/answer` returns `{ query, answer, results, count, intent }`, where `intent` carries the refined query, best score, the relevance threshold, and whether it fell back to general knowledge.

## Indexing

| Method | Path                             | Purpose                                                                                    |
| ------ | -------------------------------- | ------------------------------------------------------------------------------------------ |
| GET    | `/indexer/sources`               | List discovered sources + chunk counts.                                                    |
| GET    | `/indexer/documents`             | List unique document names.                                                                |
| POST   | `/indexer/index/{api_name}`      | Index one source. Query: `version`, `clean`, `source_type`, `batch_size`, `embed_timeout`. |
| POST   | `/indexer/index-all`             | Index every discovered source. Query: `clean`.                                             |
| POST   | `/indexer/upload/{api_name}`     | Upload client-supplied chunks (JSON body), write them to the chunks dir, then index in the background. Returns immediately; poll `/indexer/collection` for the resulting count. Body: `source_type`, `version`, `clean`, `chunks[]`. |
| GET    | `/indexer/collection`            | Qdrant collection metadata (point count, status; status may be `degraded`/`error` if Qdrant is reachable but its full info is unavailable). |
| DELETE | `/indexer/collection/{api_name}` | Delete all points for a source.                                                            |
| GET    | `/indexer/dedup/report`          | Scan for near-duplicate chunk pairs.                                                       |
| GET    | `/indexer/dedup/drift`           | Doc-vs-code drift report (stale docs).                                                     |

See [the chunking guide](../chunking/README.md) for the mappers, the chunk format, the pipeline, and config keys.

## Knowledge Database

Personal knowledge entries (notes, references, documentation, attached documents, cheatsheets, and code snippets).
Stored in dedicated Qdrant collections, separate from the RAG collection.

### Multi-collection routing

Two collections are maintained by default:

| Collection             | Config key                           | Default              | Used by              |
| ---------------------- | ------------------------------------ | -------------------- | -------------------- |
| Knowledge Base (SPA)   | `knowledge.collection_name`          | `knowledge_entries`  | KB SPA (default)     |
| AgentForge Notes       | `knowledge.notes_collection_name`    | `kb_note_entries`    | Notes macOS app      |

Clients select a collection by sending the `X-Knowledge-Collection` header with the collection name. When omitted, the default collection (`knowledge_entries`) is used. WebSocket sessions with `source=notes` auto-scope `kb_search` to the notes collection.

### Content types

`note`, `reference`, `documentation`, `document`, `cheatsheet`, `snippet`

| Type              | Typical use                                              |
| ----------------- | -------------------------------------------------------- |
| `note`            | Free-form notes and short observations                   |
| `reference`       | Bookmarks, URLs, and pointers to external resources      |
| `documentation`   | How-to guides, runbooks, and explanatory prose           |
| `document`        | Attached files and multi-page content (use with `parent_id`) |
| `cheatsheet`      | Command cheatsheets and quick-reference one-liners       |
| `snippet`         | Code samples, config fragments, and API examples         |

### CRUD

| Method | Path                          | Purpose                                                              |
| ------ | ----------------------------- | -------------------------------------------------------------------- |
| POST   | `/knowledge/entries`          | Create + index a single entry (sync). `201` on success, `409` on duplicate content. |
| POST   | `/knowledge/entries/batch`    | Bulk create (up to 100 entries). `202`.                              |
| GET    | `/knowledge/entries/{id}`     | Get a single entry by point ID.                                      |
| PUT    | `/knowledge/entries/{id}`     | Update an entry. Smart re-indexing: re-embeds only when `title`, `content`, or `notes` change; metadata-only changes skip embedding. |
| DELETE | `/knowledge/entries/{id}`     | Delete a single entry. `204`.                                        |
| DELETE | `/knowledge/entries`          | Bulk delete by filter. At least one filter required (no accidental wipe). |

`POST /knowledge/entries` body (`CreateEntryRequest`):

```jsonc
{
  "title": "Docker cleanup unused volumes",  // required, max 200 chars
  "content": "docker volume prune -f",       // required
  "content_type": "cheatsheet",              // required, one of the types above
  "language": "bash",                        // optional
  "tags": ["docker", "cleanup", "PROJ-456"], // optional, auto-lowercased
  "source_url": "https://docs.docker.com/...", // optional
  "notes": "Safe to run -- only removes unattached volumes", // optional
  "project": "AgentForge",                   // optional, default "Uncategorized"
  "metadata": { "filename": "cleanup.md" },  // optional, free-form object stored on the point
  "parent_id": "a1b2c3d4-...",               // optional, link this entry as a child of another (attachments)
  "force_unique": false                      // optional, default false; when true, skip content-hash dedup (always create a new point)
}
```

When a `POST /knowledge/entries` hits a content-hash duplicate and the request includes a `parent_id`, the existing entry is reattached under the new parent instead of returning `409`. The response includes `"_reattached": true`.

`PUT /knowledge/entries/{id}` body (`UpdateEntryRequest`): same fields (including `metadata`), all optional.

`DELETE /knowledge/entries` body (`BulkDeleteRequest`):

```jsonc
{
  "tags": ["deprecated"],           // optional
  "content_type": "note",           // optional
  "before": "2026-01-01T00:00:00Z", // optional ISO8601
  "project": "Salesforce"           // optional
}
```

Response: `{ "deleted": 12 }`.

### Search

| Method | Path                       | Purpose                                                    |
| ------ | -------------------------- | ---------------------------------------------------------- |
| POST   | `/knowledge/search`        | Semantic search with optional filters.                     |
| POST   | `/knowledge/search/smart`  | Same as above, with intent metadata (query refinement).    |

Body (`KnowledgeSearchRequest`):

```jsonc
{
  "query": "docker cleanup unused volumes", // required
  "tags": ["kubernetes"],                   // optional, OR-matched
  "content_type": "cheatsheet",             // optional
  "language": null,                         // optional
  "project": "AgentForge",                  // optional
  "limit": 10,                              // optional, default 10, max 50
  "score_threshold": null                   // optional cosine floor
}
```

Response (`SearchResponse`):

```jsonc
{
  "query": "docker cleanup unused volumes",
  "results": [
    {
      "id": "a1b2c3d4-...",
      "score": 0.94,
      "title": "Docker cleanup unused volumes",
      "content": "docker volume prune -f",
      "content_type": "cheatsheet",
      "language": "bash",
      "tags": ["docker", "cleanup", "proj-456"],
      "source_url": "https://docs.docker.com/...",
      "notes": "Safe to run -- ...",
      "project": "AgentForge",
      "metadata": null,
      "parent_id": null,
      "created_at": "2026-06-20T14:30:00Z"
    }
  ],
  "count": 1
}
```

### Tags & stats

| Method | Path               | Purpose                                       |
| ------ | ------------------ | --------------------------------------------- |
| GET    | `/knowledge/list`  | Slim browse listing: metadata only, no content body (default limit 2000). |
| GET    | `/knowledge/tags`  | Tag faceting: unique tags with counts.         |
| GET    | `/knowledge/stats` | Collection stats: totals, by type, recent, tag count. |

`GET /knowledge/tags` response:

```jsonc
{ "tags": [{ "tag": "docker", "count": 15 }, { "tag": "python", "count": 42 }] }
```

`GET /knowledge/stats` response:

```jsonc
{
  "total_entries": 234,
  "by_content_type": { "snippet": 89, "cheatsheet": 52, "reference": 38, "documentation": 22, "note": 18, "document": 10 },
  "recent_entries": 12,
  "tag_count": 67
}
```

`GET /knowledge/list` response (no `content` field — use `GET /knowledge/entries/{id}` for the body):

```jsonc
{
  "results": [
    {
      "id": "a1b2c3d4-...",
      "title": "Docker cleanup unused volumes",
      "content_type": "cheatsheet",
      "language": "bash",
      "tags": ["docker", "cleanup"],
      "parent_id": null,
      "created_at": "2026-06-20T14:30:00Z",
      "metadata": { "filename": "cleanup.md" }
    }
  ],
  "count": 1
}
```

### Attachment files

Original attachment files (e.g., the PDF that was extracted) can be stored alongside the indexed text. Stored under `knowledge.files_dir` (default `data/knowledge_files`), max size set by `knowledge.max_attachment_bytes` (default 200 MB). Deleting an entry also removes its stored file (*optional).

| Method | Path                                  | Purpose                                                                 |
| ------ | ------------------------------------- | ---------------------------------------------------------------------- |
| HEAD   | `/knowledge/entries/{id}/file`        | Check whether the original file is stored. `200` if yes, `404` if not. |
| GET    | `/knowledge/entries/{id}/file`        | Download the original file. `Content-Disposition` carries the filename. |
| POST   | `/knowledge/entries/{id}/file`        | Upload the original binary for an existing entry. `201`. Merges file metadata into the entry's `metadata` field. |

### Filter, passages & extraction

| Method | Path                                  | Purpose                                                                 |
| ------ | ------------------------------------- | ---------------------------------------------------------------------- |
| POST   | `/knowledge/filter`                   | List entries by metadata filters — no vector search, no query needed.   |
| POST   | `/knowledge/entries/{id}/context`     | Most relevant passages from one entry for a query (page-chunk search).  |
| POST   | `/knowledge/entries/{id}/rechunk`     | Rebuild page chunks for an entry indexed before chunking existed.        |
| POST   | `/knowledge/extract`                  | Extract text from an uploaded file (PDF, text, code, config).           |

`POST /knowledge/filter` body (`FilterRequest`) — every field optional; entries are matched, not ranked:

```jsonc
{
  "content_type": "note",      // optional
  "tags": ["docker"],          // optional, OR-matched
  "project": "AgentForge",     // optional
  "parent_id": "a1b2c3d4-...", // optional — list the children of a parent entry
  "limit": 50                  // optional, default 50, max 200
}
```

Response: `{ "results": [ ...EntryResponse ], "count": N }`.

`POST /knowledge/entries/{id}/context` body (`ContextRequest`): `{ "query": "...", "top_k": 8 }` (`top_k` 1-30, default 8). Returns the best-matching passages plus adjacent pages for context:

```jsonc
{
  "entry_title": "NL-ix Payslips 2025",
  "total_chunks": 13,
  "passages": [
    {
      "text": "...",
      "score": 0.91,        // 0.0 for adjacent-context pages
      "position": 12,
      "page_number": 12,
      "is_adjacent": false  // true when included only as neighbouring context
    }
  ]
}
```

`POST /knowledge/extract` takes a `multipart/form-data` upload (`file`). Large PDFs (>5 MiB) use `pdftotext` first (faster); smaller PDFs go through pdfplumber with `pdftotext` as fallback. Non-PDF files are decoded as UTF-8. Returns the extracted text plus file metadata:

```jsonc
{
  "text": "...extracted text...",
  "metadata": {
    "filename": "report.pdf",
    "extension": ".pdf",
    "size_bytes": 20480,          // original upload size
    "extracted_bytes": 18200,     // UTF-8 size of extracted text
    "mime_type": "application/pdf",
    "pages": 4                    // PDFs only
  }
}
```

`404` on an unknown entry id (`context`, `rechunk`); `400`/`422` on a missing filename or undecodable/empty upload (`extract`).

## Health

`GET /health`: `{ status, qdrant }`. No auth.

---

# agentforge-web (:8200): agent, chat, memory

The public service: the chat WebSocket, the REST API around it, and every agent runner.

## The agent WebSocket: /ws/chat

This is the primary way to drive the agent. REST can't stream the think -> act -> observe loop.

Connect to `wss://<host>/ws/chat` (optionally `?session_id=<uuid>` to resume, `?source=<client>` to namespace the session).
The client sends a query message. The server streams a sequence of typed JSON events as the run progresses, then a final result + summary.

Client -> server (JSON). The prompt goes in `text`; the mode is chosen from the prompt itself (an `@mode` prefix), so there is no `mode` field:

```jsonc
{ "type": "query", "text": "@agent list the markdown files here",
  "session_id": "...",                     // optional; only used to set the id on the first query
  "overrides": { "provider": "ollama",     // optional; provider is stamped once, on the first query
                 "source": "kb" } }         // optional; client tag (write-once); default "web"
{ "type": "cancel" }                       // stop the running job
{ "type": "confirm.response", "request_id": "...", "confirmed": true }
{ "type": "secret.response", "request_id": "...", "value": "..." }  // masked secret (e.g., sudo password)
{ "type": "ping" }                         // heartbeat
```

See [api-examples.md](api-examples.md) for the in-prompt `@mode` / `#source` / `--flag` DSL and runnable `websocat` recipes.

Server -> client: a stream of typed events.
The full set of message types lives in `web/server/protocol.py`. The main families are:

- `session.init` / `session.title`: session lifecycle.
- `agent.routing` / `agent.routed` / `agent.config`: mode + profile selection.
- `tool.call` / `tool.calls.flush`: tool execution as it happens.
- `confirm.request` / `confirm.response`: destructive-action confirmation.
- `secret.request` / `secret.response`: masked secret prompt (e.g., a sudo password). The value is memory-only, doesn't persist, and is never logged.
- `search.meta`: RAG metadata (refined query, filters, scores).
- `prompt.refined`: the opening prompt was rewritten before running (optional; see [modes.md](modes.md#prompt-refinement-optional)).
- `agent.result` / `agent.summary` / `agent.error` / `agent.cancelled`: completion.
- `context.usage`: token-usage estimate after each run.

Every event the client receives is also persisted, so a reconnect can restore the session via the REST endpoints below.

## Sessions & messages

Each chat session row carries a `source` tag (write-once at creation) so external clients can keep their history out of the Agent Chat sidebar. The Agent Chat UI uses `web` (default). Other clients pass `overrides.source` on the first query and/or `?source=` on the WebSocket URL (e.g. `kb` for the Knowledge Base SPA, `felix` / `ask-page` for other frontends). `GET /api/sessions` defaults to `source=web`; pass `source=all` or a specific tag to list others.

| Method | Path                                 | Purpose                                                                |
| ------ | ------------------------------------ | ---------------------------------------------------------------------- |
| GET    | `/api/sessions`                      | List sessions (most recent first). Query: `limit`, `offset`, `source` (default `web`, or `all`). |
| GET    | `/api/sessions/{id}`                 | Session metadata.                                                      |
| GET    | `/api/sessions/{id}/messages`        | Messages (paginated: `limit`, `before`. `limit=0` = all).              |
| GET    | `/api/sessions/{id}/messages/around` | Window around a timestamp. Query: `ts`, `window`.                      |
| GET    | `/api/sessions/{id}/token-usage`     | Real token totals for the session.                                     |
| GET    | `/api/sessions/{id}/job`             | Active worker job for the session, or 404.                             |
| PATCH  | `/api/sessions/{id}`                 | Rename. Body: `{ title }`.                                             |
| DELETE | `/api/sessions/{id}`                 | Delete the session and its messages.                                   |

## Memory

Two stores, both populated by the backend after a successful run (not by the caller):

| Method | Path                                 | Purpose                                                              |
| ------ | ------------------------------------ | -------------------------------------------------------------------- |
| GET    | `/api/memory/stats`                  | Fact count + conversation-memory collection stats.                   |
| GET    | `/api/memory/facts`                  | List extracted facts (newest first).                                 |
| DELETE | `/api/memory/facts/{key}`            | Delete one fact.                                                     |
| DELETE | `/api/memory/facts`                  | Clear all facts.                                                     |
| GET    | `/api/memory/exchanges`              | List stored Q&A exchanges (Qdrant scroll). Query: `limit`, `offset`. |
| DELETE | `/api/memory/exchanges/{id}`         | Delete one exchange.                                                 |
| DELETE | `/api/memory/exchanges`              | Clear all exchanges.                                                 |
| GET    | `/api/memory/schemas`                | Cached SQL schemas (for `@sql`).                                     |
| POST   | `/api/memory/schemas/{db}/scan`      | Refresh a schema cache.                                              |
| DELETE | `/api/memory/schemas/{db}`           | Clear one cached schema.                                             |
| PUT    | `/api/memory/schemas/cache/disabled` | Toggle "always fetch fresh". Body: `{ disabled }`.                   |

These are **read/manage** endpoints. They don't trigger extraction.
Facts (SQLite `user_facts`) and conversation memory (Qdrant `conversation_memory`) are written automatically after each run **only for FULL-tier modes** (`chat`, `search`/`@qdrant`, `pipeline`) and never in incognito.
Investigative modes (`agent`, `web_search`, `research`, `sql`, ...) keep session chat but skip cross-session memory by policy (`web/server/memory_policy.py`).
So a deployment that has only run `@search`/`@agent` will show empty memory. That's expected.

## Other REST groups

Grouped by subsystem. See the live `/docs` for full request/response schemas.

| Group      | Prefix                                                                                           | What it covers                                                                                   |
| ---------- | ------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------ |
| Uploads    | `/api/upload*`                                                                                   | Eager multi-file upload per session. Upload limits.                                              |
| Audit      | `/api/audit/*`                                                                                   | Tool/run audit log + stats (Redis streams).                                                      |
| Results    | `/api/results/*`                                                                                 | Session-scoped result cache (Redis, labelled).                                                   |
| Schemas    | `/api/schemas*`                                                                                  | Global DB schema cache (Redis) for `@sql`.                                                       |
| Scheduler  | `/api/scheduler/*`                                                                               | Cron-style recurring agent jobs (APScheduler).                                                   |
| Monitor    | `/api/monitor/*`                                                                                 | Website-change monitors + checks.                                                                |
| Connectors | `/api/connectors/*`                                                                              | Google (Gmail/Drive/BigQuery/YouTube) OAuth + GitLab token connections (see [connectors.md](connectors.md)). |
| Permissions | `/api/permissions/commands/*`                                                                   | Shell/SSH command policy: YAML baseline + SQLite runtime overrides, dry-run validate (see [SECURITY.md](SECURITY.md#command-permissions-shell--ssh)). |
| Canvas     | `/api/canvas/*`                                                                                  | Per-session pinned-items workspace.                                                              |
| Configs    | `/api/configs*`                                                                                  | Read-only view of whitelisted YAML config files.                                                 |
| Services   | `/api/services*`                                                                                 | Container/service health dashboard + log tail/stream.                                            |
| Catalog    | `/api/catalog/*`, `/api/model-catalog/*`                                                         | Provider/model metadata + cross-provider equivalence (see [model-catalog.md](model-catalog.md)). |
| Misc       | `/api/{welcome,profiles,providers,agents,tools,skills,presets,commands,instructions,dry-run}` | UI-support + config-exposure endpoints. `GET /api/tools` is the runtime tool catalog (name, description, category). |

Many of these exist to back the (separate) chat UI.
The core developer surfaces are search/index (`:8100`), the `/ws/chat` WebSocket, and `/api/memory/*`.

## Internal (LAN-only)

`/internal/*` is a worker bridge (the SAQ workers call back into the web app to read session history, update job status, broadcast events, store snapshots).
It is not part of the public API and is not meant to be reached from outside the Docker network. As defence-in-depth on top of that network isolation, requests must carry the shared `X-Internal-Token` (`AGENTFORGE_INTERNAL_TOKEN`) when it is set! The web service rejects `/internal/*` calls without it. The path is also excluded behind Traefik/NGINX from the public router and being treated as private.
