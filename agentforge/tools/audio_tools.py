"""Audio tools — Ardour range extraction and audio concatenation (ffmpeg).

Provides tools for audio editing workflows:

- ``ardour_extract_ranges``: Parse range markers from Ardour project files and
  extract each range as a separate audio file using ffmpeg stream-copy.
- ``audio_concat``: Concatenate multiple audio files with optional silence gaps
  using ffmpeg's concat demuxer (no re-encoding).

Requires ``ffmpeg`` and ``xidel`` (XML/XPath CLI) on the system.

Usage::

    from agentforge.tools import ToolRegistry
    from agentforge.tools.audio_tools import register_audio_tools

    registry = ToolRegistry()
    register_audio_tools(registry)
"""

from __future__ import annotations

import subprocess
import tempfile
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


def _run(cmd: list[str], timeout: int = 300) -> tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", f"Error: command timed out ({timeout}s limit)"
    except Exception as exc:
        return -1, "", f"Error: {exc}"


def _human_size(size_bytes: int) -> str:
    """Format byte count as human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def _get_audio_sample_rate(path: Path) -> int:
    """Get audio sample rate using ffprobe. Returns 48000 as fallback."""
    code, stdout, _ = _run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "default=noprint_wrappers=1:nokey=1",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate",
            str(path),
        ],
        timeout=15,
    )
    if code == 0 and stdout.strip().isdigit():
        return int(stdout.strip())
    return 48000


def _get_ardour_sample_rate(project_path: Path) -> int | None:
    """Extract sample-rate from an Ardour project XML via xidel.

    Returns the session sample rate (e.g., 44100, 48000) or None on failure.
    """
    code, stdout, _ = _run(
        [
            "xidel",
            str(project_path),
            "-s",
            "-e",
            "/Session/@sample-rate",
        ],
        timeout=10,
    )
    if code == 0 and stdout.strip().isdigit():
        return int(stdout.strip())
    return None


# Ardour superclock rates.
#
# Ardour 6 used: sample_rate * 5880 ticks/second (rate depended on session).
# Ardour 7+ fixed the rate at 282,240,000 ticks/second (= 48000 * 5880),
# independent of the session's actual sample rate.
# See libs/temporal/temporal/superclock.h in the Ardour source.
_ARDOUR_SUPERCLOCK_MULTIPLIER = 5880
_ARDOUR_SUPERCLOCK_RATE_V7 = 282_240_000  # fixed rate for Ardour 7+


def _ardour_ts_to_seconds(ts: str, sample_rate: int, superclock_rate: int = 0) -> float:
    """Convert an Ardour superclock timestamp to seconds.

    Uses the provided *superclock_rate* (ticks/second) if given, otherwise
    defaults to the Ardour 7+ fixed rate of 282,240,000.  For Ardour 6
    sessions, pass ``sample_rate * 5880``.
    """
    rate = superclock_rate if superclock_rate > 0 else _ARDOUR_SUPERCLOCK_RATE_V7
    return int(ts) / rate


def _seconds_to_ffmpeg_ts(seconds: float) -> str:
    """Convert seconds (float) to ffmpeg-compatible HH:MM:SS.mmm string."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


# ---------------------------------------------------------------------------
# ardour_extract_ranges
# ---------------------------------------------------------------------------


@tool(
    hint=(
        "Use ardour_extract_ranges to parse range markers from an Ardour DAW "
        "project file (.ardour XML) and extract each range as a separate audio "
        "file. Parameters: project_path (path to the .ardour XML file), "
        "source_audio (the MASTER recording — the original WAV/FLAC/M4A that "
        "was imported into the session, NOT the interchange file inside the "
        "project's interchange/ folder). The master recording is typically in "
        "the same directory as or parent directory of the .ardour file. "
        "Returns the extracted audio files. Requires xidel and ffmpeg."
    ),
)
def ardour_extract_ranges(
    project_path: str,
    source_audio: str = "",
    output_dir: str = "",
    output_format: str = "wav",
) -> str:
    """Extract audio ranges defined in an Ardour project as individual files.

    Parses range markers from the Ardour .ardour XML project file, converts
    Ardour's superclock timestamps to time offsets, and uses ffmpeg stream-copy
    to extract each range from the source audio file. No re-encoding.

    project_path: path to the .ardour project file (XML)
    source_audio: path to the full source audio file (WAV/FLAC/etc.)
    output_dir: directory to write extracted files (default: same dir as project + /export)
    output_format: output audio format extension — wav, flac, mp3 (default: wav)
    """
    proj = Path(project_path).expanduser().resolve()
    src = Path(source_audio).expanduser().resolve() if source_audio else Path()

    # Validate project file
    if not proj.exists():
        return f"Error: Ardour project file not found: {proj}"

    # Auto-detect source audio when not provided or not found.
    # Ardour interchange files (inside the project folder) are raw per-track
    # recordings — they start at 0 within themselves.  The master source is
    # the original imported recording, typically one or two levels ABOVE the
    # project folder.  We search there first before falling back to the
    # interchange folder.
    _AUDIO_EXTS = (".wav", ".flac", ".aiff", ".aif", ".m4a", ".mp3", ".ogg", ".caf")
    if not src.exists():
        stem = proj.stem  # e.g., "Lots of chair scratching"
        # Candidate locations: grandparent dir, parent dir
        _candidates: list[Path] = []
        for _search_dir in (proj.parent.parent, proj.parent):
            for _ext in _AUDIO_EXTS:
                _p = _search_dir / f"{stem}{_ext}"
                if _p.exists():
                    _candidates.append(_p)
            # Also pick up any WAV with the same stem (case-insensitive)
            for _p in _search_dir.glob("*.wav"):
                if _p.stem.lower() == stem.lower() and _p not in _candidates:
                    _candidates.append(_p)
        if _candidates:
            # Prefer WAV over other formats
            src = next((c for c in _candidates if c.suffix.lower() == ".wav"), _candidates[0])
            logger.debug("Auto-detected source audio: %s", src)
        else:
            return (
                f"Error: Source audio file not found: {source_audio}\n"
                f"Hint: Provide the MASTER recording (e.g., the WAV/FLAC in the parent "
                f"of the project folder), not the interchange file inside the project."
            )

    # Check dependencies
    code, _, _ = _run(["xidel", "--version"], timeout=5)
    if code != 0:
        return "Error: xidel is not installed or not in PATH. Install with: brew install xidel"
    code, _, _ = _run(["ffmpeg", "-version"], timeout=5)
    if code != 0:
        return "Error: ffmpeg is not installed or not in PATH."

    # Determine output directory
    if output_dir:
        out_dir = Path(output_dir).expanduser().resolve()
    else:
        out_dir = proj.parent / "export"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Get sample rate — prefer Ardour project XML, fall back to ffprobe on source
    sample_rate = _get_ardour_sample_rate(proj) or _get_audio_sample_rate(src)

    # Determine the correct superclock rate.  Ardour 7+ uses a fixed rate of
    # 282,240,000 regardless of session sample rate.  Ardour 6 used
    # sample_rate * 5880.  We try the fixed rate first; if the resulting
    # positions exceed the source file duration we fall back to the old rate.
    _fd_code, _fd_out, _ = _run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "default=noprint_wrappers=1:nokey=1",
            "-show_entries",
            "format=duration",
            str(src),
        ],
        timeout=15,
    )
    _src_duration = float(_fd_out.strip()) if _fd_out.strip() else None

    superclock_rate = _ARDOUR_SUPERCLOCK_RATE_V7  # default: Ardour 7+ fixed rate
    _rate_source = "Ardour 7+ fixed (282240000)"

    # Quick check: parse first range tick to see if the fixed rate gives a
    # position within the file.  If not, try the legacy rate.
    _peek_xpath = "//Location[@name != 'session' and contains(@start, 'a')][1]/substring-after(@start, 'a')"
    _pk_code, _pk_out, _ = _run(["xidel", str(proj), "-s", "-e", _peek_xpath], timeout=10)
    if _pk_code == 0 and _pk_out.strip().isdigit() and _src_duration:
        _peek_ticks = int(_pk_out.strip())
        _pos_v7 = _peek_ticks / _ARDOUR_SUPERCLOCK_RATE_V7
        _pos_v6 = _peek_ticks / (sample_rate * _ARDOUR_SUPERCLOCK_MULTIPLIER)
        if _pos_v7 > _src_duration and _pos_v6 <= _src_duration:
            superclock_rate = sample_rate * _ARDOUR_SUPERCLOCK_MULTIPLIER
            _rate_source = f"Ardour 6 legacy ({superclock_rate})"
        elif _pos_v7 > _src_duration and _pos_v6 > _src_duration:
            logger.debug(
                "WARNING: Both rates give positions past file end (v7=%.1fs, v6=%.1fs, file=%.1fs)",
                _pos_v7,
                _pos_v6,
                _src_duration,
            )

    logger.debug(
        "Sample rate: %d Hz, superclock rate: %d ticks/sec (%s)",
        sample_rate,
        superclock_rate,
        _rate_source,
    )

    # Determine the timeline baseline: how far into the project timeline the
    # source audio file's region is placed.  Ardour stores all positions as
    # absolute superclock ticks.  An interchange WAV starts at 0:00:00 within
    # the file itself, but the region referencing it may sit at e.g., 02:57:00
    # on the project timeline.  Subtracting this offset from each range's
    # timeline timestamp gives the correct seek position within the file.
    baseline_s = 0.0
    _baseline_source = "none (using raw timestamps)"
    src_stem = src.stem  # e.g., "Lots of chair scratching"

    def _parse_ardour_pos(raw: str) -> float | None:
        """Convert a raw xidel @position value to seconds.

        Handles 'a'-prefixed superclock (Ardour 6+) and plain integers
        (Ardour 5 samples).  Returns None if the value is zero or invalid.
        Takes only the FIRST line of multi-line xidel output (stereo files
        can produce two matches for the Source/@id join).
        """
        line = raw.strip().splitlines()[0].strip().lstrip("a")
        if line.isdigit() and int(line) > 0:
            return _ardour_ts_to_seconds(line, sample_rate, superclock_rate)
        return None

    # Try several XPath strategies in order of preference.
    _region_xpaths: list[tuple[str, str]] = [
        # 1 — AudioRegion (Ardour 6/7) by Source id join
        (
            f"//AudioRegion[Source/@id = //AudioSource[contains(@name, '{src_stem}')]/@id][1]/@position",
            "AudioRegion/Source id join",
        ),
        # 2 — AudioRegion by name match
        (
            f"//AudioRegion[contains(@name, '{src_stem}')][1]/@position",
            "AudioRegion name match",
        ),
        # 3 — Region (Ardour 8+) by name match
        (
            f"//Region[contains(@name, '{src_stem}')][1]/@position",
            "Region name match (Ardour 8+)",
        ),
        # 4 — first Region or AudioRegion anywhere
        (
            "(//AudioRegion | //Region)[1]/@position",
            "first region fallback",
        ),
        # 5 — session Location with IsSessionRange flag
        (
            "//Location[contains(@flags, 'IsSessionRange')]/@start",
            "session Location start",
        ),
    ]

    for _xpath, _strategy in _region_xpaths:
        _code, _out, _ = _run(["xidel", str(proj), "-s", "-e", _xpath], timeout=10)
        if _code == 0 and _out.strip():
            _val = _parse_ardour_pos(_out)
            if _val is not None:
                baseline_s = _val
                _baseline_source = _strategy
                logger.debug("Baseline %.3f s via %s", baseline_s, _strategy)
                break

    logger.debug("Final baseline: %.3f s (%s)", baseline_s, _baseline_source)

    # Parse ranges from Ardour XML via xidel
    # Ardour stores range markers as <Location> nodes with @start and @end
    # prefixed with 'a' (audio timeline). Filter out the 'session' location.
    xpath = (
        "//Location[@name != 'session' and contains(@start, 'a') and contains(@end, 'a')]"
        "/concat(@name, '|', substring-after(@start, 'a'), '|', substring-after(@end, 'a'))"
    )
    code, stdout, stderr = _run(["xidel", str(proj), "-s", "-e", xpath], timeout=15)

    if code != 0:
        return f"Error: Failed to parse Ardour project XML.\n{stderr[:500]}"

    lines = [line.strip() for line in stdout.strip().splitlines() if line.strip()]
    if not lines:
        return f"No range markers found in Ardour project: {proj.name}"

    # Parse and sort ranges by start time
    ranges: list[dict] = []
    for line in lines:
        parts = line.split("|")
        if len(parts) != 3:
            logger.debug("Skipping malformed range line: %s", line)
            continue
        name, start_ts, end_ts = parts
        # Subtract baseline so positions are relative to the start of the audio file
        start_s = max(0.0, _ardour_ts_to_seconds(start_ts, sample_rate, superclock_rate) - baseline_s)
        end_s = max(0.0, _ardour_ts_to_seconds(end_ts, sample_rate, superclock_rate) - baseline_s)
        ranges.append(
            {
                "name": name.strip(),
                "start_s": start_s,
                "end_s": end_s,
                "start_ffmpeg": _seconds_to_ffmpeg_ts(start_s),
                "duration_ffmpeg": _seconds_to_ffmpeg_ts(end_s - start_s),
            }
        )

    ranges.sort(key=lambda r: r["start_s"])

    # Extract each range
    extracted: list[str] = []
    errors: list[str] = []

    for r in ranges:
        # Sanitize filename
        safe_name = r["name"].replace("/", "_").replace("\\", "_").replace(" ", "_")
        out_file = out_dir / f"{safe_name}.{output_format}"

        # Place -ss before -i for fast input-side seeking (PCM/WAV has no
        # keyframes so this is accurate).  Use -t (duration) rather than -to
        # (absolute output timestamp) to avoid ambiguity with ffmpeg versions.
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            r["start_ffmpeg"],
            "-i",
            str(src),
            "-t",
            r["duration_ffmpeg"],
            "-c",
            "copy",
            str(out_file),
        ]

        logger.debug(
            "Extracting '%s': start=%s dur=%s (file-relative)",
            r["name"],
            r["start_ffmpeg"],
            r["duration_ffmpeg"],
        )
        code, _, stderr = _run(cmd, timeout=120)

        if code != 0:
            errors.append(f"  {r['name']}: ffmpeg failed — {stderr[:200]}")
        elif out_file.exists():
            duration = r["end_s"] - r["start_s"]
            extracted.append(
                f"  {out_file.name} ({_human_size(out_file.stat().st_size)}, "
                f"{duration:.1f}s, offset {r['start_ffmpeg']} + {r['duration_ffmpeg']})"
            )
        else:
            errors.append(f"  {r['name']}: output file not created")

    # Build result
    _src_dur_str = f"{_src_duration:.1f}s" if _src_duration else "unknown"

    parts = [
        f"Ardour project: {proj.name}",
        f"Source: {src} ({_src_dur_str}, sample rate: {sample_rate} Hz)",
        f"Superclock rate: {superclock_rate} ({_rate_source})",
        f"Timeline baseline: {baseline_s:.3f}s ({_baseline_source})",
        f"Ranges found: {len(ranges)}",
    ]

    if extracted:
        parts.append(f"\nExtracted {len(extracted)} file(s) to {out_dir}/:")
        parts.extend(extracted)

    if errors:
        parts.append(f"\nFailed ({len(errors)}):")
        parts.extend(errors)

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# audio_concat
# ---------------------------------------------------------------------------


@tool(
    hint=(
        "Use audio_concat to concatenate multiple audio files into one, "
        "optionally adding silence gaps between them and/or at the beginning. "
        "Uses ffmpeg concat demuxer — no re-encoding when formats match. "
        "Silence is generated via the anullsrc filter at matching sample rate."
    ),
)
def audio_concat(
    input_files: str = "",
    output_path: str = "",
    silence_between: float = 0.0,
    silence_start: float = 0.0,
    sample_rate: int = 0,
    **kwargs,
) -> str:
    """Concatenate multiple audio files into one with optional silence gaps.

    Uses ffmpeg's concat demuxer for lossless joining when input formats match.
    When silence is requested, generates silent segments at the matching sample
    rate and channels, then concatenates everything.

    Parameters:
      input_files:     REQUIRED — comma-separated list of audio file paths, e.g.,
                       "/path/to/a.wav,/path/to/b.wav" (NOT a list, NOT separate args)
      output_path:     REQUIRED — destination file path for the concatenated output
      silence_between: seconds of silence to insert between each file (default: 0)
      silence_start:   seconds of silence to insert at the beginning (default: 0)
      sample_rate:     audio sample rate in Hz (0 = auto-detect from first file)
    """
    # Accept common parameter aliases that LLMs tend to use
    input_files = input_files or kwargs.get("audio_files", "") or kwargs.get("files", "") or kwargs.get("inputs", "")
    # LLMs sometimes pass a list instead of a comma-separated string
    if isinstance(input_files, list):
        input_files = ",".join(str(f) for f in input_files)
    if not input_files:
        return "Error: input_files is required — comma-separated list of audio file paths"
    output_path = (
        output_path or kwargs.get("output_file", "") or kwargs.get("output", "") or kwargs.get("destination", "")
    )
    if not output_path:
        return "Error: output_path is required — provide a destination file path"
    silence_start = silence_start or float(kwargs.get("silence_at_start", 0))
    silence_between = silence_between or float(kwargs.get("silence_gap", 0) or kwargs.get("gap", 0))
    paths = [Path(p.strip()).expanduser().resolve() for p in input_files.split(",")]

    # Validate inputs
    for p in paths:
        if not p.exists():
            return f"Error: File not found: {p}"
        if not p.is_file():
            return f"Error: Not a file: {p}"

    if len(paths) < 2:
        return "Error: Need at least 2 audio files to concatenate."

    out = Path(output_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    # Check ffmpeg
    code, _, _ = _run(["ffmpeg", "-version"], timeout=5)
    if code != 0:
        return "Error: ffmpeg is not installed or not in PATH."

    # Auto-detect sample rate from first file
    if sample_rate <= 0:
        sample_rate = _get_audio_sample_rate(paths[0])
    logger.debug("Using sample rate: %d Hz", sample_rate)

    # Detect channels from first file
    code, ch_out, _ = _run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "default=noprint_wrappers=1:nokey=1",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=channels",
            str(paths[0]),
        ],
        timeout=15,
    )
    channels = int(ch_out.strip()) if code == 0 and ch_out.strip().isdigit() else 2

    needs_silence = silence_between > 0 or silence_start > 0

    if not needs_silence:
        # Simple concat — no silence, use concat demuxer directly
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            for p in paths:
                f.write(f"file '{p}'\n")
            concat_list = f.name

        cmd = [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            concat_list,
            "-c",
            "copy",
            str(out),
        ]
        code, _, stderr = _run(cmd, timeout=300)
        Path(concat_list).unlink(missing_ok=True)

        if code != 0:
            return f"Error: ffmpeg concat failed.\n{stderr[:500]}"
    else:
        # Generate silence segments and interleave
        # Build a concat list with silence files inserted
        silence_files: list[Path] = []
        tmpdir = Path(tempfile.mkdtemp(prefix="audio_concat_"))

        try:
            # Channel layout for anullsrc
            ch_layout = "stereo" if channels >= 2 else "mono"

            def _gen_silence(duration: float, label: str) -> Path:
                sil_path = tmpdir / f"silence_{label}.wav"
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    f"anullsrc=r={sample_rate}:cl={ch_layout}",
                    "-t",
                    str(duration),
                    str(sil_path),
                ]
                c, _, se = _run(cmd, timeout=30)
                if c != 0:
                    logger.debug("Failed to generate silence '%s': %s", label, se[:200])
                silence_files.append(sil_path)
                return sil_path

            # Build concat list
            with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, dir=str(tmpdir)) as f:
                if silence_start > 0:
                    sil = _gen_silence(silence_start, "start")
                    f.write(f"file '{sil}'\n")

                for i, p in enumerate(paths):
                    f.write(f"file '{p}'\n")
                    if silence_between > 0 and i < len(paths) - 1:
                        sil = _gen_silence(silence_between, f"gap_{i}")
                        f.write(f"file '{sil}'\n")

                concat_list = f.name

            # Concat with re-encode (needed because silence segments are wav
            # and inputs may differ in codec details)
            out_ext = out.suffix.lower()
            cmd = [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                concat_list,
            ]

            # Choose codec based on output format
            if out_ext in (".wav",):
                cmd += ["-c:a", "pcm_s16le"]
            elif out_ext in (".flac",):
                cmd += ["-c:a", "flac"]
            elif out_ext in (".mp3",):
                cmd += ["-c:a", "libmp3lame", "-b:a", "320k"]
            elif out_ext in (".ogg",):
                cmd += ["-c:a", "libvorbis", "-q:a", "6"]
            elif out_ext in (".m4a", ".aac"):
                cmd += ["-c:a", "aac", "-b:a", "256k"]
            else:
                # Default: copy if possible, else pcm
                cmd += ["-c:a", "pcm_s16le"]

            cmd.append(str(out))
            code, _, stderr = _run(cmd, timeout=300)

        finally:
            # Clean up temp silence files
            for sf in silence_files:
                sf.unlink(missing_ok=True)
            for leftover in tmpdir.glob("*"):
                leftover.unlink(missing_ok=True)
            tmpdir.rmdir()

        if code != 0:
            return f"Error: ffmpeg concat with silence failed.\n{stderr[:500]}"

    if not out.exists():
        return "Error: Output file was not created."

    # Build result
    result_parts = [f"Concatenated {len(paths)} files → {out.name} ({_human_size(out.stat().st_size)})"]

    if silence_start > 0:
        result_parts.append(f"  Silence at start: {silence_start}s")
    if silence_between > 0:
        result_parts.append(f"  Silence between files: {silence_between}s")
    result_parts.append(f"  Sample rate: {sample_rate} Hz, Channels: {channels}")
    result_parts.append("\nInput files:")
    for p in paths:
        result_parts.append(f"  {p.name} ({_human_size(p.stat().st_size)})")

    return "\n".join(result_parts)


# ---------------------------------------------------------------------------
# Bulk registration
# ---------------------------------------------------------------------------


def register_audio_tools(registry: ToolRegistry) -> int:
    """Register all audio tools with the given registry.

    Returns the number of tools registered.
    """
    registry.register_category_hint(
        "Audio",
        "Audio tools for working with audio files and DAW projects. "
        "ardour_extract_ranges parses range markers from Ardour project files "
        "and extracts each range as a separate audio file using ffmpeg. "
        "audio_concat joins multiple audio files with optional silence gaps. "
        "Requires ffmpeg (both tools) and xidel (Ardour extraction).",
    )

    tools = [
        ardour_extract_ranges,
        audio_concat,
    ]
    for func in tools:
        registry.register(func, category="Audio")
    return len(tools)
