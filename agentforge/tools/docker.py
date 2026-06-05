"""Docker tools — inspect containers, images, volumes, and disk usage.

Uses the ``docker`` CLI with JSON format flags where available for structured
output.  No Python Docker SDK dependency required.

Usage::

    from agentforge.tools import ToolRegistry
    from agentforge.tools.docker import register_docker_tools

    registry = ToolRegistry()
    register_docker_tools(registry)
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import TYPE_CHECKING

from chalkbox.logging.bridge import get_logger

from .registry import tool

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(
    cmd: list[str],
    timeout: int = 30,
    *,
    cwd: str | None = None,
    merge_stderr: bool = False,
    quiet_stderr: bool = False,
) -> str:
    """Run a command (argv list, no shell) and return its stdout."""
    try:
        result = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        output = result.stdout.strip()
        stderr = result.stderr.strip()

        if merge_stderr and stderr:
            output = f"{output}\n{stderr}".strip() if output else stderr

        if result.returncode != 0 and stderr:
            # Common Docker errors — detected regardless of quiet_stderr
            if "Cannot connect to the Docker daemon" in stderr:
                return "Error: Docker daemon is not running. Start it with: sudo systemctl start docker"
            if "permission denied" in stderr.lower():
                return "Error: Permission denied. You may need to run with sudo or add your user to the docker group."
            if not quiet_stderr and not merge_stderr:
                output += f"\nSTDERR: {stderr}"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: command timed out ({timeout}s limit)"
    except FileNotFoundError:
        return "Error: docker command not found. Is Docker installed?"
    except Exception as exc:
        return f"Error: {exc}"


def _run_json(
    cmd: list[str],
    timeout: int = 30,
    *,
    cwd: str | None = None,
    quiet_stderr: bool = False,
) -> list[dict] | dict | str:
    """Run a Docker command that outputs JSON and parse it.

    Returns parsed JSON on success, or an error string on failure.
    """
    raw = _run(cmd, timeout, cwd=cwd, quiet_stderr=quiet_stderr)
    if raw.startswith("Error:"):
        return raw

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Some commands return one JSON object per line (NDJSON)
        try:
            results = []
            for line in raw.strip().splitlines():
                if line.strip():
                    results.append(json.loads(line))
            return results
        except json.JSONDecodeError:
            return raw  # return raw text as fallback


def _count_lines(output: str) -> int:
    """Count non-empty lines in command output (replaces shell ``| wc -l``).

    Treats the ``_run`` sentinel ``(no output)`` and error strings as zero.
    """
    if not output or output == "(no output)" or output.startswith("Error:"):
        return 0
    return len([line for line in output.splitlines() if line.strip()])


def _human_size(size_bytes: int | float) -> str:
    """Convert bytes to a human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}" if unit != "B" else f"{int(size_bytes)} B"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def _format_json_table(data: list[dict], columns: list[str]) -> str:
    """Format a list of dicts as an aligned text table."""
    if not data:
        return "(none)"

    # Calculate column widths
    widths = {col: len(col) for col in columns}
    for row in data:
        for col in columns:
            val = str(row.get(col, ""))
            widths[col] = max(widths[col], len(val))

    # Header
    header = "  ".join(col.ljust(widths[col]) for col in columns)
    separator = "  ".join("-" * widths[col] for col in columns)
    lines = [header, separator]

    # Rows
    for row in data:
        line = "  ".join(str(row.get(col, "")).ljust(widths[col]) for col in columns)
        lines.append(line)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Docker system / overview
# ---------------------------------------------------------------------------


@tool
def docker_df() -> str:
    """Show Docker disk usage breakdown on the LOCAL machine.

    When to use: Understand how much disk space Docker is consuming across
        images, containers, volumes, and build cache on the local host.
    When NOT to use: Remote Docker disk usage (use ssh and run 'docker system df' remotely).
    Input: None.
    Output: Per-category totals (count, size, reclaimable) and a verbose breakdown.
    """
    # docker system df -v --format json is available in newer versions
    # Fall back to regular output if JSON fails
    data = _run_json(["docker", "system", "df", "--format", "{{json .}}"], quiet_stderr=True)

    if isinstance(data, list) and data:
        lines = ["Docker disk usage:\n"]

        for item in data:
            kind = item.get("Type", "Unknown")
            count = item.get("TotalCount", item.get("Total", "?"))
            active = item.get("Active", "?")
            size = item.get("Size", "0B")
            reclaimable = item.get("Reclaimable", "0B")

            lines.append(f"  {kind}:")
            lines.append(f"    Total: {count}, Active: {active}")
            lines.append(f"    Size: {size}")
            lines.append(f"    Reclaimable: {reclaimable}")

        # Also get verbose output for detail (last 30 lines)
        verbose = _run(["docker", "system", "df", "-v"], quiet_stderr=True)
        if verbose and "Error" not in verbose:
            verbose = "\n".join(verbose.splitlines()[-30:])
            lines.append(f"\nDetailed breakdown:\n{verbose}")

        return "\n".join(lines)

    # Fallback to plain text
    return f"Docker disk usage:\n\n{_run(['docker', 'system', 'df', '-v'], quiet_stderr=True)}"


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------


@tool
def docker_ps(all_containers: bool = True) -> str:
    """List Docker containers on the LOCAL machine.

    When to use: Check which Docker containers are running or stopped locally.
    When NOT to use: Containers on a remote host — use ssh(host, 'docker ps') instead.
    Input: all_containers — set false to show only running containers (default: true = all).
    Output: Running and stopped containers grouped separately, with image, status, and ports.
    """
    ps_cmd = ["docker", "ps"]
    if all_containers:
        ps_cmd.append("-a")
    ps_cmd += ["--format", "{{json .}}", "--no-trunc"]

    data = _run_json(ps_cmd, quiet_stderr=True)

    if isinstance(data, list) and data:
        lines = [f"Docker containers ({len(data)} total):\n"]

        running = [c for c in data if "Up" in str(c.get("Status", ""))]
        stopped = [c for c in data if c not in running]

        if running:
            lines.append(f"  Running ({len(running)}):")
            for c in running:
                name = c.get("Names", "?")
                image = c.get("Image", "?")
                status = c.get("Status", "?")
                ports = c.get("Ports", "")
                lines.append(f"    {name}")
                lines.append(f"      Image: {image}")
                lines.append(f"      Status: {status}")
                if ports:
                    lines.append(f"      Ports: {ports}")

        if stopped:
            lines.append(f"\n  Stopped ({len(stopped)}):")
            for c in stopped:
                name = c.get("Names", "?")
                image = c.get("Image", "?")
                status = c.get("Status", "?")
                lines.append(f"    {name}")
                lines.append(f"      Image: {image}")
                lines.append(f"      Status: {status}")

        return "\n".join(lines)

    # Fallback
    fallback_cmd = ["docker", "ps"]
    if all_containers:
        fallback_cmd.append("-a")
    return f"Docker containers:\n\n{_run(fallback_cmd, quiet_stderr=True)}"


@tool
def docker_stats() -> str:
    """Get live CPU, memory, and I/O usage for LOCAL running Docker containers.

    When to use: Diagnose which container is consuming excessive CPU or memory
        on the local machine.
    When NOT to use: Remote containers — use ssh(host, 'docker stats --no-stream').
    Input: None.
    Output: Per-container CPU%, memory usage/limit, network I/O, and block I/O.
    """
    data = _run_json(["docker", "stats", "--no-stream", "--format", "{{json .}}"], quiet_stderr=True)

    if isinstance(data, list) and data:
        lines = ["Container resource usage:\n"]
        for c in data:
            name = c.get("Name", "?")
            cpu = c.get("CPUPerc", "?")
            mem = c.get("MemUsage", "?")
            mem_pct = c.get("MemPerc", "?")
            net_io = c.get("NetIO", "?")
            block_io = c.get("BlockIO", "?")

            lines.append(f"  {name}:")
            lines.append(f"    CPU: {cpu}")
            lines.append(f"    Memory: {mem} ({mem_pct})")
            lines.append(f"    Network I/O: {net_io}")
            lines.append(f"    Block I/O: {block_io}")

        return "\n".join(lines)

    return f"Container stats:\n\n{_run(['docker', 'stats', '--no-stream'], quiet_stderr=True)}"


def _get_max_tail_lines() -> int:
    """Return the max_tail_lines cap from config (default 200)."""
    try:
        from ..config import get_config

        cfg = get_config()
        return int(cfg.get("tools.docker.max_tail_lines", 200))
    except Exception:
        return 200


@tool(
    hint=(
        "LOCAL containers only. If the user mentions a remote host like 'myserver' or "
        "'staging', do NOT use docker_logs — use ssh(host, 'docker logs --tail 200 container') instead."
    )
)
def docker_logs(container: str, tail: int = 50) -> str:
    """Fetch recent log output from a LOCAL Docker container.

    When to use: Inspect recent stdout/stderr from a locally running or stopped container.
    When NOT to use: Remote containers (use ssh(host, 'docker logs --tail 200 <name>')),
        parsing log content for errors (fetch first, then pass to analyze_logs).
    Input: container — container name or ID.
        tail — number of recent lines to return (default 50, capped by config at 200).
    Output: Timestamped log lines from the container's stdout+stderr.
    """
    # Type coercion — models often pass numbers as strings
    tail = int(tail)

    # Enforce config cap so the model can't dump thousands of lines
    max_lines = _get_max_tail_lines()
    if tail > max_lines:
        tail = max_lines

    output = _run(
        ["docker", "logs", "--tail", str(tail), "--timestamps", container],
        timeout=15,
        merge_stderr=True,
    )
    return f"Logs for '{container}' (last {tail} lines):\n\n{output}"


@tool
def docker_inspect(container: str) -> str:
    """Get detailed configuration and state for a LOCAL Docker container.

    When to use: Inspect a container's full config — mounts, ports, environment
        variables, restart policy, network settings, and current state.
    When NOT to use: Just checking if it is running (use docker_ps), remote
        containers (use ssh(host, 'docker inspect <name>')).
    Input: container — container name or ID.
    Output: Structured summary including image, status, environment (redacted secrets),
        mounts, port bindings, and start time.
    """
    data = _run_json(["docker", "inspect", container], quiet_stderr=True)

    if isinstance(data, list) and data:
        c = data[0]
        state = c.get("State", {})
        config = c.get("Config", {})
        host_config = c.get("HostConfig", {})
        network = c.get("NetworkSettings", {})

        lines = [
            f"Container: {c.get('Name', '?').lstrip('/')}",
            f"ID: {c.get('Id', '?')[:12]}",
            f"Image: {config.get('Image', '?')}",
            f"Status: {state.get('Status', '?')}",
            f"Started: {state.get('StartedAt', '?')}",
            f"Restart policy: {host_config.get('RestartPolicy', {}).get('Name', '?')}",
            f"Entrypoint: {config.get('Entrypoint', '?')}",
            f"Command: {config.get('Cmd', '?')}",
        ]

        # Environment variables (filter sensitive ones)
        env = config.get("Env", [])
        if env:
            safe_env = []
            for e in env:
                key = e.split("=")[0] if "=" in e else e
                if any(s in key.upper() for s in ("PASSWORD", "SECRET", "TOKEN", "KEY", "CREDENTIAL")):
                    safe_env.append(f"  {key}=***REDACTED***")
                else:
                    safe_env.append(f"  {e}")
            lines.append(f"Environment ({len(env)} vars):")
            lines.extend(safe_env[:20])  # limit output
            if len(env) > 20:
                lines.append(f"  ... and {len(env) - 20} more")

        # Mounts
        mounts = c.get("Mounts", [])
        if mounts:
            lines.append(f"\nMounts ({len(mounts)}):")
            for m in mounts:
                src = m.get("Source", "?")
                dst = m.get("Destination", "?")
                mode = m.get("Mode", "rw")
                lines.append(f"  {src} → {dst} ({mode})")

        # Ports
        ports = network.get("Ports", {})
        if ports:
            lines.append("\nPorts:")
            for port, bindings in ports.items():
                if bindings:
                    for b in bindings:
                        lines.append(f"  {b.get('HostIp', '0.0.0.0')}:{b.get('HostPort', '?')} → {port}")
                else:
                    lines.append(f"  {port} (not published)")

        return "\n".join(lines)

    # Fallback
    return _run(["docker", "inspect", container], quiet_stderr=True)


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


@tool
def docker_images(show_dangling: bool = False) -> str:
    """List local Docker images with size, tag, and creation date.

    When to use: See what Docker images are available locally, check sizes,
        or identify untagged dangling images that can be pruned.
    When NOT to use: Remote images (use ssh to run 'docker images' remotely),
        disk space overview (use docker_df).
    Input: show_dangling — set true to show only untagged/dangling images (default: false).
    Output: Per-image name:tag, ID, size, and creation time.
    """
    images_cmd = ["docker", "images"]
    if show_dangling:
        images_cmd += ["--filter", "dangling=true"]
    images_cmd += ["--format", "{{json .}}"]
    data = _run_json(images_cmd, quiet_stderr=True)

    if isinstance(data, list):
        label = "dangling" if show_dangling else "total"
        lines = [f"Docker images ({len(data)} {label}):\n"]

        if not data:
            lines.append("  (none)")
            return "\n".join(lines)

        # Sort by size descending (parse size string)
        for img in data:
            repo = img.get("Repository", "<none>")
            tag = img.get("Tag", "<none>")
            img_id = img.get("ID", "?")[:12]
            size = img.get("Size", "?")
            created = img.get("CreatedSince", "?")

            name = f"{repo}:{tag}" if repo != "<none>" else f"<untagged> ({img_id})"
            lines.append(f"  {name}")
            lines.append(f"    Size: {size}, Created: {created}")

        return "\n".join(lines)

    # Fallback
    fallback_cmd = ["docker", "images"]
    if show_dangling:
        fallback_cmd += ["--filter", "dangling=true"]
    return f"Docker images:\n\n{_run(fallback_cmd, quiet_stderr=True)}"


# ---------------------------------------------------------------------------
# Volumes
# ---------------------------------------------------------------------------


@tool
def docker_volumes() -> str:
    """List local Docker volumes and identify potentially orphaned ones.

    When to use: Audit Docker volumes on the local machine — see which are
        actively used by containers and which are orphaned and can be pruned.
    When NOT to use: Remote volumes (use ssh to run 'docker volume ls' remotely).
    Input: None.
    Output: Volumes grouped as in-use vs. potentially orphaned, with driver info
        and a prune hint for orphaned volumes.
    """
    data = _run_json(["docker", "volume", "ls", "--format", "{{json .}}"], quiet_stderr=True)

    if isinstance(data, list):
        lines = [f"Docker volumes ({len(data)} total):\n"]

        if not data:
            lines.append("  (none)")
            return "\n".join(lines)

        # Get list of volumes actually in use by containers
        # Simple heuristic: collect volume names from running containers
        used_volumes = set()
        containers_raw = _run_json(["docker", "ps", "-a", "--format", "{{json .}}"], quiet_stderr=True)
        if isinstance(containers_raw, list):
            for c in containers_raw:
                mounts_str = c.get("Mounts", "")
                if isinstance(mounts_str, str):
                    # Volume names appear in the mounts string
                    for vol in data:
                        vname = vol.get("Name", "")
                        if vname and vname in mounts_str:
                            used_volumes.add(vname)

        orphaned = []
        in_use = []
        for vol in data:
            name = vol.get("Name", "?")
            driver = vol.get("Driver", "?")

            entry = f"  {name}\n    Driver: {driver}"
            if name in used_volumes:
                in_use.append(entry)
            else:
                orphaned.append(entry)

        if in_use:
            lines.append(f"In use ({len(in_use)}):")
            lines.extend(in_use)

        if orphaned:
            lines.append(f"\n⚠ Potentially orphaned ({len(orphaned)}):")
            lines.extend(orphaned)
            lines.append("\n  Tip: Remove orphaned volumes with: docker volume prune")

        return "\n".join(lines)

    # Fallback
    return f"Docker volumes:\n\n{_run(['docker', 'volume', 'ls'], quiet_stderr=True)}"


# ---------------------------------------------------------------------------
# Networks
# ---------------------------------------------------------------------------


@tool
def docker_networks() -> str:
    """List local Docker networks with driver and scope information.

    When to use: Understand the network topology for local containers — e.g.,
        which custom bridge networks exist and how containers are connected.
    When NOT to use: Remote networks (use ssh to run 'docker network ls' remotely).
    Input: None.
    Output: Per-network name, driver (bridge/overlay/host), scope, and ID.
    """
    data = _run_json(["docker", "network", "ls", "--format", "{{json .}}"], quiet_stderr=True)

    if isinstance(data, list):
        lines = [f"Docker networks ({len(data)} total):\n"]

        for net in data:
            name = net.get("Name", "?")
            driver = net.get("Driver", "?")
            scope = net.get("Scope", "?")
            net_id = net.get("ID", "?")[:12]

            lines.append(f"  {name}")
            lines.append(f"    Driver: {driver}, Scope: {scope}, ID: {net_id}")

        return "\n".join(lines)

    return f"Docker networks:\n\n{_run(['docker', 'network', 'ls'], quiet_stderr=True)}"


# ---------------------------------------------------------------------------
# Docker Compose
# ---------------------------------------------------------------------------


@tool
def docker_compose_status(path: str = ".") -> str:
    """Show the status of services defined in a local Docker Compose file.

    When to use: Check whether Compose-managed services are up, stopped,
        or in an error state on the local machine.
    When NOT to use: Remote Compose stacks (use ssh + 'docker compose ps'),
        individual container details (use docker_inspect).
    Input: path — directory containing the docker-compose.yml (default: current directory).
    Output: Service names with their current status (running, exited, etc.)
        and port bindings.
    """
    compose_file = os.path.join(path, "docker-compose.yml")

    # Try docker compose (v2) first, fall back to docker-compose (v1)
    output = _run(["docker", "compose", "-f", compose_file, "ps"], quiet_stderr=True)
    if "Error" in output or "no configuration file" in output.lower():
        output = _run(["docker-compose", "-f", compose_file, "ps"], quiet_stderr=True)

    if "Error" in output:
        # Try without explicit file path (auto-detect in cwd)
        output = _run(["docker", "compose", "ps"], cwd=path, quiet_stderr=True)
        if "Error" in output:
            output = _run(["docker-compose", "ps"], cwd=path, quiet_stderr=True)

    return f"Docker Compose status ({path}):\n\n{output}"


# ---------------------------------------------------------------------------
# Cleanup info (read-only — doesn't actually clean)
# ---------------------------------------------------------------------------


@tool
def docker_cleanup_preview() -> str:
    """Preview reclaimable Docker resources on the LOCAL machine (read-only, no deletions).

    When to use: Find out how much disk space could be freed before running
        'docker system prune' or similar cleanup commands.
    When NOT to use: Actually cleaning up (this tool only previews — run the
        suggested commands manually after reviewing the output).
    Input: None.
    Output: Counts of dangling images, stopped containers, and dangling volumes,
        plus ready-to-run cleanup commands.
    """
    lines = ["Docker cleanup preview (read-only — nothing will be deleted):\n"]

    # Dangling images
    dangling = _run(["docker", "images", "-f", "dangling=true", "-q"], quiet_stderr=True)
    lines.append(f"  Dangling images: {_count_lines(dangling)}")

    # Stopped containers
    stopped = _run(["docker", "ps", "-a", "-f", "status=exited", "-q"], quiet_stderr=True)
    lines.append(f"  Stopped containers: {_count_lines(stopped)}")

    # Unused volumes (not referenced by any container)
    volumes = _run(["docker", "volume", "ls", "-f", "dangling=true", "-q"], quiet_stderr=True)
    lines.append(f"  Dangling volumes: {_count_lines(volumes)}")

    # Build cache (last line of summary)
    cache = _run(["docker", "builder", "du"], quiet_stderr=True)
    if cache and "Error" not in cache:
        cache_last = cache.splitlines()[-1].strip() if cache.splitlines() else ""
        lines.append(f"  Build cache: {cache_last}")

    lines.extend(
        [
            "\nCleanup commands (run manually):",
            "  docker system prune           # Remove stopped containers, dangling images, unused networks",
            "  docker system prune -a        # Also remove unused images (not just dangling)",
            "  docker volume prune           # Remove dangling volumes",
            "  docker builder prune          # Remove build cache",
            "  docker system prune -a --volumes  # Full cleanup (use with caution)",
        ]
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bulk registration
# ---------------------------------------------------------------------------


def register_docker_tools(registry: ToolRegistry) -> int:
    """Register all Docker tools with the given registry.

    Returns the number of tools registered.
    """
    registry.register_category_hint(
        "Docker",
        "Docker tools ONLY work on the LOCAL machine. "
        "If the user mentions a remote host (e.g., 'myserver', 'staging', or any SSH alias), "
        "you MUST use ssh instead — NEVER call docker_ps, docker_logs, etc. for remote containers.",
    )

    tools = [
        docker_df,
        docker_ps,
        docker_stats,
        docker_logs,
        docker_inspect,
        docker_images,
        docker_volumes,
        docker_networks,
        docker_compose_status,
        docker_cleanup_preview,
    ]
    for func in tools:
        registry.register(func, category="Docker")
    return len(tools)
