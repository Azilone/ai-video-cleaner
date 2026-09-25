#!/usr/bin/env python3
"""Re-encode a still image as WebP without embedded metadata."""

import argparse
import io
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile

try:
    from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError
except ImportError:
    print("ai-image-cleaner: install Pillow with: python3 -m pip install -r requirements-image.txt",
          file=sys.stderr)
    sys.exit(1)


class CleanerError(Exception):
    pass


AI_MARKER_PATTERN = re.compile(
    r"seedance|seedream|dreamina|bytedance|capcut|c2pa|content.credentials|"
    r"ai[ -_]?generat|\baigc\b|trainedAlgorithmicMedia|\bworkflow\b|\bprompt\b",
    re.IGNORECASE,
)


def run(command):
    try:
        return subprocess.run(command, capture_output=True, text=True, errors="replace")
    except OSError as exc:
        raise CleanerError(f"unable to run {command[0]}: {exc}") from exc


def webp_chunks(path):
    """List RIFF chunks and reject malformed WebP boundaries."""
    data = path.read_bytes()
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise CleanerError("output is not a WebP file")
    if struct.unpack_from("<I", data, 4)[0] + 8 != len(data):
        raise CleanerError("invalid WebP RIFF length")
    chunks = []
    offset = 12
    while offset < len(data):
        if offset + 8 > len(data):
            raise CleanerError("truncated WebP chunk")
        name = data[offset:offset + 4].decode("latin1")
        size = struct.unpack_from("<I", data, offset + 4)[0]
        offset += 8 + size + (size & 1)
        if offset > len(data):
            raise CleanerError("invalid WebP chunk length")
        chunks.append(name)
    return chunks


def exif_metadata(path):
    result = run(["exiftool", "-a", "-G1", "-s", "-json", str(path)])
    if result.returncode:
        raise CleanerError(result.stderr.strip() or "exiftool failed")
    try:
        tags = json.loads(result.stdout)[0]
    except (ValueError, IndexError, TypeError) as exc:
        raise CleanerError(f"invalid exiftool response: {exc}") from exc
    structural = ("System:", "File:", "ExifTool:", "RIFF:", "WebP:", "Composite:")
    metadata = {key: value for key, value in tags.items() if key != "SourceFile" and
                not key.startswith(structural)}
    markers = sorted(key for key, value in metadata.items() if
                     AI_MARKER_PATTERN.search(f"{key} {value}"))
    return sorted(metadata), markers


def c2pa_status(path):
    result = run(["c2patool", str(path)])
    detail = (result.stdout + result.stderr).strip()
    if "No claim found" in detail:
        return "absent"
    if result.returncode == 0:
        return "present"
    return "error"


def audit(path):
    try:
        with Image.open(path) as image:
            info = {"path": str(path), "format": image.format, "width": image.width,
                    "height": image.height, "frames": getattr(image, "n_frames", 1)}
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise CleanerError(f"cannot read image: {exc}") from exc
    info["metadata_tags"], info["ai_markers"] = exif_metadata(path)
    info["c2pa"] = c2pa_status(path)
    if info["format"] == "WEBP":
        info["webp_chunks"] = webp_chunks(path)
    return info


def convert_to_srgb(image):
    profile = image.info.get("icc_profile")
    if not profile:
        return image.convert("RGBA" if "A" in image.getbands() or
                             "transparency" in image.info else "RGB")
    try:
        alpha = image.convert("RGBA").getchannel("A") if ("A" in image.getbands() or
                 "transparency" in image.info) else None
        converted = ImageCms.profileToProfile(
            image.convert("RGB"), ImageCms.ImageCmsProfile(io.BytesIO(profile)),
            ImageCms.createProfile("sRGB"), outputMode="RGB")
        if alpha is not None:
            converted.putalpha(alpha)
        return converted
    except (OSError, ValueError, ImageCms.PyCMSError) as exc:
        raise CleanerError(f"cannot convert embedded color profile to sRGB: {exc}") from exc


def default_output(source):
    base = source.with_name(source.stem + "-clean.webp")
    if not base.exists():
        return base
    number = 2
    while True:
        candidate = source.with_name(f"{source.stem}-clean-{number}.webp")
        if not candidate.exists():
            return candidate
        number += 1


def write_report(path, report):
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="still image to clean")
    parser.add_argument("-o", "--output", type=Path, help="output WebP path")
    parser.add_argument("--inspect-only", action="store_true", help="inspect without creating an image")
    parser.add_argument("--quality", type=int, default=95, help="WebP quality, 0–100 (default: 95)")
    parser.add_argument("--lossless", action="store_true", help="use lossless WebP encoding")
    parser.add_argument("--report", type=Path, help="JSON report path")
    parser.add_argument("--force", action="store_true", help="overwrite an existing output or report")
    args = parser.parse_args(argv)
    if not 0 <= args.quality <= 100:
        parser.error("--quality must be between 0 and 100")
    if args.inspect_only and (args.output or args.lossless or args.quality != 95):
        parser.error("--inspect-only does not accept encoding options")
    source = args.input.resolve()
    if not source.is_file() or source.stat().st_size == 0:
        raise CleanerError("input is missing or empty")
    if not shutil.which("exiftool") or not shutil.which("c2patool"):
        raise CleanerError("exiftool and c2patool are required on PATH")
    output = (args.output or (source.with_name(source.stem + "-clean.webp")
                              if args.force else default_output(source))).resolve()
    if not args.inspect_only and output.suffix.lower() != ".webp":
        raise CleanerError("output must have a .webp extension")
    report_path = (args.report.resolve() if args.report else
                   Path(tempfile.mkdtemp(prefix="ai-image-cleaner-")) / "audit.json")
    if (report_path in (source, output) or not report_path.parent.is_dir() or
            (report_path.exists() and not args.force)):
        raise CleanerError("report path conflicts with another file or cannot be written")
    if not args.inspect_only and (source == output or not output.parent.is_dir() or
                                  (output.exists() and not args.force)):
        raise CleanerError("output path conflicts with the input or already exists (use --force)")

    before = audit(source)
    report = {"source": before, "output": None, "steps": [],
              "limits": ["No invisible watermark or AI classifier is checked.",
                         "A WebP re-encode cannot guarantee a detector's result."]}
    if args.inspect_only:
        write_report(report_path, report)
        print(f"Input: {source}\nMetadata tags: {len(before['metadata_tags'])}"
              f"\nC2PA: {before['c2pa']}\nReport: {report_path}", file=sys.stderr)
        return 0

    staged = None
    try:
        with Image.open(source) as image:
            if getattr(image, "n_frames", 1) != 1:
                raise CleanerError("animated or multipage images are not supported")
            image.load()
            had_profile = bool(image.info.get("icc_profile"))
            oriented = ImageOps.exif_transpose(image)
            pixels = convert_to_srgb(oriented)
            with tempfile.NamedTemporaryFile(prefix=".ai-image-cleaner-", suffix=".webp",
                                             dir=output.parent, delete=False) as file:
                staged = Path(file.name)
            pixels.save(staged, format="WEBP", quality=args.quality,
                        lossless=args.lossless, method=6)
        after = audit(staged)
        forbidden = {"EXIF", "XMP ", "ICCP", "ANIM", "ANMF"}
        if (after["format"] != "WEBP" or
                (after["width"], after["height"]) != tuple(pixels.size) or
                after["metadata_tags"] or after["c2pa"] != "absent" or
                forbidden.intersection(after["webp_chunks"])):
            raise CleanerError("output verification failed: metadata or provenance remains")
        staged.chmod(source.stat().st_mode & 0o666)
        staged.replace(output)
        after["path"] = str(output)
        report["output"] = after
        report["steps"] = ["exif_orientation_applied"]
        if had_profile:
            report["steps"].append("color_converted_to_srgb")
        report["steps"] += ["webp_reencoded", "metadata_removed"]
        report["encoding"] = {"lossless": args.lossless, "quality": args.quality}
        write_report(report_path, report)
        print(f"Image: {output}\nMetadata tags: {len(before['metadata_tags'])} → 0"
              f"\nC2PA: {before['c2pa']} → absent"
              "\nLimit: AI detector results and invisible watermarks are not checked."
              f"\nReport: {report_path}",
              file=sys.stderr)
        return 0
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise CleanerError(f"image conversion failed: {exc}") from exc
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CleanerError as exc:
        print(f"ai-image-cleaner: {exc}", file=sys.stderr)
        sys.exit(1)
