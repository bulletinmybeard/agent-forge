# Deploying with custom local domains

How to run the stack on a Mac or Linux box and reach it at your own domains behind an existing Traefik proxy.
The deploy scripts build on the target box (native arch, no cross-compilation) and bind-mount your private config.

This guide assumes the default `full` preset (every service). With a lighter [preset](architecture.md#deployment-presets-light-vs-full) — `deploy-remote.sh --preset light` or `AGENTFORGE_PRESET=light` — the `agentforge-api`, `qdrant`, and SAQ-dashboard services don't run, so their domains (`API_DOMAIN`, `QDRANT_DOMAIN`, `SAQ_DOMAIN`) are unused. The remote also reuses host Redis by default (`AGENTFORGE_REDIS=host`); set `AGENTFORGE_REDIS=container` for a fully bundled box.

**Ollama on the remote box:** when the Ollama provider is selected (the default), the deployed stack reaches it at `host.docker.internal:11434` on the *remote host* — so install and run Ollama there, bound to `0.0.0.0`, with your profile models pulled. Not needed if prompts use a cloud provider (and embeddings a cloud embedder).

This guide uses placeholder values throughout.

Substitute your own:

| Placeholder             | Meaning                                                |
| ----------------------- | ------------------------------------------------------ |
| `my-remote-box`         | SSH host alias (in `~/.ssh/config`) for the target     |
| `192.168.1.100`         | LAN IP of the target box                               |
| `agent.example.com`     | Public domain for the web service (`:8200`)            |
| `agent-web.example.com` | Public domain for the RAG API / indexer (`:8100`)      |
| `qdrant.example.com`    | Domain for the Qdrant dashboard                        |
| `saq.example.com`       | Domain for the SAQ dashboard                           |
| `proxy`                 | Name of the external Docker network your Traefik is on |

## Topology

Six service definitions in `docker-compose.yml`, plus the public overlay in `docker-compose.remote.yml`:

| Service                       | Build                | Port          | Exposure                             |
| ----------------------------- | -------------------- | ------------- | ------------------------------------ |
| `agentforge-api`              | `Dockerfile.api`     | `8100`        | Public via Traefik (`API_DOMAIN`)    |
| `agentforge-web`              | `Dockerfile.web`     | `8200`        | Public via Traefik (`PUBLIC_DOMAIN`) |
| `agentforge-sidecar`          | `Dockerfile.sidecar` | `8300`        | Internal                             |
| `agentforge-worker-saq`       | reuses web image     | none          | Agent job worker                     |
| `agentforge-worker-saq-tools` | reuses web image     | none          | Tools job worker                     |
| `agentforge-saq-web`          | reuses web image     | `8086`        | SAQ dashboard, via Traefik           |
| `qdrant`                      | `qdrant/qdrant`      | `6333`/`6334` | Dashboard via Traefik                |
| `redis`                       | `redis:7-alpine`     | none          | Bundled; off by default on remote (reuses host Redis via `AGENTFORGE_REDIS=host`) |

Four routers get Traefik labels in the remote overlay: `agentforge-web` (Host `agent.example.com`, excluding `/internal`), `agentforge-api` (Host `agent-web.example.com`), `qdrant` (Host `qdrant.example.com`), and `agentforge-saq-web` (Host `saq.example.com`).
Everything else stays on the Docker network.
The `agentforge-api` app exposes the indexer + search endpoints. It has no auth beyond the optional API keys, so set `security.api_keys` (or `AGENTFORGE_API_KEYS`) before exposing it publicly.

The `agentforge-web` router carries a no-buffer middleware and a `1ms` flush interval so streamed responses and the `/ws/chat` / `/ws/botty` WebSockets aren't buffered by the proxy.
Keep those labels if you re-point the router.

## TLS lives in your Traefik, not here

The compose labels only declare `tls=true`, the `websecure` entrypoint, and the Host rules.
There is no ACME resolver, no `mkcert` flow, and no Traefik static config in this repo.
Certificate provisioning (wildcard cert, Let's Encrypt, or whatever you run) is configured in your own Traefik instance.
The stack assumes a `websecure` entrypoint and a cert for your domains already exist on the proxy.

## deploy.env

Copy `deploy.example.env` to `deploy.env` (gitignored) and fill it in.
The variables the scripts read:

| Variable                     | Purpose                                                                                  |
| ---------------------------- | ---------------------------------------------------------------------------------------- |
| `REMOTE_SSH_HOST`            | SSH alias for the target, e.g., `my-remote-box`                                           |
| `REMOTE_DIR`                 | Deploy path on the target, e.g., `/opt/agentforge`                                        |
| `REMOTE_HOST`                | LAN IP/host the native worker uses to reach Redis/Qdrant/web                              |
| `PUBLIC_DOMAIN`              | Traefik Host rule for the web app (`:8200`)                                               |
| `API_DOMAIN`                 | Traefik Host rule for the RAG API / indexer (`:8100`)                                     |
| `QDRANT_DOMAIN`              | Traefik Host rule for the Qdrant dashboard                                                |
| `SAQ_DOMAIN`                 | Traefik Host rule for the SAQ dashboard                                                   |
| `PROXY_NETWORK`              | External Docker network your Traefik is attached to                                       |
| `REMOTE_REDIS_URL`           | Redis URL the containers use (host-native Redis on remote)                                |
| `AGENTFORGE_DISPATCH_MODE`   | `split` (multi-host) or `in_process` (single box)                                         |
| `AGENTFORGE_TOOL_PLUGINS`    | Comma-separated private tool plugins to load                                              |
| `AGENTFORGE_API_KEYS`        | Comma-separated API keys required on the HTTP/WS surface (wins over `config.yaml`)        |
| `AGENTFORGE_REQUIRE_AUTH`    | `1` = refuse to boot without API keys (use on public deploys)                             |
| `AGENTFORGE_ALLOW_INSECURE`  | `1` = boot open even with the Docker socket mounted / no keys (trusted-network escape hatch) |
| `AGENTFORGE_INTERNAL_TOKEN`  | Shared secret for `/internal/*` worker callbacks (sent as `X-Internal-Token`)             |
| `SIDECAR_AUTH_TOKEN`         | Shared secret the web/workers send as `X-Sidecar-Token`; sidecar rejects requests without it |
| `SIDECAR_ALLOW_PRIVATE_URLS` | `1` = let the sidecar fetch private/LAN URLs (otherwise blocked as an SSRF guard)         |
| `AGENTFORGE_PUBLIC_URL`      | Canonical app origin for OAuth redirects (anti-spoofing; see [connectors.md](connectors.md)) |

See [SECURITY.md](SECURITY.md) for what the auth/token stuff protect and a public-deploy checklist.

The deploy script (`scripts/deploy-remote.sh`, via `COMPOSE_ENV`) forwards the domain/network/dispatch vars **and** the auth/sidecar/internal-token vars into compose, so the `${VAR}` references in `docker-compose.remote.yml` and the app containers resolve.

## Deploy and teardown

`scripts/deploy-remote.sh`:

1. Sources `deploy.env`.
1. Rsyncs the repo to `REMOTE_DIR` on the target (excludes `.git`, venvs, caches, `data`, `secrets`, those stay put on the box).
1. Syncs private secrets from `~/.agentforge/` into `REMOTE_DIR/secrets/` when present.
1. Runs, on the target:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.remote.yml \
     up -d --build --scale redis=0
   ```

   `--scale redis=0` skips the bundled Redis. Containers use host-native Redis via `REMOTE_REDIS_URL` (typically `redis://host.docker.internal:6379`).

1. On macOS, installs/refreshes the native local worker (see below) unless `--no-local-worker`.
1. Health-checks `:8100/health` and `:8200/`, then prunes dangling images.

Useful flags: `--no-build`, `--no-cache`, `--config-only` (scp config + recreate, no rsync), `--api-only` / `--web-only` / `--sidecar-only`, and the `--saq-*` / `--workers-only` restart-only fast paths.

`scripts/teardown-remote.sh` brings the stack down while preserving the Qdrant volume and the `data`/`secrets` directories.
Add `--rmi` to also remove the built images, `-y` to skip the prompt.

After deploy you reach:

```
Web:    https://agent.example.com        (agent WS + REST)
API:    https://agent-web.example.com    (RAG index/search)
Qdrant: https://qdrant.example.com/dashboard
SAQ:    https://saq.example.com/
Direct (LAN): http://192.168.1.100:8100  (API)
              http://192.168.1.100:8200  (web/agent)
```

## Native local worker (optional)

Only needed when `AGENTFORGE_DISPATCH_MODE=split`: tools that need host access (SSH keys, the Docker socket, `gh`) run in a worker on the host instead of in a container.
With `in_process`, everything runs on the remote box and you can skip this.

On macOS the worker runs under launchd.
`scripts/setup-local-worker.sh install` renders `worker/com.agentforge.worker-local-tools.plist.template` into `~/Library/LaunchAgents/`, creates a venv, installs `.[service]`, and bootstraps the agent.
It reads `REMOTE_HOST` from `deploy.env` to point `REDIS_URL`, `QDRANT_HOST`, `OLLAMA_HOST`, and `AGENTFORGE_WEB_URL` at the remote box, and runs `saq -v web.server.queue.settings_tools.settings`.

```bash
scripts/setup-local-worker.sh install    # render plist + bootstrap
scripts/setup-local-worker.sh status     # loaded state + tail logs
scripts/setup-local-worker.sh uninstall  # unload + remove plist
```

Logs land in `~/Library/Logs/com.agentforge.worker-local-tools/`.
launchd is macOS-only. On Linux run the same `saq` command under systemd with the equivalent env vars.

## Why some routes aren't in /docs

`agentforge-web` serves REST under `/api/*` (in OpenAPI) and WebSockets at `/ws/chat` and `/ws/botty` (not in OpenAPI, WebSocket operations have no OpenAPI representation).
Routes under `/internal/*` are excluded from public Traefik routing by the web router's Host rule and are reachable only on the Docker network.
The indexing/search API is a separate app on `:8100`, exposed at `API_DOMAIN`. Its `/docs` is served there.
