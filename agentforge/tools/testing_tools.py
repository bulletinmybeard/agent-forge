"""Testing tools — Docker-first test runner and k6 load test executor.

Provides two tools:

- ``test_runner``: Docker-first test execution. Auto-detects pytest/jest/vitest
  from project files and runs tests inside the project's Docker container.
  Discovery order: docker-compose.yml → running container → docker compose run
  → throwaway ``docker run --rm``. Parses results with failure diagnostics and
  fix suggestions.
- ``k6_load_test``: Executes Grafana k6 load/stress tests on the host with
  summary metrics (latency percentiles, throughput, error rates) and optional
  threshold evaluation.

Usage::

    from agentforge.tools import ToolRegistry
    from agentforge.tools.testing_tools import register_testing_tools

    registry = ToolRegistry()
    register_testing_tools(registry)
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from .registry import tool

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = logging.getLogger(__name__)

_MAX_OUTPUT = 15_000

# ---------------------------------------------------------------------------
# test_runner — framework detection
# ---------------------------------------------------------------------------


def _detect_test_framework(path: str) -> tuple[str, Path]:
    """Detect test framework and project root.

    Returns (framework_name, project_root).
    Raises ValueError if no framework can be detected.
    """
    p = Path(path).resolve()
    search_dir = p if p.is_dir() else p.parent

    for d in [search_dir] + list(search_dir.parents)[:8]:
        # --- Node frameworks ---
        pkg_json = d / "package.json"
        if pkg_json.exists():
            try:
                pkg = json.loads(pkg_json.read_text())
                deps = {
                    **pkg.get("dependencies", {}),
                    **pkg.get("devDependencies", {}),
                }
                scripts = pkg.get("scripts", {})

                # Vitest (check before jest — vitest projects sometimes list jest too)
                if "vitest" in deps or any("vitest" in v for v in scripts.values()):
                    return "vitest", d

                # Jest
                if "jest" in deps or "jest" in pkg or any("jest" in v for v in scripts.values()):
                    return "jest", d

            except (json.JSONDecodeError, OSError):
                pass

        # --- Python frameworks ---
        if (d / "pyproject.toml").exists() or (d / "pytest.ini").exists() or (d / "conftest.py").exists():
            return "pytest", d
        if (d / "setup.cfg").exists():
            try:
                content = (d / "setup.cfg").read_text()
                if "[tool:pytest]" in content:
                    return "pytest", d
            except OSError:
                pass

    raise ValueError(
        f"Could not detect test framework for {path}. "
        "Ensure a package.json (jest/vitest) or pyproject.toml/pytest.ini (pytest) exists."
    )


# ---------------------------------------------------------------------------
# test_runner — Docker execution strategy
# ---------------------------------------------------------------------------

# Service names that likely run the app / tests
_APP_SERVICE_NAMES = {"app", "api", "web", "server", "backend", "worker", "test", "tests"}

# Image name fragments that help identify Python vs Node services
_PYTHON_IMAGE_HINTS = ("python", "django", "flask", "fastapi")
_NODE_IMAGE_HINTS = ("node", "next", "nuxt", "bun", "deno")


def _find_compose_file(project_root: Path) -> Path | None:
    """Find docker-compose.yml / docker-compose.yaml / compose.yml."""
    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        candidate = project_root / name
        if candidate.exists():
            return candidate
    return None


def _parse_compose_services(compose_file: Path) -> dict:
    """Parse compose file and return services dict.

    Returns {service_name: {image, build, ...}} or empty dict on failure.
    """
    try:
        import yaml
    except ImportError:
        # Fallback: parse with docker compose config
        try:
            result = subprocess.run(
                ["docker", "compose", "-f", str(compose_file), "config", "--format", "json"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                config = json.loads(result.stdout)
                return config.get("services", {})
        except Exception:
            pass
        return {}

    try:
        with open(compose_file) as f:
            data = yaml.safe_load(f) or {}
        return data.get("services", {})
    except Exception:
        return {}


def _pick_service_for_framework(services: dict, framework: str) -> str | None:
    """Pick the best compose service for the given test framework.

    Priority:
    1. Service named 'test' or 'tests'
    2. Service whose image matches the framework's language
    3. Service named 'app', 'api', 'web', 'server', 'backend'
    4. First service (if only one exists)
    """
    if not services:
        return None

    is_python = framework == "pytest"
    lang_hints = _PYTHON_IMAGE_HINTS if is_python else _NODE_IMAGE_HINTS

    # Priority 1: test-named service
    for name in ("test", "tests"):
        if name in services:
            return name

    # Priority 2: image matches language
    for name, svc in services.items():
        image = str(svc.get("image", "")).lower()
        build_ctx = str(svc.get("build", "")).lower()
        dockerfile = ""
        if isinstance(svc.get("build"), dict):
            dockerfile = str(svc["build"].get("dockerfile", "")).lower()
            build_ctx = str(svc["build"].get("context", "")).lower()

        combined = f"{image} {build_ctx} {dockerfile}"
        if any(hint in combined for hint in lang_hints):
            return name

    # Priority 3: common app service names
    for name in _APP_SERVICE_NAMES:
        if name in services:
            return name

    # Priority 4: single service
    if len(services) == 1:
        return next(iter(services))

    return None


def _is_container_running(container: str) -> bool:
    """Check if a Docker container/service is currently running."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", f"name={container}", "--filter", "status=running", "-q"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


def _is_compose_service_running(compose_file: Path, service: str) -> bool:
    """Check if a compose service is running."""
    try:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                str(compose_file),
                "ps",
                "--filter",
                "status=running",
                "-q",
                service,
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


def _detect_base_image(framework: str, project_root: Path) -> str:
    """Detect the appropriate Docker base image for throwaway containers."""
    if framework == "pytest":
        # Try to detect Python version from pyproject.toml
        pyproject = project_root / "pyproject.toml"
        if pyproject.exists():
            try:
                content = pyproject.read_text()
                match = re.search(r'python\s*(?:=|>=|~=)\s*["\']?(\d+\.\d+)', content)
                if match:
                    return f"python:{match.group(1)}-slim"
            except OSError:
                pass
        return "python:3.11-slim"

    # Node frameworks
    # Check .nvmrc or .node-version
    for version_file in (".nvmrc", ".node-version"):
        vf = project_root / version_file
        if vf.exists():
            try:
                ver = vf.read_text().strip().lstrip("v")
                if ver:
                    return f"node:{ver}-slim"
            except OSError:
                pass

    # Check engines in package.json
    pkg_json = project_root / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text())
            engines = pkg.get("engines", {})
            node_ver = engines.get("node", "")
            match = re.search(r"(\d+)", node_ver)
            if match:
                return f"node:{match.group(1)}-slim"
        except (json.JSONDecodeError, OSError):
            pass

    return "node:lts-slim"


def _build_test_command(
    framework: str,
    project_root: Path,
    target: str,
    test_filter: str,
    verbose: bool,
) -> str:
    """Build the test command (runs inside the container).

    Always uses ``-v`` for pytest so the parser gets per-test PASSED/FAILED
    lines.  ``verbose=True`` upgrades to ``-vv`` for full assertion diffs.
    Jest/vitest always run with per-test output; ``verbose=True`` adds
    ``--verbose`` for extra detail.
    """
    if framework == "pytest":
        # Detect if poetry project
        is_poetry = False
        pyproject = project_root / "pyproject.toml"
        if pyproject.exists():
            try:
                content = pyproject.read_text()
                is_poetry = "[tool.poetry]" in content
            except OSError:
                pass

        runner = "poetry run " if is_poetry else ""
        tb = "--tb=long" if verbose else "--tb=short"
        v_flag = "-vv" if verbose else "-v"
        parts = [f"{runner}pytest", tb, v_flag]
        if test_filter:
            parts.append("-k " + shlex.quote(test_filter))
        if target:
            rel = _rel_path(target, project_root)
            if rel:
                parts.append(shlex.quote(rel))
        return " ".join(parts)

    if framework == "jest":
        runner = "npx "
        if (project_root / "yarn.lock").exists():
            runner = "yarn "
        parts = [f"{runner}jest", "--no-coverage"]
        if verbose:
            parts.append("--verbose")
        parts.append("--forceExit")
        if test_filter:
            parts.append("--testNamePattern=" + shlex.quote(test_filter))
        if target:
            rel = _rel_path(target, project_root)
            if rel:
                parts.append(shlex.quote(rel))
        return " ".join(parts)

    if framework == "vitest":
        runner = "npx "
        if (project_root / "yarn.lock").exists():
            runner = "yarn "
        parts = [f"{runner}vitest", "run", "--reporter=verbose"]
        if test_filter:
            parts.append("-t " + shlex.quote(test_filter))
        if target:
            rel = _rel_path(target, project_root)
            if rel:
                parts.append(shlex.quote(rel))
        return " ".join(parts)

    return ""


def _build_docker_exec_command(
    container: str,
    test_cmd: str,
    workdir: str = "/app",
) -> list[str]:
    """Build a docker exec command (argv list, run with shell=False).

    ``test_cmd`` is a shell string passed to ``sh -c`` inside the container as a
    single argv element, so it can't break out of the host invocation.
    """
    return ["docker", "exec", "-w", workdir, container, "sh", "-c", test_cmd]


def _build_compose_exec_command(
    compose_file: Path,
    service: str,
    test_cmd: str,
    workdir: str = "/app",
) -> list[str]:
    """Build a docker compose exec command (argv list, run with shell=False)."""
    return [
        "docker",
        "compose",
        "-f",
        str(compose_file),
        "exec",
        "-T",
        "-w",
        workdir,
        service,
        "sh",
        "-c",
        test_cmd,
    ]


def _build_compose_run_command(
    compose_file: Path,
    service: str,
    test_cmd: str,
    workdir: str = "/app",
) -> list[str]:
    """Build a docker compose run --rm command (starts a new container)."""
    return [
        "docker",
        "compose",
        "-f",
        str(compose_file),
        "run",
        "--rm",
        "-T",
        "-w",
        workdir,
        service,
        "sh",
        "-c",
        test_cmd,
    ]


def _build_throwaway_command(
    image: str,
    project_root: Path,
    test_cmd: str,
    framework: str,
) -> list[str]:
    """Build a docker run --rm command for a throwaway container (argv list)."""
    # Build install + test command
    if framework == "pytest":
        # Check if poetry project
        is_poetry = False
        pyproject = project_root / "pyproject.toml"
        if pyproject.exists():
            try:
                content = pyproject.read_text()
                is_poetry = "[tool.poetry]" in content
            except OSError:
                pass

        if is_poetry:
            install_cmd = "pip install poetry && poetry install --no-interaction"
        elif (project_root / "requirements.txt").exists():
            install_cmd = "pip install -r requirements.txt"
        elif (project_root / "pyproject.toml").exists():
            install_cmd = "pip install -e '.[dev,test]' 2>/dev/null || pip install -e '.'"
        else:
            install_cmd = "pip install pytest"

        full_cmd = f"{install_cmd} && {test_cmd}"
    else:
        # Node: npm install or yarn install
        if (project_root / "yarn.lock").exists():
            install_cmd = "yarn install --frozen-lockfile 2>/dev/null || yarn install"
        else:
            install_cmd = "npm ci 2>/dev/null || npm install"

        full_cmd = f"{install_cmd} && {test_cmd}"

    return [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{project_root}:/app",
        "-w",
        "/app",
        image,
        "sh",
        "-c",
        full_cmd,
    ]


def _resolve_docker_execution(
    framework: str,
    project_root: Path,
    container: str,
    test_cmd: str,
) -> tuple[list[str], str]:
    """Resolve the Docker execution strategy.

    Returns (docker_argv, execution_method_description). ``docker_argv`` is empty
    on error, with the message carried in the second element.
    """
    # 1. Explicit container override — use docker exec directly
    if container:
        if _is_container_running(container):
            cmd = _build_docker_exec_command(container, test_cmd)
            return cmd, f"docker exec (container: {container})"
        return [], f"Error: container '{container}' is not running"

    # 2. Docker Compose auto-detect
    compose_file = _find_compose_file(project_root)
    if compose_file:
        services = _parse_compose_services(compose_file)
        service = _pick_service_for_framework(services, framework)

        if service:
            # Check if service is running → docker compose exec
            if _is_compose_service_running(compose_file, service):
                cmd = _build_compose_exec_command(compose_file, service, test_cmd)
                return cmd, f"docker compose exec (service: {service})"

            # Service exists but not running → docker compose run --rm
            cmd = _build_compose_run_command(compose_file, service, test_cmd)
            return cmd, f"docker compose run --rm (service: {service})"

    # 3. Fallback: throwaway container with auto-detected base image
    image = _detect_base_image(framework, project_root)
    cmd = _build_throwaway_command(image, project_root, test_cmd, framework)
    return cmd, f"docker run --rm (image: {image})"


def _rel_path(target: str, root: Path) -> str:
    """Return target relative to root, or absolute if outside."""
    t = Path(target).resolve()
    try:
        return str(t.relative_to(root))
    except ValueError:
        return str(t)


def _infer_framework_from_path(path: str) -> str:
    """Infer test framework from file/dir path when project files aren't available.

    Used when container is explicit and host-side detection can't run.
    Returns "pytest", "jest", "vitest", or "pytest" as default.
    """
    lower = path.lower()
    # Python signals
    if lower.endswith(".py") or "/test_" in lower or "conftest" in lower or "pytest" in lower:
        return "pytest"
    # Vitest signals (check before jest — vitest files often use .test.ts too)
    if "vitest" in lower:
        return "vitest"
    # Jest / Node signals
    if lower.endswith((".test.js", ".test.ts", ".test.jsx", ".test.tsx", ".spec.js", ".spec.ts")):
        return "jest"
    if "jest" in lower or "__tests__" in lower:
        return "jest"
    # Default to pytest (most common in this project ecosystem)
    return "pytest"


# ---------------------------------------------------------------------------
# test_runner — output parsing
# ---------------------------------------------------------------------------


def _parse_pytest_output(stdout: str, stderr: str, returncode: int, verbose: bool = False) -> str:
    """Parse pytest output into a structured summary.

    Since the test command always runs with ``-v``, we always have per-test
    PASSED/FAILED lines to parse.

    Default (verbose=False):
      - Result + duration
      - Per-file breakdown (pass/fail counts)
      - On failure: failed test list, error details with expected/actual,
        traceback locations, fix suggestions
      - Warnings (always)
      - No raw output on success

    Verbose (verbose=True):  adds full test listing and raw output.
    """
    combined = stdout + "\n" + stderr
    lines: list[str] = []
    has_failures = returncode != 0

    # --- Result line (e.g., "5 passed, 2 failed in 1.23s") ---
    summary_match = re.search(r"=+ (.+?) =+\s*$", combined, re.MULTILINE)
    if summary_match:
        lines.append(f"Result: {summary_match.group(1)}")
    elif returncode == 0:
        lines.append("Result: all tests passed")
    else:
        lines.append(f"Result: tests failed (exit code {returncode})")

    # --- Duration ---
    dur_match = re.search(r"in\s+(\d+\.\d+)s", combined)
    if dur_match:
        lines.append(f"Duration: {dur_match.group(1)}s")

    lines.append("")

    # --- Collect per-test results (always available with -v) ---
    # pytest -v: tests/test_foo.py::test_bar PASSED
    test_results: list[tuple[str, str]] = re.findall(
        r"(\S+?::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED)",
        combined,
    )

    # --- Per-file breakdown ---
    file_stats: dict[str, dict[str, int]] = {}
    for test_id, status in test_results:
        fpath = test_id.split("::")[0]
        status_lower = status.lower()
        if fpath not in file_stats:
            file_stats[fpath] = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
        file_stats[fpath][status_lower] = file_stats[fpath].get(status_lower, 0) + 1

    # Fallback: quiet-style lines if -v output wasn't captured
    if not file_stats:
        for m in re.finditer(r"^(\S+\.py)\s+([.FsExX]+)\s", combined, re.MULTILINE):
            fpath = m.group(1)
            markers = m.group(2)
            file_stats[fpath] = {
                "passed": markers.count("."),
                "failed": markers.count("F"),
                "skipped": markers.count("s") + markers.count("S"),
                "error": markers.count("E"),
            }

    if file_stats:
        lines.append(f"Files ({len(file_stats)}):")
        for fpath, stats in file_stats.items():
            parts = []
            if stats.get("passed"):
                parts.append(f"{stats['passed']} passed")
            if stats.get("failed"):
                parts.append(f"{stats['failed']} failed")
            if stats.get("error"):
                parts.append(f"{stats['error']} error")
            if stats.get("skipped"):
                parts.append(f"{stats['skipped']} skipped")
            lines.append(f"  {fpath}: {', '.join(parts)}")
        lines.append("")

    # --- On failure: always show detailed failure info ---
    if has_failures:
        # Failed test list with reasons
        failures = re.findall(
            r"FAILED\s+(\S+?)(?:\s+-\s+(.+?))?$",
            combined,
            re.MULTILINE,
        )
        if failures:
            lines.append(f"Failures ({len(failures)}):")
            for test_id, reason in failures:
                reason_str = f" — {reason}" if reason else ""
                lines.append(f"  ✗ {test_id}{reason_str}")
            lines.append("")

        # Per-failure detail blocks: extract FAILED section with surrounding context
        # pytest --tb=short format: "FAILED test_id\n file:line: in func\n E  assertion"
        failure_sections = re.split(r"_{5,}\s+", combined)
        detail_blocks: list[str] = []
        for section in failure_sections:
            if "FAILED" in section or ("E " in section and ("assert" in section.lower() or "error" in section.lower())):
                # Extract the E-lines (assertion details)
                e_lines = re.findall(r"^(E\s+.+)$", section, re.MULTILINE)
                # Extract file:line location
                locations = re.findall(r"^(\S+\.py:\d+:.*)$", section, re.MULTILINE)
                if e_lines or locations:
                    block_parts = []
                    for loc in locations[:3]:
                        block_parts.append(f"  {loc.strip()}")
                    for e in e_lines[:8]:
                        block_parts.append(f"  {e.strip()}")
                    detail_blocks.append("\n".join(block_parts))

        if detail_blocks:
            lines.append("Error details:")
            for i, block in enumerate(detail_blocks[:10]):
                if i > 0:
                    lines.append("")
                lines.append(block)
            lines.append("")
        else:
            # Fallback: extract any E-lines from the full output
            error_blocks = re.findall(r"((?:E\s+.+\n?)+)", combined)
            if error_blocks:
                lines.append("Error details:")
                for block in error_blocks[:10]:
                    cleaned = block.strip()
                    if cleaned:
                        lines.append(f"  {cleaned}")
                lines.append("")

        # Traceback locations
        tb_lines = re.findall(
            r"^(\S+\.py:\d+: \w+.*)$",
            combined,
            re.MULTILINE,
        )
        if tb_lines:
            lines.append("Traceback locations:")
            for tb in tb_lines[:15]:
                lines.append(f"  {tb.strip()}")
            lines.append("")

        # Fix suggestions
        fixes = _suggest_fixes_pytest(combined)
        if fixes:
            lines.append("Potential fixes:")
            for fix in fixes:
                lines.append(f"  → {fix}")
            lines.append("")

    # --- Warnings (always useful to surface) ---
    warning_match = re.search(r"(\d+) warnings?", combined)
    if warning_match:
        lines.append(f"Warnings: {warning_match.group(0)}")
        warn_lines = re.findall(r"((?:Pytest|Deprecation|User|Runtime)Warning:.+?)$", combined, re.MULTILINE)
        seen: set[str] = set()
        for w in warn_lines[:5]:
            w_clean = w.strip()
            if w_clean not in seen:
                seen.add(w_clean)
                lines.append(f"  {w_clean}")
        if warn_lines:
            lines.append("")

    # --- Verbose: full individual test listing ---
    if verbose and test_results:
        lines.append(f"All tests ({len(test_results)}):")
        for test_id, status in test_results:
            icon = {"PASSED": "✓", "FAILED": "✗", "SKIPPED": "○", "ERROR": "!"}.get(status, "?")
            lines.append(f"  {icon} {test_id}")
        lines.append("")

    # --- Raw output: on failure (always) or verbose ---
    if has_failures or verbose:
        lines.append("--- Raw output ---")
        raw = combined.strip()
        budget = _MAX_OUTPUT - len("\n".join(lines)) - 200
        if budget > 500 and len(raw) > budget:
            raw = raw[:budget] + f"\n... (truncated, {len(combined)} chars total)"
        elif budget <= 500:
            raw = raw[:500] + f"\n... (truncated, {len(combined)} chars total)"
        lines.append(raw)

    return "\n".join(lines)


def _suggest_fixes_pytest(output: str) -> list[str]:
    """Suggest fixes based on pytest error patterns."""
    fixes: list[str] = []
    lower = output.lower()

    if "modulenotfounderror" in lower or "no module named" in lower:
        match = re.search(r"No module named ['\"](\S+)['\"]", output)
        mod = match.group(1) if match else "the module"
        fixes.append(f"Missing import: install {mod} or check PYTHONPATH/virtualenv")

    if "assertionerror" in lower:
        fixes.append("Assertion failure: verify expected values in test or update fixtures")

    if "fixture" in lower and "not found" in lower:
        match = re.search(r"fixture ['\"](\w+)['\"]", output, re.IGNORECASE)
        fixture = match.group(1) if match else "the fixture"
        fixes.append(f"Missing fixture '{fixture}': define it in conftest.py or check imports")

    if "typeerror" in lower:
        fixes.append("TypeError: check function signatures and argument types")

    if "connectionerror" in lower or "connection refused" in lower:
        fixes.append("Connection error: ensure services (database, API, etc.) are running")

    if "timeout" in lower:
        fixes.append("Timeout: increase test timeout or check for blocking operations")

    if "permissionerror" in lower:
        fixes.append("Permission denied: check file/directory permissions")

    return fixes


def _parse_jest_output(stdout: str, stderr: str, returncode: int, verbose: bool = False) -> str:
    """Parse jest/vitest output into a structured summary.

    Default (verbose=False):
      - Result counts + duration
      - Per-suite pass/fail breakdown
      - On failure: failed test names, assertion details, expected vs
        received, fix suggestions
      - No raw output on success

    Verbose (verbose=True): adds full test listing and raw output.
    """
    combined = stdout + "\n" + stderr
    lines: list[str] = []
    has_failures = returncode != 0

    # --- Summary counts ---
    test_summary = re.search(r"Tests?:\s*(.+?)$", combined, re.MULTILINE)
    suite_summary = re.search(r"Test Suites?:\s*(.+?)$", combined, re.MULTILINE)

    if test_summary:
        lines.append(f"Tests: {test_summary.group(1).strip()}")
    if suite_summary:
        lines.append(f"Suites: {suite_summary.group(1).strip()}")

    if not test_summary and not suite_summary:
        if returncode == 0:
            lines.append("Result: all tests passed")
        else:
            lines.append(f"Result: tests failed (exit code {returncode})")

    # --- Duration ---
    time_match = re.search(r"Time:\s*(.+?)$", combined, re.MULTILINE)
    if time_match:
        lines.append(f"Duration: {time_match.group(1).strip()}")

    lines.append("")

    # --- Per-suite breakdown ---
    pass_suites = re.findall(r"PASS\s+(\S+)", combined)
    fail_suites = re.findall(r"FAIL\s+(\S+)", combined)

    if pass_suites or fail_suites:
        lines.append(f"Suites ({len(pass_suites) + len(fail_suites)}):")
        for s in fail_suites:
            lines.append(f"  ✗ {s}")
        for s in pass_suites:
            lines.append(f"  ✓ {s}")
        lines.append("")

    # --- On failure: always show detailed failure info ---
    if has_failures:
        # Individual failed test names
        test_failures = re.findall(
            r"[✕×✗]\s+(.+?)$",
            combined,
            re.MULTILINE,
        )
        if test_failures:
            lines.append(f"Failed tests ({len(test_failures)}):")
            for t in test_failures[:20]:
                lines.append(f"  ✗ {t.strip()}")
            lines.append("")

        # Assertion expressions
        expect_errors = re.findall(
            r"(expect\(.+?\)\.to\w+\(.+?\))",
            combined,
        )
        if expect_errors:
            lines.append("Assertion details:")
            for err in expect_errors[:10]:
                lines.append(f"  {err.strip()}")
            lines.append("")

        # Expected/Received pairs
        exp_recv = re.findall(
            r"(Expected:.+?)$\s*(Received:.+?)$",
            combined,
            re.MULTILINE,
        )
        if exp_recv:
            lines.append("Expected vs Received:")
            for exp, recv in exp_recv[:5]:
                lines.append(f"  {exp.strip()}")
                lines.append(f"  {recv.strip()}")
            lines.append("")

        # Stack traces / error locations
        at_lines = re.findall(r"^\s+(at\s+\S+.+?)$", combined, re.MULTILINE)
        if at_lines:
            lines.append("Stack traces:")
            seen_locs: set[str] = set()
            for loc in at_lines[:15]:
                loc_clean = loc.strip()
                if loc_clean not in seen_locs:
                    seen_locs.add(loc_clean)
                    lines.append(f"  {loc_clean}")
            lines.append("")

        # Fix suggestions
        fixes = _suggest_fixes_jest(combined)
        if fixes:
            lines.append("Potential fixes:")
            for fix in fixes:
                lines.append(f"  → {fix}")
            lines.append("")

    # --- Verbose: full individual test listing ---
    if verbose:
        all_tests = re.findall(
            r"([✓✕×✗○])\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$",
            combined,
            re.MULTILINE,
        )
        if all_tests:
            lines.append(f"All tests ({len(all_tests)}):")
            for icon, name in all_tests:
                lines.append(f"  {icon} {name.strip()}")
            lines.append("")

    # --- Raw output: on failure (always) or verbose ---
    if has_failures or verbose:
        lines.append("--- Raw output ---")
        raw = combined.strip()
        budget = _MAX_OUTPUT - len("\n".join(lines)) - 200
        if budget > 500 and len(raw) > budget:
            raw = raw[:budget] + f"\n... (truncated, {len(combined)} chars total)"
        elif budget <= 500:
            raw = raw[:500] + f"\n... (truncated, {len(combined)} chars total)"
        lines.append(raw)

    return "\n".join(lines)


def _suggest_fixes_jest(output: str) -> list[str]:
    """Suggest fixes based on jest/vitest error patterns."""
    fixes: list[str] = []
    lower = output.lower()

    if "cannot find module" in lower:
        match = re.search(r"Cannot find module ['\"](.+?)['\"]", output)
        mod = match.group(1) if match else "the module"
        fixes.append(f"Missing module '{mod}': run npm install or check import paths")

    if "tobedefined" in lower or "tobe(undefined)" in lower:
        fixes.append("Unexpected undefined: check that mocks/fixtures return expected values")

    if "expected" in lower and "received" in lower:
        fixes.append("Assertion mismatch: compare expected vs received values in the test")

    if "timeout" in lower:
        fixes.append("Test timeout: increase jest.setTimeout() or check async operations")

    if "referenceerror" in lower:
        fixes.append("ReferenceError: check variable/function names and imports")

    if "syntaxerror" in lower:
        fixes.append("SyntaxError: check for typos, missing brackets, or ESM/CJS mismatch")

    if "econnrefused" in lower or "connection refused" in lower:
        fixes.append("Connection refused: ensure backend/services are running for integration tests")

    if "snapshot" in lower and ("obsolete" in lower or "written" in lower):
        fixes.append("Snapshot mismatch: run with --updateSnapshot to accept changes if intentional")

    return fixes


# ---------------------------------------------------------------------------
# test_runner — tool entry point
# ---------------------------------------------------------------------------


@tool(
    hint=(
        "Use test_runner to run unit and integration tests inside the project's "
        "Docker container. Auto-detects pytest, jest, or vitest from project "
        "files. Prefers docker compose exec on running containers, falls back to "
        "docker compose run or throwaway docker run --rm. Parses results, "
        "highlights failures, and suggests fixes. "
        "When a container is specified, the path is relative to the project root "
        "inside the container (usually /app/). For example path='tests/' maps to "
        "/app/tests/ inside the container."
    ),
)
def test_runner(
    path: str,
    framework: str = "auto",
    test_filter: str = "",
    verbose: bool = False,
    container: str = "",
) -> str:
    """Run tests inside a Docker container. Auto-detects pytest, jest, or vitest.

    Tests always run inside Docker to ensure correct runtime versions and
    dependencies. Discovery order: docker compose exec (running service) →
    docker compose run --rm → throwaway docker run --rm.

    When ``container`` is provided, the ``path`` is treated as relative to the
    project root inside the container (typically /app/). Host-side path
    validation is skipped because the test files live inside the container.

    path: file or directory to test, relative to project root (e.g., "tests/", "onboarding/tests/")
    framework: "auto", "pytest", "jest", or "vitest" (default: auto-detect)
    test_filter: filter expression — pytest -k or jest --testNamePattern
    verbose: show individual test names (default: false)
    container: override — use this container/service name instead of auto-detect
    """
    container = container.strip()
    framework = framework.strip().lower()

    # --- Container-explicit mode: path lives inside the container, not on host ---
    if container:
        if framework in ("", "auto"):
            framework = _infer_framework_from_path(path)

        if framework not in ("pytest", "jest", "vitest"):
            return f'Error: unknown framework "{framework}". Supported: pytest, jest, vitest'

        # Build a simple test command for inside the container.
        # We don't have access to host project files, so we build the command
        # directly using the path as-is (the container has the right deps).
        test_cmd = _build_container_test_command(framework, path, test_filter, verbose)

        if not _is_container_running(container):
            return f"Error: container '{container}' is not running"

        docker_cmd = _build_docker_exec_command(container, test_cmd)
        exec_method = f"docker exec (container: {container})"

        header = [
            f"Framework: {framework} | Execution: {exec_method}",
            f"Path (in container): {path}",
            f"Test command: {test_cmd}",
            f"Docker command: {shlex.join(docker_cmd)}",
            "=" * 60,
            "",
        ]

        return _execute_and_parse(docker_cmd, framework, header, verbose)

    # --- Auto-detect mode: resolve from host project files ---
    target = Path(path).resolve()
    if not target.exists():
        return f"Error: path does not exist — {path}"

    # Detect or validate framework
    if framework in ("", "auto"):
        try:
            framework, project_root = _detect_test_framework(path)
        except ValueError as exc:
            return f"Error: {exc}"
    else:
        if framework not in ("pytest", "jest", "vitest"):
            return f'Error: unknown framework "{framework}". Supported: pytest, jest, vitest'
        try:
            _, project_root = _detect_test_framework(path)
        except ValueError:
            project_root = target if target.is_dir() else target.parent

    # Build the test command (what runs inside the container)
    test_cmd = _build_test_command(framework, project_root, path, test_filter, verbose)
    if not test_cmd:
        return f'Error: could not build command for framework "{framework}"'

    # Resolve Docker execution strategy
    docker_cmd, exec_method = _resolve_docker_execution(
        framework,
        project_root,
        container,
        test_cmd,
    )

    if not docker_cmd:
        # exec_method contains the error message
        return f"Error: {exec_method}"

    header = [
        f"Framework: {framework} | Execution: {exec_method}",
        f"Project: {project_root}",
        f"Test command: {test_cmd}",
        f"Docker command: {shlex.join(docker_cmd)}",
        "=" * 60,
        "",
    ]

    return _execute_and_parse(docker_cmd, framework, header, verbose)


def _build_container_test_command(
    framework: str,
    path: str,
    test_filter: str,
    verbose: bool,
) -> str:
    """Build a test command for container-explicit mode.

    Unlike ``_build_test_command()``, this doesn't read host project files.
    It uses the framework runner directly since the container already has the
    correct dependencies installed.

    Always uses ``-v`` for pytest so the parser gets per-test lines.
    ``verbose=True`` upgrades to ``-vv`` for full assertion diffs.
    """
    if framework == "pytest":
        tb = "--tb=long" if verbose else "--tb=short"
        v_flag = "-vv" if verbose else "-v"
        parts = ["pytest", tb, v_flag]
        if test_filter:
            parts.append("-k " + shlex.quote(test_filter))
        if path:
            parts.append(shlex.quote(path))
        return " ".join(parts)

    if framework == "jest":
        parts = ["npx jest", "--no-coverage"]
        if verbose:
            parts.append("--verbose")
        parts.append("--forceExit")
        if test_filter:
            parts.append("--testNamePattern=" + shlex.quote(test_filter))
        if path:
            parts.append(shlex.quote(path))
        return " ".join(parts)

    if framework == "vitest":
        parts = ["npx vitest", "run", "--reporter=verbose"]
        if test_filter:
            parts.append("-t " + shlex.quote(test_filter))
        if path:
            parts.append(shlex.quote(path))
        return " ".join(parts)

    return ""


def _execute_and_parse(docker_cmd: list[str], framework: str, header: list[str], verbose: bool = False) -> str:
    """Run the Docker command (argv list, shell=False), parse output, return result."""
    try:
        proc = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            timeout=300,
            env={**os.environ, "FORCE_COLOR": "0", "NO_COLOR": "1", "CI": "true"},
        )
    except subprocess.TimeoutExpired:
        return "\n".join(header) + "\nError: test run timed out after 300 seconds"
    except Exception as exc:
        return "\n".join(header) + f"\nError: {exc}"

    # Parse output based on framework
    if framework == "pytest":
        body = _parse_pytest_output(proc.stdout, proc.stderr, proc.returncode, verbose)
    else:
        body = _parse_jest_output(proc.stdout, proc.stderr, proc.returncode, verbose)

    result = "\n".join(header) + body

    if len(result) > _MAX_OUTPUT:
        result = result[:_MAX_OUTPUT] + f"\n... (truncated at {_MAX_OUTPUT} chars)"

    return result


# ---------------------------------------------------------------------------
# k6_load_test — helpers
# ---------------------------------------------------------------------------

_K6_BIN = "/opt/homebrew/bin/k6"


def _find_k6() -> str:
    """Locate the k6 binary."""
    if Path(_K6_BIN).is_file():
        return _K6_BIN
    # Fallback: check PATH
    try:
        result = subprocess.run(
            ["which", "k6"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return ""


_K6_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_K6_DURATION_RE = re.compile(r"^\d+[smh]$")


def _generate_k6_script(
    url: str,
    method: str,
    vus: int,
    duration: str,
    headers: str,
    body: str,
) -> str:
    """Generate a k6 JavaScript test script.

    Caller must validate ``method`` against ``_K6_ALLOWED_METHODS`` and
    ``duration`` against ``_K6_DURATION_RE`` first. All string values are
    embedded via ``json.dumps`` so they can't break out of the JS literals.
    """
    method = method.upper()

    # Parse headers JSON if provided
    headers_obj = "{}"
    if headers:
        try:
            h = json.loads(headers)
            headers_obj = json.dumps(h)
        except json.JSONDecodeError:
            # Try key:value format
            h = {}
            for pair in headers.split(","):
                if ":" in pair:
                    k, v = pair.split(":", 1)
                    h[k.strip()] = v.strip()
            if h:
                headers_obj = json.dumps(h)

    url_lit = json.dumps(url)

    # Build the request call
    if method == "GET":
        request_code = f"let res = http.get({url_lit}, {{ headers: {headers_obj} }});"
    elif method in ("POST", "PUT", "PATCH"):
        body_str = body if body else "null"
        # If body looks like JSON, use it directly
        try:
            json.loads(body_str)
            body_val = body_str
        except (json.JSONDecodeError, TypeError):
            body_val = json.dumps(body_str)
        request_code = f"let res = http.{method.lower()}({url_lit}, {body_val}, {{ headers: {headers_obj} }});"
    elif method == "DELETE":
        request_code = f"let res = http.del({url_lit}, null, {{ headers: {headers_obj} }});"
    else:
        request_code = f"let res = http.request({json.dumps(method)}, {url_lit}, null, {{ headers: {headers_obj} }});"

    duration_lit = json.dumps(duration)
    script = f"""import http from 'k6/http';
import {{ check, sleep }} from 'k6';

export const options = {{
  vus: {vus},
  duration: {duration_lit},
}};

export default function () {{
  {request_code}

  check(res, {{
    'status is 2xx': (r) => r.status >= 200 && r.status < 300,
    'response time < 500ms': (r) => r.timings.duration < 500,
  }});

  sleep(1);
}}
"""
    return script


def _parse_k6_output(stdout: str, stderr: str, returncode: int, thresholds: str) -> str:
    """Parse k6 end-of-run output into structured metrics."""
    # k6 writes metrics to stderr
    combined = stderr + "\n" + stdout
    lines: list[str] = []

    # Extract key metrics from k6's text summary
    metrics: dict[str, str] = {}

    # http_req_duration (latency)
    dur_match = re.search(
        r"http_req_duration[.\s]+?(?:avg=(\S+?)\s+.*?p\(95\)=(\S+?)\s+.*?p\(99\)=(\S+?)(?:\s|$))",
        combined,
    )
    if not dur_match:
        # Alternative format: metric lines with individual stats
        dur_match = re.search(
            r"http_req_duration.*?avg=(\S+?)[\s,].*?p\(95\)=(\S+?)[\s,].*?p\(99\)=(\S+)",
            combined,
        )
    if dur_match:
        metrics["avg_latency"] = dur_match.group(1)
        metrics["p95_latency"] = dur_match.group(2)
        metrics["p99_latency"] = dur_match.group(3)

    # Max latency
    max_match = re.search(r"http_req_duration.*?max=(\S+)", combined)
    if max_match:
        metrics["max_latency"] = max_match.group(1)

    # http_reqs (throughput)
    reqs_match = re.search(r"http_reqs[.\s]+?(\d+)\s+(\S+/s)", combined)
    if reqs_match:
        metrics["total_requests"] = reqs_match.group(1)
        metrics["throughput"] = reqs_match.group(2)

    # Checks
    checks_match = re.search(r"checks[.\s]+?(\d+\.?\d*%)\s", combined)
    if checks_match:
        metrics["checks_passed"] = checks_match.group(1)

    # http_req_failed (error rate)
    failed_match = re.search(r"http_req_failed.*?(\d+\.?\d*%)", combined)
    if failed_match:
        metrics["error_rate"] = failed_match.group(1)

    # VUs
    vus_match = re.search(r"vus_max[.\s]+?(\d+)", combined)
    if vus_match:
        metrics["max_vus"] = vus_match.group(1)

    # Iterations
    iters_match = re.search(r"iterations[.\s]+?(\d+)\s+(\S+/s)", combined)
    if iters_match:
        metrics["iterations"] = iters_match.group(1)
        metrics["iter_rate"] = iters_match.group(2)

    # Build summary
    if metrics:
        lines.append("Load Test Results")
        lines.append("=" * 50)
        lines.append("")

        if "avg_latency" in metrics:
            lines.append("Latency:")
            lines.append(f"  avg: {metrics['avg_latency']}")
            lines.append(f"  p95: {metrics.get('p95_latency', 'N/A')}")
            lines.append(f"  p99: {metrics.get('p99_latency', 'N/A')}")
            lines.append(f"  max: {metrics.get('max_latency', 'N/A')}")
            lines.append("")

        if "throughput" in metrics:
            lines.append("Throughput:")
            lines.append(f"  requests: {metrics.get('total_requests', 'N/A')} total")
            lines.append(f"  rate: {metrics['throughput']}")
            lines.append("")

        if "iterations" in metrics:
            lines.append(f"Iterations: {metrics['iterations']} ({metrics.get('iter_rate', 'N/A')})")

        if "error_rate" in metrics:
            lines.append(f"Error rate: {metrics['error_rate']}")

        if "checks_passed" in metrics:
            lines.append(f"Checks passed: {metrics['checks_passed']}")

        if "max_vus" in metrics:
            lines.append(f"Virtual users: {metrics['max_vus']}")

        lines.append("")
    else:
        lines.append("(could not parse structured metrics from k6 output)")
        lines.append("")

    # Evaluate thresholds if provided
    if thresholds:
        lines.append("Threshold Evaluation:")
        lines.append("-" * 30)
        threshold_results = _evaluate_thresholds(thresholds, metrics)
        for tr in threshold_results:
            lines.append(f"  {tr}")
        lines.append("")

    # Exit code
    if returncode == 0:
        lines.append("Status: PASSED")
    else:
        lines.append(f"Status: FAILED (exit code {returncode})")

    lines.append("")

    # Append raw output
    lines.append("--- Raw k6 output ---")
    raw = combined.strip()
    header_len = len("\n".join(lines))
    if len(raw) > _MAX_OUTPUT - header_len - 200:
        remaining = max(500, _MAX_OUTPUT - header_len - 200)
        raw = raw[:remaining] + f"\n... (truncated, {len(combined)} chars total)"
    lines.append(raw)

    return "\n".join(lines)


def _parse_ms(val: str) -> float | None:
    """Parse a k6 duration string to milliseconds."""
    val = val.strip().lower()
    try:
        if val.endswith("ms"):
            return float(val[:-2])
        if val.endswith("µs") or val.endswith("us"):
            return float(val[:-2]) / 1000
        if val.endswith("s") and not val.endswith("ms"):
            return float(val[:-1]) * 1000
        if val.endswith("m"):
            return float(val[:-1]) * 60000
        return float(val)
    except (ValueError, TypeError):
        return None


def _evaluate_thresholds(thresholds: str, metrics: dict[str, str]) -> list[str]:
    """Evaluate threshold expressions against parsed metrics.

    Supports: p95<500, error_rate<0.01, avg<200
    """
    results: list[str] = []

    for expr in re.split(r"[,;]\s*", thresholds):
        expr = expr.strip()
        if not expr:
            continue

        match = re.match(r"(\w+)\s*([<>]=?)\s*(\d+\.?\d*)", expr)
        if not match:
            results.append(f"  ⚠ could not parse threshold: {expr}")
            continue

        metric_name, op, threshold_str = match.group(1), match.group(2), match.group(3)
        threshold_val = float(threshold_str)

        # Map threshold metric names to our parsed metrics
        metric_map = {
            "p95": "p95_latency",
            "p95_response_time": "p95_latency",
            "p99": "p99_latency",
            "p99_response_time": "p99_latency",
            "avg": "avg_latency",
            "avg_response_time": "avg_latency",
            "max": "max_latency",
            "error_rate": "error_rate",
        }

        actual_key = metric_map.get(metric_name, metric_name)
        actual_str = metrics.get(actual_key)

        if actual_str is None:
            results.append(f"⚠ {expr} — metric not found in results")
            continue

        # Parse the actual value
        if actual_key == "error_rate":
            # error_rate comes as "0.00%" — convert to decimal
            try:
                actual_val = float(actual_str.replace("%", "")) / 100
            except ValueError:
                results.append(f"⚠ {expr} — could not parse error rate: {actual_str}")
                continue
        else:
            actual_ms = _parse_ms(actual_str)
            if actual_ms is None:
                results.append(f"⚠ {expr} — could not parse value: {actual_str}")
                continue
            actual_val = actual_ms

        # Evaluate
        passed = False
        if op == "<":
            passed = actual_val < threshold_val
        elif op == "<=":
            passed = actual_val <= threshold_val
        elif op == ">":
            passed = actual_val > threshold_val
        elif op == ">=":
            passed = actual_val >= threshold_val

        icon = "✓" if passed else "✗"
        results.append(f"{icon} {expr} (actual: {actual_str}) — {'PASS' if passed else 'FAIL'}")

    return results


# ---------------------------------------------------------------------------
# k6_load_test — tool entry point
# ---------------------------------------------------------------------------


@tool(
    hint=(
        "Use k6_load_test to run HTTP load and stress tests with Grafana k6. "
        "Generates a k6 script from parameters or runs an existing script. "
        "Reports latency (avg, p95, p99), throughput (req/s), and error rates."
    ),
)
def k6_load_test(
    url: str,
    method: str = "GET",
    vus: int = 10,
    duration: str = "30s",
    headers: str = "",
    body: str = "",
    thresholds: str = "",
    script: str = "",
) -> str:
    """Execute a k6 load test and report performance metrics.

    url: target URL to test (ignored if script is provided)
    method: HTTP method — GET, POST, PUT, DELETE (default: GET)
    vus: number of virtual users (default: 10)
    duration: test duration, e.g., "30s", "1m", "5m" (default: "30s")
    headers: request headers as JSON or "Key:Value,Key2:Value2"
    body: request body for POST/PUT (JSON string)
    thresholds: pass/fail criteria, e.g., "p95<500,error_rate<0.01"
    script: path to an existing k6 script file (overrides url/method/vus/duration)
    """
    # Locate k6
    k6_bin = _find_k6()
    if not k6_bin:
        return (
            "Error: k6 binary not found. Install via: brew install k6\n"
            "See: https://grafana.com/docs/k6/latest/set-up/install-k6/"
        )

    # Validate params
    vus = max(1, min(vus, 500))

    if script:
        script_path = Path(script).resolve()
        if not script_path.is_file():
            return f"Error: script file not found — {script}"
        script_file = str(script_path)
        cleanup = False
    elif url:
        # Validate untrusted inputs before they reach the generated JS / k6 options
        method_norm = method.strip().upper()
        if method_norm not in _K6_ALLOWED_METHODS:
            allowed = ", ".join(sorted(_K6_ALLOWED_METHODS))
            return f'Error: unsupported HTTP method "{method}". Allowed: {allowed}'
        if not _K6_DURATION_RE.match(duration.strip()):
            return f'Error: invalid duration "{duration}". Expected a number followed by s/m/h, e.g., "30s", "1m", "5h"'
        method = method_norm
        duration = duration.strip()

        # Generate script
        k6_js = _generate_k6_script(url, method, vus, duration, headers, body)
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".js",
            prefix="k6_agentforge_",
            delete=False,
        )
        tmp.write(k6_js)
        tmp.close()
        script_file = tmp.name
        cleanup = True
    else:
        return "Error: either url or script is required"

    # Build command (argv list, run with shell=False)
    cmd = [
        k6_bin,
        "run",
        "--summary-trend-stats",
        "avg,p(95),p(99),max",
        script_file,
    ]

    header_lines = [
        "k6 Load Test",
        f"Target: {url or script}" + (f" ({method})" if url else ""),
        f"Config: {vus} VUs, {duration}" if not script else f"Script: {script}",
        f"Command: {shlex.join(cmd)}",
        "=" * 60,
        "",
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            env={**os.environ, "NO_COLOR": "1", "K6_NO_USAGE_REPORT": "true"},
        )
    except subprocess.TimeoutExpired:
        return "\n".join(header_lines) + "\nError: k6 run timed out after 600 seconds"
    except Exception as exc:
        return "\n".join(header_lines) + f"\nError: {exc}"
    finally:
        if cleanup:
            try:
                os.unlink(script_file)
            except OSError:
                pass

    body_text = _parse_k6_output(proc.stdout, proc.stderr, proc.returncode, thresholds)

    result = "\n".join(header_lines) + body_text

    if len(result) > _MAX_OUTPUT:
        result = result[:_MAX_OUTPUT] + f"\n... (truncated at {_MAX_OUTPUT} chars)"

    return result


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_testing_tools(registry: ToolRegistry) -> int:
    """Register testing tools. Returns count."""
    registry.register_category_hint(
        "Testing",
        "Run unit/integration tests (pytest, jest, vitest) and HTTP load tests "
        "(k6). test_runner executes tests inside Docker containers — prefers "
        "docker compose exec on running services, falls back to docker compose "
        "run or throwaway docker run --rm. Parses results with fix suggestions. "
        "k6_load_test runs on the host and reports latency percentiles, throughput, "
        "and error rates with optional pass/fail thresholds.",
    )
    tools = [test_runner, k6_load_test]
    for func in tools:
        registry.register(func, category="Testing")
    return len(tools)
