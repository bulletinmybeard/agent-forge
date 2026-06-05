"""Media tools — video conversion (ffmpeg) and image manipulation (ImageMagick).

Provides tools for common media operations: video format conversion, image
conversion, resizing, optimization, and metadata extraction.

Both ffmpeg and ImageMagick must be installed on the system.

Usage::

    from agentforge.tools import ToolRegistry
    from agentforge.tools.media_tools import register_media_tools

    registry = ToolRegistry()
    register_media_tools(registry)
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from chalkbox.logging.bridge import get_logger

from .registry import tool

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _cfg(key: str, default):
    """Read a tools.media.* config value with fallback to *default*."""
    try:
        from agentforge.config import get_config

        cfg = get_config()
        return cfg.get(f"tools.media.{key}", default)
    except Exception:
        return default


def _video_max_size_mb() -> int:
    return int(_cfg("video.max_file_size_mb", 4096))


def _video_max_duration_s() -> int:
    return int(_cfg("video.max_duration_s", 7200))


def _video_timeout_multiplier() -> int:
    return int(_cfg("video.timeout_multiplier", 2))


def _video_default_gif_fps() -> int:
    return int(_cfg("video.default_gif_fps", 12))


def _video_default_gif_width() -> int:
    return int(_cfg("video.default_gif_width", 480))


def _image_max_size_mb() -> int:
    return int(_cfg("image.max_file_size_mb", 512))


def _image_optimize_quality() -> int:
    return int(_cfg("image.optimize_quality", 82))


def _image_strip_metadata() -> bool:
    return bool(_cfg("image.strip_metadata", True))


# Supported video formats (input and output)
_VIDEO_FORMATS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".webm",
    ".flv",
    ".wmv",
    ".m4v",
    ".ts",
    ".gif",
    ".mpg",
    ".mpeg",
    ".3gp",
    ".ogv",
}

# Supported image formats (input and output)
_IMAGE_FORMATS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".tiff",
    ".tif",
    ".svg",
    ".ico",
    ".heic",
    ".heif",
    ".avif",
    ".ppm",
    ".pgm",
}


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


def _file_size_mb(path: Path) -> float:
    """Return file size in megabytes."""
    return path.stat().st_size / (1024 * 1024)


def _human_size(size_bytes: int) -> str:
    """Format byte count as human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def _get_video_duration(path: Path) -> float | None:
    """Get video duration in seconds using ffprobe, or None on failure."""
    code, stdout, _ = _run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            str(path),
        ],
        timeout=30,
    )
    if code != 0:
        return None
    try:
        info = json.loads(stdout)
        return float(info["format"]["duration"])
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def _validate_input(path: Path, max_size_mb: float, allowed_exts: set[str] | None = None) -> str | None:
    """Validate an input file. Returns error string or None if OK."""
    if not path.exists():
        return f"Error: File does not exist: {path}"
    if not path.is_file():
        return f"Error: Not a file: {path}"
    size_mb = _file_size_mb(path)
    if size_mb > max_size_mb:
        return f"Error: File too large ({size_mb:.1f} MB). Maximum: {max_size_mb} MB"
    if allowed_exts and path.suffix.lower() not in allowed_exts:
        return f"Error: Unsupported format '{path.suffix}'. Supported: {', '.join(sorted(allowed_exts))}"
    return None


# ---------------------------------------------------------------------------
# video_convert (ffmpeg)
# ---------------------------------------------------------------------------


@tool(
    hint=(
        "Use video_convert to convert video files between formats, create GIFs, "
        "extract clips, change codecs, or compress videos. Powered by ffmpeg. "
        "The output format is auto-detected from the output path extension. "
        "Use trim_start/trim_end for extracting clips. "
        "Set quality to 'low' for maximum compression, 'high' for best quality."
    ),
)
def video_convert(
    input_path: str,
    output_path: str,
    trim_start: str = "",
    trim_end: str = "",
    quality: str = "medium",
    scale: str = "",
    fps: int = 0,
    audio: bool = True,
) -> str:
    """Convert a video file to another format, trim clips, or create GIFs.

    Supports all common video formats: MP4, MKV, AVI, MOV, WebM, GIF, etc.
    Output format is determined by the output file extension.

    input_path: source video file path
    output_path: destination file path (extension determines format)
    trim_start: start time for trimming (e.g., '00:01:30' or '90')
    trim_end: end time for trimming (e.g., '00:02:00' or '120')
    quality: quality preset — 'low' (small file), 'medium' (balanced), 'high' (best quality)
    scale: resize — width:height (e.g., '1280:720', '640:-1' for auto-height, '-1:480')
    fps: output frame rate (0 = keep original; useful for GIFs, e.g., 10 or 15)
    audio: include audio track (set false for GIFs or silent videos)
    """
    inp = Path(input_path).expanduser().resolve()
    out = Path(output_path).expanduser().resolve()

    # Validate input
    err = _validate_input(inp, _video_max_size_mb(), _VIDEO_FORMATS)
    if err:
        return err

    # Check ffmpeg is available
    code, _, _ = _run(["ffmpeg", "-version"], timeout=10)
    if code != 0:
        return "Error: ffmpeg is not installed or not in PATH."

    # Check duration limit
    duration = _get_video_duration(inp)
    if duration and duration > _video_max_duration_s():
        return (
            f"Error: Video is {duration / 60:.0f} minutes long. "
            f"Maximum: {_video_max_duration_s() / 60:.0f} minutes. "
            "Use trim_start/trim_end to extract a shorter clip."
        )

    # Validate output format
    out_ext = out.suffix.lower()
    if out_ext not in _VIDEO_FORMATS:
        return f"Error: Unsupported output format '{out_ext}'. Supported: {', '.join(sorted(_VIDEO_FORMATS))}"

    # Ensure output directory exists
    out.parent.mkdir(parents=True, exist_ok=True)

    # Build ffmpeg command
    cmd = ["ffmpeg", "-y", "-i", str(inp)]

    # Trim
    if trim_start:
        cmd += ["-ss", trim_start]
    if trim_end:
        cmd += ["-to", trim_end]

    # GIF output — special pipeline
    is_gif = out_ext == ".gif"

    if is_gif:
        # GIF: use palettegen for high quality
        filter_parts = []
        if fps > 0:
            filter_parts.append(f"fps={fps}")
        elif not fps:
            filter_parts.append(f"fps={_video_default_gif_fps()}")
        if scale:
            filter_parts.append(f"scale={scale}:flags=lanczos")
        elif not scale:
            filter_parts.append(f"scale={_video_default_gif_width()}:-1:flags=lanczos")
        filter_str = ",".join(filter_parts)
        cmd += ["-vf", f"{filter_str},split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"]
        cmd += ["-loop", "0"]
    else:
        # Regular video conversion
        vf_parts = []
        if scale:
            vf_parts.append(f"scale={scale}")
        if fps > 0:
            vf_parts.append(f"fps={fps}")
        if vf_parts:
            cmd += ["-vf", ",".join(vf_parts)]

        # Quality presets (CRF-based for H.264/H.265)
        crf_map = {"low": "32", "medium": "23", "high": "18"}
        crf = crf_map.get(quality, "23")

        if out_ext in (".mp4", ".m4v", ".mkv"):
            cmd += ["-c:v", "libx264", "-crf", crf, "-preset", "medium"]
        elif out_ext == ".webm":
            cmd += ["-c:v", "libvpx-vp9", "-crf", crf, "-b:v", "0"]

        # Audio handling
        if not audio:
            cmd += ["-an"]
        elif out_ext in (".mp4", ".m4v", ".mkv"):
            cmd += ["-c:a", "aac", "-b:a", "128k"]

    cmd.append(str(out))

    logger.info("ffmpeg: %s → %s", inp.name, out.name)

    # Estimate timeout from duration
    est_duration = duration or 60
    timeout = max(120, int(est_duration * _video_timeout_multiplier()))

    code, stdout, stderr = _run(cmd, timeout=timeout)

    if code != 0:
        # Extract useful error from ffmpeg stderr
        err_lines = [l for l in stderr.splitlines() if l and not l.startswith("  ")]
        err_msg = "\n".join(err_lines[-5:]) if err_lines else stderr[:500]
        return f"Error: ffmpeg conversion failed.\n{err_msg}"

    if not out.exists():
        return "Error: Output file was not created. Check input format compatibility."

    out_size = _human_size(out.stat().st_size)
    in_size = _human_size(inp.stat().st_size)

    result = f"Converted: {inp.name} ({in_size}) → {out.name} ({out_size})"

    # Report duration if applicable
    out_duration = _get_video_duration(out)
    if out_duration:
        mins, secs = divmod(int(out_duration), 60)
        result += f"\nDuration: {mins}m {secs}s"

    return result


# ---------------------------------------------------------------------------
# image_convert (ImageMagick)
# ---------------------------------------------------------------------------


@tool(
    hint=(
        "Use image_convert to convert images between formats. "
        "Powered by ImageMagick. Output format is auto-detected from the extension. "
        "Supports: JPG, PNG, GIF, WebP, BMP, TIFF, SVG, ICO, HEIC, AVIF."
    ),
)
def image_convert(input_path: str, output_path: str, quality: int = 0) -> str:
    """Convert an image file to another format.

    Output format is determined by the output file extension.

    input_path: source image file path
    output_path: destination file path (extension determines format)
    quality: output quality 1-100 (0 = format default; relevant for JPEG/WebP)
    """
    inp = Path(input_path).expanduser().resolve()
    out = Path(output_path).expanduser().resolve()

    err = _validate_input(inp, _image_max_size_mb(), _IMAGE_FORMATS)
    if err:
        return err

    out_ext = out.suffix.lower()
    if out_ext not in _IMAGE_FORMATS:
        return f"Error: Unsupported output format '{out_ext}'. Supported: {', '.join(sorted(_IMAGE_FORMATS))}"

    out.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["magick", str(inp)]
    if 0 < quality <= 100:
        cmd += ["-quality", str(quality)]
    cmd.append(str(out))

    logger.info("magick convert: %s → %s", inp.name, out.name)
    code, stdout, stderr = _run(cmd, timeout=120)

    if code != 0:
        return f"Error: ImageMagick conversion failed.\n{stderr[:500]}"

    if not out.exists():
        return "Error: Output file was not created."

    return f"Converted: {inp.name} ({_human_size(inp.stat().st_size)}) → {out.name} ({_human_size(out.stat().st_size)})"


# ---------------------------------------------------------------------------
# image_resize (ImageMagick)
# ---------------------------------------------------------------------------


@tool(
    hint=(
        "Use image_resize to resize images. Specify dimensions as WxH (e.g., '800x600'), "
        "width only ('800x' — auto height), height only ('x600'), or percentage ('50%'). "
        "By default writes in-place; set output_path to create a new file."
    ),
)
def image_resize(
    input_path: str,
    dimensions: str,
    output_path: str = "",
    quality: int = 0,
    keep_aspect: bool = True,
) -> str:
    """Resize an image to specified dimensions.

    input_path: source image file path
    dimensions: target size — 'WxH' (e.g., '800x600'), 'Wx' (auto height), 'xH' (auto width), or 'N%'
    output_path: destination path (default: overwrite input file)
    quality: output quality 1-100 (0 = format default)
    keep_aspect: maintain aspect ratio (default true; set false to force exact dimensions)
    """
    inp = Path(input_path).expanduser().resolve()
    out = Path(output_path).expanduser().resolve() if output_path else inp

    err = _validate_input(inp, _image_max_size_mb(), _IMAGE_FORMATS)
    if err:
        return err

    out.parent.mkdir(parents=True, exist_ok=True)

    # Build resize geometry
    geometry = dimensions
    if not keep_aspect and "!" not in geometry:
        geometry += "!"

    cmd = ["magick", str(inp), "-resize", geometry]
    if 0 < quality <= 100:
        cmd += ["-quality", str(quality)]
    cmd.append(str(out))

    logger.info("magick resize: %s → %s (%s)", inp.name, out.name, dimensions)
    code, stdout, stderr = _run(cmd, timeout=120)

    if code != 0:
        return f"Error: ImageMagick resize failed.\n{stderr[:500]}"

    if not out.exists():
        return "Error: Output file was not created."

    # Get output dimensions
    dim_code, dim_out, _ = _run(["magick", "identify", "-format", "%wx%h", str(out)], timeout=15)
    dim_str = dim_out if dim_code == 0 else "unknown"

    action = "Resized" if str(out) == str(inp) else "Resized and saved"
    return f"{action}: {out.name} → {dim_str} ({_human_size(out.stat().st_size)})"


# ---------------------------------------------------------------------------
# image_metadata (ImageMagick + identify)
# ---------------------------------------------------------------------------


@tool(
    hint=(
        "Use image_metadata to extract metadata and properties from image files. "
        "Returns dimensions, format, color space, file size, and EXIF data "
        "(camera, lens, GPS, date, etc.)."
    ),
)
def image_metadata(input_path: str, exif: bool = True) -> str:
    """Extract metadata and properties from an image file.

    Returns format, dimensions, color depth, file size, and optionally EXIF
    data (camera model, lens, GPS coordinates, date taken, etc.).

    input_path: image file path
    exif: include EXIF metadata if available (default true)
    """
    inp = Path(input_path).expanduser().resolve()

    err = _validate_input(inp, _image_max_size_mb())
    if err:
        return err

    # Basic image info via identify
    code, stdout, stderr = _run(
        [
            "magick",
            "identify",
            "-verbose",
            str(inp),
        ],
        timeout=30,
    )

    if code != 0:
        return f"Error: Could not read image metadata.\n{stderr[:500]}"

    # Parse the verbose output for key fields
    lines = stdout.splitlines()
    info: dict[str, str] = {}
    exif_data: dict[str, str] = {}
    in_exif = False

    for line in lines:
        stripped = line.strip()

        # Top-level properties
        if stripped.startswith("Format:"):
            info["Format"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Geometry:"):
            info["Dimensions"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Colorspace:"):
            info["Color Space"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Depth:"):
            info["Color Depth"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Channel depth:"):
            info["Channel Depth"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Filesize:"):
            info["File Size"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Units:"):
            info["Units"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Resolution:") or stripped.startswith("Print size:"):
            info[stripped.split(":")[0]] = stripped.split(":", 1)[1].strip()

        # EXIF section
        if exif:
            if "exif:" in stripped.lower():
                in_exif = True
            if in_exif and ":" in stripped:
                key, _, val = stripped.partition(":")
                key = key.strip().replace("exif:", "").replace("exif-", "")
                val = val.strip()
                if val and key and len(val) < 200:
                    exif_data[key] = val

    # Build output
    result_parts = [f"Image: {inp.name}"]
    result_parts.append(f"Path: {inp}")
    result_parts.append(f"Size: {_human_size(inp.stat().st_size)}")

    for k, v in info.items():
        result_parts.append(f"{k}: {v}")

    if exif and exif_data:
        result_parts.append("\nEXIF Data:")
        # Prioritize interesting fields
        priority = [
            "Make",
            "Model",
            "LensModel",
            "LensMake",
            "ExposureTime",
            "FNumber",
            "ISOSpeedRatings",
            "FocalLength",
            "DateTimeOriginal",
            "DateTime",
            "GPSLatitude",
            "GPSLongitude",
            "Software",
            "ImageWidth",
            "ImageLength",
        ]
        seen = set()
        for key in priority:
            if key in exif_data:
                result_parts.append(f"  {key}: {exif_data[key]}")
                seen.add(key)
        # Remaining fields
        for key, val in sorted(exif_data.items()):
            if key not in seen:
                result_parts.append(f"  {key}: {val}")

    return "\n".join(result_parts)


# ---------------------------------------------------------------------------
# image_optimize (ImageMagick)
# ---------------------------------------------------------------------------


@tool(
    hint=(
        "Use image_optimize to reduce image file size while maintaining visual quality. "
        "Good for preparing images for web, email, or storage savings. "
        "Supports stripping metadata, resampling, and format-specific optimizations."
    ),
)
def image_optimize(
    input_path: str,
    output_path: str = "",
    quality: int = 82,
    strip_metadata: bool = True,
    max_dimension: int = 0,
) -> str:
    """Optimize an image for smaller file size.

    Applies format-specific compression, optional metadata stripping, and
    optional downscaling. Good for web-ready images.

    input_path: source image file path
    output_path: destination path (default: overwrite input file)
    quality: compression quality 1-100 (default 82 — good balance)
    strip_metadata: remove EXIF and other metadata (default true)
    max_dimension: max width or height in pixels (0 = no resize; e.g., 1920 for web)
    """
    inp = Path(input_path).expanduser().resolve()
    out = Path(output_path).expanduser().resolve() if output_path else inp

    err = _validate_input(inp, _image_max_size_mb(), _IMAGE_FORMATS)
    if err:
        return err

    out.parent.mkdir(parents=True, exist_ok=True)
    original_size = inp.stat().st_size

    cmd = ["magick", str(inp)]

    # Resize to max dimension (preserving aspect ratio)
    if max_dimension > 0:
        cmd += ["-resize", f"{max_dimension}x{max_dimension}>"]

    # Strip metadata
    if strip_metadata:
        cmd.append("-strip")

    # Quality
    cmd += ["-quality", str(quality)]

    # Format-specific optimizations
    ext = (out.suffix or inp.suffix).lower()
    if ext in (".jpg", ".jpeg"):
        cmd += ["-sampling-factor", "4:2:0", "-interlace", "JPEG"]
    elif ext == ".png":
        # PNG: Use adaptive filtering for better compression
        cmd += [
            "-define",
            "png:compression-filter=5",
            "-define",
            "png:compression-level=9",
            "-define",
            "png:compression-strategy=1",
        ]
    elif ext == ".webp":
        cmd += ["-define", "webp:method=6"]  # best compression

    cmd.append(str(out))

    logger.info("magick optimize: %s (quality=%d, strip=%s)", inp.name, quality, strip_metadata)
    code, stdout, stderr = _run(cmd, timeout=120)

    if code != 0:
        return f"Error: ImageMagick optimization failed.\n{stderr[:500]}"

    if not out.exists():
        return "Error: Output file was not created."

    new_size = out.stat().st_size
    savings = original_size - new_size
    pct = (savings / original_size * 100) if original_size > 0 else 0

    if savings > 0:
        savings_str = f"Saved {_human_size(savings)} ({pct:.1f}% reduction)"
    else:
        savings_str = f"Size increased by {_human_size(-savings)} (already optimized?)"

    result = (
        f"Optimized: {out.name}\n"
        f"  Before: {_human_size(original_size)}\n"
        f"  After:  {_human_size(new_size)}\n"
        f"  {savings_str}"
    )

    if max_dimension > 0:
        dim_code, dim_out, _ = _run(["magick", "identify", "-format", "%wx%h", str(out)], timeout=15)
        if dim_code == 0:
            result += f"\n  Dimensions: {dim_out}"

    return result


# ---------------------------------------------------------------------------
# Bulk registration
# ---------------------------------------------------------------------------


def register_media_tools(registry: ToolRegistry) -> int:
    """Register all media tools with the given registry.

    Returns the number of tools registered.
    """
    registry.register_category_hint(
        "Media",
        "Media tools convert and manipulate video and image files. "
        "video_convert uses ffmpeg for video format conversion, trimming, GIF creation, "
        "and compression. image_convert, image_resize, image_optimize, and image_metadata "
        "use ImageMagick for image format conversion, resizing, optimization, and "
        "metadata extraction. Both ffmpeg and ImageMagick must be installed. "
        "Maximum file sizes: 4 GB for video, 512 MB for images. "
        "Maximum video duration: 2 hours (use trim_start/trim_end for longer videos).",
    )

    tools = [
        video_convert,
        image_convert,
        image_resize,
        image_metadata,
        image_optimize,
    ]
    for func in tools:
        registry.register(func, category="Media")
    return len(tools)
