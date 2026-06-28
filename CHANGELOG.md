# Changelog

All notable changes to AgentForge are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- CI **Type Check (ty)** job: [Astral `ty`](https://docs.astral.sh/ty/) (Rust-based, pairs with Ruff) on `agentforge`, `chunking`, `sidecar`, `sandbox`, `tests`
- `[tool.ty]` configuration in `pyproject.toml` with `app/` and `web/` excluded until diagnostics are cleared; `ty` added to the `dev` extra

### Changed

- **Dependencies simplified to production + dev only**: all runtime packages (framework, tools, service stack, SQLAlchemy drivers) live in `[project.dependencies]`; `[project.optional-dependencies]` now has only `dev` (ruff, ty, pytest). Removed granular extras (`bedrock`, `browser`, `service`, `all`, …)
- CI runs on **pull requests only** (removed `push` to `master`) so merge does not duplicate the same lint/build/typecheck pass and added `concurrency` to cancel stale PR runs
- Cleared all `ty` diagnostics in `agentforge/` (138 → 0): `chat()` overloads, tool registry typing, Playwright wait literals, config coercion helpers, and assorted narrowings

## [0.11.0] - 2026-06-28

### Added

- Apple Reminders agent tools (`reminders_status`, `reminders_lists`, `reminders_show`, `reminders_add`, `reminders_edit`, `reminders_complete`, `reminders_delete`) in `agentforge/tools/reminders_tools.py`
- `remindctl` backend (`brew install steipete/tap/remindctl`) with `osascript` / AppleScript fallback when `remindctl` is absent
- Split-deploy support: reminders tools register on every worker so remote agents pass `has_tool()` and cross-dispatch to the Mac `local` worker. Execution uses EventKit on Darwin only
- Server-side due-date normalization (`tomorrow`, `today`, `tomorrow 09:00`) and rejection of past absolute ISO dates
- Title-to-ID resolution for `reminders_delete` and `reminders_complete` (exact title match against open reminders)
- Agent prompt rule and per-turn reminder-query suffix in `_run_agent` to steer models toward `reminders_*` tools
- `tests/test_reminders_tools.py`: registration, remindctl arg building, date validation, title resolution
- `@notes` custom agent example documents Apple Reminders alongside `kb_search` (`markdown/custom-agents/notes.md`)

### Changed

- `@agent` and `@pipeline` base tool sets include all `reminders_*` tools (macOS execution and omitted from tool specs on non-Darwin workers)
- `custom_agents.example.yaml` and `markdown/custom-agents/notes.md` expanded with Reminders tool guidance and scheduler/monitor boundaries

## [0.10.0] - 2026-06-27

### Added

- Attachment file storage: `HEAD/GET/POST /knowledge/entries/{id}/file` for checking, downloading, and uploading original attachment files (e.g., PDFs stored alongside their extracted text)
- `KnowledgeFileService`: filesystem-backed store for original attachments with configurable size limit
- Multi-collection routing via `X-Knowledge-Collection` header: the KB SPA uses `knowledge_entries` (default), AgentForge Notes uses `kb_note_entries`
- `knowledge_registry` module: collection-scoped service caching, FastAPI dependency injection, and `ensure_all_collections()` startup hook
- `force_unique` flag on `CreateEntryRequest`. Bypasses content-hash dedup so the same text can be stored under multiple parents
- Auto-relink: creating a duplicate entry with a `parent_id` reattaches the existing entry under the new parent instead of returning 409
- `metadata` field on `UpdateEntryRequest` (was already on create and now on updatable)
- Config keys: `knowledge.notes_collection_name`, `knowledge.files_dir`, `knowledge.max_attachment_bytes`
- `@kb` and `@notes` custom agent examples in `custom_agents.example.yaml`
- `markdown/custom-agents/notes.md` system prompt for the Notes agent
- Notes-aware WebSocket session routing: `source=notes` sessions auto-scope `kb_search` to the notes collection
- `extracted_bytes` in `/knowledge/extract` response metadata
- pdftotext page-marker formatting (`--- Page N ---`) and scaled timeouts for large PDFs

### Changed

- Knowledge API routes use FastAPI `Depends()` injection instead of a module-level singleton. Each request resolves the correct collection-scoped service
- `KnowledgeVectorService` accepts an optional `collection_name` parameter (defaults to `settings.knowledge.collection_name`)
- PDF extraction prefers `pdftotext` for large files (>5 MiB) and `pdfplumber` for smaller ones (was pdfplumber-first with pdftotext fallback)
- `delete_entry` also removes stored attachment files for the entry (*optionally)

## [0.9.0] - 2026-06-24

### Changed

- RAG `@` aliases centralized in `agentforge/mode_prefixes.py` (shared by `mode_routing` and `intent_classifier`).
- Botty engine reads `analysis_interval`, `max_frequency_seconds`, and `dismissal_cooldown_seconds` from config.
- `canvas.enabled` and `botty.enabled` in `config.yaml` now gate Canvas init and the `/ws/botty` route (defaults remain `true`).
- `prompt_lab.enabled` gates Prompt Lab DB init and `/api/prompt-lab/*` endpoints; `canvas.enabled` also gates the `/api/canvas/*` router (not just init).
- Chat sessions are namespaced by `chat_sessions.source` (`web`, `kb`, …): clients pass `overrides.source` and/or `?source=` on `/ws/chat`; worker auto-create reads the active job's overrides. Agent Chat lists `source=web` only.
- `web/server/api.py` imports hoisted to module top; `sql_schema_tool` stays lazy (private/gitignored module).
- In-code RAG comments in `ws_endpoint.py` now treat `@qdrant` as canonical (`@docs` / `@find` as aliases).
- `OllamaSettings` profile resolution always delegates to `agentforge.config.ConfigManager` (removed duplicate `_merge_profile_chain` fallback).
- Config loading consolidated: `app/config.py` and `agentforge.config.ConfigManager` both use `load_merged_yaml()` (framework-config + config.yaml + split profiles). `ConfigManager.raw` exposes the merged dict. When gitignored config files are absent, `load_merged_yaml()` falls back to the committed `*.example.yaml` templates (CI / fresh clone).
- Legacy per-product Google connector plugins (`gmail`, `google_drive`, `bigquery`, `youtube`) removed; unified `google` connector only. Unmigrated SQLite rows are skipped at startup (see `scripts/list-legacy-connections.py`).
- Knowledge Database content types are now `note`, `reference`, `documentation`, `document`, `cheatsheet`, and `snippet` (replacing the earlier `code` / `command` / `url` / `config` / `error_solution` / `api_example` set). Update clients and any indexed entries accordingly.
- `custom_agents.yaml` is gitignored (per-deployment, like `config.yaml`); shipped template is `custom_agents.example.yaml` with example fallback in the loader.
- RAG search mode: `@qdrant` is canonical; `@docs` and `@find` are documented aliases (all three route to the same mode and can appear anywhere in a prompt).
- Mode prefix detection extracted from `ws_endpoint.py` to `web/server/mode_routing.py`.
- Put.io / Premiumize (`cloud_tools`) moved from `register_core_tools` to `register_optional_tools` (still registered by `register_all_tools` when credentials are set).

### Added

- `GET /knowledge/list`: slim metadata listing for the browse view (no content body; optional `limit`, default 2000).
- `custom_agents.example.yaml` template for custom agents (copy to gitignored `custom_agents.yaml`).
- `scripts/list-legacy-connections.py`: read-only audit of legacy per-product Google connector rows.
- `tests/test_mode_routing.py`: prefix stripping for `@qdrant` / `@docs` / `@find`.
- `tests/test_config_loader.py`: merged YAML parity between `app.config` and `agentforge.config`.
- `tests/test_feature_flags.py`: default `canvas.enabled` / `botty.enabled` settings.
- `tests/test_mode_prefixes.py`, `tests/test_botty_engine.py`: shared RAG aliases and Botty rate limits.
- `web/server/session_source.py`, `tests/test_session_source.py`: shared session `source` resolution for WS and worker paths.

### Removed

- `get_connector_config()` (unused after unified Google OAuth cleanup).
- `strip_agent_prefix()` (unused after mode-routing extraction).
- `translate_legacy_locality()` (empty map; inlined at call sites).
- `tools.shell.sudo_password` startup warning in the CLI (the key was already ignored; interactive sudo is the only path).
- `AGENTFORGE_WORKER_LOCALITY` env fallback in worker role resolution (use `AGENTFORGE_WORKER_ROLE`).
- Legacy `connectors.google.gmail.credentials_dir` config path for OAuth client secrets (use `connectors.credentials_dir` or `GMAIL_CREDENTIALS_DIR`).

### Breaking

- Knowledge Database content type rename (see Changed above).
- Legacy per-product Google connector plugins removed (see Changed above).
- Chat session listing is scoped by `source`; clients must pass `overrides.source` / `?source=` where appropriate.

## [0.8.0] - 2026-06-21

### Added

- **Knowledge Database** (`/knowledge/*` on `agentforge-api`): a personal store for user-created entries (`code`, `command`, `url`, `config`, `error_solution`, `note`, `api_example`) in its own Qdrant collection (`knowledge_entries`), separate from the RAG index. CRUD, bulk create/delete, and semantic search with tag/type/project filters, tag faceting, and stats
- Smart re-indexing on update: re-embeds only when `title`, `content`, or `notes` change; metadata-only edits skip the embedding call
- Parent/child attachments via `parent_id`, with per-page chunking so a parent entry and its attached documents are searchable as passages
- `POST /knowledge/filter`: list entries by metadata filters (incl. `parent_id`) without a vector search
- `POST /knowledge/entries/{id}/context`: most relevant passages from one entry for a query, plus adjacent pages for context
- `POST /knowledge/entries/{id}/rechunk`: rebuild page chunks for entries indexed before chunking existed
- `POST /knowledge/extract`: server-side text extraction from uploaded files (PDF via pdfplumber, `pdftotext` fallback; text/code/config decoded as UTF-8), reusing AgentForge's extraction path instead of frontend JS
- `metadata` free-form field on all knowledge points (request + response)
- `knowledge` config block: `collection_name`, `dedup_threshold`, `composite_template` (env prefix `KNOWLEDGE_`)
- SAQ batch job for bulk knowledge ingestion

## [0.7.0] - 2026-06-18

### Added

- Diff-preview confirmation gate for `write_file` and `append_file` tools -- shows a unified diff and requires user approval before writing, matching the existing `code_edit` flow
- Agent-level destructive shell/ssh guard: `sed -i`, `perl -pi`, and other in-place edits are intercepted via `run_confirm()` before execution
- `sed -i` / `perl -pi` patterns added to CommandGuard's deterministic `_DESTRUCTIVE_PATTERNS` regex
- `sed -i` / `perl -pi` added to the LLM classification prompt (`command_guard.md`) as explicit destructive examples
- `skip_confirm` parameter on `_dispatch_tool()` for callers that already ran confirmation at the agent level

### Fixed

- Cross-role dispatch now respects `skip_confirm` separately from `internal`, preventing double-confirmation on pre-gated tools

## [0.6.0] - 2026-06-14

### Added

- **GitHub connector** (`@github` / `@gh`): token (PAT) auth, github.com-only. Reuses the `gh` CLI by injecting each connection's PAT as `GH_TOKEN` rather than reimplementing the REST API; read-only connections apply a best-effort allowlist of read `gh` subcommands
- Per-connection read/write toggle for token connectors (GitLab, GitHub) via `PATCH /api/connectors/{id}`, re-registering the agent so its tools and prompt reflect the mode
- Answer bookmarks: `command_notes` extended with `kind` + `content`, so agent answers can be saved, not just tool calls
- `source_branch` / `target_branch` filters on `gitlab_merge_requests`
- `needs_url` / `default_url` on connector types, so SaaS-only connectors (GitHub) omit the URL field in the connect form

### Changed

- Token connections are verified with a live API call before they are saved so invalid tokens or hosts now fail immediately instead of creating a potential dead connection
- `/api/agents` returns a `source` field so clients can distinguish connector connections from built-in and custom modes
- Connection listings expose `read_write`
- Expanded `user_context.example.md` with richer explanations

### Fixed

- The name-greeting parser tolerates bullet variations in `user_context.md`

### Removed

- The browser-location feature: `location_service.py`, the `/api/location` endpoint, and the location prompt-injection

## [0.5.0] - 2026-06-13

First public-ready release: cleanup and hardening, a unified Google connector, and the GitLab toolset reintroduced.

### Added

- `GET /api/tools` endpoint listing every registered runtime tool (name, description, category)
- YouTube connector tools: `youtube_search`, `youtube_video_details`, `youtube_channel_details`, `youtube_playlist_items`, `youtube_my_subscriptions`
- Full GitLab connector toolset: 28 `gitlab_*` tools covering projects, branches, merge requests, pipelines, jobs, runners, and users, with a per-connection read/write toggle and confirmation gates on destructive actions
- Configurable per-request history character limit, so clients that send large payloads (e.g., the AskPage DOM) aren't truncated
- Botty semantic search over chat session titles and message content, wired into run calls

### Changed

- Unified the Gmail, Google Drive, and BigQuery connectors into a single `Google` connector with per-connection product selection and a shared OAuth client
- Derive better connector labels automatically (e.g., the local part of an account email)
- Corrected search-provider settings precedence between environment variables and YAML config
- TMDB: accept a v4 API Read Access Token (sent as `Authorization: Bearer`) alongside the v3 API key
- Refreshed the hardcoded modes info returned by the `/agent` endpoint
- Improved sticky-mode handling for the dry-run feature
- Made the `GET` uploads endpoint a catch-all (dropped `session_id` from the path)

### Removed

- Deprecated and outdated configs, files, and logic

### Breaking

- Google connector config moved from the per-product `connectors.gmail`, `connectors.google_drive`, and `connectors.big_query` blocks to a single `connectors.google` block. Update `config.yaml` accordingly (see `config.example.yaml`).

## [0.1.0] - 2026-06-05

### Added

- Initial public release of AgentForge, a self-hosted AI agent and RAG platform
- Multi-backend LLM routing (Ollama, AWS Bedrock, OpenAI-compatible) selected per role via named profiles
- Think -> act -> observe agent loop with tool calling, error recovery, and optional web-search escalation
- Built-in tool registry: filesystem, shell, system info, Docker, Git, SSH, network, web, media, code editing, and more
- RAG over Qdrant with query refinement, reranking, and dedup/drift detection
- Typed multi-step pipelines, parallel fan-out, and discovery
- Connector framework for external accounts (Gmail, Google Drive, GitLab)
- Layered memory: extracted facts, semantic conversation recall, result store, and an audit log
- Docker Compose stack: `agentforge-web`, `agentforge-api`, scraper sidecar, SAQ workers, Redis, and Qdrant
- `/ws/chat` agent WebSocket protocol plus the REST API around it
