# ai-video-cleaner

Clean explicit, locally visible AI-provenance signals from MP4/MOV/M4V files.

## Why?

AI videos can be flagged by Instagram, TikTok, and other platforms.

## Before → After

| Before | After |
| --- | --- |
| ❌ AI video flagged by a platform | ✅ Metadata cleaned |
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
python3 -m unittest discover -s tests -v
```

The report JSON and an optional HTML frame sheet are written to the paths shown by the command. A clean local report is not proof that a video is undetectable: invisible watermarks and platform classifiers are not tested.
