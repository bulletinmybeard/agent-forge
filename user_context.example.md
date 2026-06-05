# User Context

Optional Markdown injected into every agent's system prompt. Copy to
`user_context.md` (gitignored) and describe anything the model should always
know: your workspace layout, SSH host aliases, project paths, Qdrant
collections, and conventions.

Example:

- Hosts: `gpu-box` (Linux, Docker + Ollama), reachable over SSH.
- Projects live under `~/code/`.
- Prefer concise answers; cite file paths as `path:line`.
