# Authoring tools and private overlays

How to add your own tools without forking, route them to the right worker, and keep everything private out of the published tree.

## The @tool decorator

Tools are plain functions decorated with `@tool` (`agentforge/tools/registry.py`):

```python
from agentforge.tools.registry import tool

@tool(locality="local", hint="Always pass --tail 200 for log reads")
def read_service_log(service: str, tail: int = 200) -> str:
    """Read the last N lines of a service log.

    service: the service name to read
    tail: how many trailing lines to return
    """
    ...
```

Decorator parameters:

| Param               | Default   | Purpose                                                                |
| ------------------- | --------- | ---------------------------------------------------------------------- |
| `locality`          | `"local"` | Which worker runs it: `"local"` or `"remote"`.                         |
| `hint`              | `None`    | A line injected into the system prompt to steer usage.                 |
| `confirm`           | `None`    | Confirmation template with `{arg}` placeholders for destructive tools. |
| `confirm_condition` | `None`    | Callable taking the tool kwargs, returning a prompt or `None`.         |

The schema is built by introspection: the first docstring line becomes the tool description, Google-style `name: description` lines become per-argument descriptions, type hints map to JSON-schema types (`int`/`float`/`str`/`bool`/`list`/`dict`, anything else falls back to `string`), and arguments without a default are marked required.
Keep the signature typed and the docstring accurate. That is what the model sees.

`locality` is a hint about where the tool should run on a split deployment.
Authoritative routing lives in `tool_routing.yaml` (below): each rule maps tool name patterns to a `role` and SAQ queue. The decorator is what new tools declare at authoring time; the YAML is what operators tune per host without editing code. On startup, `register_all_tools` calls `registry.check_routing_drift()` once — it logs (never raises) when a `@tool(locality=...)` disagrees with the matched YAML rule. Fix drift by aligning the decorator with the YAML, or adding/adjusting a rule.

## Loading a plugin

Two seams, both in `agentforge/tools/__init__.py`.
You do not edit the package.

Environment variable `AGENTFORGE_TOOL_PLUGINS`: comma-separated `module:function` specs:

```bash
AGENTFORGE_TOOL_PLUGINS=plugins.cloud_tools:register_cloud_tools,plugins.hub_tools:register_hub_tools
```

Entry-point group `agentforge.tools`: for installed packages, exposed via your package metadata.
Both paths resolve to the same contract.

Each spec points at a register function:

```python
# plugins/cloud_tools.py
from agentforge.tools.registry import tool

@tool(locality="remote")
def putio_list(folder_id: int = 0) -> str:
    """List items in a Put.io folder.

    folder_id: parent folder id (0 = root)
    """
    ...

def register_cloud_tools(registry) -> int:
    """Register this module's tools. Returns how many were added."""
    return registry.register_decorated()
```

The contract is `register(registry) -> int`: register your tools and return the count (a `None` return counts as `0`).
The `registry` exposes `register(func, *, name=None, category=None)`, `register_decorated()` (registers everything decorated with `@tool` in scope and returns the new count), `get(name)`, and `list_tools()`.
Loader failures are swallowed per-plugin, so one bad module won't take down the rest. Check startup logs if a tool doesn't appear.

## Tool routing

`tool_routing.yaml` maps tools to roles and SAQ queues.
First matching rule wins. Unmatched tools fall to `default_role`.

```yaml
dispatch:
  mode: in_process # in_process | split   (env AGENTFORGE_DISPATCH_MODE)
  tool_timeout_seconds: 900 # env AGENTFORGE_SAQ_TOOL_TIMEOUT
  agent_timeout_seconds: 900 # env AGENTFORGE_SAQ_AGENT_TIMEOUT

default_role: local

roles:
  local:
    queue: agentforge:tools:local
  remote:
    queue: agentforge:tools:remote

rules:
  - tools: ["read_service_log", "putio_*"]
    role: remote
```

Dispatch modes:

- `in_process`: every tool runs in the current worker. The single-host/dev default. No separate native worker needed.
- `split`: each tool is dispatched to its role's queue. Needs a worker running for every role a tool can route to.

Relevant env overrides: `AGENTFORGE_TOOL_ROUTING` (path to the YAML), `AGENTFORGE_DISPATCH_MODE`, `AGENTFORGE_WORKER_ROLE`, `AGENTFORGE_SAQ_TOOL_TIMEOUT`, `AGENTFORGE_SAQ_AGENT_TIMEOUT`.

## Private overlays

Everything personal stays in gitignored files that merge on top of the published tree at load.
The published repo ships generic examples. Your private copy never gets committed.

| Overlay                    | Overlays / merges into                                                                                                          |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `config.yaml`              | The live config (secrets, API keys, private sections). The published example is `config.example.yaml`.                          |
| `custom_agents.local.yaml` | Extra custom agents. Merged over `custom_agents.yaml` (local entries win). Template: `custom_agents.local.yaml.example`.          |
| `tool_routing.local.yaml`  | Extra routing. Its `rules` are prepended (checked first). `roles`/`modes` are merged.                                           |
| `markdown/local/`          | Private agent prompt files referenced from `custom_agents.local.yaml`.                                                          |
| `plugins/*`                | Private tool modules. Only `plugins/__init__.py` is committed. The rest is gitignored and loaded via `AGENTFORGE_TOOL_PLUGINS`. |
| `secrets/`                 | Connector tokens and credential files (e.g., `client_secret.json`).                                                              |
| `deploy.env`               | Deploy settings. The published example is `deploy.example.env`.                                                                 |

How the merges work in code:

- `custom_agents.local.yaml`: `_load_custom_agents()` in `web/server/ws_endpoint.py` loads `custom_agents.yaml`, then `agents.update(local_agents)` so local definitions override.
- `tool_routing.local.yaml`: `_load()` in `agentforge/tools/routing.py` prepends local `rules` and dict-merges `roles`/`modes`.
- `config.yaml` is gitignored. `config.example.yaml` is the template. Mirror any new key into both so a fresh checkout still loads.

To add a private agent that uses your tools: write the prompt in `markdown/local/myagent.md`, declare the agent in `custom_agents.local.yaml` (alias, profile, tool list, prompt path), and route its tools in `tool_routing.local.yaml` if they need a specific worker.
Nothing in the published tree changes.

## See also

- [tools.md](tools.md): the built-in tools your plugins sit alongside.
- [../markdown/README.md](../markdown/README.md): the `markdown/` instruction-markdown layout (`skills/`, `custom-agents/`, `local/`).
- [README.md](README.md): the full docs index.
