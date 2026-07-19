# Changelog

All notable changes to AgentForge are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
