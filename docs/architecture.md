# Stack architecture

AgentForge runs as a Docker Compose stack.
It is not a library or CLI you install from PyPI: the agent loop, RAG search, queue workers, and chat backend all depend on each other.
The full stack is eight services; a [light preset](#deployment-presets-light-vs-full) trims it to the web app + one worker for MacBook use.

## Services

The full stack (`scripts/deploy-local.sh`, the default `full` preset) starts eight services backed by two named volumes:

| Service                       | Image / build        | Port            | Role                                                          |
| ----------------------------- | -------------------- | --------------- | ------------------------------------------------------------- |
| `qdrant`                      | `qdrant/qdrant`      | `6333` / `6334` | Vector database (REST + gRPC).                                |
| `redis`                       | `redis:7-alpine`     | internal        | Tool cache, queues, audit streams, pub/sub, schema cache.     |
| `agentforge-api`              | `Dockerfile.api`     | `8100`          | RAG indexing + vector search. LAN-only.                       |
| `agentforge-web`              | `Dockerfile.web`     | `8200`          | Chat WebSocket + REST + agent runners. The public entrypoint. |
| `agentforge-sidecar`          | `Dockerfile.sidecar` | `8300`          | Hardened Firefox extraction for stealthy web fetches.         |
| `agentforge-worker-saq`       | web image            | n/a             | SAQ worker for agent jobs (`settings_shared`).                |
| `agentforge-worker-saq-tools` | web image            | n/a             | SAQ worker for tool jobs (`settings_tools`).                  |
| `agentforge-saq-web`          | web image            | `8086`          | SAQ queue dashboard.                                          |

The worker and dashboard services reuse the `agentforge-web` image, so a build produces three images: `agentforge-{api,web,sidecar}`.
The sidecar publishes to `127.0.0.1:8300` on the host (not the LAN!). Other containers reach it as `agentforge-sidecar:8300` over the Docker network. It's token-gated and SSRF-guarded. See for more: [SECURITY.md](SECURITY.md).
The `agentforge-qdrant-data` and `agentforge-redis-data` volumes hold the vector index and Redis append-only data. Per-session SQLite databases live in `./data`.

## Deployment presets (light vs full)

You don't have to run everything. Which services start is controlled by Docker Compose `profiles:`, and the deploy scripts derive `COMPOSE_PROFILES` from a preset:

| Preset    | Services (besides the core)                              | Use case                           |
| --------- | ------------------------------------------------------- | ---------------------------------- |
| **full**  | `api`, `sidecar`, `dashboard` (+ `qdrant`/`redis`)      | the complete stack, remote deploys |
| **light** | just `qdrant` + `redis` (or none, with host reuse)      | a MacBook; just driving the agent   |

The always-on core is `agentforge-web` + `agentforge-worker-saq` (no profile). Everything else is opt-in via a profile: `redis`, `qdrant`, `api`, `sidecar`, `dashboard`, `full` (= api + sidecar + dashboard), and `split` (the tools worker).

**Dispatch is set by topology, not by the preset.** A single-host **local** box always uses `in_process`: the one shared worker runs tool jobs too, so the `split` tools-worker never runs. A **remote** deploy defaults to `split` (host-access tools run on a native worker on your own machine — see [Worker locality](#worker-locality-saq)), which is when the `split` profile / tools-worker is added. So a *local* `full` stack is seven containers (no tools worker); a *remote* `full` stack is seven too (no bundled Redis, but with the tools worker).

Pick a preset with `scripts/deploy-local.sh --preset light` (or `AGENTFORGE_PRESET` in `deploy.local.env` for local / `deploy.env` for remote); `--profiles "a,b,c"` is the raw override. The same flags work for `deploy-remote.sh`.

**Reusing host services.** Set `AGENTFORGE_REDIS=host` and/or `AGENTFORGE_QDRANT=host` to skip the bundled container and point the app at a Redis/Qdrant already running on the host (`host.docker.internal` on macOS, via `HOST_REDIS_URL` / `HOST_QDRANT_HOST`). `AGENTFORGE_QDRANT=off` drops the vector DB entirely.

**What degrades in light mode.** Redis is always required (queues + session events). Without Qdrant, RAG search (`/search`, `/answer`), `@qdrant` (and its `@docs`/`@find` aliases), and semantic conversation memory are unavailable — the app boots and logs a warning rather than failing (set `memory.semantic.enabled: false` in `config.yaml` to silence it). Web search is unavailable unless a provider key is configured, so `@search` simply doesn't show up. The sidecar being off means `web_fetch_rendered` falls back to local Playwright.

## The two apps

The stack runs two FastAPI apps with different exposure:

- **`agentforge-api`** (`app/main.py`, `:8100`): the indexing + search API (`/indexer/*`, `/search/*`). Kept on the LAN. Not meant to face the internet.
- **`agentforge-web`** (`web/server/app.py`, `:8200`): the chat WebSocket (`/ws/chat`, `/ws/botty`), the REST API, and every agent runner. This is the service you put behind a proxy. It also serves a built React SPA if one is present at the client-dist path. This repo ships none, so bring your own client.

WebSocket endpoints don't appear in `/openapi.json` or `/docs`. OpenAPI has no representation for them.
That is expected, not a missing route.

## The embedded framework

`agentforge-web` imports the `agentforge` package directly, the agent loop, profile router, backend clients, and tool registry, vendored in so the service is self-contained:

- **Agent loop**: the think -> act -> observe iteration behind every tool-calling mode. It reads each tool's locality to decide whether to run it in-process or hand it to a SAQ worker.
- **Backend clients**: Ollama, AWS Bedrock, and any OpenAI-compatible endpoint (DeepInfra, OpenRouter, ...), with shared retry handling.
- **Profile router**: classifies each prompt and picks a profile before the real work starts.
- **Tool registry**: the built-in tools plus any you add through the plugin seam (see [plugin-authoring.md](plugin-authoring.md)).

## Data stores

| Store             | Holds                                                                                              |
| ----------------- | -------------------------------------------------------------------------------------------------- |
| Qdrant            | Document vectors, the semantic conversation-memory collection, and the personal Knowledge Database (`knowledge_entries`). |
| Redis             | Tool-result cache, SAQ queues, audit streams, session pub/sub, schema cache.                       |
| SQLite (`./data`) | Chat history, extracted facts, scheduler/monitor jobs, and the Canvas / Prompt Lab / Botty stores. |

## Worker locality (SAQ)

Every tool carries a locality tag.
The agent loop routes each tool call to a worker on the host that can actually run it, dispatched through SAQ (`saq[hiredis]`) over Redis:

- **All-in-Docker (local).** Every container runs on one host, so the workers run role `remote` and execute every tool inside the worker containers. This is what `scripts/deploy-local.sh` gives you, nothing else to set up.
- **Split-host (remote).** The stack runs on a server while a native worker runs on a second machine (e.g., your MacBook) for tools that need that host's shell, SSH keys, or Docker socket. `scripts/deploy-remote.sh` deploys the stack and installs the native worker. Routing is keyed on `AGENTFORGE_DISPATCH_MODE` / `AGENTFORGE_WORKER_ROLE`.

## Request flow

A chat message takes this path:

1. The client opens a WebSocket to `agentforge-web` (`/ws/chat`).
2. The web app classifies the message into a mode and starts the matching runner.
3. RAG modes call `agentforge-api` -> Qdrant for retrieval. Agent modes run the tool loop.
4. Tools run in-process or get dispatched to a SAQ worker by locality. The sidecar handles stealthy web fetches.
5. Redis caches tool results and carries memory + audit events. SQLite persists the session.
6. The answer streams back over the WebSocket.

## Optional web features

These ship with `agentforge-web` but are independent of core chat/RAG. Each has its own SQLite tables under `./data/` (Canvas shares the chat DB file).

| Feature      | Toggle / surface                         | Purpose                                                                 |
| ------------ | ---------------------------------------- | ----------------------------------------------------------------------- |
| **Canvas**   | `canvas.enabled` (default `true`)        | Per-session scratch pad: auto-collects URLs, `#tags`, and attachments. REST at `/api/canvas/*`; `session.init` reports `canvas_enabled`. |
| **Botty**    | `botty.enabled` (default `true`)         | Proactive session-awareness companion on `/ws/botty` (nudges, recall). Disable to drop the WebSocket route entirely. |
| **Prompt Lab** | `prompt_lab.enabled` (default `true`)  | Multi-profile prompt comparison for developers/UI: `/api/prompt-lab/*` (separate `prompt_lab.db`). Uses opening-prompt refinement when `prompt_refinement.enabled` is set. |

**Session namespacing.** `chat_sessions.source` tags which client created a session (`web` for the Agent Chat UI, `kb` for the Knowledge Base SPA, etc.). The tag is stamped once at creation from `overrides.source`, the WebSocket `?source=` query param, or the active worker job's overrides. `GET /api/sessions?source=web` (the default) keeps external sessions out of the Agent Chat sidebar.

Botty honors `analysis_interval` (process every Nth completed run), `max_frequency_seconds` (minimum gap between nudges), and `dismissal_cooldown_seconds` (quiet period after a dismiss). Model roles and the Qdrant `insights` collection are configured under `botty:` but not yet used by the engine.

## Configuration

Two files at the repo root, both gitignored (copy from the `*.example.yaml`):

- **`config.yaml`**: app config: Qdrant, search, memory, per-mode toggles, and the API-key list.
- **`framework-config.yaml`**: backends, credentials, and named profiles. Provider-specific model maps live under `profiles/providers/*.yaml`.

Environment variables override matching keys (`OLLAMA_HOST`, `DEEPINFRA_API_KEY`, `AGENTFORGE_PROVIDER`, ...).
`profiles/` is bind-mounted read-only, so a profile change is a config redeploy, not an image rebuild.

## Running it

Both modes need a reachable LLM backend. When the Ollama provider is selected (the default), **Ollama must be running on whichever host runs the stack** — your Mac locally, the remote box for a remote deploy — listening on `0.0.0.0:11434` so the containers reach it via `host.docker.internal`, with your profile models pulled. If prompts point at a cloud provider you don't need Ollama for them; only embeddings still default to local Ollama (switch them to a cloud embedder to drop Ollama entirely).

- **Local (everything in Docker):** `scripts/deploy-local.sh` builds and starts the full stack on `localhost`. Tear down with `scripts/teardown-local.sh` (data preserved unless you pass `--volumes`).
- **Remote (behind a proxy):** `scripts/deploy-remote.sh` adds the `docker-compose.remote.yml` overlay (Traefik labels + host Redis) and the native worker. See [local-domains.md](local-domains.md).
