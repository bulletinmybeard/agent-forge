# Modes

Every prompt runs in a mode. You pick one with an `@prefix` at the start of the message (see [api-examples.md](api-examples.md) for how to send it). Without a prefix the message goes to plain chat, or the server classifies it for you. `@docs` is the one prefix that can appear anywhere in the prompt; the rest must lead.

Modes also read the in-prompt `#source` filters and `--flags` documented in [api-examples.md](api-examples.md#specify-the-mode-sources-and-flags-per-prompt).

Files uploaded with a prompt (via `/api/upload/{session_id}`) are injected as context for the run. Images go to the model's vision input and text documents are appended to the message. This works in plain chat, `@agent`, and the worker modes (`@search`, `@logs`, `@sql`, `@discover`, `@pipeline`, `@review`, `@research`, `@coding`).

## Built-in modes

| Prefix                | Mode       | What it does                                                                                             |
| --------------------- | ---------- | -------------------------------------------------------------------------------------------------------- |
| _(none)_              | chat       | General LLM knowledge. No vector search, no tools. For indexed data use `@docs`.                         |
| `@docs`               | search     | RAG over your indexed data in Qdrant. `#source` tags filter by source. Can appear anywhere.              |
| `@search`             | web_search | Live web search (`web_search`, `web_fetch`, `web_fetch_rendered`).                                       |
| `@agent`              | agent      | Full tool-calling agent (files, shell, git, Docker, SSH, ...). Iterates until the task is done.          |
| `@sql`                | sql        | Generate and run SQL from natural language. Needs the SQL plugin (see below).                            |
| `@logs`               | logs       | Log analysis: read logs, run shell/SSH, cross-reference with the web.                                    |
| `@discover`           | discover   | Multi-phase investigation: scope the system, probe areas concurrently, synthesize, optionally fix.       |
| `@pipeline`           | pipeline   | Typed multi-step workflow with deterministic tools (no raw shell).                                       |
| `@scheduler`          | scheduler  | Turn natural language into recurring jobs (APScheduler).                                                 |
| `@monitor`            | monitor    | Watch a website for changes: snapshot, then poll on a schedule.                                          |
| `@review`             | review     | Parallel code review: specialist sub-agents merged into one report.                                      |
| `@research`           | research   | Parallel web research: a planner fans out sub-investigations, then merges a sourced report.              |
| `@coding`, `@code`    | coding     | Bulk code transforms with diff preview, confirm, snapshots, and undo.                                    |
| `@conn`, `@connector` | connector  | External account connectors: Google (Gmail, Drive, BigQuery, YouTube) and GitLab. See [connectors.md](connectors.md). |

A few that are worth more than one line:

- **`@docs`** searches the data you have indexed (see [the chunking guide](../chunking/README.md)). Filter to a source with `#name`; names resolve through `search.source_aliases` in `config.yaml`, and multiple tags combine with OR. Example: `@docs #api how do I authenticate? --brief`.
- **`@agent`** runs tools in a think-act-observe loop until the task is done. Destructive operations (delete, edit, ...) pause for a confirmation, with a "yes to all" option to auto-approve the rest of the run.
- **`@sql`** first calls `sql_extract_schema` (cached in Redis) to learn the tables, then generates and runs the query with `execute_sql`. A `#name` tag targets a specific database, and write queries (`INSERT`/`UPDATE`/`DELETE`) require confirmation. This mode needs the optional SQL tools (see [tools.md](tools.md#optional-sql-tools)) plus a `databases` / `sql_databases` entry in `config.yaml`.
- **`@pipeline`** uses purpose-built tools (`read_file`, `grep_text`, `find_files`, `search_knowledge_base`, `execute_sql`, `save_result` / `load_result`, `git_log` / `git_show`) instead of raw `shell`, and keeps a per-session result cache.
- **`@review`** spins up specialist sub-agents (error handling, type design, test coverage, code quality) that read the code independently, then merges their findings. Pass a path or it defaults to the current directory.
- **`@coding`** (alias `@code`) is map-reduce: ripgrep discovery and regex narrowing are deterministic, only the per-file edit runs through an LLM, and those calls fan out in parallel. You get unified-diff preview cards, then a confirm, then verified writes with a snapshot. Undo a whole run with `@coding undo <id>`. A path argument is required.

Some modes depend on services that a [light deployment](architecture.md#deployment-presets-light-vs-full) may not run: `@docs` needs Qdrant (off when `AGENTFORGE_QDRANT=off`), and `@search` needs a configured web-search provider key. When the backing service is absent the mode is simply unavailable, not broken.

## Prompt refinement (optional)

Off by default. When `prompt_refinement.enabled` is set in `config.yaml`, AgentForge rewrites your **opening** prompt for clarity/grammar/facts before the model runs, using the `input-refiner` profile (a light model). It applies to the Prompt Lab (`/api/prompt-lab/run`, every call) and the agent endpoint (`/ws/chat`, the chat/agent/custom-agent modes, **first message of a session only** — follow-up turns are left as typed). Search/RAG modes are excluded; they already refine their *query* for embedding (the separate `refinement.input_enabled` setting).

The original is always kept: the lab response carries `original_prompt` + `refined_prompt` (set only when the text actually changed), and the agent emits a `prompt.refined` event. A refiner failure never breaks the run — it falls back to the original prompt.

## Custom agents

Custom agents are focused presets defined in `custom_agents.yaml` and loaded at startup. Each is an `@agent`-style loop restricted to a curated tool allowlist and a task-specific system prompt. Edit the file and restart to add your own.

| Prefix      | Agent          | Purpose                                                            |
| ----------- | -------------- | ------------------------------------------------------------------ |
| `@docker`   | docker-ops     | Docker container and Compose management                            |
| `@debug`    | debug          | Systematic root-cause investigation across logs, code, and infra   |
| `@security` | security-audit | Security scan: secrets, dependencies, Docker, SSL, configurations  |
| `@perf`     | perf-analysis  | Performance profiling: slow queries, resource bottlenecks, latency |
| `@health`   | infra-health   | Full-stack infrastructure health check                             |
| `@test`     | test-mode      | Run tests, diagnose failures, and suggest fixes                    |
| `@api`      | api-test       | API endpoint testing, validation, and exploration                  |
| `@felix`    | felix          | Autonomous diagnostic-repair: diagnose, fix, verify (Docker, disk/system, HTTP) |

The chat UI lists whatever agents are currently configured (it reads `GET /api/agents`), so your set may differ from the defaults above.

## Connectors (`@conn`)

`@conn` reaches external accounts you have linked, with multi-account support. Two connectors ship: **Google** (one OAuth connection where you pick the products — Gmail, Drive, BigQuery, YouTube) and **GitLab** (a personal access token with a read/write toggle and the full `gitlab_*` toolset). Each connection gets a label that doubles as a `#hashtag` for targeting (`@conn #work-gitlab list my open MRs`); without a tag the mode routes by keyword and recency, and you can also address a connector directly (`@google`, `@gitlab`). Connector tools run in-process (they are bound to each connection's credentials) rather than on the worker queue. Setup is in [connectors.md](connectors.md).

## See also

- [tools.md](tools.md): the individual tools each agent mode can call.
- [api-examples.md](api-examples.md): sending a prompt with a mode, `#source`, and `--flags`.
