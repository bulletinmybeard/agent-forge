# Changelog

All notable changes to AgentForge are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Knowledge Database content types are now `note`, `reference`, `documentation`, `document`, `cheatsheet`, and `snippet` (replacing the earlier `code` / `command` / `url` / `config` / `error_solution` / `api_example` set). Update clients and any indexed entries accordingly.
- `@felix` moved out of tracked `custom_agents.yaml` into `custom_agents.local.yaml.example` (copy to gitignored `custom_agents.local.yaml` to enable).
- RAG search mode: `@qdrant` is canonical; `@docs` and `@find` are documented aliases (all three route to the same mode and can appear anywhere in a prompt).

### Added

- `GET /knowledge/list`: slim metadata listing for the browse view (no content body; optional `limit`, default 2000).
- `custom_agents.local.yaml.example` template for private agent overlays.

### Removed

- `tools.shell.sudo_password` startup warning in the CLI (the key was already ignored; interactive sudo is the only path).
- `AGENTFORGE_WORKER_LOCALITY` env fallback in worker role resolution (use `AGENTFORGE_WORKER_ROLE`).
- Legacy `connectors.google.gmail.credentials_dir` config path for OAuth client secrets (use `connectors.credentials_dir` or `GMAIL_CREDENTIALS_DIR`).

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
