"""SSH tools — execute commands on remote hosts via SSH and local key management.

Provides a general-purpose ``ssh`` for running any command on a remote
host, plus a ``health_check`` that runs a battery of system diagnostics
in a single SSH session.

Hosts are validated against an allowlist in ``config.yaml → tools.ssh.allowed_hosts``
to prevent the model from connecting to arbitrary machines.

Usage::

    from agentforge.tools import ToolRegistry
    from agentforge.tools.ssh_tools import register_ssh_tools

    registry = ToolRegistry()
    register_ssh_tools(registry)
"""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from chalkbox.logging.bridge import get_logger

from agentforge.config import get_config
from agentforge.tools.command_policy import evaluate
from agentforge.tools.command_policy_store import get_effective_policy

from .registry import tool

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(cmd: list[str], timeout: int = 30, merge_stderr: bool = False) -> str:
    """Run a command (argv list, no shell) and return its stdout (or stderr on failure).

    merge_stderr folds stderr into stdout (equivalent to a shell ``2>&1``) so that
    output written to stderr is captured alongside stdout in a single stream.
    """
    try:
        result = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        # Fold stderr into stdout when requested, mirroring the old "2>&1" behaviour.
        if merge_stderr:
            combined = result.stdout
            if result.stderr:
                combined = combined + result.stderr if combined else result.stderr
            output = combined.strip()
            return output or "(no output)"
        output = result.stdout.strip()
        if result.returncode != 0 and result.stderr:
            stderr = result.stderr.strip()
            if output:
                output += f"\nSTDERR: {stderr}"
            else:
                output = f"Error: {stderr}"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: command timed out ({timeout}s limit)"
    except FileNotFoundError as exc:
        # Mirror the old shell behaviour where a missing binary surfaced as an error string.
        return f"Error: command not found: {exc.filename}"
    except Exception as exc:
        return f"Error: {exc}"


def _get_allowed_hosts() -> list[str]:
    """Return the allowed SSH hosts from config."""
    try:
        cfg = get_config()
        hosts = cfg.get("tools.ssh.allowed_hosts")
        if isinstance(hosts, list):
            return hosts
    except Exception:
        pass
    return []


def _get_connection_timeout() -> int:
    """Return the SSH connection timeout from config (default 10s)."""
    try:
        cfg = get_config()
        return int(cfg.get("tools.ssh.connection_timeout", 10))
    except Exception:
        return 10


def _strict_host_key_checking() -> str:
    """Return the StrictHostKeyChecking policy from config (default 'accept-new').

    Value passes straight through to ``-o StrictHostKeyChecking=<value>`` — e.g.
    ``yes``, ``accept-new``, ``no``. Production should set ``yes`` with
    pre-provisioned known_hosts so a MITM on the first connection can't be trusted.
    """
    try:
        cfg = get_config()
        return str(cfg._raw.get("tools", {}).get("ssh", {}).get("strict_host_key_checking", "accept-new"))
    except Exception:
        return "accept-new"


def _validate_host(host: str) -> str | None:
    """Check host against the allowlist.  Returns an error string if denied, None if OK."""
    allowed = _get_allowed_hosts()
    if not allowed:
        return "Error: No SSH hosts configured. Add allowed hosts to config.yaml → tools.ssh.allowed_hosts"
    if host not in allowed:
        return (
            f"Error: Host '{host}' is not in the allowed hosts list. "
            f"Allowed: {', '.join(allowed)}. "
            f"Add it to config.yaml → tools.ssh.allowed_hosts to permit access."
        )
    return None


def _validate_ssh_command(command: str) -> str | None:
    """Validate a remote command against the effective SSH command policy."""
    if not command.strip():
        return None

    policy = get_effective_policy("ssh")
    verdict = evaluate("ssh", command, policy)
    if verdict.action == "deny":
        return f"Error: {verdict.reason}"
    return None


# ---------------------------------------------------------------------------
# ssh — general-purpose remote execution
# ---------------------------------------------------------------------------


@tool(
    hint=(
        "When running log commands (docker logs, journalctl, cat on large files, etc.), "
        "ALWAYS use --tail 200 to avoid overwhelming the context window. "
        "Only omit --tail when the user explicitly asks for the full/complete log."
    )
)
def ssh(host: str, command: str = "", timeout: int = 0) -> str:
    """Run a command on a REMOTE host via SSH — use this for anything on remote machines.

    When to use: Any command on a remote host (myserver, staging, etc.), including
        remote Docker commands, logs, diagnostics, or system checks.
    When NOT to use: Local commands (use shell instead), file transfers (use scp/rsync),
        local system health (use shell with 'free -h && df -h && uptime').

    Use this tool whenever the user mentions a remote host like 'myserver' or
    'staging'. For remote Docker commands, use ssh — NOT docker_ps/docker_logs
    (those only work locally).

    host: SSH host name or alias (e.g., 'myserver', 'staging')
    command: the shell command to execute remotely
    timeout: SSH connect timeout in seconds (0 = use config default)

    Examples:
      ssh('myserver', 'docker ps')
      ssh('myserver', 'docker logs --tail 200 hub')
      ssh('myserver', 'journalctl -u nginx --no-pager --tail 200')
      ssh('myserver', 'uptime')
      ssh('myserver', 'df -h && free -h')
      ssh('staging', 'systemctl status nginx')
    """
    # Type coercion
    timeout = int(timeout)

    # The model sometimes calls ssh with only a host and no command. Return a
    # clear message so it self-corrects, rather than raising a TypeError on the
    # missing positional arg (which surfaces as an opaque "tool failed").
    if not command or not command.strip():
        return "Error: ssh requires a 'command' argument (e.g., ssh(host, 'docker ps'))."

    # Validate host against allowlist
    err = _validate_host(host)
    if err:
        return err

    err = _validate_ssh_command(command)
    if err:
        return err

    # Use config default if caller didn't specify a timeout
    if timeout <= 0:
        timeout = _get_connection_timeout()

    # Append 2>&1 so the REMOTE command's stderr (e.g., docker logs output) is
    # merged into stdout on the remote side. This runs in the remote shell — the
    # local process invokes ssh with shell=False, so no local quoting is needed.
    remote_command = f"{command} 2>&1"

    # Build SSH command with safety flags as separate argv elements:
    #   -o ConnectTimeout  — don't hang on unreachable hosts
    #   -o BatchMode=yes   — never prompt for password (fail fast)
    #   -o StrictHostKeyChecking — policy from config (default accept-new)
    ssh_cmd = [
        "ssh",
        "-o",
        f"ConnectTimeout={timeout}",
        "-o",
        "BatchMode=yes",
        "-o",
        f"StrictHostKeyChecking={_strict_host_key_checking()}",
        host,
        remote_command,
    ]

    # Allow generous execution time: connect timeout + 60s for the command itself
    exec_timeout = timeout + 60

    output = _run(ssh_cmd, timeout=exec_timeout)

    # Provide clearer error messages for common SSH failures.
    # NOTE: Host aliases and keys are fully configured in the user's
    # ~/.ssh/config — do NOT suggest checking keys or config to the model,
    # as that leads it to debug SSH setup instead of reporting the failure.
    if "Error:" in output:
        lower = output.lower()
        if "permission denied" in lower:
            return (
                f"Error: SSH authentication failed for '{host}'. "
                f"This is a transient or server-side issue — do NOT attempt "
                f"to debug SSH keys or config. Report the failure to the user."
            )
        if "could not resolve hostname" in lower:
            return f"Error: Host '{host}' could not be resolved. Verify the host alias is correct and try again."
        if "connection refused" in lower:
            return f"Error: Connection refused by '{host}'. The SSH service may not be running on the remote host."
        if "connection timed out" in lower or "timed out" in lower:
            return (
                f"Error: Connection to '{host}' timed out after {timeout}s. "
                f"The host may be unreachable or a firewall is blocking SSH."
            )

    return f"[{host}] $ {command}\n\n{output}"


# ---------------------------------------------------------------------------
# health_check — compound diagnostics in a single SSH session
# ---------------------------------------------------------------------------


@tool
def health_check(host: str = "") -> str:
    """Run a comprehensive system health check on a remote host via SSH.

    When to use: Get system diagnostics for a remote host in one command
        (disk, memory, CPU, processes).
    When NOT to use: Local system checks (use shell('free -h && df -h && uptime')),
        when you only need one metric (use ssh directly), if the host is not in
        the allowed hosts list.

    Collects disk usage, memory, CPU load, uptime, and top processes
    in a single SSH connection. The host must be in the allowed hosts list.

    For LOCAL system health, use shell('free -h && df -h && uptime') instead.

    host: SSH host name or alias (e.g., 'myserver', 'staging')
    """
    if not host:
        return (
            "Error: health_check requires a remote SSH host. "
            "For local system health, use: shell('free -h && df -h && uptime')."
        )
    # Validate host against allowlist
    err = _validate_host(host)
    if err:
        return err

    timeout = _get_connection_timeout()

    # Compound command — all diagnostics in one SSH session.
    # Each section is separated by a marker so the model can parse them.
    health_cmd = (
        'echo "===DISK===" && df -h 2>/dev/null && '
        'echo "" && echo "===MEMORY===" && free -h 2>/dev/null && '
        'echo "" && echo "===CPU===" && top -bn1 2>/dev/null | head -20 && '
        'echo "" && echo "===LOAD===" && uptime 2>/dev/null && '
        'echo "" && echo "===TOP_PROCESSES_BY_MEM===" && '
        "ps aux --sort=-%mem 2>/dev/null | head -10 && "
        'echo "" && echo "===TOP_PROCESSES_BY_CPU===" && '
        "ps aux --sort=-%cpu 2>/dev/null | head -10"
    )

    # The compound command (with && / pipes) runs in the REMOTE shell. Pass it as a
    # single argv element after the host; the local ssh invocation uses shell=False.
    ssh_cmd = [
        "ssh",
        "-o",
        f"ConnectTimeout={timeout}",
        "-o",
        "BatchMode=yes",
        "-o",
        f"StrictHostKeyChecking={_strict_host_key_checking()}",
        host,
        health_cmd,
    ]

    # Allow generous timeout for the full battery
    exec_timeout = timeout + 90

    output = _run(ssh_cmd, timeout=exec_timeout)

    if output.startswith("Error:"):
        # Re-use ssh's error messaging for clarity
        lower = output.lower()
        if "permission denied" in lower:
            return f"Error: SSH authentication failed for '{host}'. Check your SSH key configuration."
        if "could not resolve hostname" in lower:
            return f"Error: Host '{host}' could not be resolved."
        if "connection refused" in lower:
            return f"Error: Connection refused by '{host}'."
        if "timed out" in lower:
            return f"Error: Connection to '{host}' timed out after {timeout}s."
        return output

    return f"System health report for '{host}':\n\n{output}"


# ---------------------------------------------------------------------------
# scp — file transfer between local and remote (or remote-to-remote)
# ---------------------------------------------------------------------------


def _extract_host(path: str) -> str | None:
    """Return the host part of a ``host:path`` string, or *None* for local paths."""
    # A colon in the first component means remote — but skip Windows drive
    # letters like ``C:\...`` (single char before colon).
    if ":" in path:
        head, _ = path.split(":", 1)
        if len(head) > 1:  # not a drive letter
            return head
    return None


@tool(
    hint=(
        "Use host:path notation for remote paths (e.g., 'myserver:/var/log/app.log'). "
        "Local paths are plain filesystem paths. "
        "Supports local→remote, remote→local, and remote→remote transfers."
    )
)
def scp(
    source: str,
    destination: str,
    recursive: bool = True,
    preserve: bool = True,
    compress: bool = True,
    verbose: bool = False,
) -> str:
    """Copy files or directories between local and remote hosts via SCP.

    When to use: Transfer a file or directory once — download from a remote
        host, upload to one, or copy between two remote hosts.
    When NOT to use: Copying between two local paths (use copy_file),
        incremental or repeated syncs of a directory tree (use rsync,
        which is faster for large or repeated transfers).
    Input: source — source path. Prefix with 'host:' for remote paths
        (e.g., 'myserver:/var/log/app.log' or '/tmp/file.txt').
        destination — destination path, same notation.
        At least one of source or destination must be remote.
    Output: Transfer summary showing direction and size, or an error message.
    Hint: Both hosts must be in config.yaml tools.ssh.allowed_hosts.
        Use rsync for large directory trees — it resumes interrupted transfers.

    Examples:
      scp('myserver:/var/log/app.log', '/tmp/app.log')          # download
      scp('/tmp/deploy.tar.gz', 'myserver:/opt/releases/')      # upload
      scp('myserver:/etc/nginx/', 'staging:/etc/nginx/')         # remote to remote
      scp('/tmp/data/', 'myserver:/backups/data/')               # upload dir
    """
    # Type coercion for booleans that arrive as strings
    recursive = str(recursive).lower() not in ("false", "0", "no")
    preserve = str(preserve).lower() not in ("false", "0", "no")
    compress = str(compress).lower() not in ("false", "0", "no")
    verbose = str(verbose).lower() not in ("false", "0", "no")

    # Validate any remote hosts involved
    src_host = _extract_host(source)
    dst_host = _extract_host(destination)

    if src_host is None and dst_host is None:
        return "Error: At least one path must be remote (host:path). For local copies use copy_file."

    for host in (src_host, dst_host):
        if host is not None:
            err = _validate_host(host)
            if err:
                return err

    conn_timeout = _get_connection_timeout()

    # Build argv — each flag and -o option is a separate element (no local shell).
    cmd: list[str] = ["scp"]
    if recursive:
        cmd.append("-r")
    if preserve:
        cmd.append("-p")
    if compress:
        cmd.append("-C")
    if verbose:
        cmd.append("-v")
    cmd += ["-o", f"ConnectTimeout={conn_timeout}"]
    cmd += ["-o", "BatchMode=yes"]
    cmd += ["-o", f"StrictHostKeyChecking={_strict_host_key_checking()}"]
    cmd += [source, destination]

    # Generous timeout: connect + transfer time
    exec_timeout = conn_timeout + 300

    output = _run(cmd, timeout=exec_timeout)

    # Determine transfer direction for the summary
    if src_host and dst_host:
        direction = f"{src_host} → {dst_host}"
    elif src_host:
        direction = f"{src_host} → local"
    else:
        direction = f"local → {dst_host}"

    if output.startswith("Error:"):
        lower = output.lower()
        if "permission denied" in lower:
            return (
                f"Error: SCP authentication/permission failed ({direction}). Check file permissions on the remote host."
            )
        if "no such file" in lower or "not found" in lower:
            return f"Error: Source path not found — {source}"
        return output

    # Show what happened
    summary = f"SCP transfer complete ({direction})\n  {source} → {destination}"
    if verbose and output and output != "(no output)":
        summary += f"\n\n{output}"
    return summary


# ---------------------------------------------------------------------------
# rsync — efficient file synchronization
# ---------------------------------------------------------------------------


@tool(
    hint=(
        "Prefer rsync over scp for large transfers, directory syncs, or when you need "
        "delta transfers. Use host:path notation for remote paths. "
        "Use dry_run=true to preview changes before executing."
    )
)
def rsync(
    source: str,
    destination: str,
    archive: bool = True,
    compress: bool = True,
    delete: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
    exclude: str = "",
) -> str:
    """Synchronize a directory tree between local and a remote host via rsync.

    When to use: Large or repeated file transfers, deploying a directory to
        a remote server, mirroring directories, or any sync where only changed
        files should be re-sent (delta transfer).
    When NOT to use: One-off single-file copies (use scp, which is simpler),
        purely local copies (use copy_file), remote host has no rsync installed
        (use scp instead).
    Input: source — source path. Prefix with 'host:' for remote
        (e.g., 'myserver:/var/www/' or '/opt/app/').
        destination — destination path, same notation.
        At least one must be remote.
        delete — if true, delete files at destination missing from source.
        dry_run — set true to preview changes without executing them.
        exclude — comma-separated glob patterns to skip (e.g., '*.log,node_modules').
    Output: Transfer statistics showing bytes sent and files changed, or DRY RUN
        preview listing what would change.
    Hint: Always use dry_run=true first when delete=true, to confirm the scope
        of deletions before running for real.

    Examples:
      rsync('/var/www/', 'myserver:/var/www/')
      rsync('myserver:/var/log/', '/tmp/logs/')
      rsync('/opt/app/', 'staging:/opt/app/', delete=true, dry_run=true)
      rsync('/src/', 'myserver:/deploy/', exclude='*.pyc,__pycache__,.git')
    """
    # Type coercion
    archive = str(archive).lower() not in ("false", "0", "no")
    compress = str(compress).lower() not in ("false", "0", "no")
    delete = str(delete).lower() not in ("false", "0", "no")
    dry_run = str(dry_run).lower() not in ("false", "0", "no")
    verbose = str(verbose).lower() not in ("false", "0", "no")

    # Validate remote hosts
    src_host = _extract_host(source)
    dst_host = _extract_host(destination)

    if src_host is None and dst_host is None:
        return "Error: At least one path must be remote (host:path). For local copies use copy_file."

    for host in (src_host, dst_host):
        if host is not None:
            err = _validate_host(host)
            if err:
                return err

    conn_timeout = _get_connection_timeout()

    # Build argv — each flag is its own element (no local shell).
    cmd: list[str] = ["rsync"]
    if archive:
        cmd.append("-a")
    if compress:
        cmd.append("-z")
    if delete:
        cmd.append("--delete")
    if dry_run:
        cmd.append("-n")
    if verbose:
        cmd.append("-v")

    # Always show progress summary
    cmd.append("--stats")
    cmd.append("--human-readable")

    # SSH transport options. rsync parses the -e value itself (splitting on
    # whitespace), so the whole "ssh ..." string is a single argv element.
    ssh_opts = (
        f"-o ConnectTimeout={conn_timeout} -o BatchMode=yes -o StrictHostKeyChecking={_strict_host_key_checking()}"
    )
    cmd += ["-e", f"ssh {ssh_opts}"]

    # Exclusion patterns — one argv element each, no shell quoting.
    if exclude:
        for pattern in exclude.split(","):
            pattern = pattern.strip()
            if pattern:
                cmd.append(f"--exclude={pattern}")

    cmd += [source, destination]

    # Generous timeout for potentially large syncs
    exec_timeout = conn_timeout + 600

    output = _run(cmd, timeout=exec_timeout)

    # Direction label
    if src_host and dst_host:
        direction = f"{src_host} → {dst_host}"
    elif src_host:
        direction = f"{src_host} → local"
    else:
        direction = f"local → {dst_host}"

    prefix = "DRY RUN — " if dry_run else ""

    if output.startswith("Error:"):
        lower = output.lower()
        if "permission denied" in lower:
            return f"Error: Rsync permission denied ({direction}). Check SSH keys and file permissions."
        if "no such file" in lower or "not found" in lower:
            return f"Error: Source path not found — {source}"
        if "command not found" in lower:
            return "Error: rsync is not installed on one of the hosts."
        return output

    return f"{prefix}Rsync ({direction}):\n\n{output}"


# ---------------------------------------------------------------------------
# Helpers (local)
# ---------------------------------------------------------------------------


def _is_macos() -> bool:
    return platform.system() == "Darwin"


# ---------------------------------------------------------------------------
# ssh_keygen — local key generation
# ---------------------------------------------------------------------------


@tool
def ssh_keygen(
    key_type: str = "ed25519",
    bits: int = 0,
    path: str = "",
    comment: str = "",
    add_to_agent: bool = True,
    keychain: bool = False,
) -> str:
    """Generate a new SSH key pair on the LOCAL machine.

    When to use: Create a new SSH identity for server access, GitHub/GitLab
        authentication, code signing, or any service that accepts SSH public keys.
    When NOT to use: Running remote commands (use ssh), transferring files
        (use scp/rsync), listing existing keys (use shell('ls ~/.ssh')).
    Input: key_type — algorithm: ed25519 (recommended, default), rsa, ecdsa.
        bits — key size. For rsa: 2048 / 3072 / 4096 (default 4096).
               For ecdsa: 256 / 384 / 521 (default 521). Ignored for ed25519.
        path — private key file path (default: ~/.ssh/id_{key_type}).
               The public key is written alongside as {path}.pub.
        comment — label embedded in the public key, e.g., 'user@host' or
                  'deploy-key'. Defaults to current user@hostname if omitted.
        add_to_agent — load the private key into the running ssh-agent
                       immediately after creation (default: true).
        keychain — macOS only: store the passphrase in Keychain so the key
                   reloads automatically after reboot (default: false).
                   No effect on Linux.
    Output: Paths to both key files, fingerprint, and the full public key string
        ready to paste into authorized_keys or a hosting service dashboard.

    Examples:
      ssh_keygen()
      ssh_keygen('rsa', bits=4096, comment='deploy@prod-server')
      ssh_keygen('ed25519', path='~/.ssh/id_github', comment='github-robin')
      ssh_keygen('ed25519', add_to_agent=True, keychain=True)
    """
    # Type coercion — args arrive as strings from the LLM
    bits = int(bits) if bits else 0
    add_to_agent = str(add_to_agent).lower() not in ("false", "0", "no")
    keychain = str(keychain).lower() not in ("false", "0", "no")

    key_type = key_type.lower().strip()
    if key_type not in {"ed25519", "rsa", "ecdsa"}:
        return f"Error: Unsupported key type '{key_type}'. Valid types: ed25519, rsa, ecdsa."

    # Resolve output path
    if not path:
        path = f"~/.ssh/id_{key_type}"
    key_path = Path(path).expanduser().resolve()
    pub_path = Path(str(key_path) + ".pub")

    # Guard against silently overwriting an existing key
    if key_path.exists():
        return f"Error: Key already exists at {key_path}. Choose a different path or remove the existing key first."

    # Ensure ~/.ssh exists with strict permissions
    key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    # Build ssh-keygen command
    #   -t : key type
    #   -f : output file
    #   -N : passphrase (empty — non-interactive)
    #   -C : comment
    #   -b : bits (rsa/ecdsa only)
    parts = ["ssh-keygen", "-t", key_type, "-f", str(key_path), "-N", ""]

    if comment:
        parts += ["-C", comment]

    if key_type == "rsa":
        parts += ["-b", str(bits if bits > 0 else 4096)]
    elif key_type == "ecdsa":
        parts += ["-b", str(bits if bits > 0 else 521)]

    keygen_out = _run(parts, timeout=30)

    if keygen_out.startswith("Error:") or not key_path.exists():
        return f"Error: ssh-keygen failed.\n{keygen_out}"

    # Read the public key
    pub_key = pub_path.read_text().strip() if pub_path.exists() else "(could not read)"

    # Fingerprint
    fp_out = _run(["ssh-keygen", "-l", "-f", str(key_path)], timeout=10)
    fingerprint = fp_out if not fp_out.startswith("Error:") else "(unavailable)"

    lines = [
        "SSH key pair generated:",
        "",
        f"  Private key : {key_path}",
        f"  Public key  : {pub_path}",
        f"  Fingerprint : {fingerprint}",
        "",
        "Public key (paste into authorized_keys or a hosting service):",
        pub_key,
    ]

    # Add to ssh-agent
    if add_to_agent:
        lines.append("")
        if _is_macos() and keychain:
            # --apple-use-keychain (Monterey+) or -K (older macOS)
            agent_out = _run(["ssh-add", "--apple-use-keychain", str(key_path)], timeout=15, merge_stderr=True)
            if "unknown option" in agent_out.lower() or agent_out.startswith("Error:"):
                agent_out = _run(["ssh-add", "-K", str(key_path)], timeout=15, merge_stderr=True)
            label = agent_out if agent_out != "(no output)" else "Added to agent and Keychain."
            lines.append(f"ssh-agent + Keychain: {label}")
            lines.append("")
            lines.append("Add to ~/.ssh/config to auto-load from Keychain on login:")
            lines.append("  Host *")
            lines.append("    AddKeysToAgent yes")
            lines.append("    UseKeychain yes")
            lines.append(f"    IdentityFile {key_path}")
        else:
            agent_out = _run(["ssh-add", str(key_path)], timeout=15, merge_stderr=True)
            label = agent_out if agent_out != "(no output)" else "Key added to agent."
            lines.append(f"ssh-agent: {label}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bulk registration
# ---------------------------------------------------------------------------


def register_ssh_tools(registry: ToolRegistry) -> int:
    """Register all SSH tools with the given registry.

    Returns the number of tools registered.
    """
    registry.register_category_hint(
        "SSH",
        "SSH tools run commands on REMOTE hosts. Just use the host alias "
        "(e.g., 'myserver', 'staging') — all connection details and keys are "
        "pre-configured. Do NOT pass SSH key paths, identity files, usernames, "
        "IPs, or any extra connection arguments.",
    )

    tools = [
        ssh,
        health_check,
        scp,
        rsync,
        ssh_keygen,
    ]
    for func in tools:
        registry.register(func, category="SSH")
    return len(tools)
