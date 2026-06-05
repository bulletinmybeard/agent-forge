"""System inspection tools — CPU, memory, disk, GPU, processes, and more.

Structured wrappers around system commands that return clean, parsed output
so the model can reason about system state without parsing raw CLI output.

Usage::

    from agentforge.tools import ToolRegistry
    from agentforge.tools.system import register_system_tools

    registry = ToolRegistry()
    register_system_tools(registry)
"""

from __future__ import annotations

import os
import platform
import shlex
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from chalkbox.logging.bridge import get_logger

from .registry import tool

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(cmd: str, timeout: int = 15) -> str:
    """Run a shell command and return its stdout (or stderr on failure)."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,  # noqa: S602
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout.strip()
        if result.returncode != 0 and result.stderr:
            output += f"\nSTDERR: {result.stderr.strip()}"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: command timed out ({timeout}s limit)"
    except Exception as exc:
        return f"Error: {exc}"


def _human_size(size_bytes: int | float) -> str:
    """Convert bytes to a human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}" if unit != "B" else f"{int(size_bytes)} B"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def _is_linux() -> bool:
    return platform.system() == "Linux"


def _is_macos() -> bool:
    return platform.system() == "Darwin"


# ---------------------------------------------------------------------------
# System context (non-tool utility for prompt injection)
# ---------------------------------------------------------------------------


def get_system_context() -> dict[str, str]:
    """Return a dict of system environment facts for injecting into prompts.

    This is **not** a tool — it's a helper meant to be called at setup time so
    that agent system prompts can include facts like the home directory path,
    the current OS, and the default shell.

    Returns a dict with keys:
        os, os_release, os_version, arch, hostname, home, cwd, shell, user,
        python, summary  (one-line prompt-ready string)
    """
    home = os.path.expanduser("~")
    user = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    shell = os.environ.get("SHELL", "unknown")
    os_name = platform.system()  # Darwin / Linux / Windows
    os_release = platform.release()  # e.g., 24.3.0
    os_version = platform.version()  # full version string
    arch = platform.machine()  # arm64 / x86_64
    hostname = platform.node()
    cwd = os.getcwd()
    python_ver = platform.python_version()

    # Friendly OS label
    if os_name == "Darwin":
        os_label = f"macOS ({os_release})"
    elif os_name == "Linux":
        os_label = f"Linux ({os_release})"
    else:
        os_label = f"{os_name} ({os_release})"

    # Discover sibling projects (directories next to cwd)
    sibling_projects: list[str] = []
    try:
        parent = Path(cwd).parent
        siblings = sorted(d.name for d in parent.iterdir() if d.is_dir() and not d.name.startswith("."))
        sibling_projects = siblings
    except OSError:
        pass

    summary = (
        f"OS={os_label}, arch={arch}, home={home}, cwd={cwd}, "
        f"shell={shell}, user={user}. "
        f"Use '{home}' when the user says '~' or 'home directory'."
    )

    # macOS-specific shell hints so the LLM generates BSD-compatible commands
    if os_name == "Darwin":
        summary += (
            " IMPORTANT — this is macOS (BSD userland, not GNU). "
            "Use BSD-compatible syntax: "
            "sed -i '' (NOT sed -i), "
            "no xargs -d (use tr + xargs -0), "
            "date uses -j -f (NOT date -d), "
            "stat -f '%z' (NOT stat -c '%s'), "
            "readlink has no -f (use realpath), "
            "grep -P not available (use grep -E or perl). "
            "A safety layer auto-fixes some of these, but prefer correct syntax."
        )
    if sibling_projects:
        parent_path = str(Path(cwd).parent)
        summary += (
            f" Sibling projects in {parent_path}/: "
            f"{', '.join(sibling_projects)}. "
            f"When the user mentions a project name, check siblings first."
        )

    # Condensed variant: bare essentials for tool-call iterations (no BSD
    # warnings, no sibling discovery, no protected-path rules).
    condensed_summary = f"OS={os_label}, home={home}, cwd={cwd}, shell={shell}"

    return {
        "os": os_name,
        "os_label": os_label,
        "os_release": os_release,
        "os_version": os_version,
        "arch": arch,
        "hostname": hostname,
        "home": home,
        "cwd": cwd,
        "shell": shell,
        "user": user,
        "python": python_ver,
        "sibling_projects": sibling_projects,
        "summary": summary,
        "condensed_summary": condensed_summary,
    }


# ---------------------------------------------------------------------------
# System overview (tool)
# ---------------------------------------------------------------------------


@tool
def system_overview() -> str:
    """Get a quick summary of the LOCAL system: OS, kernel, hostname, architecture, and uptime.

    When to use: First step when you need to understand what kind of system
        you're working on — OS version, architecture, and uptime.
    When NOT to use: Remote host overview (use ssh + health_check),
        detailed CPU/memory/disk (use cpu_info, memory_info, disk_usage).
    Input: None.
    Output: Hostname, OS + kernel version, architecture, Python version, uptime,
        and logged-in user count.
    """
    lines = [
        f"Hostname: {platform.node()}",
        f"OS: {platform.system()} {platform.release()}",
        f"Version: {platform.version()}",
        f"Architecture: {platform.machine()}",
        f"Python: {platform.python_version()}",
    ]

    # Uptime
    uptime = _run("uptime -p 2>/dev/null || uptime")
    lines.append(f"Uptime: {uptime}")

    # Logged-in users
    users = _run("who | awk '{print $1}' | sort -u | wc -l")
    lines.append(f"Logged-in users: {users.strip()}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CPU
# ---------------------------------------------------------------------------


@tool
def cpu_info() -> str:
    """Get LOCAL CPU details: model, core count, load averages, and top CPU-consuming processes.

    When to use: Diagnose high CPU load, confirm hardware specs, or check
        system load trends on the local machine.
    When NOT to use: Remote CPU info (use ssh + health_check or shell),
        GPU information (use gpu_info).
    Input: None.
    Output: CPU model string, core count, 1/5/15-minute load averages,
        and the top processes by CPU usage.
    """
    lines = []

    # Processor model
    if _is_linux():
        model = _run("grep -m1 'model name' /proc/cpuinfo | cut -d: -f2")
        lines.append(f"Model: {model.strip()}")
    elif _is_macos():
        model = _run("sysctl -n machdep.cpu.brand_string")
        lines.append(f"Model: {model.strip()}")

    # Core counts
    try:
        physical = os.cpu_count()
        lines.append(f"CPU cores: {physical}")
    except Exception:
        pass

    # Load averages
    try:
        load1, load5, load15 = os.getloadavg()
        lines.append(f"Load average: {load1:.2f} (1m), {load5:.2f} (5m), {load15:.2f} (15m)")
    except OSError:
        load = _run("cat /proc/loadavg 2>/dev/null || uptime")
        lines.append(f"Load: {load}")

    # Top CPU consumers
    top = _run("ps aux --sort=-%cpu 2>/dev/null | head -6 || ps aux | head -6")
    lines.append(f"\nTop processes by CPU:\n{top}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


@tool
def memory_info() -> str:
    """Get LOCAL memory usage: total, used, free, cached, buffers, and swap.

    When to use: Check whether the local machine is under memory pressure,
        or identify which processes are consuming the most RAM.
    When NOT to use: Remote memory (use ssh + health_check or 'free -h'),
        GPU VRAM (use gpu_info).
    Input: None.
    Output: Memory breakdown in human-readable sizes, swap usage with a
        warning if swap is in use, and top processes by memory consumption.
    """
    lines = []

    if _is_linux():
        # Parse /proc/meminfo for structured data
        try:
            meminfo = Path("/proc/meminfo").read_text()
            mem = {}
            for line in meminfo.splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].rstrip(":")
                    value = int(parts[1]) * 1024  # kB → bytes
                    mem[key] = value

            total = mem.get("MemTotal", 0)
            free = mem.get("MemFree", 0)
            available = mem.get("MemAvailable", 0)
            buffers = mem.get("Buffers", 0)
            cached = mem.get("Cached", 0)
            swap_total = mem.get("SwapTotal", 0)
            swap_free = mem.get("SwapFree", 0)
            swap_used = swap_total - swap_free

            used = total - available
            used_pct = (used / total * 100) if total else 0

            lines.extend(
                [
                    f"Total: {_human_size(total)}",
                    f"Used: {_human_size(used)} ({used_pct:.1f}%)",
                    f"Free: {_human_size(free)}",
                    f"Available: {_human_size(available)}",
                    f"Buffers: {_human_size(buffers)}",
                    f"Cached: {_human_size(cached)}",
                    f"Swap total: {_human_size(swap_total)}",
                    f"Swap used: {_human_size(swap_used)}",
                    f"Swap free: {_human_size(swap_free)}",
                ]
            )

            if swap_used > 0:
                swap_pct = (swap_used / swap_total * 100) if swap_total else 0
                lines.append(f"⚠ Swap in use: {swap_pct:.1f}% — possible memory pressure")

        except Exception:
            lines.append(_run("free -h"))
    elif _is_macos():
        lines.append(_run("vm_stat"))
        lines.append("\n" + _run("sysctl hw.memsize"))
    else:
        lines.append(_run("free -h 2>/dev/null || echo 'Memory info not available'"))

    # Top memory consumers
    top = _run("ps aux --sort=-%mem 2>/dev/null | head -6 || ps aux | head -6")
    lines.append(f"\nTop processes by memory:\n{top}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Disk
# ---------------------------------------------------------------------------


@tool
def disk_usage() -> str:
    """Get disk usage across all mounted filesystems on the LOCAL machine.

    When to use: Check how full each disk/partition is, and get warnings for
        any filesystem above 85% capacity.
    When NOT to use: Directory-level size breakdown (use dir_size or ncdu_report),
        finding large files (use find_large_files), remote disk usage (use ssh + 'df -h').
    Input: None.
    Output: Human-readable table of all filesystems with size/used/available/percent,
        plus CRITICAL/WARNING annotations for high-usage partitions.
    """
    lines = ["Filesystem usage:\n"]

    output = _run("df -h --output=source,fstype,size,used,avail,pcent,target 2>/dev/null || df -h")
    lines.append(output)

    # Flag any filesystem above 85%
    warnings = []
    for line in output.splitlines()[1:]:  # skip header
        parts = line.split()
        if len(parts) >= 6:
            pct_str = parts[-2] if "%" in parts[-2] else ""
            if pct_str:
                try:
                    pct = int(pct_str.rstrip("%"))
                    mount = parts[-1]
                    if pct >= 90:
                        warnings.append(f"  CRITICAL: {mount} is {pct}% full")
                    elif pct >= 85:
                        warnings.append(f"  WARNING: {mount} is {pct}% full")
                except ValueError:
                    pass

    if warnings:
        lines.append("\n⚠ Disk warnings:")
        lines.extend(warnings)

    return "\n".join(lines)


@tool
def disk_io() -> str:
    """Get disk I/O statistics on the LOCAL Linux machine.

    When to use: Diagnose I/O bottlenecks — high read/write rates or await
        times indicating a disk-bound process on Linux.
    When NOT to use: macOS (not supported — use Activity Monitor or iostat manually),
        general disk space (use disk_usage), remote hosts (use ssh + iostat).
    Input: None.
    Output: Per-device I/O stats via iostat, or raw /proc/diskstats as fallback.
    """
    if not _is_linux():
        return "Disk I/O stats are only available on Linux (via /proc/diskstats or iostat)."

    # Try iostat first (more readable)
    iostat = _run("iostat -x 1 1 2>/dev/null")
    if "Error" not in iostat and iostat != "(no output)":
        return f"Disk I/O (iostat):\n{iostat}"

    # Fallback to /proc/diskstats
    return f"Disk I/O (/proc/diskstats):\n{_run('cat /proc/diskstats')}"


# ---------------------------------------------------------------------------
# GPU
# ---------------------------------------------------------------------------


@tool
def gpu_info() -> str:
    """Get GPU information and utilization on the LOCAL machine.

    When to use: Check VRAM usage, GPU utilization, temperature, and driver
        version for AI/ML workloads or gaming on the local host.
    When NOT to use: Remote GPU info (use ssh + nvidia-smi), CPU information (use cpu_info).
    Input: None.
    Output: NVIDIA GPU details via nvidia-smi (model, driver, VRAM used/total,
        utilization %, temperature) with warnings for high load or temperature.
        Falls back to AMD ROCm or Apple Silicon if NVIDIA is not detected.
    """
    # Try NVIDIA first
    nvidia = _run(
        "nvidia-smi --query-gpu=name,driver_version,memory.total,memory.used,"
        "memory.free,utilization.gpu,utilization.memory,temperature.gpu "
        "--format=csv,noheader,nounits 2>/dev/null"
    )

    if "Error" not in nvidia and "command not found" not in nvidia.lower():
        lines = ["NVIDIA GPU(s):\n"]
        for i, row in enumerate(nvidia.strip().splitlines()):
            parts = [p.strip() for p in row.split(",")]
            if len(parts) >= 8:
                name, driver, mem_total, mem_used, mem_free, gpu_util, mem_util, temp = parts[:8]
                lines.extend(
                    [
                        f"  GPU {i}: {name}",
                        f"    Driver: {driver}",
                        f"    VRAM: {mem_used}MB / {mem_total}MB ({mem_free}MB free)",
                        f"    GPU utilization: {gpu_util}%",
                        f"    Memory utilization: {mem_util}%",
                        f"    Temperature: {temp}°C",
                    ]
                )

                # Warnings
                try:
                    if int(gpu_util) >= 90:
                        lines.append("    ⚠ GPU under heavy load")
                    if int(temp) >= 85:
                        lines.append("    ⚠ Temperature is high")
                    vram_pct = int(mem_used) / int(mem_total) * 100
                    if vram_pct >= 90:
                        lines.append(f"    ⚠ VRAM nearly full ({vram_pct:.0f}%)")
                except (ValueError, ZeroDivisionError):
                    pass

        return "\n".join(lines)

    # Try AMD ROCm
    rocm = _run("rocm-smi --showuse --showtemp 2>/dev/null")
    if "Error" not in rocm and "command not found" not in rocm.lower():
        return f"AMD GPU (ROCm):\n{rocm}"

    # Try Apple Silicon (macOS)
    if _is_macos():
        gpu = _run("system_profiler SPDisplaysDataType 2>/dev/null")
        if "Error" not in gpu:
            return f"GPU info (macOS):\n{gpu}"

    return "No GPU detected or GPU tools (nvidia-smi, rocm-smi) not available."


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------


@tool
def process_list(sort_by: str = "cpu", top_n: int = 15) -> str:
    """List the top processes by CPU or memory usage on the LOCAL machine.

    When to use: Find which process is consuming the most CPU or RAM locally —
        useful for debugging runaway processes or unexpected resource use.
    When NOT to use: Remote process list (use ssh + 'ps aux'), Docker container
        stats (use docker_stats), service health (use service_status).
    Input: sort_by — 'cpu' or 'memory' (default: 'cpu').
        top_n — number of processes to return (default: 15).
    Output: Process list with PID, user, CPU%, memory%, and command.
    """
    # LLMs sometimes send numbers as strings — coerce defensively
    top_n = int(top_n)
    sort_field = "-%cpu" if sort_by.lower() in ("cpu", "c") else "-%mem"

    output = _run(f"ps aux --sort={sort_field} 2>/dev/null | head -{top_n + 1} || ps aux | head -{top_n + 1}")
    return f"Top {top_n} processes by {sort_by}:\n\n{output}"


@tool
def service_status(name: str) -> str:
    """Check the status of a systemd service on the LOCAL Linux machine.

    When to use: Verify whether a service (nginx, docker, postgresql, etc.)
        is active, failed, or stopped on the local host.
    When NOT to use: macOS (systemd not available — use shell + launchctl),
        remote service status (use ssh + 'systemctl status <name>'),
        Docker container status (use docker_ps).
    Input: name — systemd service name (e.g., 'nginx', 'docker', 'postgresql').
    Output: Full systemctl status output including active state, recent journal
        entries, and PID.
    """
    if not _is_linux():
        return "Service status checks are only available on Linux with systemd."

    output = _run(f"systemctl status {shlex.quote(name)} --no-pager -l 2>/dev/null")
    if "could not be found" in output.lower():
        return f"Service '{name}' not found. Check the name with: systemctl list-units --type=service"
    return f"Service '{name}':\n{output}"


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------


@tool
def network_info() -> str:
    """Get LOCAL network interface information: IPs, status, DNS, and listening ports.

    When to use: Find the local machine's IP addresses, check which ports are
        listening, or verify DNS configuration.
    When NOT to use: Remote network info (use ssh + 'ip addr' or 'ifconfig'),
        checking external connectivity (use curl_fetch or web_search).
    Input: None.
    Output: Interface IP addresses and flags, DNS nameservers, and the list of
        locally listening TCP ports with associated processes.
    """
    lines = ["Network interfaces:\n"]

    if _is_linux():
        # Try ip command first
        output = _run("ip -brief addr 2>/dev/null")
        if "Error" not in output:
            lines.append(output)
        else:
            lines.append(_run("ifconfig 2>/dev/null || echo 'No network tools available'"))

        # DNS
        dns = _run("cat /etc/resolv.conf 2>/dev/null | grep nameserver")
        if dns and "Error" not in dns:
            lines.append(f"\nDNS servers:\n{dns}")

    elif _is_macos():
        lines.append(_run("ifconfig | grep -E 'flags|inet '"))
        dns = _run("scutil --dns 2>/dev/null | grep nameserver | head -5")
        if dns:
            lines.append(f"\nDNS servers:\n{dns}")
    else:
        lines.append(_run("ip addr 2>/dev/null || ifconfig 2>/dev/null"))

    # Listening ports
    ports = _run("ss -tlnp 2>/dev/null | head -20 || netstat -tlnp 2>/dev/null | head -20")
    if ports and "Error" not in ports:
        lines.append(f"\nListening ports:\n{ports}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Temperatures
# ---------------------------------------------------------------------------


@tool
def temperatures() -> str:
    """Get hardware temperature readings (CPU, GPU) on the LOCAL machine.

    When to use: Check for thermal throttling, overheating, or unusual
        temperature spikes on the local machine.
    When NOT to use: Remote temperatures (use ssh + sensors),
        GPU utilization rather than temperature (use gpu_info).
    Input: None.
    Output: CPU sensor readings via lm-sensors (Linux) or thermal zones fallback,
        plus NVIDIA GPU temperature if available. macOS requires third-party tools.
    """
    lines = []

    if _is_linux():
        # Try lm-sensors
        sensors = _run("sensors 2>/dev/null")
        if "Error" not in sensors and "command not found" not in sensors.lower():
            lines.append(f"Sensor readings:\n{sensors}")
        else:
            # Fallback to thermal zones
            thermal = _run("cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null")
            if thermal and "Error" not in thermal:
                lines.append("Thermal zones (millidegrees C):")
                for i, temp_str in enumerate(thermal.strip().splitlines()):
                    try:
                        temp_c = int(temp_str) / 1000
                        lines.append(f"  Zone {i}: {temp_c:.1f}°C")
                    except ValueError:
                        pass
            else:
                lines.append("No temperature sensors found. Install lm-sensors: apt install lm-sensors")

    # GPU temps via nvidia-smi
    gpu_temp = _run("nvidia-smi --query-gpu=name,temperature.gpu --format=csv,noheader 2>/dev/null")
    if "Error" not in gpu_temp and "command not found" not in gpu_temp.lower():
        lines.append(f"\nGPU temperatures:\n{gpu_temp}")

    if _is_macos():
        # macOS doesn't have easy CLI temp access without third-party tools
        lines.append("Temperature readings on macOS require third-party tools (e.g., osx-cpu-temp).")

    if not lines:
        return "No temperature data available."

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bulk registration
# ---------------------------------------------------------------------------


def register_system_tools(registry: ToolRegistry) -> int:
    """Register all system inspection tools with the given registry.

    Returns the number of tools registered.
    """
    registry.register_category_hint(
        "System",
        "System tools inspect the LOCAL machine only. For remote host diagnostics, use ssh or health_check.",
    )

    tools = [
        system_overview,
        cpu_info,
        memory_info,
        disk_usage,
        disk_io,
        gpu_info,
        process_list,
        service_status,
        network_info,
        temperatures,
    ]
    for func in tools:
        registry.register(func, category="System")
    return len(tools)
