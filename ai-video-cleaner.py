#!/usr/bin/env python3
"""Inspect and reduce locally observable provenance signals in MP4/MOV video."""

import argparse
import base64
from datetime import datetime
import html
import importlib.util
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile

MARKERS = (
    "Seedance", "Seedream", "Dreamina", "ByteDance", "CapCut",
    "C2PA", "Content Credentials", "AI generated", "AIGC",
    "trainedAlgorithmicMedia",
)
METADATA_PATTERN = re.compile(
    r"seedance|seedream|dreamina|bytedance|capcut|c2pa|content.credentials|"
    r"ai[ -_]?generat|\baigc\b|\bai\b|trainedAlgorithmicMedia|\bworkflow\b|\bprompt\b",
    re.IGNORECASE,
)
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
LOCAL_SIGNALS = ("metadata", "c2pa", "byte_markers", "known_provenance")
SUBTLE_SATURATION = 1.04
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff",
                  ".bmp", ".gif", ".avif", ".heic", ".heif"}


class CleanerError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code
        self.reported = False


def run(command):
    try:
        return subprocess.run(command, capture_output=True, text=True, errors="replace")
    except OSError as exc:
        raise CleanerError(f"unable to run {command[0]}: {exc}") from exc


def signal(status, evidence=None):
    return {"status": status, "evidence": evidence or []}


def parse_date(value):
    try:
        if not DATE_PATTERN.fullmatch(value):
            raise ValueError("expected format: YYYY-MM-DDTHH:MM:SSZ")
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise CleanerError(f"invalid --create-dt value: {value} ({exc})") from exc


def stream_summary(stream):
    fields = ("index", "codec_type", "codec_name", "width", "height", "duration",
              "start_time", "nb_frames", "r_frame_rate", "time_base", "pix_fmt",
              "tags", "side_data_list")
    return {key: stream.get(key) for key in fields if key in stream}


def probe(path):
    result = run(["ffprobe", "-v", "error", "-show_format", "-show_streams",
                  "-of", "json", str(path)])
    if result.returncode:
        raise CleanerError(result.stderr.strip() or "ffprobe failed", 2)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CleanerError(f"invalid ffprobe response: {exc}", 2) from exc


def inspect_mp4(path):
    """Check top-level box boundaries and scan byte markers in bounded memory."""
    size = path.stat().st_size
    boxes = []
    brand = None
    with path.open("rb") as file:
        offset = 0
        while offset < size:
            if size - offset < 8:
                raise CleanerError(f"truncated MP4 box at offset {offset}", 3)
            file.seek(offset)
            box_size, box_type = struct.unpack(">I4s", file.read(8))
            large = box_size == 1
            header_size = 16 if large else 8
            if large:
                if size - offset < 16:
                    raise CleanerError(f"truncated MP4 box at offset {offset}", 3)
                box_size = struct.unpack(">Q", file.read(8))[0]
            elif box_size == 0:
                box_size = size - offset
            if box_size < header_size or box_size > size - offset:
                raise CleanerError(f"invalid MP4 box size at offset {offset}", 3)
            boxes.append({"offset": offset, "type": box_type.decode("latin1"),
                          "size": box_size, "large": large})
            if offset == 0 and box_type == b"ftyp" and box_size >= header_size + 4:
                brand = file.read(4).decode("latin1")
            offset += box_size

        counts = {marker: 0 for marker in MARKERS}
        overlap = max(map(len, MARKERS)) - 1
        file.seek(0)
        tail = b""
        while True:
            chunk = file.read(1024 * 1024)
            if not chunk:
                break
            data = tail + chunk
            for marker in MARKERS:
                needle = marker.encode("ascii")
                start = max(0, len(tail) - len(needle) + 1)
                counts[marker] += sum(1 for _ in re.finditer(
                    re.escape(needle), data[start:], re.IGNORECASE))
            tail = data[-overlap:]
    return boxes, brand, {key: value for key, value in counts.items() if value}


def inspect_exif(path):
    result = run(["exiftool", "-a", "-G1", "-s", "-n", "-json", str(path)])
    if result.returncode:
        raise CleanerError(result.stderr.strip() or "exiftool failed", 3)
    try:
        tags = json.loads(result.stdout)[0]
    except (ValueError, IndexError, TypeError) as exc:
        raise CleanerError(f"invalid exiftool response: {exc}", 3) from exc
    return {key: value for key, value in tags.items()
            if not key.startswith(("System:", "File:", "ExifTool:")) and key != "SourceFile"}


def inspect_c2pa(path):
    result = run(["c2patool", str(path)])
    output = (result.stdout + result.stderr).strip()
    if "No claim found" in output:
        return signal("absent", ["c2patool: No claim found"])
    if result.returncode == 0 and output:
        try:
            claim = json.loads(result.stdout)
            active = claim.get("active_manifest")
            manifest = claim.get("manifests", {}).get(active, {}) if active else {}
            evidence = {"active_manifest": active,
                        "claim_generator": manifest.get("claim_generator"),
                        "assertion_labels": [item.get("label") for item in
                                             manifest.get("assertions", [])
                                             if isinstance(item, dict)]}
        except (ValueError, TypeError, AttributeError):
            evidence = {"raw_output": output[:4000]}
        return signal("present", [evidence])
    return signal("error", [output or f"c2patool exit code {result.returncode}"])


def inspect_known_provenance(path, visible=False):
    if shutil.which("remove-ai-watermarks") is None:
        return (signal("unverified", ["remove-ai-watermarks is not installed"]),
                signal("unverified", ["optional visual detection is unavailable"]))
    command = ["remove-ai-watermarks", "video", "identify"]
    if not visible:
        command.append("--no-visible")
    command += ["--json", str(path)]
    result = run(command)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "identify failed"
        known = signal("error", [detail])
        visual = signal("error" if visible else "unverified", [detail])
        return known, visual
    try:
        info = json.loads(result.stdout)
        markers = info.get("metadata_markers") or {}
        known = signal("present" if markers else "absent", sorted(markers))
        mark = info.get("visible_mark")
        visual = (signal("present", [mark]) if mark else signal("absent", ["no stable mark recognized"]))
        if not visible:
            visual = signal("unverified", ["visual detection not requested"])
        return known, visual
    except (ValueError, TypeError) as exc:
        known = signal("error", [f"invalid identify JSON: {exc}"])
        return known, signal("error" if visible else "unverified", ["invalid identify JSON"])


def verdict(signals, tools):
    required = ("metadata", "c2pa", "byte_markers")
    if any(tools[name]["status"] == "error" for name in ("ffprobe", "exiftool", "mp4")):
        return "verification_incomplete"
    if any(signals[name]["status"] == "error" for name in required):
        return "verification_incomplete"
    if any(signals[name]["status"] == "present" for name in (*required, "known_provenance")):
        return "signals_present"
    if signals["known_provenance"]["status"] == "error":
        return "verification_incomplete"
    return "local_signals_absent"


def audit(path, display_path=None, check_visible=False):
    media = {"path": str(display_path or path), "size": path.stat().st_size}
    tools = {}
    signals = {
        "metadata": signal("unverified"), "c2pa": signal("unverified"),
        "byte_markers": signal("unverified"), "known_provenance": signal("unverified"),
        "visible_mark": signal("unverified"),
        "invisible_watermark": signal("unverified", ["no compatible local detector configured"]),
        "platform_classifier": signal("unverified", ["no external service used"]),
        "audio_watermark": signal("unverified", ["no audio detector used"]),
    }
    try:
        info = probe(path)
        media["format"] = info.get("format", {})
        if display_path is not None:
            media["format"]["filename"] = str(display_path)
        media["streams"] = [stream_summary(s) for s in info.get("streams", [])]
        tools["ffprobe"] = signal("ok", ["read successfully"])
    except CleanerError as exc:
        tools["ffprobe"] = signal("error", [str(exc)])

    try:
        tags = inspect_exif(path)
        media["exif_tags"] = tags
        hits = {key: value for key, value in tags.items()
                if METADATA_PATTERN.search(f"{key} {value}")}
        if "format" in media:
            for key, value in media["format"].get("tags", {}).items():
                if METADATA_PATTERN.search(f"{key} {value}"):
                    hits[f"ffprobe:{key}"] = value
            for stream in media.get("streams", []):
                for key, value in stream.get("tags", {}).items():
                    if METADATA_PATTERN.search(f"{key} {value}"):
                        hits[f"ffprobe:stream{stream.get('index')}:{key}"] = value
        signals["metadata"] = signal("present" if hits else "absent", hits)
        tools["exiftool"] = signal("ok", ["read successfully"])
    except CleanerError as exc:
        signals["metadata"] = signal("error", [str(exc)])
        tools["exiftool"] = signal("error", [str(exc)])

    try:
        boxes, brand, markers = inspect_mp4(path)
        media["boxes"] = boxes
        media["ftyp_brand"] = brand
        types = {box["type"] for box in boxes}
        if not brand or not {"ftyp", "mdat", "moov"} <= types:
            raise CleanerError("required MP4 boxes ftyp/mdat/moov are missing", 3)
        signals["byte_markers"] = signal("present" if markers else "absent", markers)
        tools["mp4"] = signal("ok", ["top-level box boundaries are consistent; internal content not verified"])
    except CleanerError as exc:
        signals["byte_markers"] = signal("error", [str(exc)])
        tools["mp4"] = signal("error", [str(exc)])

    signals["c2pa"] = inspect_c2pa(path)
    signals["known_provenance"], signals["visible_mark"] = inspect_known_provenance(
        path, visible=check_visible)
    media["signals"] = signals
    media["tools"] = tools
    media["verdict"] = verdict(signals, tools)
    return media


def has_explicit_signals(item):
    return any(item["signals"][name]["status"] == "present" for name in LOCAL_SIGNALS)


def ffmpeg_output(source, target, drop_audio, creation_time, reencode=False,
                  ignore_rotation=False):
    command = ["ffmpeg", "-nostdin", "-v", "error"]
    if ignore_rotation:
        command += ["-display_rotation:v:0", "0"]
    command += ["-i", str(source),
               "-map", "0:v:0"]
    if not drop_audio:
        command += ["-map", "0:a?"]
    if reencode:
        command += ["-vf", f"eq=saturation={SUBTLE_SATURATION:.2f}"]
        command += ["-c:v", "libx264", "-crf", "18", "-preset", "slow"]
        if not drop_audio:
            command += ["-c:a", "copy"]
    else:
        command += ["-c", "copy"]
    command += ["-map_metadata", "-1", "-map_metadata:s", "-1",
                "-map_chapters", "-1", "-sn", "-dn"]
    if drop_audio:
        command += ["-an"]
    if creation_time:
        command += ["-metadata", f"creation_time={creation_time}"]
    command += ["-f", "mp4", "-y", str(target)]
    result = run(command)
    if result.returncode:
        raise CleanerError(f"ffmpeg failed: {result.stderr.strip()}")


def stream_hashes(path, drop_audio=False):
    command = ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0"]
    if not drop_audio:
        command += ["-map", "0:a?"]
    command += ["-c", "copy", "-f", "streamhash", "-hash", "SHA256", "-"]
    result = run(command)
    if result.returncode:
        raise CleanerError(f"stream hash calculation failed: {result.stderr.strip()}", 3)
    return [line.split(",", 2)[-1] for line in result.stdout.splitlines() if ",SHA256=" in line]


def display_geometry(stream):
    width, height = stream.get("width"), stream.get("height")
    rotations = [side.get("rotation") for side in stream.get("side_data_list", [])
                 if side.get("rotation") is not None]
    if rotations and int(rotations[0]) % 180:
        return height, width
    return width, height


def display_rotation(stream):
    for side in stream.get("side_data_list", []):
        if side.get("rotation") is not None:
            return int(side["rotation"]) % 360
    return 0


def validate_output(source_audit, output_audit, drop_audio, copied,
                    ignore_rotation=False):
    source_streams = source_audit.get("streams", [])
    output_streams = output_audit.get("streams", [])
    source_video = next((s for s in source_streams if s.get("codec_type") == "video"), None)
    output_video = [s for s in output_streams if s.get("codec_type") == "video"]
    source_audio = [s for s in source_streams if s.get("codec_type") == "audio"]
    output_audio = [s for s in output_streams if s.get("codec_type") == "audio"]
    errors = []
    if source_video is None or len(output_video) != 1:
        errors.append("one source and one output video stream are required")
    if len(output_audio) != (0 if drop_audio else len(source_audio)):
        errors.append("audio stream count changed")
    if source_video and output_video:
        expected_geometry = ((source_video.get("width"), source_video.get("height"))
                             if ignore_rotation else display_geometry(source_video))
        if expected_geometry != display_geometry(output_video[0]):
            errors.append("display dimensions or orientation changed")
        expected_rotation = 0 if ignore_rotation else display_rotation(source_video)
        if copied and display_rotation(output_video[0]) != expected_rotation:
            errors.append("video rotation changed")
        if copied and (source_video.get("width"), source_video.get("height")) != (
                output_video[0].get("width"), output_video[0].get("height")):
            errors.append("encoded dimensions changed despite stream copy")
        if source_video.get("nb_frames") and output_video[0].get("nb_frames"):
            if source_video["nb_frames"] != output_video[0]["nb_frames"]:
                errors.append("video frame count changed")
        try:
            before = float(source_audit["format"]["duration"])
            after = float(output_audit["format"]["duration"])
            if abs(before - after) > 0.1:
                errors.append("duration changed by more than 0.1 s")
        except (KeyError, ValueError, TypeError):
            errors.append("duration could not be compared")
        if source_audio and output_audio:
            try:
                source_offset = float(source_audio[0]["start_time"]) - float(source_video["start_time"])
                output_offset = float(output_audio[0]["start_time"]) - float(output_video[0]["start_time"])
                if abs(source_offset - output_offset) > 0.1:
                    errors.append("audio/video sync changed by more than 0.1 s")
            except (KeyError, ValueError, TypeError):
                errors.append("audio/video sync could not be compared")
    evidence = {}
    if not errors:
        before_hashes = stream_hashes(Path(source_audit["path"]), drop_audio)
        after_hashes = stream_hashes(Path(output_audit["path"]), drop_audio)
        evidence["source_stream_hashes"] = before_hashes
        evidence["output_stream_hashes"] = after_hashes
        if not before_hashes or not after_hashes:
            errors.append("stream hashes are missing")
        elif copied and before_hashes != after_hashes:
            errors.append("encoded streams changed despite stream-copy mode")
        elif not drop_audio and before_hashes[1:] != after_hashes[1:]:
            errors.append("audio streams changed")
    return {"status": "ok" if not errors else "error", "errors": errors, **evidence}


def signal_changes(source_audit, output_audit):
    return {name: {"source": source_audit["signals"][name]["status"],
                   "output": output_audit["signals"][name]["status"]}
            for name in source_audit["signals"]}


def temporary_mp4(directory):
    with tempfile.NamedTemporaryFile(prefix=".ai-video-cleaner-", suffix=".mp4",
                                     dir=directory, delete=False) as file:
        return Path(file.name)


def default_output(source):
    base = source.with_name(source.stem + "-clean.mp4")
    if not base.exists():
        return base
    number = 2
    while True:
        candidate = source.with_name(f"{source.stem}-clean-{number}.mp4")
        if not candidate.exists():
            return candidate
        number += 1


def make_report_directory():
    return Path(tempfile.mkdtemp(prefix="ai-video-cleaner-"))


def write_report(path, report, show):
    content = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    path.write_text(content, encoding="utf-8")
    if show:
        print(content, end="")


def human_signal_summary(item):
    if item is None:
        return "unavailable"
    state = item.get("verdict")
    if state == "verification_incomplete":
        return "verification incomplete"
    if state == "local_signals_absent":
        return "no explicit local signals found"
    signals = item.get("signals", {})
    details = []
    for name, label in (("metadata", "AI metadata"),
                        ("c2pa", "claim C2PA"),
                        ("known_provenance", "known provenance")):
        if signals.get(name, {}).get("status") == "present":
            details.append(label)
    markers = signals.get("byte_markers", {})
    if markers.get("status") == "present":
        evidence = markers.get("evidence", {})
        if isinstance(evidence, dict):
            details.extend(f"{name} ×{count}" for name, count in evidence.items())
        else:
            details.append("text markers")
    return ", ".join(details) or "explicit signals detected"


def print_human_summary(report, report_path, output_path=None):
    print("\n── ai-video-cleaner ─────────────────────────", file=sys.stderr)
    print(f"Input    : {report['source']['path']}", file=sys.stderr)
    print(f"Before   : {human_signal_summary(report['source'])}", file=sys.stderr)
    if report.get("error"):
        print(f"Result   : failed — {report['error']}", file=sys.stderr)
    elif output_path is None:
        status = ("verification incomplete" if report.get("verdict") == "verification_incomplete"
                  else "inspection complete")
        print(f"Result   : {status}", file=sys.stderr)
    else:
        changes = {
            "encoded_video_unchanged": "streams copied without re-encoding",
            "video_reencoded": "video re-encoded",
            "visible_correction_attempted": "visible correction attempted; human review required",
        }
        print(f"Action   : {changes.get(report.get('content_change'), 'processing complete')}",
              file=sys.stderr)
        if report.get("visual_adjustments"):
            print("Color    : saturation slightly increased (+4%)", file=sys.stderr)
        source_audio = sum(s.get("codec_type") == "audio" for s in
                           report["source"].get("streams", []))
        output_audio = sum(s.get("codec_type") == "audio" for s in
                           report["output"].get("streams", []))
        if source_audio == 0:
            audio_text = "no audio stream in input"
        elif output_audio == 1:
            audio_text = "1 stream kept"
        elif output_audio:
            audio_text = f"{output_audio} streams kept"
        else:
            audio_text = "streams removed on request"
        print(f"Audio    : {audio_text}", file=sys.stderr)
        comparison = report.get("comparison") or {}
        if comparison.get("status") == "ok":
            exact = comparison.get("source_stream_hashes") == comparison.get("output_stream_hashes")
            check = ("encoded streams identical" if exact else
                     "duration, orientation, and sync checked")
            print(f"Check    : {check}", file=sys.stderr)
        print(f"After    : {human_signal_summary(report.get('output'))}", file=sys.stderr)
        print(f"Video    : {output_path}", file=sys.stderr)
    print(f"Report   : {report_path}", file=sys.stderr)
    sheet = report.get("contact_sheet", {})
    if sheet.get("path"):
        print(f"Frames   : {sheet['path']} ({sheet['frames']} previews)", file=sys.stderr)
    elif sheet.get("error"):
        print(f"Frames   : not created — {sheet['error']}", file=sys.stderr)
    print("Limit    : detector scores and invisible watermarks are not checked.",
          file=sys.stderr)


def create_contact_sheet(source, destination, display_name=None):
    """Create a self-contained HTML review sheet with about one frame per second."""
    with tempfile.TemporaryDirectory(prefix="ai-video-frames-") as directory:
        frame_pattern = str(Path(directory) / "%04d.jpg")
        command = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(source),
                   "-map", "0:v:0", "-vf",
                   "fps=1:start_time=0:round=down,scale=320:-2",
                   "-frames:v", "180", "-q:v", "3", "-y", frame_pattern]
        result = run(command)
        if result.returncode:
            raise CleanerError(f"frame extraction failed: {result.stderr.strip()}", 3)
        frames = sorted(Path(directory).glob("*.jpg"))
        if not frames:
            raise CleanerError("no frames extracted for the contact sheet", 3)
        cards = []
        for index, frame in enumerate(frames):
            encoded = base64.b64encode(frame.read_bytes()).decode("ascii")
            cards.append(f'<figure><img src="data:image/jpeg;base64,{encoded}" '
                         f'alt="Sample at about {index} seconds"><figcaption>'
                         f'≈ {index} s</figcaption></figure>')
        title = html.escape(display_name or source.name)
        truncation = ("<p>The sheet is limited to the first 180 seconds.</p>"
                      if len(frames) == 180 else "")
        document = ("<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
                    "<meta name=\"viewport\" content=\"width=device-width\">"
                    f"<title>Video inspection — {title}</title>"
                    "<style>body{font:16px system-ui;margin:2rem;background:#111;color:#eee}"
                    "main{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));"
                    "gap:1rem}figure{margin:0;background:#222;padding:.5rem}"
                    "img{display:block;width:100%;height:auto}figcaption{padding:.4rem}</style>"
                    f"<h1>Video inspection — {title}</h1>"
                    "<p>About one frame per second. Timestamps are approximate; "
                    "this sheet does not measure AI-detection probability.</p>"
                    f"{truncation}<main>{''.join(cards)}</main></html>\n")
        staged = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".html",
                                             prefix=".ai-video-sheet-", dir=destination.parent,
                                             delete=False) as file:
                staged = Path(file.name)
                file.write(document)
            staged.replace(destination)
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)
    return len(frames)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    value_options = {"-o", "--output", "--report", "--contact-sheet", "--create-dt",
                     "--quality"}
    source_arg = None
    skip = False
    for item in argv:
        if skip:
            skip = False
        elif item in value_options:
            skip = True
        elif not item.startswith("-"):
            source_arg = item
            break
    if source_arg and Path(source_arg).suffix.lower() in IMAGE_SUFFIXES:
        image_script = Path(__file__).with_name("ai-image-cleaner.py")
        if importlib.util.find_spec("PIL") is None:
            local_python = Path(__file__).with_name(".venv") / "bin" / "python"
            if local_python.is_file():
                return subprocess.call([str(local_python), str(image_script), *argv])
        spec = importlib.util.spec_from_file_location(
            "ai_image_cleaner", image_script)
        image_cleaner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(image_cleaner)
        try:
            return image_cleaner.main(argv)
        except image_cleaner.CleanerError as exc:
            print(f"ai-image-cleaner: {exc}", file=sys.stderr)
            return 1
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="MP4/MOV/M4V video")
    parser.add_argument("-o", "--output", type=Path, help="output MP4")
    parser.add_argument("--inspect-only", action="store_true", help="inspect without modifying the video")
    parser.add_argument("--contact-sheet", type=Path,
                        help="with --inspect-only, create a local HTML sheet of timestamped frames")
    parser.add_argument("--report", type=Path, help="JSON report path")
    parser.add_argument("--drop-audio", action="store_true", help="remove all audio streams")
    video_mode = parser.add_mutually_exclusive_group()
    video_mode.add_argument("--reencode", action="store_true",
                            help="re-encode as H.264 CRF 18 with +4% saturation")
    video_mode.add_argument("--copy-video", action="store_true",
                            help="copy the video stream without re-encoding (default)")
    parser.add_argument("--ignore-rotation", action="store_true",
                        help="ignore incorrect rotation metadata; keep pixels as encoded")
    parser.add_argument("--keep-audio", action="store_true", help="compatibility flag; audio is kept by default")
    parser.add_argument("--remove-visible-seedance", action="store_true",
                        help="fix only a stable, recognized Seedance badge")
    parser.add_argument("--create-dt", help="UTC creation time (YYYY-MM-DDTHH:MM:SSZ)")
    parser.add_argument("--show-report", action="store_true", help="print the report to stdout")
    parser.add_argument("--force", action="store_true", help="overwrite existing output files")
    args = parser.parse_args(argv)
    if args.drop_audio and args.keep_audio:
        parser.error("--drop-audio and --keep-audio are incompatible")
    if args.inspect_only and (args.output or args.drop_audio or args.reencode or args.copy_video
                              or args.ignore_rotation
                              or args.remove_visible_seedance or args.create_dt):
        parser.error("--inspect-only does not accept cleaning options")
    if args.contact_sheet and not args.inspect_only:
        parser.error("--contact-sheet requires --inspect-only")
    source = args.input.resolve()
    if not source.is_file() or source.stat().st_size == 0:
        raise CleanerError("input is missing or empty", 2)
    output = (args.output or (source.with_name(source.stem + "-clean.mp4")
                              if args.force else default_output(source))).resolve()
    automatic_report_dir = make_report_directory() if args.report is None else None
    report_path = (args.report.resolve() if args.report else
                   automatic_report_dir / "audit.json")
    sheet_path = (args.contact_sheet.resolve() if args.contact_sheet else
                  automatic_report_dir / "frames.html"
                  if automatic_report_dir is not None and not args.inspect_only else None)
    if report_path in (source, output):
        raise CleanerError("the report path must differ from the input and output")
    if sheet_path is not None:
        if sheet_path in (source, output, report_path):
            raise CleanerError("the contact sheet path must differ from the other files")
        if not sheet_path.parent.is_dir():
            raise CleanerError("the contact sheet directory does not exist")
        if sheet_path.exists() and not args.force:
            raise CleanerError(f"the contact sheet already exists: {sheet_path} (use --force)")
    if not report_path.parent.is_dir():
        raise CleanerError("the report directory does not exist")
    if report_path.exists() and not args.force:
        raise CleanerError(f"the report already exists: {report_path} (use --force)")
    if not args.inspect_only:
        if source == output:
            raise CleanerError("the output must differ from the input")
        if not output.parent.is_dir():
            raise CleanerError("the output directory does not exist")
        if output.exists() and not args.force:
            raise CleanerError(f"the output already exists: {output} (use --force)")
    required_tools = ("ffprobe", "exiftool", "c2patool")
    if not args.inspect_only or sheet_path is not None:
        required_tools += ("ffmpeg",)
    for tool in required_tools:
        if shutil.which(tool) is None:
            raise CleanerError(f"{tool} is not available on PATH")
    creation_time = parse_date(args.create_dt) if args.create_dt else None
    source_audit = audit(source, check_visible=args.remove_visible_seedance)
    report = {"source": source_audit, "output": None, "steps": [],
              "comparison": None, "verdict": source_audit["verdict"]}
    if args.inspect_only:
        if sheet_path is not None:
            report["contact_sheet"] = {
                "path": str(sheet_path),
                "frames": create_contact_sheet(source, sheet_path),
                "sampling": "about one frame per second, maximum 180",
            }
        write_report(report_path, report, args.show_report)
        print_human_summary(report, report_path)
        return 3 if source_audit["verdict"] == "verification_incomplete" else 0

    formats = set(source_audit.get("format", {}).get("format_name", "").split(","))
    if "mov" not in formats or not any(s.get("codec_type") == "video"
                                         for s in source_audit.get("streams", [])):
        report["error"] = "input is not a valid MP4/MOV/M4V video"
        write_report(report_path, report, args.show_report)
        print_human_summary(report, report_path)
        error = CleanerError(report["error"], 2)
        error.reported = True
        raise error
    if args.remove_visible_seedance and source_audit["signals"]["visible_mark"]["status"] == "error":
        report["error"] = "visible detection is unavailable: install remove-ai-watermarks[video]"
        write_report(report_path, report, args.show_report)
        print_human_summary(report, report_path)
        error = CleanerError(report["error"], 1)
        error.reported = True
        raise error

    temps = []
    candidate = None
    copied = True
    try:
        candidate = temporary_mp4(output.parent)
        temps.append(candidate)
        reencode = args.reencode
        ffmpeg_output(source, candidate, args.drop_audio, creation_time,
                      reencode=reencode, ignore_rotation=args.ignore_rotation)
        copied = not reencode
        report["steps"].append("remux_copy" if copied else
                               "reencode_requested")
        if args.ignore_rotation:
            report["steps"].append("rotation_metadata_ignored")
        if reencode:
            report["visual_adjustments"] = {"saturation_multiplier": SUBTLE_SATURATION}
            report["steps"].append("color_boost")
        candidate_audit = audit(candidate, display_path=output)

        if has_explicit_signals(candidate_audit) and shutil.which("remove-ai-watermarks"):
            stripped = temporary_mp4(output.parent)
            temps.append(stripped)
            result = run(["remove-ai-watermarks", "video", "metadata", str(candidate),
                          "--remove", "--remove-all", "-o", str(stripped)])
            if result.returncode:
                raise CleanerError(f"metadata cleanup failed: {result.stderr.strip() or result.stdout.strip()}", 3)
            candidate = stripped
            report["steps"].append("metadata_strip")
            candidate_audit = audit(candidate, display_path=output)

        if args.remove_visible_seedance:
            if source_audit["signals"]["visible_mark"]["status"] == "absent":
                report["steps"].append("visible_seedance_not_found")
            else:
                before_visible_hash = stream_hashes(candidate, drop_audio=True)
                visible = temporary_mp4(output.parent)
                temps.append(visible)
                result = run(["remove-ai-watermarks", "video", "visible", str(candidate),
                              "--mark", "seedance", "--backend", "cv2", "-o", str(visible)])
                if result.returncode == 2 and visible.stat().st_size == 0:
                    report["steps"].append("visible_seedance_not_found")
                elif result.returncode:
                    raise CleanerError(f"visible correction failed: {result.stderr.strip() or result.stdout.strip()}", 3)
                else:
                    if before_visible_hash == stream_hashes(visible, drop_audio=True):
                        raise CleanerError("visible correction did not change the video stream", 3)
                    candidate = visible
                    copied = False
                    report["steps"].append("visible_seedance_removed")
                    report["visible_action"] = {
                        "tool_output": result.stdout.strip(),
                        "manual_region_review": "required",
                    }
            candidate_audit = audit(candidate, display_path=output, check_visible=True)

        report["output"] = candidate_audit
        report["verdict"] = candidate_audit["verdict"]
        # Use the actual temporary path for quality checks and stream hashes.
        quality_audit = dict(candidate_audit, path=str(candidate))
        report["comparison"] = validate_output(source_audit, quality_audit,
                                                args.drop_audio, copied,
                                                args.ignore_rotation)
        report["comparison"]["signal_changes"] = signal_changes(source_audit, candidate_audit)
        if "visible_seedance_removed" in report["steps"]:
            report["content_change"] = "visible_correction_attempted"
        elif copied:
            report["content_change"] = "encoded_video_unchanged"
        else:
            report["content_change"] = "video_reencoded"
        if candidate_audit["verdict"] != "local_signals_absent":
            raise CleanerError("local signals remain or verification failed", 3)
        if report["comparison"]["status"] != "ok":
            raise CleanerError("quality check failed: " + "; ".join(
                report["comparison"]["errors"]), 3)
        if args.remove_visible_seedance and candidate_audit["signals"]["visible_mark"]["status"] != "absent":
            raise CleanerError("visible mark verification is inconclusive", 3)
        if sheet_path is not None:
            try:
                report["contact_sheet"] = {
                    "path": str(sheet_path),
                    "frames": create_contact_sheet(candidate, sheet_path, output.name),
                    "sampling": "about one frame per second, maximum 180",
                }
            except CleanerError as exc:
                report["contact_sheet"] = {"error": str(exc)}
        candidate.chmod(source.stat().st_mode & 0o666)
        candidate.replace(output)
        report["output"]["path"] = str(output)
        write_report(report_path, report, args.show_report)
        print_human_summary(report, report_path, output)
        return 0
    except CleanerError as exc:
        report["error"] = str(exc)
        if report["output"] is None and candidate and candidate.exists() and candidate.stat().st_size:
            report["output"] = audit(candidate, display_path=output)
            report["verdict"] = report["output"]["verdict"]
        write_report(report_path, report, args.show_report)
        print_human_summary(report, report_path)
        exc.reported = True
        raise
    finally:
        for path in temps:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CleanerError as exc:
        if not exc.reported:
            print(f"ai-video-cleaner: {exc}", file=sys.stderr)
        sys.exit(exc.code)
