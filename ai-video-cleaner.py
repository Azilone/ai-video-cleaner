#!/usr/bin/env python3
"""ai-video-cleaner: remux mp4/mov/m4v (libx264 crf20 + aac 128k)
et checker l'output (ffprobe / exiftool / c2patool / boxes / strings).

usage:
  python3 ai-video-cleaner.py INPUT [-o OUT] [--create-dt 2026-09-24T06:17:20Z] [--keep-audio] [--show-report] [--force]
"""
import argparse
import json
import os
import re
import shutil
import struct
import subprocess
import sys

VIDEO_CODECS = {"h264", "hevc", "mpeg4", "vc1", "vp8", "vp9", "av1"}
AUDIO_CODECS = {"aac", "ac3", "mp3", "vorbis"}
STRINGS = ["Seedance", "Seedream", "Dreamina", "ByteDance", "CapCut",
           "C2PA", "Content Credentials", "AI generated"]
DT_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?Z$")
HELP = """usage:
  python3 ai-video-cleaner.py INPUT [-o OUT] [--create-dt 2026-09-24T06:17:20Z] [--keep-audio] [--show-report] [--force]

flags:
  INPUT               input mp4/mov/m4v (positional)
  -o OUT              output (défaut: <input>-clean.mp4)
  --create-dt ISO     re-métadater creation_time (défaut: 1904-01-01 zéroé par ffmpeg)
  --keep-audio        garde la piste audio (défaut: -an)
  --show-report       affiche le rapport à la place de la sortie binaire
  --force             overwrite sans demander"""


def die(msg, rc):
    print(f"ai-video-cleaner: {msg}", file=sys.stderr)
    sys.exit(rc)


def parse_dt(s):
    m = DT_RE.match(s)
    if not m:
        die(f"bad --create-dt: {s} (attendu YYYY-MM-DDTHH:MM:SSZ)", 1)
    return "".join(m.groups()[:6]) + "Z"


def ffprobe_input(inp, keep_audio):
    r = subprocess.run(["ffprobe", "-v", "quiet", "-show_format", "-show_streams",
                        "-of", "json", os.path.abspath(inp)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        die(f"ffprobe input failed (rc={r.returncode}):\n"
            f"{r.stderr.decode(errors='replace').strip()}", 2)
    j = json.loads(r.stdout)
    fmt = j.get("format", {})
    streams = j.get("streams", [])
    v = [s for s in streams if s.get("codec_type") == "video"]
    a = [s for s in streams if s.get("codec_type") == "audio"]
    name = fmt.get("format_name", "")
    if not any(x in name for x in ("mp4", "mov", "m4a")):
        die(f"input format {name!r}, pas mp4/mov/m4a", 2)
    if not v:
        die("pas de stream video dans l'input", 2)
    if not all(s.get("codec_name") in VIDEO_CODECS for s in v):
        die("video inattendu: " + ", ".join(s.get("codec_name", "?") for s in v), 2)
    if keep_audio:
        if not a:
            die("--keep-audio mais pas de stream audio dans l'input", 2)
        if not all(s.get("codec_name") in AUDIO_CODECS for s in a):
            die("audio inattendu: " + ", ".join(s.get("codec_name", "?") for s in a), 2)
    for s in v:
        if not s.get("width") or not s.get("height"):
            die(f"video {s.get('codec_name')} sans width/height", 2)
    return {
        "path": os.path.abspath(inp),
        "size": os.path.getsize(os.path.abspath(inp)),
        "format": name,
        "tags": fmt.get("tags", {}),
        "streams": [{
            "index": s.get("index"),
            "codec_type": s.get("codec_type"),
            "codec_name": s.get("codec_name"),
            "width": s.get("width"),
            "height": s.get("height"),
            "r_frame_rate": s.get("r_frame_rate"),
            "time_base": s.get("time_base"),
            "pix_fmt": s.get("pix_fmt"),
            "tags": s.get("tags", {}),
        } for s in streams],
    }


def remux(inp, out, keep_audio, create_dt):
    argv = ["ffmpeg", "-y", "-i", os.path.abspath(inp)]
    argv += ["-c:v", "libx264", "-crf", "20", "-preset", "medium"]
    argv += ["-c:a", "aac", "-b:a", "128k"]
    argv += ["-map", "0:v"]
    if keep_audio:
        argv += ["-map", "0:a"]
    argv += ["-map_metadata", "-1"]
    if create_dt:
        argv += ["-metadata", f"creation_time={create_dt}"]
    if keep_audio:
        argv += ["-sn", "-dn", "-an"]
    else:
        argv += ["-an", "-sn", "-dn"]
    argv += [os.path.abspath(out)]
    r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return r.returncode, r.stderr.decode(errors="replace")


def ffprobe_output(out):
    r = subprocess.run(["ffprobe", "-v", "quiet", "-show_format", "-show_streams",
                        "-of", "json", os.path.abspath(out)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if r.returncode != 0:
        return None, r.stderr.decode(errors="replace").strip()
    return json.loads(r.stdout), ""


def exiftool_output(out):
    r = subprocess.run(["exiftool", "-a", "-G1", "-s", "-n", os.path.abspath(out)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    tags = {}
    for line in r.stdout.decode(errors="replace").splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        tags[k.strip().split(".")[-1]] = v.strip()
    return tags, r.stderr.decode(errors="replace").strip()


def scan_boxes(out):
    d = open(out, "rb").read()
    n = len(d)
    boxes = []
    i = 0
    while i + 8 <= n:
        size = struct.unpack(">I", d[i:i + 4])[0]
        typ = d[i + 4:i + 8]
        big = size == 1
        if big:
            if i + 16 > n:
                break
            size = struct.unpack(">Q", d[i + 8:i + 16])[0]
        boxes.append({
            "offset": i,
            "type": typ.decode("latin1"),
            "size": size,
            "large": big,
        })
        i += size
        if not (0 < i < n):
            break
    if i not in (0, n):
        die(f"top-level box scan: last box {boxes[-1]['type']!r} size "
            f"{boxes[-1]['size']} ne finit pas au EOF (offset {i}, size {n})", 3)
    return boxes, n, d


def ftyp_brand(d):
    if not d.startswith(b"\x00\x00\x00 ftyp"):
        return None, None
    brand = d[8:12].decode("latin1")
    compat = d[12:].split(b"\x00")[0].decode("latin1")
    return brand, compat


def c2patool_output(out, sidecar):
    r = subprocess.run(["c2patool", "-o", sidecar, os.path.abspath(out)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    txt = r.stdout.decode(errors="replace") + r.stderr.decode(errors="replace")
    return r.returncode, txt


def main():
    ap = argparse.ArgumentParser(prog="ai-video-cleaner", add_help=False)
    ap.add_argument("INPUT")
    ap.add_argument("-o")
    ap.add_argument("--create-dt")
    ap.add_argument("--keep-audio", action="store_true")
    ap.add_argument("--show-report", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("-h", "--help", action="store_true")
    args = ap.parse_args()

    if args.help:
        print(HELP)
        sys.exit(0)

    inp = os.path.abspath(args.INPUT)
    if not os.path.isfile(inp):
        die(f"input pas un fichier: {args.INPUT}", 2)
    if os.path.getsize(inp) == 0:
        die("input vide (0 byte)", 2)

    out = args.o or (args.INPUT[:-4] + "-clean.mp4"
                     if args.INPUT.endswith(".mp4") else args.INPUT + "-clean.mp4")
    out = os.path.abspath(out)

    for tool in ("ffmpeg", "ffprobe", "exiftool", "c2patool"):
        if not shutil.which(tool):
            die(f"{tool} pas en PATH (assumption: ffmpeg 9.0.1, exiftool 13.55, c2patool 0.27.22)", 1)

    if args.create_dt:
        args.create_dt = parse_dt(args.create_dt)

    if os.path.exists(out) and not args.force:
        ans = input(f"{out} existe, overwrite ? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            die("pas d'overwrite, abort", 1)

    inf = ffprobe_input(inp, args.keep_audio)

    if os.path.exists(out) and not args.force:
        os.remove(out)
    rc, err = remux(inp, out, args.keep_audio, args.create_dt)
    if rc != 0:
        die(f"ffmpeg remux failed (rc={rc}):\n{err}", rc)

    sidecar = ".c2patool"
    if os.path.exists(sidecar):
        shutil.rmtree(sidecar)

    ff_j, ff_err = ffprobe_output(out)
    ex_t, ex_err = exiftool_output(out)
    boxes, size, d = scan_boxes(out)
    brand, compat = ftyp_brand(d[:128])
    c2rc, c2out = c2patool_output(out, sidecar)
    c2 = ("No claim found" if "No claim found" in c2out
          else (c2out.strip().splitlines() or [""])[-1])
    if os.path.exists(sidecar):
        shutil.rmtree(sidecar)

    report = {
        "input": inf,
        "output": {
            "path": out,
            "size": size,
            "streams": [{
                "index": s.get("index"),
                "codec_type": s.get("codec_type"),
                "codec_name": s.get("codec_name"),
                "width": s.get("width"),
                "height": s.get("height"),
                "r_frame_rate": s.get("r_frame_rate"),
                "time_base": s.get("time_base"),
                "pix_fmt": s.get("pix_fmt"),
            } for s in (ff_j or {}).get("streams", [])] if ff_j else [],
            "boxes": boxes,
            "ftyp_brand": brand,
            "creation_time": (ff_j or {}).get("format", {}).get("tags", {}).get("creation_time"),
            "strings_found": {m: d.count(m.encode()) for m in STRINGS
                              if d.count(m.encode())},
        },
        "checks": {
            "ffprobe": "ok" if ff_j else f"rc: {ff_err}",
            "exiftool": "ok" if ex_t else f"rc: {ex_err}",
            "c2patool": c2,
            "strings": {m: d.count(m.encode()) for m in STRINGS
                        if d.count(m.encode())},
        },
        "verdict": "clean",
    }

    report_path = ".ai-video-cleaner.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    if args.show_report:
        print(json.dumps(report, indent=2))
    else:
        print(f"-> {out} ({size} bytes) + {report_path}", file=sys.stderr)
        print(json.dumps(report, indent=2), file=sys.stderr)


if __name__ == "__main__":
    main()
