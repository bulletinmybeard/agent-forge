# AgentForge

[![CI](https://github.com/bulletinmybeard/agent-forge/actions/workflows/ci.yml/badge.svg)](https://github.com/bulletinmybeard/agent-forge/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Docker](https://img.shields.io/badge/stack-Docker%20Compose-2496ED?logo=docker&logoColor=white)](./docker-compose.yml)

<!-- prettier-ignore -->
> [!NOTE]
> **Experimental: a personal learning project.**
> AgentForge is active and rough in places, and not fully covered by tests.
> I built it to learn how AI fits into my daily work, and how coding agents like Gemini CLI, Codex, and Claude Code work under the hood: how they decide which tools to call, how they manage chat history and context, and where the sharp edges are.
> Expect breaking changes. Treat it as a sandbox, not a product.

AgentForge is a self-hosted AI agent and RAG platform.
It indexes your code, docs, and data into a vector store and answers questions over them, runs a tool-calling agent (filesystem, shell, Docker, Git, SSH, web, ...), and drives multi-step pipelines, all over pluggable LLM backends selected per task.

It runs as a **Docker Compose stack**.
The full stack is the chat backend, RAG API, queue workers, Qdrant, Redis, and scraper sidecar. A [light preset](#light-mode) trims it to the chat backend + one worker for MacBook use.

## Built on AgentForge

These projects use AgentForge as their backend and don't run without it:

- [agent-forge-ask-page](https://github.com/bulletinmybeard/agent-forge-ask-page): a Chrome side-panel extension — pick any element or scan the whole page, then ask an LLM to extract, summarise, list, translate, or audit it, with real tool calls (web fetch, file download).
- [agent-forge-felix](https://github.com/bulletinmybeard/agent-forge-felix): an autonomous diagnose -> fix -> verify CLI agent for operational problems (Docker, disk, HTTP).

## Features

- **Backends**: Ollama (local + cloud relay), AWS Bedrock, and any OpenAI-compatible API (DeepInfra, OpenRouter, ...). Selected per role via named profiles. Switch the whole stack with one `provider_override`.
- **Agent loop**: think -> act -> observe with tool calling, error recovery, and optional web-search escalation.
- **Tools**: filesystem, shell, system info, Docker, Git, SSH, archives, network diagnostics, web search/fetch/render, media, code editing, and more.
- **RAG**: index OpenAPI/SQL schemas, source code, docs, and transcripts into Qdrant. Query with refinement, reranking, and dedup.
- **Connectors**: link external accounts as agent tools — Gmail, Drive, BigQuery, and YouTube through one Google OAuth client, plus GitLab via a personal access token. Multi-account, in-process, read-only by default.
- **Pluggable**: add your own tools via a `register(registry)` entry point. No fork needed.
- **Pipelines**: typed multi-step runner, parallel fan-out, and discovery.

## Documentation

Operator guides live in [`docs/`](docs/README.md):

- [Stack architecture](docs/architecture.md): how the containers fit together: services, ports, worker localities, data stores, and request flow. **Start here.**
- [HTTP API](docs/api.md): REST + the `/ws/chat` agent WebSocket, memory endpoints, and the live OpenAPI spec.
- [Modes](docs/modes.md): the `@mode` prefixes (built-in modes, custom agents, connectors) and when to use each.
- [Tools](docs/tools.md): every built-in agent tool by category, plus locality and confirmation gates.
- [Chunking and indexing into Qdrant](chunking/README.md): the mappers (OpenAPI, SQL/tbls, live DB, code, CLI docs, Markdown), the index pipeline, the `/indexer/*` + `/search/*` endpoints, and dedup/drift QA.
- [Deploying with custom local domains](docs/local-domains.md): running the stack behind Traefik, the `deploy.env` knobs, and the split-host worker.
- [Connectors](docs/connectors.md): linking external accounts — the unified Google OAuth connector (Gmail, Drive, BigQuery, YouTube) and the GitLab token connector.
- [Authoring tools and private overlays](docs/plugin-authoring.md): adding private tools, the `AGENTFORGE_TOOL_PLUGINS` seam, and the local overlay files.
- [Instruction markdown](markdown/README.md): the `skills/` and `custom-agents/` markdown you edit to tune agents without touching Python.
- [Security](docs/SECURITY.md): the auth model, sidecar/internal tokens, interactive sudo, SSRF and read-only guards.

## Run it locally

You need Docker (with Compose) and an LLM backend.
The default backend is [Ollama](https://ollama.com). **When the Ollama provider is selected (the default), Ollama must be running on the same host as the stack** and your Mac for a local deploy, the remote box for a remote one — and reachable by the containers at `host.docker.internal:11434`, which means it has to listen on `0.0.0.0`, not just `127.0.0.1`:

```bash
OLLAMA_HOST=0.0.0.0:11434 ollama serve     # or set OLLAMA_HOST=0.0.0.0 for a brew/launchd service
```

Pull the models your profiles reference. To run on a cloud backend instead (Bedrock, DeepInfra, OpenRouter), point the providers at it in `framework-config.yaml` and then you don't need Ollama for prompts at all. Note embeddings default to local Ollama, so move those to a cloud embedder too if you want to skip Ollama entirely.

```bash
git clone https://github.com/bulletinmybeard/agent-forge.git
cd agent-forge
scripts/deploy-local.sh          # builds + starts the whole stack
```

The agent backend (WebSocket + REST) is then on **http://localhost:8200** and the RAG API on **http://localhost:8100** (health at `/api/health`).
This repo ships no frontend. Connect a WebSocket client to `/ws/chat`, or build a SPA into the web image to have it served from `:8200`.
Tear down with `scripts/teardown-local.sh` (data preserved unless you pass `--volumes`).

The script seeds `config.yaml` + `framework-config.yaml` from the committed examples on first run. Edit them to point at your backends.

### Light mode

The full stack is eight containers. On a MacBook you can run just the agent, web app + one SAQ worker (+ Redis, + Qdrant) with in-process tool dispatch:

```bash
scripts/deploy-local.sh --preset light
```

Put repeatable local settings in `deploy.local.env` (copy from `deploy.local.example.env`) — preset, plus `AGENTFORGE_QDRANT=host` / `AGENTFORGE_REDIS=host` to reuse services you already run (`brew services`), or `AGENTFORGE_QDRANT=off` to skip the vector DB. It's local-only and kept separate from the remote `deploy.env`. Without Qdrant, RAG/`@docs`/semantic-memory are off (the agent still works); without a web-search key, `@search` is simply unavailable. See [Stack architecture -> Deployment presets](docs/architecture.md#deployment-presets-light-vs-full).

## Service stack

The full stack (default `full` preset). The [light preset](#light-mode) runs only `agentforge-web` + one SAQ worker (+ Redis/Qdrant):

| Service                 | Port     | Role                                                     |
| ----------------------- | -------- | -------------------------------------------------------- |
| `agentforge-web`        | `8200`   | Chat WebSocket + REST + agent runners (the entrypoint).  |
| `agentforge-api`        | `8100`   | RAG indexing + vector search (LAN-only).                 |
| `agentforge-sidecar`    | `8300`   | Hardened browser extraction for stealthy web fetches.    |
| `qdrant`                | `6333`   | Vector database.                                         |
| `redis`                 | internal | Tool cache, SAQ queues, audit streams, pub/sub.          |
| SAQ workers + dashboard | `8086`   | Run the agent/tool jobs. The dashboard shows the queues. |

See [docs/architecture.md](docs/architecture.md) for how it all connects, and [docs/local-domains.md](docs/local-domains.md) for deploying behind a proxy.

## Configuration

`config.yaml` holds app settings (Qdrant, search, memory, per-mode toggles, the API-key list).
`framework-config.yaml` holds backends, credentials, and named profiles. Provider-specific model profiles live under `profiles/providers/*.yaml`.
All of these are gitignored; copy them from the committed `*.example.yaml` templates.
Environment variables override matching keys (`OLLAMA_HOST`, `DEEPINFRA_API_KEY`, `AGENTFORGE_PROVIDER`, ...).
Optional prompt refinement (rewrite the opening prompt for clarity before it runs) is off by default — see `prompt_refinement` in `config.yaml` and [docs/modes.md](docs/modes.md#prompt-refinement-optional).

### Provider profiles

Copy the providers you actually use:

```bash
cp profiles/providers/ollama.example.yaml profiles/providers/ollama.yaml
```

**`ollama.yaml` is the minimum** — Ollama is the default provider and the base layer every capability tier falls back to. The cloud providers (`bedrock`, `deepinfra`, `openrouter`) are optional; only create those if you set `AGENTFORGE_PROVIDER` (or `ai.provider_override`) to one of them. With only `ollama.yaml` present, `/api/providers` lists Ollama as the sole provider — fully functional. The loader skips `*.example.yaml` templates, so they sit harmlessly next to your real configs.

## Authentication

API-key auth is **off by default** (open).
Set one or more keys to require them on every HTTP + WebSocket request. Do this before exposing AgentForge on a public host, since the agent can run shell, SSH, and Docker.

Generate a key (the `agf_` prefix is just a recognisable convention):

```bash
echo "agf_$(openssl rand -hex 32)"                                   # openssl
python3 -c "import secrets; print('agf_' + secrets.token_hex(32))"   # Python fallback
```

Add it to `config.yaml` (or set `AGENTFORGE_API_KEYS`, comma-separated, which wins):

```yaml
security:
  api_keys:
    - "agf_<your_generated_key>"
```

Clients send the key as `Authorization: Bearer agf_...` or `X-API-Key: agf_...`.
Browsers (which can't set WebSocket headers) pass it as a `Sec-WebSocket-Protocol` subprotocol or `?api_key=agf_...`.
`/health` and internal worker callbacks are exempt.

For public deploys, `AGENTFORGE_REQUIRE_AUTH=1` refuses to boot without keys (and the app already fails closed when the Docker socket is mounted with none)! `AGENTFORGE_ALLOW_INSECURE=1` is the trusted-network escape hatch. See [docs/SECURITY.md](docs/SECURITY.md) for the full checklist and [deploy.example.env](deploy.example.env) for every knob.

## Custom tools

Add your own tools without forking: expose a `register(registry)` function and advertise it under the `agentforge.tools` entry-point group (or point `AGENTFORGE_TOOL_PLUGINS` at it).
Full guide: [docs/plugin-authoring.md](docs/plugin-authoring.md).

## Development

```bash
pip install -e ".[dev]"     # ruff + pytest
ruff check .                # lint
ruff format --check .       # formatting
pytest                      # tests
```

CI runs lint, format, and a build + smoke check on every push and pull request (`.github/workflows/ci.yml`).

To exercise the framework directly without the Docker/web stack, see the [sandbox harness](sandbox/README.md): a short Python script driving `AIClient` / `ToolRegistry` / the agent loop against your Ollama.

## License

MIT, see [LICENSE](LICENSE).
