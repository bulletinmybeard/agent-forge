# sandbox/

A no-UI harness for exercising the AgentForge framework directly. Write a short Python script, point it at your Ollama (and optionally Qdrant), and drive the `AIClient` / `ToolRegistry` / agent loop without the web stack.

Only the runner scaffolding ships here. Write your own probe scripts under `sandbox/scripts/`. There are no bundled examples.

## Setup

1. Run Ollama locally (or set `sandbox/config.yaml` at your box). Pull a model that matches a profile in `framework-config.yaml`.
2. Optional: a running Qdrant + Redis if your script touches RAG / queues.
3. `sandbox/config.yaml` is the top layer of a 3-way merge (`framework-config.yaml` -> `config.yaml` -> `sandbox/config.yaml`). Edit it to point `ollama.host` / `qdrant.host` / `redis.url` wherever you need.

## Write a script

Put it in `sandbox/scripts/` and run it from the repo root (`python sandbox/scripts/my_probe.py`):

```python
import sandbox            # MUST be first — patches sys.path + env from config
import argparse
import sandbox_conf as conf

parser = argparse.ArgumentParser()
conf.add_common_args(parser)          # --profile / --verbose
args = parser.parse_args()

ai, reasoning_off = conf.make_burst_client(args)
conf.print_burst_header(args, ai, "My probe", "1 prompt", reasoning_off)

reply = ai.chat([{"role": "user", "content": "Say hi in one word."}])
print(reply)
conf.print_tokens()
```

## What `sandbox_conf` gives you

- ANSI helpers (`c`, `GREEN`, `BOLD`, `profile_tag`, `print_summary`).
- `bootstrap(provider=None)`: load `framework-config.yaml`. Optional provider override (`AGENTFORGE_PROVIDER`).
- `make_registry()`: a `ToolRegistry` with all core tools registered.
- `make_burst_client(args)`: bootstrap + build an `AIClient` from `--profile`.
- `add_common_args(parser)`: standard `--profile` / `--verbose` / `--list`.
- Automatic token accounting (`AIClient.chat` is instrumented at import).

Set `SANDBOX_LOG_LEVEL=INFO` to see HTTP requests. Default is `WARNING`.

## See also

- [docs/architecture.md](../docs/architecture.md): the full stack this harness sidesteps.
- [README.md](../README.md): project overview and the Docker-based way to run everything.
