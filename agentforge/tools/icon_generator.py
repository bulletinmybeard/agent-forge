"""Icon generator — produce all favicon/app icon variants from a source image.

Given a high-resolution source image (ideally 1024×1024 or larger), generates
a complete set of favicons, Apple touch icons, Android/PWA icons, Windows
tiles, and an HTML snippet + web app manifest.

Uses ImageMagick (``magick`` or ``convert``) for all raster operations.

Usage::

    from agentforge.tools import ToolRegistry
    from agentforge.tools.icon_generator import register_icon_generator_tools

    registry = ToolRegistry()
    register_icon_generator_tools(registry)
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from chalkbox.logging.bridge import get_logger

from .registry import tool

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Icon definitions — preset → list of (subdir, filename, width, height)
# ---------------------------------------------------------------------------

_MINIMAL_ICONS: list[tuple[str, str, int, int]] = [
    ("favicon", "favicon-16x16.png", 16, 16),
    ("favicon", "favicon-32x32.png", 32, 32),
    ("apple", "apple-touch-icon.png", 180, 180),
    ("android", "android-chrome-192x192.png", 192, 192),
    ("android", "android-chrome-512x512.png", 512, 512),
    ("android", "maskable-icon-512x512.png", 512, 512),
]

_FULL_ICONS: list[tuple[str, str, int, int]] = [
    # Favicon
    ("favicon", "favicon-16x16.png", 16, 16),
    ("favicon", "favicon-32x32.png", 32, 32),
    ("favicon", "favicon-48x48.png", 48, 48),
    # Apple
    ("apple", "apple-touch-icon.png", 180, 180),
    ("apple", "apple-touch-icon-152x152.png", 152, 152),
    ("apple", "apple-touch-icon-167x167.png", 167, 167),
    ("apple", "apple-touch-icon-180x180.png", 180, 180),
    # Android / PWA
    ("android", "android-chrome-192x192.png", 192, 192),
    ("android", "android-chrome-512x512.png", 512, 512),
    ("android", "maskable-icon-512x512.png", 512, 512),
    ("android", "icon-384x384.png", 384, 384),
    ("android", "icon-1024x1024.png", 1024, 1024),
    # Windows tiles
    ("windows", "mstile-70x70.png", 70, 70),
    ("windows", "mstile-150x150.png", 150, 150),
    ("windows", "mstile-310x150.png", 310, 150),
    ("windows", "mstile-310x310.png", 310, 310),
    ("windows", "mstile-144x144.png", 144, 144),
    # Generic utility sizes
    ("generic", "icon-64x64.png", 64, 64),
    ("generic", "icon-96x96.png", 96, 96),
    ("generic", "icon-128x128.png", 128, 128),
    ("generic", "icon-192x192.png", 192, 192),
    ("generic", "icon-256x256.png", 256, 256),
    ("generic", "icon-512x512.png", 512, 512),
]

# Sizes to embed in the multi-resolution favicon.ico
_ICO_SIZES = [16, 32, 48]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(cmd: list[str], timeout: int = 120) -> tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", f"Error: command timed out ({timeout}s limit)"
    except Exception as exc:
        return -1, "", f"Error: {exc}"


def _magick_bin() -> str:
    """Return the ImageMagick binary name — 'magick' (v7) or 'convert' (v6)."""
    for name in ("magick", "convert"):
        code, _, _ = _run(["which", name], timeout=5)
        if code == 0:
            return name
    return "magick"  # fallback — will fail with a clear error


def _human_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def _resize(magick: str, src: str, dest: str, w: int, h: int) -> tuple[bool, str]:
    """Resize source image to w×h and save as dest. Returns (ok, message)."""
    cmd = [magick, src, "-resize", f"{w}x{h}!", "-strip", dest]
    code, _, stderr = _run(cmd)
    if code != 0:
        return False, f"Failed: {Path(dest).name} — {stderr[:200]}"
    return True, ""


def _generate_ico(magick: str, src: str, dest: str, sizes: list[int]) -> tuple[bool, str]:
    """Generate a multi-resolution .ico file."""
    # ImageMagick can create ICO files with multiple embedded sizes
    cmd = [magick, src]
    for s in sizes:
        cmd += ["(", "-clone", "0", "-resize", f"{s}x{s}", ")"]
    cmd += ["-delete", "0", "-colors", "256", dest]
    code, _, stderr = _run(cmd)
    if code != 0:
        return False, f"Failed: favicon.ico — {stderr[:200]}"
    return True, ""


def _generate_html_snippet(preset: str, icons: list[tuple[str, str, int, int]]) -> str:
    """Generate the HTML <link> tags for the generated icons."""
    lines = ["<!-- Favicons — generated by AgentForge icon generator -->"]

    # favicon.ico
    lines.append('<link rel="icon" href="/favicon.ico" sizes="16x16 32x32 48x48">')

    # PNG favicons
    for subdir, name, w, h in icons:
        if subdir == "favicon" and name.endswith(".png"):
            lines.append(f'<link rel="icon" type="image/png" sizes="{w}x{h}" href="/favicon/{name}">')

    # Apple touch icons
    apple_icons = [(s, n, w, h) for s, n, w, h in icons if s == "apple"]
    if apple_icons:
        # Main one (180x180) — no sizes attr for compatibility
        lines.append('<link rel="apple-touch-icon" href="/apple/apple-touch-icon.png">')
        for subdir, name, w, h in apple_icons:
            if name != "apple-touch-icon.png":
                lines.append(f'<link rel="apple-touch-icon" sizes="{w}x{h}" href="/apple/{name}">')

    # Windows tile
    lines.append('<meta name="msapplication-TileImage" content="/windows/mstile-144x144.png">')
    lines.append('<meta name="msapplication-TileColor" content="#ffffff">')

    # Web manifest reference
    lines.append('<link rel="manifest" href="/site.webmanifest">')

    return "\n".join(lines)


def _generate_webmanifest(icons: list[tuple[str, str, int, int]]) -> dict:
    """Generate a site.webmanifest JSON dict."""
    manifest_icons = []

    # Android/PWA icons
    for subdir, name, w, h in icons:
        if subdir == "android":
            entry = {
                "src": f"/android/{name}",
                "sizes": f"{w}x{h}",
                "type": "image/png",
            }
            if "maskable" in name:
                entry["purpose"] = "maskable"
            else:
                entry["purpose"] = "any"
            manifest_icons.append(entry)

    return {
        "name": "",
        "short_name": "",
        "icons": manifest_icons,
        "theme_color": "#ffffff",
        "background_color": "#ffffff",
        "display": "standalone",
    }


# ---------------------------------------------------------------------------
# Tool function
# ---------------------------------------------------------------------------


@tool(
    hint=(
        "Use generate_icons to create all favicon, Apple touch icon, Android/PWA, "
        "and Windows tile variants from a single source image. Generates a complete "
        "icon set including favicon.ico, HTML snippet, and site.webmanifest. "
        "Source image should be at least 512×512 (ideally 1024×1024). "
        "Presets: 'minimal' (7 essential icons) or 'full' (25+ icons for all platforms). "
        "Output is organized into subfolders: favicon/, apple/, android/, windows/, generic/."
    ),
)
def generate_icons(
    source_image: str,
    output_dir: str,
    preset: str = "minimal",
    archive: bool = False,
) -> str:
    """Generate all favicon and app icon variants from a source image.

    source_image: path to the source PNG/JPEG/WebP image (512×512 minimum, 1024×1024 recommended)
    output_dir: directory where icon subfolders will be created
    preset: 'minimal' (7 essential icons) or 'full' (25+ icons for all platforms)
    archive: if true, also creates a .zip archive of the output for easy sharing
    """
    src = Path(source_image).expanduser().resolve()
    out = Path(output_dir).expanduser().resolve()

    # Validate source
    if not src.exists():
        return f"Error: Source image does not exist: {src}"
    if not src.is_file():
        return f"Error: Not a file: {src}"
    if src.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"}:
        return f"Error: Unsupported source format '{src.suffix}'. Use PNG, JPEG, or WebP."

    # Check ImageMagick
    magick = _magick_bin()

    # Get source dimensions
    code, stdout, _ = _run([magick, "identify", "-format", "%wx%h", str(src)])
    if code != 0:
        return "Error: Could not read source image dimensions. Is ImageMagick installed?"

    try:
        src_w, src_h = map(int, stdout.split("x"))
    except (ValueError, IndexError):
        return f"Error: Could not parse image dimensions from: {stdout}"

    if src_w < 512 or src_h < 512:
        return (
            f"Error: Source image is {src_w}×{src_h} — minimum 512×512 required. "
            f"Recommended: 1024×1024 or larger for best quality."
        )

    # Select preset
    if preset == "full":
        icons = _FULL_ICONS
    else:
        icons = _MINIMAL_ICONS
        preset = "minimal"  # normalize

    # Create output directory structure
    out.mkdir(parents=True, exist_ok=True)
    subdirs = sorted({subdir for subdir, _, _, _ in icons})
    for subdir in subdirs:
        (out / subdir).mkdir(parents=True, exist_ok=True)

    # Generate all icon sizes
    generated = []
    errors = []
    seen: set[str] = set()  # avoid duplicate sizes

    for subdir, name, w, h in icons:
        key = f"{subdir}/{name}"
        if key in seen:
            continue
        seen.add(key)

        dest = out / subdir / name
        ok, err = _resize(magick, str(src), str(dest), w, h)
        if ok:
            generated.append((subdir, name, w, h, dest.stat().st_size))
        else:
            errors.append(err)

    # Generate favicon.ico (multi-resolution)
    ico_path = out / "favicon.ico"
    ok, err = _generate_ico(magick, str(src), str(ico_path), _ICO_SIZES)
    if ok:
        generated.append((".", "favicon.ico", 48, 48, ico_path.stat().st_size))
    else:
        errors.append(err)

    # Generate HTML snippet
    html_snippet = _generate_html_snippet(preset, icons)
    html_path = out / "icon-tags.html"
    html_path.write_text(html_snippet, encoding="utf-8")

    # Generate site.webmanifest
    manifest = _generate_webmanifest(icons)
    manifest_path = out / "site.webmanifest"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # Create archive if requested
    archive_path = None
    if archive:
        archive_base = out / "icons"
        archive_path = shutil.make_archive(str(archive_base), "zip", str(out))

    # Build summary
    total_size = sum(s for _, _, _, _, s in generated)
    lines = [
        f"Generated **{len(generated)}** icons ({preset} preset) from {src.name} ({src_w}×{src_h})",
        f"Output: {out}",
        f"Total size: {_human_size(total_size)}",
        "",
    ]

    # Group by subdirectory
    by_dir: dict[str, list[tuple[str, int, int, int]]] = {}
    for subdir, name, w, h, size in generated:
        by_dir.setdefault(subdir, []).append((name, w, h, size))

    for subdir in sorted(by_dir):
        items = by_dir[subdir]
        dir_label = subdir if subdir != "." else "(root)"
        lines.append(f"**{dir_label}/**")
        for name, w, h, size in items:
            lines.append(f"  {name} — {w}×{h} ({_human_size(size)})")
        lines.append("")

    lines.append("**Generated files:**")
    lines.append("  icon-tags.html — HTML <link> tags to paste into your <head>")
    lines.append("  site.webmanifest — PWA manifest with icon references")

    if archive_path:
        lines.append(f"  icons.zip — downloadable archive ({_human_size(Path(archive_path).stat().st_size)})")

    if errors:
        lines.append("")
        lines.append(f"**Errors ({len(errors)}):**")
        for e in errors:
            lines.append(f"  {e}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_icon_generator_tools(registry: ToolRegistry) -> int:
    """Register the icon generator tool with the given registry."""
    registry.register(generate_icons, category="Media")
    return 1
