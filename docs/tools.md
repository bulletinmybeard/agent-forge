# Tools

The tool-calling modes (`@agent`, `@logs`, the custom agents, and others) reach the world through a registry of built-in tools.
This page lists every tool agent-forge ships, grouped by the module it lives in (`agentforge/tools/`).

The exact set exposed on any one run depends on the mode and the active profile: `@chat` gets none, `@agent` and `@pipeline` get the full set, `@logs`/`@search` get a focused subset, and custom agents get only their allowlist. Some tools are credential-gated and only appear when their key or config is set (TMDB, cloud storage, SQL). Plugins can add more (see [Plugins and availability](#plugins-and-availability)).

## Filesystem

| Tool               | Description                                      |
| ------------------ | ------------------------------------------------ |
| `read_file`        | Read file contents                               |
| `read_dir`         | List directory contents                          |
| `file_info`        | File metadata (size, modified time, permissions) |
| `write_file`       | Write content to a file                          |
| `append_file`      | Append content to an existing file               |
| `create_directory` | Create a directory                               |
| `copy_file`        | Copy a file or directory                         |
| `move_file`        | Move or rename a file or directory               |
| `delete_file`      | Delete a file (asks for confirmation)            |
| `find_files`       | Recursive file search by name or pattern         |
| `find_large_files` | Find files over a size threshold                 |
| `dir_size`         | Calculate a directory's total size               |
| `grep_text`        | Search file contents with a regex                |

## System

| Tool              | Description                                |
| ----------------- | ------------------------------------------ |
| `system_overview` | OS, kernel, hostname, architecture, uptime |
| `cpu_info`        | CPU details and load                       |
| `memory_info`     | RAM, swap, and cache usage                 |
| `disk_usage`      | Disk space per filesystem                  |
| `disk_io`         | Disk I/O statistics (Linux)                |
| `gpu_info`        | GPU info and utilization                   |
| `process_list`    | Top processes by CPU/memory                |
| `service_status`  | systemd service status (Linux)             |
| `network_info`    | Network interfaces, DNS, and ports         |
| `temperatures`    | Hardware temperature readings              |

## Shell

| Tool    | Description                                               |
| ------- | --------------------------------------------------------- |
| `shell` | Run a shell command locally (guarded by a command policy) |

Commands run argv-only (no `shell=True`). The destructive-command guard fails **closed** (treats anything it can't classify as destructive and pauses for confirmation). Sudo is never read from disk. If a command needs it, you're prompted interactively (WebSocket+API). Auto-elevating root-owned actions can be opt-in to via `tools.shell.auto_sudo`. See [SECURITY.md](SECURITY.md).

## Docker

| Tool                     | Description                        |
| ------------------------ | ---------------------------------- |
| `docker_ps`              | List containers                    |
| `docker_logs`            | Container logs (tail limited)      |
| `docker_stats`           | Live container resource usage      |
| `docker_inspect`         | Container or image configuration   |
| `docker_images`          | List local images                  |
| `docker_volumes`         | List volumes                       |
| `docker_networks`        | List networks                      |
| `docker_df`              | Docker disk usage                  |
| `docker_compose_status`  | Compose project status             |
| `docker_cleanup_preview` | Preview what a prune would reclaim |

## Git

| Tool         | Description                               |
| ------------ | ----------------------------------------- |
| `git_clone`  | Clone a repository                        |
| `git_status` | Working tree status                       |
| `git_log`    | Commit history                            |
| `git_show`   | Show a commit (changed files and/or diff) |
| `git_diff`   | Show uncommitted working-tree changes     |

## SSH and remote

| Tool           | Description                         |
| -------------- | ----------------------------------- |
| `ssh`          | Run a command on a remote host      |
| `health_check` | Check a remote host's health        |
| `scp`          | Copy files to or from a remote host |
| `rsync`        | Sync files or directories remotely  |
| `ssh_keygen`   | Generate or inspect SSH keys        |

## Network and diagnostics

| Tool            | Description                            |
| --------------- | -------------------------------------- |
| `download_file` | Download a file from a URL             |
| `curl_fetch`    | Make an HTTP request with curl         |
| `dns_lookup`    | DNS resolution (A, AAAA, MX, TXT, ...) |
| `net_probe`     | Ping and port probe for a host         |
| `http_check`    | HTTP endpoint health/response check    |

`download_file` and `curl_fetch` validate URLs against SSRF patterns (link-local, loopback, and reserved ranges are blocked!).

## Web

| Tool                 | Description                                      |
| -------------------- | ------------------------------------------------ |
| `web_search`         | Search the web and return results                |
| `web_fetch`          | Fetch and extract a page as text                 |
| `web_fetch_rendered` | Fetch a page via headless browser (SPA-friendly) |
| `web_screengrab`     | Screenshot a rendered page                       |

These run in the sidecar (locality `remote`). See [Plugins and availability](#plugins-and-availability). The sidecar validates targets (no private/loopback/link-local URLs unless `SIDECAR_ALLOW_PRIVATE_URLS=1`) and requires `X-Sidecar-Token` when `SIDECAR_AUTH_TOKEN` is set. See [SECURITY.md](SECURITY.md) for more.

## CLI helpers

| Tool                 | Description                               |
| -------------------- | ----------------------------------------- |
| `jq_query`           | Query JSON with a jq expression           |
| `jq_transform`       | Transform JSON with jq                    |
| `yq_query`           | Query YAML/TOML/XML with yq               |
| `yq_convert`         | Convert between YAML, JSON, TOML, and XML |
| `tree_view`          | Directory tree visualization              |
| `gh_command`         | Run GitHub CLI commands                   |
| `ncdu_report`        | Disk usage analysis                       |
| `ytdlp_info`         | Get video metadata (yt-dlp)               |
| `ytdlp_download`     | Download video or audio                   |
| `ytdlp_list_formats` | List available formats for a video        |

## Code editing

| Tool           | Description                                         |
| -------------- | --------------------------------------------------- |
| `code_edit`    | LLM-assisted file edit with verification (confirms) |
| `revert_file`  | Restore a file from its snapshot (confirms)         |
| `revert_lines` | Revert a line range from a snapshot (confirms)      |

## Code quality and testing

| Tool           | Description                                                      |
| -------------- | ---------------------------------------------------------------- |
| `linter_run`   | Run linters, formatters, type checkers (ruff, mypy, eslint, ...) |
| `test_runner`  | Run tests in Docker (pytest/jest/vitest), parse results          |
| `k6_load_test` | k6 HTTP load testing: latency, throughput, error rates           |

## Archives and data

| Tool              | Description                                          |
| ----------------- | ---------------------------------------------------- |
| `archive_create`  | Create a tar/zip archive                             |
| `archive_extract` | Extract an archive                                   |
| `diff_files`      | Compare two files (text, JSON, CSV, YAML, TOML, ...) |

`archive_extract` validates each member's path before writing, so zip-slip / tar-slip archives can't escape the target directory.

## Media

| Tool             | Description                                    |
| ---------------- | ---------------------------------------------- |
| `video_convert`  | Convert/trim video, create GIFs (ffmpeg)       |
| `image_convert`  | Convert image formats                          |
| `image_resize`   | Resize images by dimensions or percentage      |
| `image_optimize` | Compress images for the web                    |
| `image_metadata` | Extract EXIF, dimensions, camera info          |
| `generate_icons` | Generate a favicon/app-icon set from one image |

## Audio

| Tool                    | Description                                |
| ----------------------- | ------------------------------------------ |
| `ardour_extract_ranges` | Extract time ranges from an Ardour session |
| `audio_concat`          | Concatenate multiple audio files           |

## Logs

| Tool           | Description                                        |
| -------------- | -------------------------------------------------- |
| `analyze_logs` | Parse a log file: extract errors, patterns, health |

## Notifications

| Tool            | Description                                            |
| --------------- | ------------------------------------------------------ |
| `notify`        | Send a desktop notification (macOS, terminal-notifier) |
| `notify_list`   | List delivered notifications by group                  |
| `notify_remove` | Dismiss notifications by group                         |

`notify`'s click-to-`-execute` action is gated behind `tools.notify.allow_execute` (default off), so a notification can't run an arbitrary command unless you opt in.

## Infrastructure

Inspect the stack's own datastores. They reach Qdrant via `QDRANT_HOST`/`QDRANT_PORT` and Redis via `REDIS_URL`.

| Tool            | Description                                                       |
| --------------- | ----------------------------------------------------------------- |
| `qdrant_admin`  | Inspect Qdrant: collections, info, sample points, counts, sources |
| `redis_inspect` | Inspect Redis: info, keys, get, dbsize, memory stats (read-only)  |

## TMDB (movies and TV)

Structured movie, TV, and person lookups from [The Movie Database](https://www.themoviedb.org/). Always registered, but every tool errors until a TMDB credential is set: either a (free) v3 API key or a v4 API Read Access Token, supplied via the `TMDB_API_KEY` environment variable or `tools.tmdb.api_key` in `framework-config.yaml`. `@search` prefers these over web search for entertainment queries.

| Tool             | Description                                      |
| ---------------- | ------------------------------------------------ |
| `movie_search`   | Search movies by title (optional year)           |
| `movie_details`  | Full movie info: cast, director, runtime, rating |
| `tv_search`      | Search TV shows by title                         |
| `tv_details`     | Full TV show info: seasons, cast, rating         |
| `person_search`  | Search actors, directors, crew                   |
| `person_details` | Full bio and filmography                         |
| `trending_media` | What's trending now (movie, tv, person, or all)  |
| `multi_search`   | Search movies, TV, and people at once            |

## Cloud storage (optional)

Put.io and Premiumize.me file and transfer management. These register only when their credentials are set: the Put.io tools need `PUTIO_TOKEN`, the Premiumize tools need `PREMIUMIZE_API_KEY`. With neither, none appear.

| Tool                         | Service    | Description                                |
| ---------------------------- | ---------- | ------------------------------------------ |
| `putio_list_files`           | Put.io     | List files in a folder                     |
| `putio_list_recursive`       | Put.io     | Recursively list a folder tree             |
| `putio_search_files`         | Put.io     | Search files by name                       |
| `putio_add_transfer`         | Put.io     | Add a magnet/URL transfer                  |
| `putio_list_transfers`       | Put.io     | List active/recent transfers               |
| `putio_clean_transfers`      | Put.io     | Clear completed/errored transfers          |
| `putio_cancel_transfers`     | Put.io     | Cancel active transfers (confirms)         |
| `putio_delete_files`         | Put.io     | Delete files (confirms)                    |
| `putio_delete_empty_folders` | Put.io     | Delete empty folders (confirms)            |
| `putio_get_download_url`     | Put.io     | Resolve a file to a direct download URL    |
| `premiumize_list_files`      | Premiumize | List files/folders                         |
| `premiumize_search_files`    | Premiumize | Search files by name                       |
| `premiumize_add_transfer`    | Premiumize | Add a transfer                             |
| `premiumize_list_transfers`  | Premiumize | List transfers                             |
| `premiumize_get_direct_link` | Premiumize | Resolve a hoster URL to a direct link      |
| `premiumize_check_links`     | Premiumize | Check URLs against the debrid cache        |
| `premiumize_delete_item`     | Premiumize | Delete an item (confirms)                  |
| `check_hoster_availability`  | Premiumize | Check whether a hoster URL is downloadable |

## Optional SQL tools

Loaded as a plugin (not part of the core set), these back `@sql`. They need the plugin registered via `AGENTFORGE_TOOL_PLUGINS` and a `databases` / `sql_databases` entry in `config.yaml`.

| Tool                 | Description                                                          |
| -------------------- | -------------------------------------------------------------------- |
| `sql_extract_schema` | Extract the full schema (tables, columns, PKs, FKs), cached in Redis |
| `execute_sql`        | Run a SQL query (row-capped; writes confirm)                         |
| `db_query_plan`      | `EXPLAIN ANALYZE`: execution plan, index usage, timing               |

## Plugins and availability

- **Locality.** Most tools run on the host (locality `local`). The web tools (`web_search`, `web_fetch`, `web_fetch_rendered`, `web_screengrab`) run `remote`, in the scraper sidecar. Routing is set in `tool_routing.yaml`.
- **Confirmation.** Destructive tools pause for approval: `delete_file`, `code_edit`, `revert_file`, `revert_lines`, SQL writes, and the Put.io/Premiumize delete + cancel tools. The chat UI shows a dialog with a "yes to all" option for the rest of a run.
- **Credentials.** Some tools only register (or only work) once their secret is set: TMDB (`TMDB_API_KEY`), Put.io (`PUTIO_TOKEN`), Premiumize (`PREMIUMIZE_API_KEY`), and the SQL plugin (`databases` in `config.yaml`). The infra tools (`qdrant_admin`, `redis_inspect`) talk to the stack's own Qdrant/Redis, no extra secret.
- **External binaries.** Some tools shell out and only work if the binary is present: `ffmpeg` (video), ImageMagick (images, icons), `yt-dlp`, `jq`/`yq`, `ncdu`, `tree`, `gh`, `k6`, `ripgrep`. Missing binaries fail that one tool, not the run.
- **Adding tools.** Third-party packages register their own via a `register(registry)` entry point under the `agentforge.tools` group, or the `AGENTFORGE_TOOL_PLUGINS` env var. See [plugin-authoring.md](plugin-authoring.md).

## See also

- [modes.md](modes.md): which mode exposes which tools.
- [api-examples.md](api-examples.md): driving a tool-calling run over the WebSocket.
