"""Sandbox bootstrap — runs on `import sandbox`, before any app.* imports.

Sets env vars from sandbox/config.yaml so that app.config.Settings() picks
them up when app.* modules are first imported (Pydantic reads env vars at
instantiation time, not at class definition time).
"""

import os
import sys
from pathlib import Path

import yaml

_sandbox_dir = Path(__file__).parent
# app/, web/, agentforge/ all live at the repo root (sandbox's parent).
_repo_root = _sandbox_dir.parent

# ── 1. Patch sys.path so app.*, web.*, agentforge.* are importable ───────────

if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

# ── 2. Set env vars from sandbox/config.yaml BEFORE any app.* import ─────────
# app/config.py creates settings = Settings() at module level — the first
# import of any app.* module triggers it. Env vars must already be in place.

_sandbox_cfg_path = _sandbox_dir / "config.yaml"
if _sandbox_cfg_path.exists():
    with open(_sandbox_cfg_path) as _f:
        _sandbox_cfg = yaml.safe_load(_f) or {}

    _env_map = {
        ("ollama", "host"): "OLLAMA_HOST",
        ("qdrant", "host"): "QDRANT_HOST",
        ("redis", "url"): "REDIS_URL",
    }
    for (section, key), env_var in _env_map.items():
        value = _sandbox_cfg.get(section, {}).get(key)
        if value:
            os.environ.setdefault(env_var, str(value))
