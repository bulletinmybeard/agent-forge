# Docker Best Practices Skill

You have been given this skill because the user's query involves Docker containers, Dockerfiles, or container orchestration. Follow these guidelines when advising on Docker-related topics.

## Dockerfile Guidelines

1. **Multi-stage builds** — Always recommend multi-stage builds to minimise image size. Separate build dependencies from runtime dependencies.
2. **Pin base image versions** — Never use `latest`. Pin to specific digests or version tags (e.g., `python:3.12-slim-bookworm`).
3. **Non-root user** — Always add a `USER` directive. Create a dedicated user with minimal permissions.
4. **Minimise layers** — Combine related `RUN` commands with `&&`. Each layer adds to the image size.
5. **Use COPY, not ADD** — `ADD` has implicit tar extraction and URL fetching which is rarely desired.
6. **HEALTHCHECK** — Include a `HEALTHCHECK` directive for production images.
7. **.dockerignore** — Always ensure a `.dockerignore` exists to exclude `.git/`, `node_modules/`, `__pycache__/`, test fixtures, and IDE files.
8. **Order for cache** — Put rarely-changing instructions (OS deps, pip install) before frequently-changing ones (COPY source code).

## Docker Compose Guidelines

1. **Named volumes** for persistent data — never bind-mount database files directly from the host without a volume abstraction.
2. **Health checks** — Use `healthcheck` in compose to gate `depends_on` with `condition: service_healthy`.
3. **Resource limits** — Set `deploy.resources.limits` for memory and CPU in production compose files.
4. **Environment variables** — Use `.env` files or secrets; never hardcode credentials in compose files.
5. **Network isolation** — Define explicit networks; don't rely on the default bridge network for multi-service setups.

## Container Runtime

1. **Read-only root filesystem** where possible (`--read-only`).
2. **No privileged mode** unless absolutely necessary — use specific capabilities
   (`--cap-add`) instead of `--privileged`.
3. **Log to stdout/stderr** — Let the orchestrator handle log aggregation.
4. **Graceful shutdown** — Ensure the entrypoint handles SIGTERM properly.
   Use `exec` form for ENTRYPOINT to avoid PID 1 signal issues.

## Security Checklist

- [ ] No secrets in the image (use build args or runtime secrets)
- [ ] Base image has no known critical CVEs (`docker scout cves`)
- [ ] No unnecessary packages installed
- [ ] File permissions are restrictive (no world-writable files)
- [ ] Container runs as non-root
- [ ] Network exposure is minimal (only required ports)

## Response Format

When reviewing a Dockerfile or compose file, structure your response as:
1. **Summary** — What the file does in 1-2 sentences
2. **Issues** — Numbered list of problems found, ordered by severity
3. **Recommendations** — Concrete fixes with code snippets
4. **Improved version** — If significant changes are needed, provide the full
   improved file
