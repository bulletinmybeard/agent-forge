# Changelog

All notable changes to AgentForge are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.16.0] - 2026-09-13

### Added

- **`@plan` / `@build`**: investigate a repo, write a markdown plan under `~/agent-forge/plans/`, wait for Approve / Keep drafting, then run queued workers. Plan is read-only. Build writes in place (`write_file unique=false`). Pin to the `local` worker in split mode. See [docs/modes.md](docs/modes.md)
- Build apply bundle next to the plan (`*.apply.json.gz`): **undo the build** restores pre-build files; **apply the changes again** / `@build redo` writes the recorded bytes back without an LLM
- **`@review`** styles: `single` (default, one large-context reviewer), `deep` (specialists + merge), `classic`. Read-only (no `write_file` / `code_edit`). Report written to disk with a timestamped name. Apply-from-review is `@agent`, not `@coding` or `@review`
- Session debug bundle: `GET /api/debug/sessions/{uuid}` (SQLite + audit; optional Loki when `LOKI_URL` is set). Script: `scripts/debug-session.sh`
- Plan-approval confirm `kind=plan`: ignores Yes-all / session auto-accept; UI shows Approve / Keep drafting
- `file.diff` write/edit receipts (`action=written|edited` + `post_hash`) after a successful confirm
- Session title generation and token totals for `@plan` / `@build` / `@review`

### Changed

- Ollama `extra_body` only forwards keys `Client.chat()` accepts. `reasoning_effort: max` / `think: "max"` map to `think: "high"` (the Python client rejects `max` and unknown kwargs, which used to fall coder over to heavy)
- Agent summary tool map counts real calls, not `{name: 1}`
- Confirm broker fail-closed on timeout (deny, not fail-open)

### Fixed

- Plan confirm no longer auto-starts `@build` from leftover Yes-all
- Tool calls persist from `ctx.metadata["agent_iterations"]` (AgentLoop has no `_iterations`)
- Write-file unique remapping of occupied parents (`foo` → `foo_1/`) skipped for plan/build in-place writes

## [0.15.0] - 2026-08-23

### Added

- **`@trip` / `@tripplanner` custom agent**: plans A→B drives or city walking tours with timed stops, then publishes an interactive Leaflet map. OpenRouteService for geocode / directions / POIs (`ORS_API_KEY` or `tools.ors.api_key`). Map at `GET /trips/{uuid}`; toggling a stop re-routes via `POST /api/trips/{uuid}/route` (the key never reaches the browser). Optional detours over +45 min / +50 km vs origin→destination are disabled on publish. See [docs/modes.md](docs/modes.md), [docs/tools.md](docs/tools.md#openrouteservice-trips), [docs/api.md](docs/api.md#trips)
- OpenRouteService + trip tools: `ors_geocode`, `ors_reverse`, `ors_route`, `ors_pois`, `trip_publish`, `trip_get`, `wiki_place_image` (Wikipedia REST thumbnail; never invent image URLs). Routed `remote` in `tool_routing.yaml`
- Trip JSON store under `data/trips` (`AGENTFORGE_TRIPS_DIR` override). FastAPI trip routes registered before the SPA catch-all
- Session profile overrides: Web UI `overrides.profiles[<role>]` (`model` / `temperature` / `max_tokens`) applied in `AIClient` for agent, chat, custom-agent, search, logs, SQL, discovery, coding, scheduler, and monitor
- Fence-aware `<think>` stripping (`agentforge/backends/_thinking.py`) so quoted tags inside fenced/inline code survive. Ollama native `message.thinking` is read instead of dropped
- Agent loop: empty-content / native-thinking nudge, plan-fragment detection after tools, salvage leftover thinking only after a nudge. Backends no longer promote thinking as the final answer (that cut multi-step runs short)
- `agent.summary` `models` chain filled from the runner's `AIClient` (not only the request-scoped contextvar)
- `GET /api/memory/schemas` returns `schema_tool_available` and never 503s when the private SQL plugin is missing
- Compose: `ORS_API_KEY`; bind-mount `markdown/local` and `plugins/`; `deploy-remote.sh` copies those overlays on `--config-only`
- Tests: trip service/API/ORS tools, session overrides, Ollama thinking, inline `<think>` strip, models-used

### Changed

- `mail_api_tools` is private: `plugins.mail_api_tools:register_mail_api_tools` via `AGENTFORGE_TOOL_PLUGINS`. Public `agentforge.tools.mail_api_tools` raises `ImportError`
- `.dockerignore` excludes private overlays (`custom_agents.yaml`, `tool_routing.local.yaml`, `deploy.local.env`, …) and the `scripts/` tree; `plugins/` is still COPY'd so private plugins bake on a private build host
- `Dockerfile.web` creates `/app/data/trips`
- Agent prompt: put the full answer in response content, not only in a private thinking channel; copy tool-result code verbatim

### Fixed

- Naive `<think>` regex no longer destroys quoted examples inside fenced/inline code
- Memory schemas endpoints 503'd the whole Memory modal when `sql_schema_tool` was absent
- Custom-agent / search / logs summaries omitted the models chain

## [0.14.0] - 2026-07-30

### Added

- **Direct tool-run API** for IDE clients (no LLM loop): `POST /api/tools/run`, `GET /api/tools/run/{job_id}`, `DELETE /api/tools/run/{job_id}` (cancel + SAQ abort), `GET /api/tools/run-allowlist`. Allowlisted tools (`linter_run`, `test_runner`, docker/git helpers, …) route via `tool_routing.yaml` to the native tools worker (SAQ) or run in-process. Config `tools_run.allowed_tools` plus `AGENTFORGE_TOOLS_RUN_ALLOW` / `AGENTFORGE_TOOLS_RUN_DENY`. See [docs/api.md](docs/api.md#direct-tool-run) and [docs/tools.md](docs/tools.md#direct-tool-run-api)
- **Playbooks**: curated command-combinations + Jinja2 output templates for `@discover`. Registry `playbooks.yaml` + templates under `markdown/playbooks/*.jinja`, retrieved semantically from a dedicated Qdrant collection (`agentforge_playbooks`, threshold-gated). A match seeds the investigation's probe commands and replaces LLM scoping (single pass, `skip_analysis`), and the rendered template pre-structures the synthesis input. Per-playbook `synthesis: diagnostic|informational` picks the cleanup-plan or descriptive-report prompt. CLI: `python -m agentforge.playbooks.cli reindex|list|match`. Config under `playbooks.*`
- **Recap**: `POST /api/sessions/{id}/recap` returns a cumulative running summary of a session. Only messages after the previous recap are read (its `sequence` is the watermark) and the previous recap text seeds the prompt, so the summary covers the whole session at incremental cost. Persisted as a volatile `recap` message so it survives reloads without entering model context. Idle-safe: fewer than `recap.min_new_messages` (default **2**) or no new messages returns the previous recap with no LLM call. Config under `recap.*`
- **`@rebase` skill** (`markdown/skills/git-rebase.md`): drives the local rebase-onto-default-branch flow, proposes per-file conflict resolutions and waits for approval, and enforces `--force-with-lease` (never bare `--force`, never the default branch)
- **AgentForge Email** knowledge collection: `knowledge.mail_collection_name` (`kb_mail_entries`), allowed via `X-Knowledge-Collection` / session `source=mail`. Search accepts `projects: [...]` (MatchAny) for multi-account All Inboxes filtering
- **JetBrains / IntelliJ custom-agent prompt** (`markdown/custom-agents/intellij.md`) for IDE-context agent runs

### Changed

- Session compaction and recap share one conversation flattener (`web/server/summarise.py`) instead of each carrying its own copy
- `Dockerfile.web` ships `playbooks.yaml` alongside `skills.yaml`
- New dependency: `jinja2` (playbook templates)
- **`linter_run`**: multi-tool quality groups (ruff/flake8, black/isort checks, …); `tool_name=black|isort|flake8|mypy|…` resolves via a built-in known-tool map when missing from config; improved `--fix` for black/isort
- **Secret redaction**: high-entropy catch-all defaults **off** (fewer CamelCase false positives); skip pure CamelCase / snake_case identifiers when HE is on; clearer `SECRET_REDACTION_ENABLED` force on/off; compose + native tools-worker launchd plist default redaction **off** for whole-file coding paths
- `deploy-remote.sh` health probes: retry loop, web checks `/api/health` (not `/`), correct compose container name prefix, non-fatal SSH/health failures so the Done banner still prints

### Fixed

- **Command policy store no longer migrates on the shell/SSH hot path.** Opening the chat DB ran `create_tables()` (a full Alembic upgrade) on every command, which serialised each one behind a SQLite write-lock upgrade (`journal_mode=DELETE`) and deadlocked when the native worker and the web container share `web_chat.db`. A genuinely missing table is now repaired lazily by `get_runtime_override`; YAML policy stays authoritative either way
- **`@discover` no longer double-persists `discovery.plan`** (plan widget rendered twice on session reload)

## [0.13.0] - 2026-07-19

### Added

- **Command permission profiles** (Claude/Grok-style presets): `tools.command_permission_profiles` in config (builtins `tight`, `open`) plus user-saved SQLite profiles. REST under `/api/permissions/profiles` (list, get, apply, put, delete). Synthetic apply ids: `__yaml__` (clear overrides → config only), `__blank__` (empty lists). Migration `004_perm_profiles`. See [docs/SECURITY.md](docs/SECURITY.md)
- Migration **`003_command_policy`**: ensure `command_policy_overrides` exists on DBs already at head without the table
- `agent.tool_exec` **done** events include truncated `output` preview (≤1500 chars) for live clients (Felix `-vv`, Web UI)
- Worker-mode **broadcast** forwards `agent.tool_exec` / `agent.iteration` / `agent.thinking` (previously only `tool.call` / `tool.calls.flush` reached live clients in split mode)
- Dry-run **tools** step for `custom:*` modes uses the custom agent’s declared tool list (not the full profile catalog)
- Docker Compose **bind-mounts** `custom_agents.yaml` and `custom_agents.local.yaml` so private agents (e.g. `@felix`) load on remote deploys
- `deploy-remote.sh` seeds missing custom-agent YAML placeholders and syncs `custom_agents.local.yaml` on config-only deploys
- Read-only gate: pure version/help probes (`npm --version`, `node -v`, …) and bare `version`/`help` subcommands allowed under `read_only` runs

### Changed

- Command policy store uses the chat DB (`web.database_path`, default `data/web_chat.db`)
- Runtime override merge is a **full document** (empty lists clear YAML baseline; no silent fall-through)
- `config.example.yaml` documents permission profiles and uses `data/web_chat.db` as the chat DB example

### Fixed

- Felix/Web clients not receiving tool result previews on SAQ worker runs

## [0.12.0] - 2026-07-18

### Added

- **Command permissions** for `shell` / `ssh`: segment-aware allowlist, denylist, and confirm modes (`tools.*.permissions` in config). YAML baseline plus runtime overrides (SQLite), enforced **before** CommandGuard / user confirm. REST API under `/api/permissions/commands/*` (get/put overrides, dry-run validate with optional draft policy). See [docs/SECURITY.md](docs/SECURITY.md)
- **Alembic migrations** for SQLite: chat DB (sessions, tools, monitor, connectors, **canvas**) and **prompt_lab** DB. Auto-upgrade on web boot; Docker `agentforge-web` entrypoint runs `upgrade-all` before uvicorn/SAQ. Legacy DBs are stamped (no re-CREATE). Applied history in `schema_migrations` (`revision`, **filename**, `applied_at`). CLI: `python -m web.server.database.cli upgrade-all|upgrade|current|applied|history|revision`. See [docs/architecture.md](docs/architecture.md#sqlite-schema-alembic)
- CI **Type Check (ty)** job: [Astral `ty`](https://docs.astral.sh/ty/) (Rust-based, pairs with Ruff) on `agentforge`, `chunking`, `sidecar`, `sandbox`, `tests`
- `[tool.ty]` configuration in `pyproject.toml` with `app/` and `web/` excluded until diagnostics are cleared; `ty` added to the `dev` extra
- `alembic` dependency and console entry point `agentforge-db`

### Changed

- **Dependencies simplified to production + dev only**: all runtime packages (framework, chunking CLIs, and the full headless service stack) live in `[project.dependencies]`; `[project.optional-dependencies]` now has only `dev` (ruff, ty, pytest). Removed granular extras (`bedrock`, `browser`, `service`, `all`, …)
- CI runs on **pull requests only** (removed `push` to `master`) so merge does not duplicate the same lint/build/typecheck pass and added `concurrency` to cancel stale PR runs
- Cleared all `ty` diagnostics in `agentforge/` (138 → 0): `chat()` overloads, tool registry typing, Playwright wait literals, config coercion helpers, and assorted narrowings
- `ChatDatabase.create_tables()` / Canvas / Prompt Lab schema setup use Alembic instead of ad-hoc `create_all` + `ALTER TABLE` blocks
- Shell `allowed_commands` / `blocked_patterns` still work as legacy keys under `tools.shell` when `permissions.*` lists are empty

### Removed

- Hand-written SQLite column migrations inside `web/server/database/manager.py` (replaced by Alembic revisions)

## [0.11.0] - 2026-06-28

### Added

- Apple Reminders agent tools (`reminders_status`, `reminders_lists`, `reminders_show`, `reminders_add`, `reminders_edit`, `reminders_complete`, `reminders_delete`) in `agentforge/tools/reminders_tools.py`
- `remindctl` backend (`brew install steipete/tap/remindctl`) with `osascript` / AppleScript fallback when `remindctl` is absent
- Split-deploy support: reminders tools register on every worker so remote agents pass `has_tool()` and cross-dispatch to the Mac `local` worker. Execution uses EventKit on Darwin only
- Server-side due-date normalization (`tomorrow`, `today`, `tomorrow 09:00`) and rejection of past absolute ISO dates
- Title-to-ID resolution for `reminders_delete` and `reminders_complete` (exact title match against open reminders)
- Agent prompt rule and per-turn reminder-query suffix in `_run_agent` to steer models toward `reminders_*` tools

## [0.10.0] - 2026-06-27

See git history for earlier releases.
