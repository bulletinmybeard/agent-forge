# HTTP API

AgentForge is a headless backend, so the REST + WebSocket API is the whole surface.
It runs as two FastAPI apps with different exposure:

| App              | Module              | Port   | Exposure                                                      |
| ---------------- | ------------------- | ------ | ------------------------------------------------------------- |
| `agentforge-api` | `app/main.py`       | `8100` | RAG indexing + search. LAN-only.                              |
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

The knowledge-base API: index prepared chunks into Qdrant and query them.
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

## Health

`GET /health`: `{ status, qdrant }`. No auth.

---

# agentforge-web (:8200): agent, chat, memory

The public service: the chat WebSocket, the REST API around it, and every agent runner.

## The agent WebSocket: /ws/chat

This is the primary way to drive the agent. REST can't stream the think -> act -> observe loop.

Connect to `wss://<host>/ws/chat`.
The client sends a query message. The server streams a sequence of typed JSON events as the run progresses, then a final result + summary.

Client -> server (JSON). The prompt goes in `text`; the mode is chosen from the prompt itself (an `@mode` prefix), so there is no `mode` field:

```jsonc
{ "type": "query", "text": "@agent list the markdown files here",
  "session_id": "...",                     // optional; only used to set the id on the first query
  "overrides": { "provider": "ollama" } }  // optional; provider is stamped once, on the first query
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

| Method | Path                                 | Purpose                                                                |
| ------ | ------------------------------------ | ---------------------------------------------------------------------- |
| GET    | `/api/sessions`                      | List sessions (most recent first). Query: `limit`, `offset`, `source`. |
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
Facts (SQLite `user_facts`) and conversation memory (Qdrant `conversation_memory`) are written automatically after each run **only for FULL-tier modes** (`chat`, `search`/`@docs`, `pipeline`) and never in incognito.
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
