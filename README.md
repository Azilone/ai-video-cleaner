# ai-video-cleaner

Clean explicit, locally visible AI-provenance signals from MP4/MOV/M4V files.

## TL;DR

```text
video.mp4  ── inspect locally ──▶  video-clean.mp4
```

- Runs locally. No video is uploaded.
- Copies video and audio streams by default, so encoded media stays unchanged.
- Removes metadata and checks C2PA and known text markers.
- Does not detect invisible watermarks or predict platform labels.

## Before → After

| Before | After |
| --- | --- |
| `video.mp4` | `video-clean.mp4` |
| Metadata, C2PA, or known AI markers may be present | Explicit local signals are removed or reported |
| Original video and audio | Video and audio copied unchanged by default |

The tool re-audits the output and fails if local signals remain or quality checks do not pass.

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
