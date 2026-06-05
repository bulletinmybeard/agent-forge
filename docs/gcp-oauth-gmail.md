# Google OAuth for the Gmail connector

Setting up the Google OAuth client so the `@gmail` / `@email` connector mode can read mail.
The connector requests read-only Gmail scope and stores encrypted tokens.
Drive uses the same connector plumbing with its own client.

## What the connector does

The Gmail plugin (`agentforge/connectors/gmail/`) implements a standard authorization-code OAuth flow with offline access (refresh tokens):

- Scope: `https://www.googleapis.com/auth/gmail.readonly` (read-only).
- Auth URI: `https://accounts.google.com/o/oauth2/v2/auth`.
- Token URI: `https://oauth2.googleapis.com/token`.
- Default aliases: `@gmail` and `@email`.

Tokens are encrypted (Fernet) and stored in the `connections` SQLite table.
The flow runs through REST endpoints on `agentforge-web`, not a standalone script.

## 1. Create the OAuth client in GCP

1. In the Google Cloud Console, pick or create a project.
2. Enable the Gmail API for that project.
3. Configure the OAuth consent screen. While testing, add your Google account as a test user so consent succeeds.
4. Create an OAuth client credential. A Desktop or Web client both work. The loader accepts either the `installed` or `web` key in the downloaded JSON.
5. Note the redirect URI. The callback path is `/api/connectors/auth/callback` on the web app. If the client JSON contains a `redirect_uris` entry the connector uses the first one. Otherwise it builds the URI from a **canonical app origin**: `AGENTFORGE_PUBLIC_URL` if set, else `https://{PUBLIC_DOMAIN}`, producing `{origin}/api/connectors/auth/callback`. Only when neither is set does it fall back to deriving the origin from request headers (`X-Forwarded-Proto` / `X-Forwarded-Host`) — that path is for local dev; set `AGENTFORGE_PUBLIC_URL` (or `PUBLIC_DOMAIN`) behind a proxy so a spoofed `Host`/`X-Forwarded-*` header can't steer the redirect. Register the URI that matches your canonical origin!

## 2. Provide the client credentials

Two ways.
Either drop the downloaded `client_secret.json` into the credentials directory, or set the client id/secret in config.

Credentials directory resolution order: `GMAIL_CREDENTIALS_DIR` env, `connectors.credentials_dir` in config, then `~/.agentforge/`.
In the container the default is `/app/secrets`, which the deploy script populates from your local `~/.agentforge/`.

Config keys (`config.example.yaml`):

```yaml
connectors:
  encryption_key: "" # env CONNECTOR_ENCRYPTION_KEY
  credentials_dir: /app/secrets
  gmail:
    client_id: "" # env CONNECTOR_GMAIL_CLIENT_ID
    client_secret: "" # env CONNECTOR_GMAIL_CLIENT_SECRET
  google_drive:
    client_id: "" # env CONNECTOR_GOOGLE_DRIVE_CLIENT_ID
    client_secret: "" # env CONNECTOR_GOOGLE_DRIVE_CLIENT_SECRET
```

If `client_secret.json` is present in the credentials directory it takes precedence. The config keys / env vars are the fallback.

## 3. Set the encryption key

Connector tokens are encrypted at rest with Fernet.
Generate a key once:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Set it as `connectors.encryption_key` (or `CONNECTOR_ENCRYPTION_KEY`).
The app refuses to encrypt/decrypt tokens without it.
Treat the key as a secret and back it up: rotating it makes every stored token unreadable, forcing a re-auth.

## 4. Connect an account

The OAuth flow is REST-driven (`web/server/connectors/api.py`):

| Method | Path                            | Purpose                                                                                        |
| ------ | ------------------------------- | ---------------------------------------------------------------------------------------------- |
| GET    | `/api/connectors/types`         | List connector types.                                                                          |
| POST   | `/api/connectors/auth/start`    | Begin auth. Body: `{"connector_type": "gmail", "label": "..."}`. Returns `auth_url` + `state`. |
| GET    | `/api/connectors/auth/callback` | Google redirects here with `code` + `state`. Tokens are exchanged and stored.                  |
| GET    | `/api/connectors`               | List active connections.                                                                       |
| POST   | `/api/connectors/{id}/test`     | Verify a connection.                                                                           |

`auth/start` builds the consent URL with `access_type=offline` and `prompt=consent` so a refresh token is issued, and tracks `state` (Redis when available, else in-memory, 10-minute TTL) for CSRF protection.
Open the returned `auth_url`, grant access, and the callback persists the encrypted connection.

## 5. Use @gmail / @email

There is no `custom_agents.yaml` entry to add.
On startup and whenever a connection is created, the connector manager registers a dynamic agent per active connection and binds its aliases (`@gmail`, `@email`, plus a label-derived slug).
The agent gets connection-scoped read tools and runs with history disabled.
Once a Gmail connection exists, prefix a message with `@gmail` or `@email` to route it there.

Access tokens are refreshed automatically: each tool call decrypts the stored tokens, refreshes if expired, and re-encrypts.
No manual token maintenance.

## See also

- [modes.md](modes.md): the `@conn` / `@gmail` / `@email` connector modes this wires up.
- [SECURITY.md](SECURITY.md): how connector tokens are encrypted and the auth model around them.
