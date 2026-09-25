"""End-to-end checks for the upload, inspection and download flow."""

from io import BytesIO
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import web


@unittest.skipUnless(shutil.which("exiftool") and shutil.which("c2patool"),
                     "Media inspection tools are required")
class WebFlowTests(unittest.TestCase):
    def setUp(self):
        self.storage = tempfile.TemporaryDirectory()
        self.jobs_patch = patch.object(web, "JOBS", Path(self.storage.name))
        self.jobs_patch.start()
        self.client = web.app.test_client()

    def tearDown(self):
        self.jobs_patch.stop()
        self.storage.cleanup()

    def test_image_is_inspected_cleaned_verified_and_downloaded(self):
        source = BytesIO()
        exif = Image.Exif()
        exif[305] = "Dreamina"
        Image.new("RGB", (12, 8), "red").save(source, "JPEG", exif=exif)
        source.seek(0)

        inspection = self.client.post("/inspect", data={"file": (source, "photo.jpg")})
        self.assertEqual(inspection.status_code, 200)
        self.assertEqual(inspection.json["kind"], "image")
        self.assertEqual(inspection.json["inspection"]["headline"], "Indices d'IA détectés")
        job_id = inspection.json["job_id"]

        result = self.client.post(f"/clean/{job_id}", json={"quality": 90})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["result"]["headline"], "Contrôle local réussi")
        download = self.client.get(result.json["download_url"])
        self.assertEqual(download.status_code, 200)
        download.close()
        preview = self.client.get(result.json["file_url"])
        with Image.open(BytesIO(preview.data)) as image:
            self.assertEqual(image.format, "WEBP")
            self.assertEqual(image.size, (12, 8))
        preview.close()

    def test_ordinary_image_fields_do_not_count_as_ai_detection(self):
        source = BytesIO()
        Image.new("RGB", (8, 8), "blue").save(source, "PNG")
        source.seek(0)
        inspection = self.client.post("/inspect", data={"file": (source, "plain.png")})
        self.assertEqual(inspection.status_code, 200)
        self.assertEqual(inspection.json["inspection"]["headline"],
                         "Aucun indice local d'IA détecté")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                         "Video tools are required")
    def test_video_detection_and_final_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "clip.mp4"
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                 "-i", "color=c=red:s=32x32:d=0.2", "-metadata", "comment=Dreamina",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(source)],
                check=True,
            )
            with source.open("rb") as video:
                inspection = self.client.post("/inspect", data={"file": (video, "clip.mp4")})

        self.assertEqual(inspection.status_code, 200)
        self.assertEqual(inspection.json["inspection"]["headline"],
                         "Indices de provenance détectés")
        result = self.client.post(f"/clean/{inspection.json['job_id']}",
                                  json={"reencode": True})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["result"]["headline"], "Contrôle local réussi")
        preview = self.client.get(result.json["file_url"])
        self.assertTrue(preview.data.startswith(b"\x00\x00"))
        preview.close()


if __name__ == "__main__":
    unittest.main()
