# Security

AgentForge runs a tool-calling agent that can execute shell, SSH, Docker, and SQL. Treat the `agentforge-web` surface as privileged. This page lists the security controls and a checklist for exposing the stack beyond your own machine.

> Experimental project: see the README note! The controls below exist, but review them against your
> own threat model before any internet-facing deploy.

## Authentication

API-key auth is **off by default** (open). Enable it before exposing AgentForge on any untrusted network.

- Set keys via `security.api_keys` in `config.yaml` or `AGENTFORGE_API_KEYS` (comma-separated; env wins here). When set, every HTTP and WebSocket request needs a key except `GET /health`, `GET /api/health`, and the internal worker callbacks.
- Clients send `Authorization: Bearer agf_...` or `X-API-Key: agf_...`. Browsers pass the key as a `Sec-WebSocket-Protocol` subprotocol or `?api_key=agf_...` (query param shows up in proxy logs / prefer the subprotocol).
- `AGENTFORGE_REQUIRE_AUTH=1` makes the app refuse to boot without keys. Use it on public deploys so an unauthenticated surface can never start by accident.
- The app already **fails closed** when the Docker socket is mounted and no keys are set (the container can control the host, so an open surface there is fatal).
- `AGENTFORGE_ALLOW_INSECURE=1` is an escape hatch that boots open even when the above would abort. Use it only on a trusted network during a transition, and remove it once keys are in place.

Enforcement lives in `app/security.py` (`enforce_auth_policy`).

## Internal worker bridge

`/internal/*` lets the SAQ workers call back into the web app. It is never part of the public API.
Set `AGENTFORGE_INTERNAL_TOKEN` (sent as `X-Internal-Token`) so the web service rejects `/internal/*` requests without it.
Defence-in-depth on top of network isolation and Traefik path exclusion. Use the same value for the web and worker containers.

## Browser sidecar

The sidecar (`agentforge-sidecar`, port `8300`) renders pages in a real browser, so it is a prime SSRF target.

- It binds to `127.0.0.1:8300` on the host (not the LAN); containers reach it over the Docker network.
- `SIDECAR_AUTH_TOKEN` (sent as `X-Sidecar-Token`) gates `/extract*` and the unsubscribe endpoint / `/health` stays open. Empty = auth disabled (only safe when the port is off the LAN).
- It refuses private/loopback/link-local/reserved targets by default. Set `SIDECAR_ALLOW_PRIVATE_URLS=1` only if you deliberately need internal-URL extraction.

## Privilege escalation (sudo)

There is no on-disk `sudo_password`. When the `shell` tool needs sudo, the password is requested interactively over the `secret.request` / `secret.response` WebSocket pair (masked in the UI, masked `getpass` on the CLI). The value is memory-only — never persisted or logged. Auto-prepending `sudo` to root-targeted file mutations is opt-in via `tools.shell.auto_sudo` (default off), and even then it surfaces the same prompt — elevation is never silent.

## Other Stuff

- **SQL** (`@sql`): `readonly` connections are enforced at the transaction level (Postgres `SET TRANSACTION READ ONLY`; MySQL session hook + a never-commit backstop), not just by convention.
- **Shell / command tools**: run argv-only (`shell=False`) with proper escaping; the destructive-command guard fails **closed** (treats unknown as destructive) and refuses to run without a confirmation handler.
- **Network / web tools**: `curl_fetch`, `download_file`, `web_fetch_rendered`, and `web_screengrab` validate URLs against SSRF patterns (block link-local, loopback, reserved ranges).
- **Archives**: `archive_extract` validates each member before writing (zip-slip / tar-slip blocked).
- **`notify`**: the `-execute` action is gated behind `tools.notify.allow_execute` (default off).
- **Config viewer**: `/api/configs*` redacts secret values (inline scalars, connection URLs, secret blocks) before returning them.
- **OAuth**: redirect URIs are built from a canonical origin (`AGENTFORGE_PUBLIC_URL` / `PUBLIC_DOMAIN`) rather than spoofable request headers — see [connectors.md](connectors.md).

Deployment instructions live in `deploy.example.env`.
Deployment topology is in [local-domains.md](local-domains.md).
