# AgentForge docs

Guides for running and extending AgentForge.

- [architecture.md](architecture.md): how the Docker stack fits together: the services, ports, worker localities, data stores, and request flow. Start here.
- [api.md](api.md): the HTTP + WebSocket API: search/index endpoints, the `/ws/chat` agent protocol, memory endpoints, and where the live OpenAPI spec lives.
- [api-examples.md](api-examples.md): runnable `curl` + `websocat` recipes, from a first prompt to processing the response, plus the in-prompt `@mode` / `#source` / `--flag` DSL.
- [modes.md](modes.md): the `@mode` prefixes (built-in modes + custom agents + connectors), what each does, and when to use it.
- [tools.md](tools.md): every built-in agent tool, grouped by category, plus locality, confirmation gates, and how plugins add more.
- [SECURITY.md](SECURITY.md): the security controls (auth, sidecar/internal tokens, interactive sudo, SSRF and read-only guards).
- [model-catalog.md](model-catalog.md): comparing models across providers to find equivalents (`/api/model-catalog/*`), the per-provider catalog it draws on (`/api/catalog/*`), the `data/catalogs/*.json` files, the `catalog:*` Redis cache, and the `--with-catalog` deploy flow.
- [chunking/README.md](../chunking/README.md): the chunking mappers (OpenAPI, SQL/tbls, live DB, code, CLI docs, Markdown), chunk layout, the index pipeline, the `/indexer/*` and `/search/*` endpoints, and dedup/drift QA. Served by the `agentforge-api` app on port `8100`.
- [local-domains.md](local-domains.md): deploying the stack to a Mac/Linux box behind an existing Traefik proxy with custom domains, the `deploy.env` parameters, and the optional native (launchd) tool worker.
- [connectors.md](connectors.md): linking external accounts — the unified Google OAuth connector (Gmail, Drive, BigQuery, YouTube) and the GitLab token connector, plus the OAuth client setup and the REST flow.
- [plugin-authoring.md](plugin-authoring.md): adding private tools with `@tool`, the `AGENTFORGE_TOOL_PLUGINS` seam, tool routing, and the private overlay files (`config.yaml`, `custom_agents.local.yaml`, `tool_routing.local.yaml`, `markdown/local/`, `plugins/`).
- [markdown/README.md](../markdown/README.md): the service-level instruction markdown — the `skills/` instruction sets and `custom-agents/` system prompts you can edit to tune agents without touching Python.
- [sandbox/README.md](../sandbox/README.md): the no-UI harness for driving the framework (`AIClient` / `ToolRegistry` / agent loop) directly from a Python script, without the web stack.

## Two apps, two ports

The stack runs two FastAPI apps:

| App              | Module              | Port   | Scope                                                      |
| ---------------- | ------------------- | ------ | ---------------------------------------------------------- |
| `agentforge-api` | `app/main.py`       | `8100` | RAG indexing + vector search. LAN-only.                    |
| `agentforge-web` | `web/server/app.py` | `8200` | Chat WebSocket + REST + agent runners. Public via Traefik. |

Only `agentforge-web` is exposed publicly.
The indexing/search API stays on the LAN.
WebSocket endpoints (`/ws/chat`, `/ws/botty`) never appear in `/openapi.json` or `/docs`. OpenAPI 3.x has no representation for WebSocket operations.
That is expected, not a missing route.
