# Connectors

Connectors link AgentForge to external accounts and expose their data as agent tools. Two kinds ship today:

- **Google** (`@google` / `@conn`): one OAuth connection where you pick which products to enable — Gmail, Drive, BigQuery, and YouTube (read-only, except BigQuery query execution).
- **GitLab** (`@gitlab` / `@conn`): a personal access token (no OAuth redirect), with a read/write toggle and the full `gitlab_*` toolset.
- **GitHub** (`@github` / `@gh` / `@conn`): a personal access token (no OAuth redirect), github.com-only, reusing the `gh` CLI with a read/write toggle.

Multi-account is supported: link several Google accounts, GitLab instances, and/or GitHub accounts side by side. Each connection gets a label that doubles as a `#hashtag` for targeting (`@conn #work-gitlab list my open MRs`); without a tag the mode routes by keyword and recency. Connector tools run **in-process** — they are bound to each connection's live credentials — rather than on the worker queue.

Tokens are encrypted at rest (Fernet) in the `connections` SQLite table. The flows run through REST endpoints on `agentforge-web`, not a standalone script.

## The Google connector

`agentforge/connectors/google/` implements a standard authorization-code OAuth flow with offline access (refresh tokens). One client, one consent screen, many products.

| Product  | What the tools do | Scope             |
| -------- | ----------------- | ----------------- |
| Gmail    | Read mail         | `gmail.readonly`  |
| Drive    | Read files        | `drive.readonly`  |
| BigQuery | Run queries       | `bigquery`        |
| YouTube  | Search and read   | `youtube.readonly`|

Base scopes (`openid`, `userinfo.email`) are always requested so the connection can resolve the account email for its label. The scopes actually sent are that base set plus one per selected product, so connect with only the products you need.

- Auth URI: `https://accounts.google.com/o/oauth2/v2/auth`
- Token URI: `https://oauth2.googleapis.com/token`
- Callback path: `/api/connectors/auth/callback`
- Default alias: `@google`, plus a label-derived slug

### 1. Create the OAuth client in GCP

1. In the Google Cloud Console, pick or create a project.
2. Enable the APIs for the products you plan to use: Gmail API, Google Drive API, BigQuery API, YouTube Data API v3.
3. Configure the OAuth consent screen. While testing, add your Google account as a test user so consent succeeds.
4. Create an OAuth client credential. A Desktop or Web client both work. The loader accepts either the `installed` or `web` key in the downloaded JSON.
5. Note the redirect URI. The callback path is `/api/connectors/auth/callback` on the web app. If the client JSON contains a `redirect_uris` entry the connector uses the first one. Otherwise it builds the URI from a **canonical app origin**: `AGENTFORGE_PUBLIC_URL` if set, else `https://{PUBLIC_DOMAIN}`, producing `{origin}/api/connectors/auth/callback`. Only when neither is set does it fall back to deriving the origin from request headers (`X-Forwarded-Proto` / `X-Forwarded-Host`) — that path is for local dev; set `AGENTFORGE_PUBLIC_URL` (or `PUBLIC_DOMAIN`) behind a proxy so a spoofed `Host` / `X-Forwarded-*` header can't steer the redirect. Register the URI that matches your canonical origin!

### 2. Provide the client credentials

Two ways. Either drop the downloaded `client_secret.json` into the credentials directory, or set the client id/secret in config.

Credentials directory resolution order: `GMAIL_CREDENTIALS_DIR` env, `connectors.credentials_dir` in config, then `~/.agentforge/`. In the container the default is `/app/secrets`, which the deploy script populates from your local `~/.agentforge/`.

Config keys (`config.example.yaml`):

```yaml
connectors:
  encryption_key: "" # env CONNECTOR_ENCRYPTION_KEY
  credentials_dir: /app/secrets
  google:
    client_id: "" # env CONNECTOR_GOOGLE_CLIENT_ID
    client_secret: "" # env CONNECTOR_GOOGLE_CLIENT_SECRET
```

All four products share this one client. Earlier versions used separate `connectors.gmail` / `connectors.google_drive` / `connectors.big_query` blocks; those are gone (see the 0.5.0 entry in [CHANGELOG.md](../CHANGELOG.md)).

If `client_secret.json` is present in the credentials directory it takes precedence. The config keys / env vars are the fallback.

### 3. Set the encryption key

Connector tokens are encrypted at rest with Fernet. Generate a key once:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set it as `connectors.encryption_key` (or `CONNECTOR_ENCRYPTION_KEY`). The app refuses to encrypt/decrypt tokens without it. Treat the key as a secret and back it up: rotating it makes every stored token unreadable, forcing a re-auth.

## The GitLab connector

`agentforge/connectors/gitlab/` uses **token auth** — a GitLab personal access token — not OAuth, so there is no redirect flow.

1. In GitLab, create a personal access token. Read-only work needs the `read_api` scope; for write actions (update/merge MRs, manage pipelines and runners) use `api`.
2. Create the connection by posting the token (see `POST /api/connectors/auth/token` below).
3. The connection exposes the full `gitlab_*` toolset: projects, branches, merge requests, pipelines, jobs, runners, and users.

`read_write` controls whether the agent may modify GitLab. In read-only mode the write tools refuse. In read/write mode, destructive actions (merge an MR, delete a branch or project, retry/cancel a pipeline, pause/resume a runner) still pause for confirmation. Toggle it later with `PATCH /api/connectors/{id}`.

## The GitHub connector

`agentforge/connectors/github/` uses **token auth** — a GitHub personal access token, like GitLab, with no OAuth redirect. It is **github.com-only**, so the connect form has no URL field.

Rather than reimplementing the GitHub REST API, it **reuses the `gh_command` tool**: each call runs the GitHub CLI (`gh`) with the connection's PAT injected as `GH_TOKEN`, keeping the full `gh` surface (PRs, issues, releases, Actions, cross-repo `gh search`, raw `gh api`). Two consequences:

- The host running the connector's tools needs the **`gh` binary installed** — but not `gh auth login`; the PAT supplies auth. With no connection active, `gh_command` falls back to the host's own `gh auth`, so general `@agent` use is unchanged.
- Tools run in-process, bound to the connection's token.

1. In GitHub, create a personal access token — a fine-grained PAT scoped to the repos/permissions you need (a classic `repo` PAT also works). For read-only use, grant only read permissions.
2. Create the connection (`POST /api/connectors/auth/token`, `connector_type: "github"`). `url` defaults to `https://github.com`.
3. The connection exposes `gh_command`, bound to this account.

`read_write` controls writes. In **read-write** mode the agent can create/edit PRs and issues, comment, manage releases, etc. In **read-only** mode a best-effort allowlist permits only read `gh` subcommands (`list` / `view` / `diff` / `search`, and GET `gh api`) and blocks the rest — softer than GitLab's hard tool-gating, since `gh` is a single passthrough. Toggle it later with `PATCH /api/connectors/{id}`.

- Default aliases: `@github` / `@gh`, plus a label-derived slug
- Account label: the GitHub username (resolved from the token)
- GitHub Enterprise: supported via the stored `url` (mapped to `GH_HOST`), though the UI only offers github.com

## Connect an account

The flow is REST-driven (`web/server/connectors/api.py`). OAuth connectors (Google) use `auth/start` + `auth/callback`; token connectors (GitLab, GitHub) use `auth/token`. Token connections are verified with a live API call before they are saved, so a bad token or host fails fast instead of leaving a dead connection.

| Method | Path                              | Purpose                                                                                                            |
| ------ | --------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| GET    | `/api/connectors/types`           | List connector types and, for Google, the selectable products.                                                    |
| POST   | `/api/connectors/auth/start`      | Begin OAuth. Body: `{"connector_type": "google", "label": "...", "products": ["gmail", "drive"]}`. Returns `auth_url` + `state`. |
| GET    | `/api/connectors/auth/callback`   | Google redirects here with `code` + `state`. Tokens are exchanged and stored.                                     |
| POST   | `/api/connectors/auth/token`      | Create a token connection (GitLab or GitHub). Body: `{"connector_type": "gitlab", "url": "...", "token": "...", "read_write": false, "label": "..."}`. For GitHub use `connector_type: "github"`; `url` is optional (defaults to `https://github.com`). |
| GET    | `/api/connectors`                 | List active connections.                                                                                           |
| GET    | `/api/connectors/{id}`            | One connection.                                                                                                   |
| PATCH  | `/api/connectors/{id}`            | Update a connection (e.g. `label`, `read_write`).                                                                  |
| DELETE | `/api/connectors/{id}`            | Remove a connection.                                                                                              |
| POST   | `/api/connectors/{id}/test`       | Verify a connection.                                                                                              |
| POST   | `/api/connectors/{id}/reconnect`  | Re-run auth for an existing connection.                                                                           |

`auth/start` builds the consent URL with `access_type=offline` and `prompt=consent` so a refresh token is issued, and tracks `state` (Redis when available, else in-memory, 10-minute TTL) for CSRF protection. Open the returned `auth_url`, grant access, and the callback persists the encrypted connection.

## Use a connector

There is no `custom_agents.yaml` entry to add. On startup and whenever a connection is created, the connector manager registers a dynamic agent per connection and binds its aliases (`@google` / `@gitlab` / `@github`, plus a label-derived slug). When one account has several connections, they are also reachable through one aggregated account agent. Each agent gets connection-scoped tools and runs with history disabled.

Prefix a message with the connector or label to route there: `@conn #work-gitlab list my open MRs`, or `@google summarise my unread mail`. Without a tag, `@conn` routes by keyword and recency.

OAuth access tokens refresh automatically: each tool call decrypts the stored tokens, refreshes if expired, and re-encrypts. No manual token maintenance.

## Legacy Google connections

Before v0.5.0, each Google product was a separate connector type (`gmail`, `google_drive`, `bigquery`, `youtube`). They are now unified under one `google` connection where you pick products at connect time.

The runtime still registers the old plugins (`listable=False`) so **existing SQLite rows** keep working. New connections should always use `connector_type: "google"`.

To see whether you still have legacy rows (chat DB, table `connections`):

```sql
SELECT id, connector_type, label, account_identifier
FROM connections
WHERE connector_type IN ('gmail', 'google_drive', 'bigquery', 'youtube');
```

Migration path when you are ready:

1. Note each legacy row's label and which product it covered.
2. Delete the legacy connection via `DELETE /api/connectors/{id}` (or the UI).
3. Re-connect with `POST /api/connectors/auth/start` using `connector_type: "google"` and the right `products` list.
4. Once no legacy rows remain, the old plugin registrations can be removed from the codebase.

## See also

- [modes.md](modes.md): the `@conn` / `@google` / `@gitlab` / `@github` connector modes this wires up.
- [SECURITY.md](SECURITY.md): how connector tokens are encrypted and the auth model around them.
