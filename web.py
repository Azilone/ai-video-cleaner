"""Minimal two-step web interface for the existing media cleaners."""

import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from flask import Flask, jsonify, render_template, request, send_file, url_for
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename


ROOT = Path(__file__).resolve().parent
JOBS = Path(tempfile.gettempdir()) / "ai-cleaner-web-jobs"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff",
                    ".bmp", ".gif", ".avif", ".heic", ".heif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v"}
MAX_UPLOAD_BYTES = 500 * 1024 * 1024
JOB_LIFETIME = 60 * 60
VIDEO_SIGNALS = (
    ("metadata", "Métadonnées IA"),
    ("c2pa", "Signature C2PA"),
    ("byte_markers", "Marqueurs dans le fichier"),
    ("known_provenance", "Provenance connue"),
    ("visible_mark", "Marque visible"),
    ("invisible_watermark", "Filigrane invisible"),
    ("platform_classifier", "Détecteur de plateforme"),
    ("audio_watermark", "Filigrane audio"),
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES + 1024 * 1024


def prune_jobs():
    JOBS.mkdir(mode=0o700, exist_ok=True)
    cutoff = time.time() - JOB_LIFETIME
    for directory in JOBS.iterdir():
        if directory.is_dir() and directory.stat().st_mtime < cutoff:
            shutil.rmtree(directory, ignore_errors=True)


def job_path(job_id):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return None
    directory = JOBS / job_id
    return directory if (directory / "job.json").is_file() else None


def read_job(directory):
    return json.loads((directory / "job.json").read_text(encoding="utf-8"))


def run_cleaner(arguments, timeout):
    return subprocess.run(
        [sys.executable, str(ROOT / "ai-video-cleaner.py"), *arguments],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def failure_message(result):
    detail = result.stderr or ""
    app.logger.warning("Cleaning failed: %s", detail[-2000:])
    if "animated or multipage" in detail:
        return "Les images animées ne sont pas prises en charge."
    if "not available on PATH" in detail or "are required on PATH" in detail:
        return "Le serveur n'a pas tous les outils nécessaires."
    return "Le fichier n'a pas pu être traité."


def signal_status(value):
    return {
        "present": "Détecté", "absent": "Absent", "unverified": "Non vérifié",
        "error": "Erreur", "ok": "OK",
    }.get(value, "Non vérifié")


def signal_details(value):
    evidence = value.get("evidence") or []
    if isinstance(evidence, dict):
        return ", ".join(str(key) for key in evidence)[:180]
    if isinstance(evidence, list):
        pieces = []
        for item in evidence:
            if isinstance(item, dict):
                pieces.extend(str(key) for key in item if key != "raw_output")
            elif isinstance(item, str) and not item.startswith(("c2patool:", "no ", "optional ")):
                pieces.append(item)
        return ", ".join(pieces)[:180]
    return ""


def video_summary(media):
    signals = media.get("signals", {})
    rows = []
    for key, label in VIDEO_SIGNALS:
        value = signals.get(key, {})
        rows.append({"label": label, "status": signal_status(value.get("status")),
                     "detail": signal_details(value) if value.get("status") == "present" else ""})
    verdict = media.get("verdict")
    if verdict == "signals_present":
        headline, tone = "Indices de provenance détectés", "warning"
    elif verdict == "local_signals_absent":
        headline, tone = "Aucun indice local détecté", "success"
    else:
        headline, tone = "Analyse incomplète", "warning"
    return {"headline": headline, "tone": tone, "rows": rows,
            "note": "Les indices locaux ne prouvent pas à eux seuls qu'une vidéo est créée par IA."}


def image_summary(media):
    tags = media.get("metadata_tags", [])
    markers = media.get("ai_markers", [])
    c2pa = media.get("c2pa", "error")
    chunks = set(media.get("webp_chunks", []))
    rows = [
        {"label": "Indices d'IA", "status": "Détecté" if markers else "Absent",
         "detail": ", ".join(markers[:8])},
        {"label": "Métadonnées", "status": f"{len(tags)} champ(s)",
         "detail": ", ".join(tags[:8])},
        {"label": "Signature C2PA", "status": signal_status(c2pa), "detail": ""},
    ]
    if media.get("format") == "WEBP":
        rows.append({"label": "Blocs EXIF / XMP / ICC", "status": "Détecté" if
                     {"EXIF", "XMP ", "ICCP"} & chunks else "Absent", "detail": ""})
    if markers:
        headline, tone = "Indices d'IA détectés", "warning"
    elif c2pa == "present":
        headline, tone = "Signature de provenance détectée", "warning"
    elif c2pa == "absent":
        headline, tone = "Aucun indice local d'IA détecté", "success"
    else:
        headline, tone = "Analyse incomplète", "warning"
    return {"headline": headline, "tone": tone, "rows": rows,
            "note": "Les métadonnées ne prouvent pas à elles seules qu'une image est créée par IA."}


def summary(media, kind):
    return video_summary(media) if kind == "video" else image_summary(media)


def result_summary(report, kind):
    after = summary(report["output"], kind)
    if kind == "video":
        comparison = report.get("comparison") or {}
        after["rows"].append({"label": "Intégrité vidéo / audio",
                              "status": "OK" if comparison.get("status") == "ok" else "À vérifier",
                              "detail": "Durée, orientation et pistes contrôlées" if
                              comparison.get("status") == "ok" else ""})
        verified = (report.get("verdict") == "local_signals_absent" and
                    comparison.get("status") == "ok")
    else:
        verified = (not report["output"].get("metadata_tags") and
                    report["output"].get("c2pa") == "absent" and
                    not {"EXIF", "XMP ", "ICCP"} &
                    set(report["output"].get("webp_chunks", [])))
        after["rows"].append({"label": "Image convertie et vérifiée",
                              "status": "OK" if verified else "À vérifier", "detail": ""})
        after["rows"].append({"label": "Filigrane invisible / détecteur externe",
                              "status": "Non vérifié", "detail": ""})
    after["headline"] = "Contrôle local réussi" if verified else "Contrôle incomplet"
    after["tone"] = "success" if verified else "warning"
    after["note"] = "Les filigranes invisibles et les détecteurs de plateformes ne sont pas vérifiés."
    return after


@app.get("/")
def index():
    return render_template("index.html", max_mb=MAX_UPLOAD_BYTES // (1024 * 1024))


@app.errorhandler(RequestEntityTooLarge)
def too_large(_error):
    return jsonify(error="Ce fichier dépasse la limite de 500 Mo."), 413


@app.post("/inspect")
def inspect():
    prune_jobs()
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify(error="Choisissez une image ou une vidéo."), 400
    name = secure_filename(upload.filename)
    suffix = Path(name).suffix.lower()
    if suffix not in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS:
        return jsonify(error="Format non pris en charge."), 400

    kind = "image" if suffix in IMAGE_EXTENSIONS else "video"
    job_id = uuid4().hex
    directory = JOBS / job_id
    directory.mkdir(mode=0o700)
    source = directory / f"input{suffix}"
    try:
        upload.save(source)
        if not source.stat().st_size:
            return jsonify(error="Le fichier est vide."), 400
        if source.stat().st_size > MAX_UPLOAD_BYTES:
            return jsonify(error="Ce fichier dépasse la limite de 500 Mo."), 413
        (directory / "job.json").write_text(
            json.dumps({"name": name, "suffix": suffix, "kind": kind}), encoding="utf-8")
        report = directory / "inspect.json"
        try:
            result = run_cleaner([str(source), "--inspect-only", "--report", str(report)], 300)
        except subprocess.TimeoutExpired:
            return jsonify(error="L'analyse a pris trop de temps."), 504
        if not report.is_file() or result.returncode not in (0, 3):
            return jsonify(error=failure_message(result)), 422
        media = json.loads(report.read_text(encoding="utf-8"))["source"]
        return jsonify(job_id=job_id, kind=kind, inspection=summary(media, kind))
    finally:
        if not (directory / "inspect.json").is_file():
            shutil.rmtree(directory, ignore_errors=True)


@app.post("/clean/<job_id>")
def clean(job_id):
    prune_jobs()
    directory = job_path(job_id)
    if directory is None:
        return jsonify(error="Fichier expiré. Envoyez-le à nouveau."), 404
    job = read_job(directory)
    source = directory / f"input{job['suffix']}"
    output_suffix = ".webp" if job["kind"] == "image" else ".mp4"
    output = directory / f"output{output_suffix}"
    report = directory / "clean.json"
    if output.exists():
        return jsonify(error="Ce fichier a déjà été nettoyé. Envoyez-le à nouveau."), 409
    options = request.get_json(silent=True) or {}
    if not isinstance(options, dict):
        return jsonify(error="Paramètres invalides."), 400
    args = [str(source), "--output", str(output), "--report", str(report)]
    if job["kind"] == "image":
        try:
            quality = int(options.get("quality", 95))
        except (ValueError, TypeError):
            return jsonify(error="Qualité invalide."), 400
        if not 0 <= quality <= 100:
            return jsonify(error="Qualité invalide."), 400
        args += ["--quality", str(quality)]
        if options.get("lossless") is True:
            args.append("--lossless")
    else:
        for key, flag in (("reencode", "--reencode"), ("drop_audio", "--drop-audio"),
                          ("ignore_rotation", "--ignore-rotation")):
            if options.get(key) is True:
                args.append(flag)
    try:
        result = run_cleaner(args, 900)
    except subprocess.TimeoutExpired:
        return jsonify(error="Le nettoyage a pris trop de temps."), 504
    if result.returncode or not output.is_file() or not report.is_file():
        return jsonify(error=failure_message(result)), 422
    data = json.loads(report.read_text(encoding="utf-8"))
    directory.touch()
    return jsonify(result=result_summary(data, job["kind"]),
                   file_url=url_for("file", job_id=job_id),
                   download_url=url_for("file", job_id=job_id, download=1),
                   download_name=f"{Path(job['name']).stem or 'fichier'}-clean{output_suffix}")


@app.get("/file/<job_id>")
def file(job_id):
    prune_jobs()
    directory = job_path(job_id)
    if directory is None:
        return jsonify(error="Fichier expiré. Envoyez-le à nouveau."), 404
    job = read_job(directory)
    suffix = ".webp" if job["kind"] == "image" else ".mp4"
    output = directory / f"output{suffix}"
    if not output.is_file():
        return jsonify(error="Aucun fichier nettoyé disponible."), 404
    return send_file(output, mimetype="image/webp" if suffix == ".webp" else "video/mp4",
                     as_attachment=request.args.get("download") == "1",
                     download_name=f"{Path(job['name']).stem or 'fichier'}-clean{suffix}",
                     conditional=True)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000)
