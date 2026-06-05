"""Archive tools — create and extract compressed archives.

Automatically selects the right CLI tool based on the file extension:
zip, tar.gz, tar.bz2, tar.xz, tar, rar, 7z.

Usage::

    from agentforge.tools import ToolRegistry
    from agentforge.tools.archive_tools import register_archive_tools

    registry = ToolRegistry()
    register_archive_tools(registry)
"""

from __future__ import annotations

import os
import subprocess
import tarfile
import zipfile
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


def _run(argv: list[str], timeout: int = 120, cwd: str | None = None) -> str:
    """Run a command (argv list, no shell) and return stdout (or stderr on failure).

    Always invoked with an explicit argument vector and ``shell=False`` so that
    paths/archive names can never be interpreted as shell syntax (command
    injection). ``cwd`` replaces the previous ``cd '{parent}' && ...`` pattern.
    """
    try:
        result = subprocess.run(
            argv,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
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
        # Surface the missing binary so the caller can map it to a friendly msg.
        return f"Error: command not found: {exc.filename or argv[0]}"
    except Exception as exc:
        return f"Error: {exc}"


def _is_within(dest_real: str, target: str) -> bool:
    """True if ``target`` resolves to a path inside ``dest_real`` (or equals it)."""
    target_real = os.path.realpath(target)
    return target_real == dest_real or target_real.startswith(dest_real + os.sep)


def _unsafe_member_name(name: str) -> bool:
    """True if an archive member name is an absolute path or contains '..' parts."""
    norm = name.replace("\\", "/")
    if norm.startswith("/") or (len(norm) >= 2 and norm[1] == ":"):  # POSIX abs or Windows drive
        return True
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    return ".." in parts


def _detect_format(path: str) -> str | None:
    """Detect archive format from file extension.

    Returns a format key like 'zip', 'tar.gz', 'rar', '7z', etc.
    Returns None if the extension is not recognized.
    """
    p = path.lower()
    if p.endswith(".tar.gz") or p.endswith(".tgz"):
        return "tar.gz"
    if p.endswith(".tar.bz2") or p.endswith(".tbz2"):
        return "tar.bz2"
    if p.endswith(".tar.xz") or p.endswith(".txz"):
        return "tar.xz"
    if p.endswith(".tar"):
        return "tar"
    if p.endswith(".zip"):
        return "zip"
    if p.endswith(".rar"):
        return "rar"
    if p.endswith(".7z"):
        return "7z"
    return None


# Per-format create command builders (argv lists, run with shell=False).
# zip/rar/7z create from the source's parent dir (passed as cwd) so the archive
# stores a relative basename; tar uses -C for the same effect.
def _create_argv(fmt: str, archive: str, parent: str, basename: str) -> tuple[list[str], str | None]:
    """Return (argv, cwd) for creating an archive of ``basename`` under ``parent``."""
    builders: dict[str, tuple[list[str], str | None]] = {
        "zip": (["zip", "-r", archive, basename], parent),
        "tar.gz": (["tar", "-czf", archive, "-C", parent, basename], None),
        "tar.bz2": (["tar", "-cjf", archive, "-C", parent, basename], None),
        "tar.xz": (["tar", "-cJf", archive, "-C", parent, basename], None),
        "tar": (["tar", "-cf", archive, "-C", parent, basename], None),
        "rar": (["rar", "a", archive, basename], parent),
        "7z": (["7z", "a", archive, basename], parent),
    }
    return builders[fmt]


# Formats whose extraction is handled by the Python stdlib (members are inspected
# before any write). Everything else falls back to an external tool with a
# list-then-validate pre-check.
_TAR_FORMATS = {"tar.gz", "tar.bz2", "tar.xz", "tar"}

_SUPPORTED_FORMATS = ", ".join(f".{fmt}" for fmt in ("zip", "tar.gz", "tar.bz2", "tar.xz", "tar", "rar", "7z"))


# ---------------------------------------------------------------------------
# archive_create
# ---------------------------------------------------------------------------


@tool
def archive_create(source: str, archive: str) -> str:
    """Create a compressed archive from a local file or directory.

    When to use: Bundle a directory or file into a single archive for backup,
        transfer, or distribution.
    When NOT to use: Extracting an existing archive (use archive_extract),
        remote archiving (use ssh + tar on the remote host).
    Input: source — path to the file or directory to compress.
        archive — output archive path; the format is determined by the extension.
        Supported: .zip, .tar.gz/.tgz, .tar.bz2/.tbz2, .tar.xz/.txz, .tar, .rar, .7z
    Output: Confirmation with the archive path and size.
    Hint: Use .tar.gz for maximum compatibility; use .7z for best compression ratio.
    """
    # Resolve paths
    source_path = Path(source).expanduser().resolve()
    archive_path = Path(archive).expanduser().resolve()

    # Validate source exists
    if not source_path.exists():
        return f"Error: Source path does not exist: {source_path}"

    # Detect format
    fmt = _detect_format(str(archive_path))
    if fmt is None:
        return f"Error: Unrecognized archive extension for '{archive_path.name}'. Supported: {_SUPPORTED_FORMATS}"

    # Ensure parent directory of archive exists
    archive_path.parent.mkdir(parents=True, exist_ok=True)

    # Build argv (no shell — paths are passed as discrete arguments)
    argv, cwd = _create_argv(
        fmt,
        archive=str(archive_path),
        parent=str(source_path.parent),
        basename=source_path.name,
    )

    output = _run(argv, cwd=cwd)

    if output.startswith("Error:"):
        # Check for missing tool
        lower = output.lower()
        if "command not found" in lower or "not found" in lower:
            tool_name = argv[0]
            return f"Error: '{tool_name}' is not installed. Install it to create .{fmt} archives."
        return output

    # Verify the archive was created
    if archive_path.exists():
        size = archive_path.stat().st_size
        if size < 1024:
            size_str = f"{size} B"
        elif size < 1024 * 1024:
            size_str = f"{size / 1024:.1f} KB"
        else:
            size_str = f"{size / (1024 * 1024):.1f} MB"
        return f"Archive created: {archive_path} ({size_str})"

    return f"Error: Archive was not created. Command output:\n{output}"


# ---------------------------------------------------------------------------
# archive_extract
# ---------------------------------------------------------------------------


@tool
def archive_extract(archive: str, destination: str = "") -> str:
    """Extract a local archive file to a directory.

    When to use: Unpack a downloaded or locally created archive.
    When NOT to use: Creating a new archive (use archive_create),
        remote extraction (use ssh + tar on the remote host).
    Input: archive — path to the archive file.
        destination — directory to extract into (default: same directory as the archive).
        Supported: .zip, .tar.gz/.tgz, .tar.bz2/.tbz2, .tar.xz/.txz, .tar, .rar, .7z
    Output: Confirmation showing where the archive was extracted to.
    """
    # Resolve paths
    archive_path = Path(archive).expanduser().resolve()

    # Validate archive exists
    if not archive_path.exists():
        return f"Error: Archive does not exist: {archive_path}"
    if not archive_path.is_file():
        return f"Error: Not a file: {archive_path}"

    # Detect format
    fmt = _detect_format(str(archive_path))
    if fmt is None:
        return f"Error: Unrecognized archive extension for '{archive_path.name}'. Supported: {_SUPPORTED_FORMATS}"

    # Resolve destination
    if destination:
        dest_path = Path(destination).expanduser().resolve()
    else:
        dest_path = archive_path.parent

    # Ensure destination exists
    dest_path.mkdir(parents=True, exist_ok=True)

    # Extract with member validation to block path traversal (zip/tar-slip).
    # zip/tar are handled in pure Python so members can be inspected before any
    # write; 7z/rar fall back to external tools with a list-then-validate
    # pre-check (see _extract_external).
    if fmt == "zip":
        output = _extract_zip(archive_path, dest_path)
    elif fmt in _TAR_FORMATS:
        output = _extract_tar(archive_path, dest_path)
    else:
        output = _extract_external(fmt, archive_path, dest_path)

    if output.startswith("Error:"):
        return output

    return f"Extracted '{archive_path.name}' to {dest_path}\n\n{output}"


# ---------------------------------------------------------------------------
# Extraction backends (member-validated)
# ---------------------------------------------------------------------------


def _extract_zip(archive_path: Path, dest_path: Path) -> str:
    """Extract a .zip via stdlib, rejecting any member that escapes ``dest_path``."""
    dest_real = os.path.realpath(dest_path)
    try:
        with zipfile.ZipFile(archive_path) as zf:
            names = zf.namelist()
            for name in names:
                if _unsafe_member_name(name) or not _is_within(dest_real, os.path.join(dest_real, name)):
                    return f"Error: refusing to extract — member escapes destination: {name!r}"
            zf.extractall(dest_path)
    except zipfile.BadZipFile:
        return f"Error: not a valid zip archive: {archive_path.name}"
    except Exception as exc:
        return f"Error: {exc}"
    return f"Extracted {len(names)} entries."


def _extract_tar(archive_path: Path, dest_path: Path) -> str:
    """Extract a tar (any compression) via stdlib, rejecting unsafe members.

    Rejects absolute / '..' names, device nodes, and hard/sym links whose target
    escapes the destination. On Python 3.12+ ``filter="data"`` is also applied as
    defence in depth, but the explicit pre-check keeps behavior consistent across
    versions and yields a clear error message.
    """
    dest_real = os.path.realpath(dest_path)
    try:
        with tarfile.open(archive_path) as tf:
            members = tf.getmembers()
            for m in members:
                if _unsafe_member_name(m.name) or not _is_within(dest_real, os.path.join(dest_real, m.name)):
                    return f"Error: refusing to extract — member escapes destination: {m.name!r}"
                if m.isdev():
                    return f"Error: refusing to extract — device node member: {m.name!r}"
                if m.issym() or m.islnk():
                    # Link target is resolved relative to the link's own directory.
                    link_base = os.path.dirname(os.path.join(dest_real, m.name))
                    if _unsafe_member_name(m.linkname) or not _is_within(
                        dest_real, os.path.join(link_base, m.linkname)
                    ):
                        return f"Error: refusing to extract — link target escapes destination: {m.name!r}"
            try:
                tf.extractall(dest_path, filter="data")
            except TypeError:
                tf.extractall(dest_path)
    except tarfile.ReadError:
        return f"Error: not a valid tar archive: {archive_path.name}"
    except Exception as exc:
        return f"Error: {exc}"
    return f"Extracted {len(members)} entries."


def _list_external_members(fmt: str, archive_path: Path) -> tuple[list[str] | None, str]:
    """List member names for 7z/rar via the external tool (argv, no shell).

    Returns (names, error). ``names`` is None if listing failed; ``error`` carries
    a message (which may flag a missing tool) for the caller to surface.
    """
    if fmt == "7z":
        out = _run(["7z", "l", "-slt", "-ba", str(archive_path)])
        if out.startswith("Error:"):
            return None, out
        names = [line[len("Path = ") :] for line in out.splitlines() if line.startswith("Path = ")]
        return names, ""
    # rar: `unrar lb` prints bare member names, one per line.
    out = _run(["unrar", "lb", str(archive_path)])
    if out.startswith("Error:"):
        return None, out
    names = [line for line in out.splitlines() if line.strip()]
    return names, ""


def _extract_external(fmt: str, archive_path: Path, dest_path: Path) -> str:
    """Extract 7z/rar via the external tool after a list-then-validate pre-check.

    Residual risk: the listing and the extraction are two separate invocations,
    so a TOCTOU race on a mutating archive is theoretically possible. For local,
    at-rest archives (the only supported input) this is not a practical concern.
    """
    names, err = _list_external_members(fmt, archive_path)
    if names is None:
        lower = err.lower()
        if "command not found" in lower or "not found" in lower:
            tool_name = "7z" if fmt == "7z" else "unrar"
            return f"Error: '{tool_name}' is not installed. Install it to extract .{fmt} archives."
        return err

    dest_real = os.path.realpath(dest_path)
    for name in names:
        if _unsafe_member_name(name) or not _is_within(dest_real, os.path.join(dest_real, name)):
            return f"Error: refusing to extract — member escapes destination: {name!r}"

    if fmt == "7z":
        argv = ["7z", "x", str(archive_path), f"-o{dest_path}", "-y"]
        tool_name = "7z"
    else:
        argv = ["unrar", "x", "-o+", str(archive_path), f"{dest_path}/"]
        tool_name = "unrar"

    output = _run(argv)
    if output.startswith("Error:"):
        lower = output.lower()
        if "command not found" in lower or "not found" in lower:
            return f"Error: '{tool_name}' is not installed. Install it to extract .{fmt} archives."
        return output
    return output


# ---------------------------------------------------------------------------
# Bulk registration
# ---------------------------------------------------------------------------


def register_archive_tools(registry: ToolRegistry) -> int:
    """Register all archive tools with the given registry.

    Returns the number of tools registered.
    """
    registry.register_category_hint(
        "Archive",
        "Archive tools create and extract compressed files on the LOCAL machine. "
        "Supported formats: .zip, .tar.gz, .tar.bz2, .tar.xz, .tar, .rar, .7z. "
        "The correct CLI tool is chosen automatically based on the file extension.",
    )

    tools = [
        archive_create,
        archive_extract,
    ]
    for func in tools:
        registry.register(func, category="Archive")
    return len(tools)
