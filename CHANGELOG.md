# Changelog

All notable changes to AgentForge are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
