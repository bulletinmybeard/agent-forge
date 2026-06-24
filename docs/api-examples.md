# API examples

Runnable `curl` (and `websocat`) recipes, from a first prompt to processing the response.
For the full endpoint reference see [api.md](api.md); this page is the hands-on companion.

Two surfaces, two ports:

- `agentforge-api` on `:8100` is the RAG service. Its `/search*` endpoints are plain HTTP, so a prompt-to-answer over your indexed data is a single `curl`.
- `agentforge-web` on `:8200` is the chat/agent service. The agent runs over a **WebSocket** (`/ws/chat`), because it streams a think-act-observe loop that REST can't. Drive it with `websocat` or `wscat`. Everything around it (sessions, results, audit, memory, catalog) is plain HTTP.

The examples use `localhost`. Set shorthands if you like:

```bash
RAG=http://localhost:8100      # agentforge-api
WEB=http://localhost:8200      # agentforge-web
```

On a deployed stack behind Traefik (see [local-domains.md](local-domains.md)), swap these for your domains: `RAG=https://<API_DOMAIN>` and `WEB=https://<PUBLIC_DOMAIN>`. For the WebSocket, use `wss://<PUBLIC_DOMAIN>/ws/chat`.

`jq` is used to pull fields out of responses. It and `websocat` are external tools (`brew install jq websocat`).
Auth is off by default; see [Authentication](#authentication) at the end for the header to add once keys are set.

## Quick checks

Liveness and what is indexed:

```bash
curl $RAG/health                 # RAG service: {"status":"ok", ...}
curl $WEB/api/health             # chat service

curl $RAG/indexer/sources        # which sources are indexed + chunk counts
curl $RAG/indexer/documents      # unique document names
curl $WEB/api/catalog/providers  # model-catalog providers + counts
```

## Ask a question over your data (RAG, `curl`)

This is the prompt-to-answer path that works with plain `curl`.
`/search/answer` runs the full pipeline (refine the query, search Qdrant, re-rank, generate an answer):

```bash
curl -sX POST $RAG/search/answer \
  -H 'content-type: application/json' \
  -d '{"query": "how do I index a new source?"}' | jq -r '.answer'
```

Scope it to one source, cap the results, ask for a short answer:

```bash
curl -sX POST $RAG/search/answer \
  -H 'content-type: application/json' \
  -d '{
        "query": "which endpoints delete a chat session?",
        "source_name": "agentforge-rest",
        "chunk_type": "endpoints",
        "limit": 5,
        "brief": true
      }' | jq -r '.answer'
```

Two lighter variants when you want the hits, not a written answer:

```bash
# Raw vector search: returns {query, results, count}, no LLM
curl -sX POST $RAG/search \
  -H 'content-type: application/json' \
  -d '{"query": "rate limiting", "limit": 5}' | jq '.results[].payload.source_name'

# Intent-aware: LLM refines the query, then searches + re-ranks
curl -sX POST $RAG/search/smart \
  -H 'content-type: application/json' \
  -d '{"query": "where do we validate the api key?", "source_type": "code"}'
```

The `SearchRequest` body accepts `query` (required) plus optional `limit`, `score_threshold`, `source_type`, `source_name`, `source_names` (a list searches several sources at once), `chunk_type`, `domain_group`, `document_name`, `brief`, and `session_id`.

## Drive the agent and chat (`websocat`)

Connect to the WebSocket and send a `query` message. The field is `text` (not `query`), and the mode is chosen from the prompt itself (see the next section), so a bare prompt runs plain chat:

```bash
websocat -n ws://localhost:8200/ws/chat <<<'{"type":"query","text":"hello, what can you do?"}'
```

The server streams a sequence of typed JSON events.
The final answer arrives in an `agent.result` event, whose payload is `{"type":"agent.result","text": "...","elapsed": <seconds>}`.
Pull just that out:

```bash
websocat -n ws://localhost:8200/ws/chat \
  <<<'{"type":"query","text":"@agent list the markdown files under docs/ and count them"}' \
  | jq -rc 'select(.type=="agent.result") | .text'
```

To watch the run unfold (routing, tool calls, result), keep the whole `agent.*` stream:

```bash
websocat -n ws://localhost:8200/ws/chat \
  <<<'{"type":"query","text":"@qdrant how does session compaction work?"}' \
  | jq -c 'select(.type | startswith("agent.") or startswith("tool."))'
```

A typical event order is `session.init`, `agent.routing`, `agent.routed`, `agent.config`, then `tool.call` events during the loop, then `agent.result` and `agent.summary`.

Continue a conversation by reusing the session id. The server creates one on the first query (echoed in `session.init`); pass it back on the next connect:

```bash
# first turn (let the server mint the id, then read it from session.init)
websocat -n ws://localhost:8200/ws/chat \
  <<<'{"type":"query","text":"@agent what os am i on?","session_id":"demo-1"}'

# follow-up in the same session
websocat -n 'ws://localhost:8200/ws/chat?session_id=demo-1' \
  <<<'{"type":"query","text":"and how much disk is free?"}'
```

Pin the backend for a session with `overrides.provider` on the first message (it is stamped once and not changed afterwards):

```bash
websocat -n ws://localhost:8200/ws/chat \
  <<<'{"type":"query","text":"summarise this repo","overrides":{"provider":"ollama"}}'
```

Other client messages: `{"type":"ping"}`, `{"type":"cancel"}` to stop a running job, and `{"type":"confirm.response","request_id":"...","confirmed":true}` to approve a destructive tool when the server sends a `confirm.request`.

## Specify the mode, sources, and flags per prompt

The agent has no separate `mode` field. You select behaviour inline, inside `text`. Three kinds of tokens:

### `@mode` (start of the prompt)

| Prefix             | What it runs                                        |
| ------------------ | --------------------------------------------------- |
| `@chat`            | Plain LLM, no RAG, no tools                         |
| `@qdrant`          | RAG over your indexed data (canonical; works anywhere in text) |
| `@docs`, `@find`   | Aliases for `@qdrant` (same mode)                  |
| `@search`          | Web search                                          |
| `@agent`           | Full tool-calling agent (filesystem, shell, ...)    |
| `@sql`             | Look up the schema, then run SQL                    |
| `@logs`            | Log analysis                                        |
| `@discover`        | Multi-area system discovery                         |
| `@pipeline`        | Typed multi-step pipeline                           |
| `@review`          | Parallel code review                                |
| `@research`        | Multi-agent research                                |
| `@coding`, `@code` | Coding mode (plan, edit, dry-run, undo)             |
| `@scheduler`       | Create/list/delete recurring jobs                   |
| `@monitor`         | Create/list/delete website-change monitors          |

Custom agents add more aliases (`@docker`, `@security`, ...); they are defined in `custom_agents.yaml` (plus `custom_agents.local.yaml` for private overlays such as `@felix`). Connectors add their own (`@google`, `@gitlab`, `@github`, or `@conn`).
Without a prefix the server classifies the prompt for you.

### `#source` (anywhere in the prompt)

`#<name>` filters RAG to that indexed source. Names resolve through `search.source_aliases` in `config.yaml`, so `#api` can expand to several collections. `#<type>` shorthands (e.g., `#help`) map to a `source_type`. Multiple tags combine with OR, and trailing punctuation is stripped (`#mydb?` reads as `mydb`).

### `--flags` (anywhere in the prompt)

| Flag                    | Effect                                                 |
| ----------------------- | ------------------------------------------------------ |
| `--limit=N`             | Cap the number of results                              |
| `--source=<type>`       | Filter by source type (`openapi`, `code`, `docs`, ...) |
| `--api=<name>`          | Filter to one source by name                           |
| `--type=<chunk_type>`   | Filter by chunk type (`endpoints`, `tables`, ...)      |
| `--domain=<group>`      | Filter by domain group                                 |
| `--document_name=<x>`   | Pin a specific document                                |
| `--brief` / `--verbose` | Shorter or fuller answer                               |
| `--no-floor`            | Return hits even below the relevance score floor       |

Putting them together (all inside `text`):

```bash
# scoped RAG, short answer
{"type":"query","text":"@qdrant #git how do I amend the last commit? --brief"}

# only endpoint chunks, top 5
{"type":"query","text":"@qdrant what deletes a session? --type=endpoints --limit=5"}

# agent with a verbose trace
{"type":"query","text":"@agent count the python files under app/ --verbose"}

# query a specific database
{"type":"query","text":"@sql #mydb how many rows are in each table?"}
```

The same intent over the `:8100` REST search is expressed as JSON fields instead of tokens: `#git` becomes `"source_name": "git"`, `--type=endpoints` becomes `"chunk_type": "endpoints"`, `--limit=5` becomes `"limit": 5`. The `@mode` / `#` / `--` DSL itself is parsed by the chat WebSocket, not by `/search*`.

## Process the responses

Everything a run produces is reachable over plain HTTP afterwards.

List recent sessions, then read one session's messages:

```bash
curl -s "$WEB/api/sessions?limit=10" | jq '.[].id'
curl -s "$WEB/api/sessions/demo-1/messages?limit=0" | jq '.messages[] | {type, text}'
```

The result store keeps the final output of each run under a label, for follow-up automation:

```bash
curl -s "$WEB/api/results/demo-1"            | jq '.results[].label'
curl -s "$WEB/api/results/demo-1/agent_result" | jq -r '.text // .'
```

Audit streams record every tool call and run:

```bash
curl -s "$WEB/api/audit/runs?session_id=demo-1" | jq '.entries[] | {query, iterations, tools, duration_ms}'
curl -s "$WEB/api/audit/tools?since_minutes=30" | jq '.entries[].tool_name'
curl -s "$WEB/api/audit/stats?since_minutes=60"
```

Memory (only written for FULL-tier modes like `@chat`, `@qdrant`, `@pipeline`):

```bash
curl -s "$WEB/api/memory/facts"     | jq '.facts[]?'
curl -s "$WEB/api/memory/exchanges?limit=5"
```

## Authentication

Auth is off by default (open).
Once `security.api_keys` is set (or `AGENTFORGE_API_KEYS`), every request needs a key, except `GET /health`.

```bash
# HTTP: either header works
curl $RAG/indexer/sources -H 'Authorization: Bearer agf_your_key'
curl $RAG/indexer/sources -H 'X-API-Key: agf_your_key'

# WebSocket: query param (browsers can't set headers) or the subprotocol
websocat -n 'ws://localhost:8200/ws/chat?api_key=agf_your_key' <<<'{"type":"query","text":"hi"}'
```
