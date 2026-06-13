# User Context

This file is injected **verbatim** into every agent's system prompt, so the model always knows who you are,
where your stuff lives, and how you like to work. Copy it to `user_context.md` (gitignored)
and replace the placeholders with your own details.
Keep it concise and factual — everything here is sent on every run!

> The `Name:` line below also powers the welcome greeting ("Good afternoon, <first name>").
> Keep it as a list item — `- Name: Your Name`. Any bullet works (`-`, `*`, `+`)
> or none at all; the first word is used as your name.

## Identity

- Name: Your Name
- Location: City, Country
- Role: e.g., Full-Stack Engineer

## Preferences

- Communication: concise and direct; cite file paths as `path:line`
- Languages/tools: e.g., Python, TypeScript, Docker, Git
- Prefer maintainable, explicit code over clever one-liners
- Avoid new dependencies without a clear benefit; preserve existing project conventions

## Environment

Describe your machines and how to reach them, so the agent targets the right host/path.

- SSH hosts (aliases from `~/.ssh/config`):
  - `gpu-box` — Linux box on the LAN (Docker + Ollama)
  - `vps` — cloud VM
- Workspace roots (resolve short project names under these before asking for a path):
  - `~/code/` — personal projects
  - `~/work/` — work repos

## Projects

When a project is referenced by short name only, resolve it under a workspace root above.

- `my-api` — `~/work/my-api/`
- `my-app` — `~/code/my-app/`

## Conventions & safety

- Python projects use Poetry/Hatch; Compose files live at the project root
- Secrets live in `.env` at the project root (never committed)
- Never delete, remove, or overwrite files unless explicitly asked
- Files suffixed `_1`, `_2`, ... are intentional versioned outputs from `write_file` — never clean them up
