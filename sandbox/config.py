"""Sandbox config — 3-layer YAML merge producing a Settings object and raw dict.

Load order (last wins):
  1. framework-config.yaml  — profile + framework defaults
  2. config.yaml            — your local service overrides
  3. sandbox/config.yaml    — sandbox-specific overrides

Env var bootstrapping (OLLAMA_HOST, QDRANT_HOST, REDIS_URL) happens in
__init__.py so it runs before any app.* module is first imported.

Usage in sandbox scripts:
    import sandbox                          # must be first — bootstrap
    from sandbox.config import settings     # fully merged Settings object
    from sandbox.config import raw          # the merged YAML dict, if needed
"""

import sys
from pathlib import Path

import sandbox  # noqa: F401 — ensure bootstrap has run
from agentforge.config import _deep_merge, load_merged_yaml, load_yaml_file  # noqa: E402
from app.config import Settings  # noqa: E402

_repo_root = Path(__file__).parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

_service_yaml = load_merged_yaml(_repo_root / "config.yaml")
_sandbox_yaml = load_yaml_file(Path(__file__).parent / "config.yaml", "sandbox/config.yaml")

raw: dict = _deep_merge(_service_yaml, _sandbox_yaml)

# Env vars are already set by __init__.py — Settings() picks them up correctly.
settings = Settings()
