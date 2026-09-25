# ai-video-cleaner

Clean embedded metadata and locally visible provenance from videos and still images.

## Images

The same command accepts JPG, PNG, WebP, TIFF, BMP, GIF, AVIF, HEIC, and HEIF when Pillow can decode them. Still images are re-encoded as WebP. EXIF orientation is applied, an embedded color profile is converted to sRGB, and metadata is omitted. Animated and multipage images are rejected.

Install the image dependency once (Python 3.9+):

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements-image.txt
```

`exiftool` and `c2patool` must also be on `PATH`. Then run the same entry point:

```bash
python3 ai-video-cleaner.py /chemin/vers/photo.jpg
```

This writes `/chemin/vers/photo-clean.webp`. Existing output names get `-2`, `-3`, and so on. The source is preserved.
An audit report is saved at the path shown by the command.

```bash
python3 ai-video-cleaner.py photo.png --inspect-only
python3 ai-video-cleaner.py photo.png --lossless --report photo.audit.json
python3 ai-video-cleaner.py photo.png --quality 90 -o cleaned.webp
```

The saved WebP is checked for EXIF/XMP/ICC chunks, metadata tags, and C2PA claims. Invisible pixel watermarks and AI classifier scores are not checked. Removing metadata and re-encoding cannot guarantee that an AI detector will classify an image differently.

## Videos

## Why?

AI videos can be flagged by Instagram, TikTok, and other platforms.

## Before → After

| Before | After |
| --- | --- |
| ❌ Embedded provenance in the file | ✅ Locally visible metadata cleaned |
| ❌ Spoofed or leftover metadata | ✅ C2PA checked |
| ❌ C2PA or known AI markers | ✅ Local signals checked |

Runs locally. No video is uploaded. Video and audio are copied unchanged by default.

> ⚠️ This checks local signals only. It does not guarantee platform approval or remove invisible watermarks.

## How to start

Requirements: Python 3.9+ and these tools on `PATH`:

```text
ffmpeg  ffprobe  exiftool  c2patool
```

Run it:

```bash
python3 ai-video-cleaner.py path/to/video.mp4
```

Output:

```text
path/to/video-clean.mp4
```

Inspect without creating a video:

```bash
python3 ai-video-cleaner.py path/to/video.mp4 --inspect-only
```

Useful options:

```bash
python3 ai-video-cleaner.py INPUT --reencode          # H.264 CRF 18, saturation +4%
python3 ai-video-cleaner.py INPUT --drop-audio        # remove audio
python3 ai-video-cleaner.py INPUT --ignore-rotation   # ignore incorrect rotation metadata
python3 ai-video-cleaner.py INPUT --force              # overwrite an explicit output
```

Optional visible-badge cleanup requires `remove-ai-watermarks[video]` in a separate Python 3.11+ environment:

```bash
uv tool install --force 'remove-ai-watermarks[video]'
python3 ai-video-cleaner.py INPUT --remove-visible-seedance
```

## Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The report JSON and an optional HTML frame sheet are written to the paths shown by the command. A clean local report is not proof that a video is undetectable: invisible watermarks and platform classifiers are not tested.
