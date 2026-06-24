# markdown/

Service-level instruction markdown — edit these to tune AgentForge without touching Python.

- `skills/` — domain instruction sets referenced by `skills.yaml` (`instruction_file:`). Injected into the system prompt when a skill matches.
- `custom-agents/` — per-agent system prompts referenced by `custom_agents.yaml` / `custom_agents.example.yaml` (`system_prompt:`). One file per custom agent mode (`@docker`, `@debug`, ...).

Paths in both YAMLs are resolved relative to the repo root, so add a new file here and point its config entry at `markdown/<dir>/<name>.md`.

The framework's own prompts (agent loop, review, coding, profile router) live inside the package at `agentforge/prompts/` and ship with the wheel — they stay there so `pip install agent-forge` is self-contained. Connector prompts live with their connector code under `agentforge/connectors/<name>/prompt.md`.

## See also

- [docs/plugin-authoring.md](../docs/plugin-authoring.md): wiring private agents/tools, including the `markdown/local/` overlay.
- [docs/modes.md](../docs/modes.md): the `@mode` prefixes these custom-agents and skills back.
