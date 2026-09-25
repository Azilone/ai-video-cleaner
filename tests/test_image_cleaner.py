"""Checks for the image path exposed through the existing command."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest

try:
    from PIL import Image
except ImportError:
    Image = None

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("video_cleaner", ROOT / "ai-video-cleaner.py")
cleaner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleaner)


@unittest.skipUnless(Image and shutil.which("exiftool") and shutil.which("c2patool"),
                     "Pillow, exiftool and c2patool are required")
class ImageCleanerTests(unittest.TestCase):
    def test_same_command_removes_metadata_and_applies_orientation(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / "portrait.jpg"
            image = Image.new("RGB", (12, 8), (30, 150, 210))
            exif = Image.Exif()
            exif[305] = "Dreamina export"
            exif[274] = 6
            image.save(source, exif=exif)
            report_path = directory / "audit.json"
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cleaner.main([str(source), "--report", str(report_path)]), 0)
            output = directory / "portrait-clean.webp"
            self.assertTrue(output.is_file())
            report = json.loads(report_path.read_text())
            self.assertIn("IFD0:Software", report["source"]["metadata_tags"])
            self.assertEqual(report["output"]["metadata_tags"], [])
            self.assertEqual(report["output"]["c2pa"], "absent")
            self.assertEqual((report["output"]["width"], report["output"]["height"]), (8, 12))
            self.assertFalse({"EXIF", "XMP ", "ICCP"} & set(report["output"]["webp_chunks"]))
            self.assertTrue(source.is_file())
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cleaner.main([str(source)]), 0)
            self.assertTrue((directory / "portrait-clean-2.webp").is_file())

    def test_transparency_and_lossless_output(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / "transparent.png"
            Image.new("RGBA", (6, 4), (12, 34, 56, 0)).save(source)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cleaner.main([str(source), "--lossless"]), 0)
            with Image.open(directory / "transparent-clean.webp") as result:
                self.assertEqual(result.mode, "RGBA")
                self.assertEqual(result.getpixel((0, 0))[3], 0)

    def test_rejects_animated_image_without_output(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / "animated.gif"
            frames = [Image.new("RGB", (4, 4), color) for color in ("red", "blue")]
            frames[0].save(source, save_all=True, append_images=frames[1:], duration=100)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cleaner.main([str(source)]), 1)
            self.assertFalse((directory / "animated-clean.webp").exists())


if __name__ == "__main__":
    unittest.main()
