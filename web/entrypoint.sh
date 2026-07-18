#!/bin/sh
# agentforge-web entrypoint: apply SQLite migrations, then exec the container command.
#
# Migrations also run inside FastAPI lifespan (create_tables → upgrade). Running
# them here makes deploy logs explicit and fails the container before uvicorn
# if a migration cannot be applied.
set -e

echo "[agentforge-web] Running database migrations (chat + prompt_lab)…"
python -m web.server.database.cli upgrade-all
echo "[agentforge-web] Migrations complete."

exec "$@"
