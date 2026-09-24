# ai-video-cleaner

Remux mp4/mov/m4v (libx264 crf20 + aac 128k) qui nettoie les exports
Seedance / Dreamina (ByteDance) : brand ftyp `isom`, 1 track video,
dates 1904, mdat avant moov, pas de metadata, pas d'audio par défaut.

## Usage

```bash
python3 ai-video-cleaner.py INPUT [-o OUT] \
  [--create-dt 2026-09-24T06:17:20Z] \
  [--keep-audio] \
  [--show-report] \
  [--force]
```

| flag | |
|---|---|
| `INPUT` | input mp4/mov/m4v (positional) |
| `-o OUT` | output (défaut `<input>-clean.mp4`) |
| `--create-dt ISO` | re-métadater `creation_time` (défaut 1904 zéroé par `-map_metadata -1`) |
| `--keep-audio` | garde la piste audio (défaut `-an`) |
| `--show-report` | affiche le rapport (sinon stderr) |
| `--force` | overwrite sans demander |

## Boucle

1. **Pré-check** — `ffprobe` sur l'input : format `mp4/mov/m4a`,
   ≥1 video (`h264/hevc/mpeg4/vc1/vp8/vp9/av1`),
   `--keep-audio` exige ≥1 audio (`aac/ac3/mp3/vorbis`).
   Sinon exit 2.

2. **Remux** —
   ```
   ffmpeg -y -i INPUT \
     -c:v libx264 -crf 20 -preset medium \
     -c:a aac -b:a 128k \
     -map 0:v [-map 0:a si --keep-audio] \
     -map_metadata -1 \
     [-metadata creation_time=...] \
     -an -sn -dn \
     OUT
   ```

3. **Checks sur l'output** —
   - `ffprobe -show_format -show_streams -of json` → streams/dur/tags
   - `exiftool -a -G1 -s -n` → ftyp, mvhd, tracks, udta
   - box scan top-level python : `ftyp + mdat + moov (+ free)`
   - strings : grep -F des 8 marqueurs
    `Seedance Seedream Dreamina ByteDance CapCut C2PA "Content Credentials" "AI generated"`
   - `c2patool -o .c2patool OUT` → `No claim found`

4. **Rapport JSON** — `.ai-video-cleaner.json` dans le cwd +
   `--show-report`.

## Rapport

```json
{
  "input":  { "path", "size", "format", "tags", "streams": [...] },
  "output": { "path", "size", "streams", "boxes", "ftyp_brand",
              "creation_time", "strings_found" },
  "checks": { "ffprobe", "exiftool", "c2patool", "strings" },
  "verdict": "clean"
}
```

## Sample

`ai-video/` :

- `ai-video.mp4` — export Seedance/Dreamina (4,715,336 bytes,
  mp42, 1 audio aac 44.1k stereo + 1 video h264 High 480x854 @60fps,
  ~6.07s)
- `ai-video-clean.mp4` — sortie du script (867,598 bytes = 847KiB,
  ftyp `isom`, 1 track h264 854x480, mdat avant moov,
  x264 core 165 r3222)

Vérification :

```bash
python3 ai-video-cleaner.py ai-video/ai-video.mp4 \
  --create-dt 2026-09-24T06:17:20Z \
  --show-report
```

Attendu :

- output 847KiB, 1 track h264 854×480 60fps, dur 6.066992
- ftyp `isom`, brands `isomiso2avc1mp41`
- creation_time 2026-09-24T06:17:20Z
- 0x strings (8 marqueers)
- c2patool `No claim found`

## Assumptions

- `ffmpeg` + `ffprobe` + `exiftool` + `c2patool` en PATH
  (ffmpeg 9.0.1, exiftool 13.55, c2patool 0.27.22)
- python3 stdlib (3.9+)
- C2PA : pas de claim dans le MP4 brut → `No claim found`
  (claim côté export Dreamina)
- watermark invisible Seedance 2.5 : pas de détecteur public

## Exit codes

| code | |
|---|---|
| 0 | OK |
| 1 | bad flag / tool pas en PATH / pas d'overwrite |
| 2 | input pas mp4/mov/m4a / pas de video / codec inattendu |
| 3 | box scan top-level pas au EOF |
