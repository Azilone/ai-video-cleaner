"""Unit and sample-video checks for local auditing and lossless remuxing."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "ai-video-cleaner.py"
SAMPLE = SCRIPT.parent / "ai-video" / "ai-video.mp4"
SPEC = importlib.util.spec_from_file_location("video_cleaner", SCRIPT)
cleaner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleaner)


class CleanerTests(unittest.TestCase):
    def test_date_rejects_invalid_calendar_date(self):
        with self.assertRaises(cleaner.CleanerError):
            cleaner.parse_date("2026-02-30T06:17:20Z")
        with self.assertRaises(cleaner.CleanerError):
            cleaner.parse_date("2026-09-24T06:17:20+07:00")

    def test_box_scan_and_marker_at_chunk_boundary(self):
        prefix = b"\x00\x00\x00\x10ftypisom\x00\x00\x00\x00"
        payload = b"x" * (1024 * 1024 - len(prefix) - 3) + b"See" + b"dance"
        mdat = struct.pack(">I4s", len(payload) + 8, b"mdat") + payload
        moov = struct.pack(">I4s", 8, b"moov")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.mp4"
            path.write_bytes(prefix + mdat + moov)
            boxes, brand, markers = cleaner.inspect_mp4(path)
        self.assertEqual([box["type"] for box in boxes], ["ftyp", "mdat", "moov"])
        self.assertEqual(brand, "isom")
        self.assertEqual(markers, {"Seedance": 1})

    def test_box_scan_rejects_overrun(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.mp4"
            path.write_bytes(struct.pack(">I4s", 100, b"mdat"))
            with self.assertRaises(cleaner.CleanerError):
                cleaner.inspect_mp4(path)

    def test_c2pa_present_absent_and_error(self):
        cases = [
            (1, "", "Error: No claim found", "absent"),
            (0, '{"claim": {}}', "", "present"),
            (2, "", "invalid manifest", "error"),
        ]
        for code, stdout, stderr, expected in cases:
            with self.subTest(expected=expected), mock.patch.object(
                    cleaner, "run", return_value=subprocess.CompletedProcess(
                        [], code, stdout, stderr)):
                self.assertEqual(cleaner.inspect_c2pa(Path("video.mp4"))["status"], expected)

    def test_exif_metadata_and_tool_error(self):
        output = json.dumps([{"System:FileName": "video.mp4",
                              "QuickTime:Title": "Dreamina export"}])
        with mock.patch.object(cleaner, "run", return_value=subprocess.CompletedProcess(
                [], 0, output, "")):
            self.assertEqual(cleaner.inspect_exif(Path("video.mp4")),
                             {"QuickTime:Title": "Dreamina export"})
        with mock.patch.object(cleaner, "run", return_value=subprocess.CompletedProcess(
                [], 1, "", "failed")):
            with self.assertRaises(cleaner.CleanerError):
                cleaner.inspect_exif(Path("video.mp4"))

    def test_verdict_distinguishes_present_absent_and_incomplete(self):
        signals = {name: cleaner.signal("absent") for name in cleaner.LOCAL_SIGNALS}
        tools = {name: cleaner.signal("ok") for name in ("ffprobe", "exiftool", "mp4")}
        self.assertEqual(cleaner.verdict(signals, tools), "local_signals_absent")
        signals["c2pa"] = cleaner.signal("present")
        self.assertEqual(cleaner.verdict(signals, tools), "signals_present")
        signals["c2pa"] = cleaner.signal("error")
        self.assertEqual(cleaner.verdict(signals, tools), "verification_incomplete")

    def test_display_geometry_accounts_for_rotation(self):
        portrait = {"width": 480, "height": 854,
                    "side_data_list": [{"rotation": 90}]}
        landscape = {"width": 854, "height": 480}
        self.assertEqual(cleaner.display_geometry(portrait),
                         cleaner.display_geometry(landscape))


@unittest.skipUnless(SAMPLE.exists() and all(shutil.which(tool) for tool in
                    ("ffmpeg", "ffprobe", "exiftool", "c2patool")),
                    "sample video and media tools are required")
class IntegrationTests(unittest.TestCase):
    def test_one_argument_copies_streams_and_creates_report_sheet(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / "clip.mp4"
            shutil.copyfile(SAMPLE, source)

            def report_dir():
                return Path(tempfile.mkdtemp(prefix="reports-", dir=directory))

            logs = io.StringIO()
            with mock.patch.object(cleaner, "make_report_directory",
                                   side_effect=report_dir), \
                    contextlib.redirect_stderr(logs):
                self.assertEqual(cleaner.main([str(source)]), 0)
            output = directory / "clip-clean.mp4"
            self.assertTrue(output.is_file())
            self.assertEqual(source.stat().st_size, SAMPLE.stat().st_size)
            reports = list(directory.glob("reports-*/audit.json"))
            self.assertEqual(len(reports), 1)
            result = json.loads(reports[0].read_text())
            self.assertEqual(result["comparison"]["status"], "ok")
            self.assertEqual(result["steps"], ["remux_copy"])
            self.assertNotIn("visual_adjustments", result)
            self.assertEqual(result["content_change"], "encoded_video_unchanged")
            before = result["comparison"]["source_stream_hashes"]
            after = result["comparison"]["output_stream_hashes"]
            self.assertEqual(before, after)
            self.assertEqual(result["source"]["streams"][1]["width"],
                             result["output"]["streams"][0]["width"])
            self.assertEqual(cleaner.display_rotation(result["output"]["streams"][0]), 90)
            self.assertEqual(result["contact_sheet"]["frames"], 6)
            self.assertTrue(Path(result["contact_sheet"]["path"]).is_file())
            self.assertIn("Input", logs.getvalue())
            self.assertIn("Video", logs.getvalue())
            self.assertIn("Report", logs.getvalue())

            with mock.patch.object(cleaner, "make_report_directory",
                                   side_effect=report_dir), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cleaner.main([str(source)]), 0)
            self.assertTrue((directory / "clip-clean-2.mp4").is_file())

    def test_inspect_and_copy_video_preserve_video_and_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            report = directory / "source.json"
            with contextlib.redirect_stderr(io.StringIO()):
                code = cleaner.main([str(SAMPLE), "--inspect-only", "--report", str(report)])
            self.assertEqual(code, 0)
            source_report = json.loads(report.read_text())
            self.assertEqual(source_report["source"]["signals"]["c2pa"]["status"], "absent")
            self.assertEqual(source_report["source"]["signals"]["byte_markers"]["status"], "present")

            output = directory / "result.mp4"
            with contextlib.redirect_stderr(io.StringIO()):
                code = cleaner.main([str(SAMPLE), "-o", str(output),
                                     "--copy-video", "--report",
                                     str(output.with_suffix(".audit.json"))])
            self.assertEqual(code, 0)
            result = json.loads(output.with_suffix(".audit.json").read_text())
            self.assertEqual(result["verdict"], "local_signals_absent")
            self.assertEqual(result["steps"], ["remux_copy"])
            self.assertEqual(result["content_change"], "encoded_video_unchanged")
            self.assertEqual(result["comparison"]["status"], "ok")
            self.assertEqual(result["output"]["format"]["filename"], str(output.resolve()))
            self.assertEqual(result["comparison"]["source_stream_hashes"],
                             result["comparison"]["output_stream_hashes"])
            self.assertEqual([s["codec_type"] for s in result["output"]["streams"]],
                             ["video", "audio"])

    def test_ignore_erroneous_rotation_keeps_portrait_pixels_without_reencoding(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "upright.mp4"
            report = Path(directory) / "upright.json"
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cleaner.main([str(SAMPLE), "--ignore-rotation",
                                               "-o", str(output), "--report", str(report)]), 0)
            result = json.loads(report.read_text())
            self.assertEqual(result["steps"], ["remux_copy", "rotation_metadata_ignored"])
            self.assertEqual(result["comparison"]["status"], "ok")
            self.assertEqual(result["comparison"]["source_stream_hashes"],
                             result["comparison"]["output_stream_hashes"])
            video = result["output"]["streams"][0]
            self.assertEqual((video["width"], video["height"]), (480, 854))
            self.assertEqual(cleaner.display_rotation(video), 0)

    def test_contact_sheet_is_self_contained_and_does_not_modify_video(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            sheet = directory / "frames.html"
            report = directory / "source.json"
            before = SAMPLE.stat().st_size
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cleaner.main([str(SAMPLE), "--inspect-only",
                                               "--contact-sheet", str(sheet),
                                               "--report", str(report)]), 0)
            self.assertEqual(SAMPLE.stat().st_size, before)
            page = sheet.read_text(encoding="utf-8")
            self.assertIn("data:image/jpeg;base64,", page)
            self.assertIn("≈ 0 s", page)
            self.assertIn("≈ 5 s", page)
            self.assertEqual(json.loads(report.read_text())["contact_sheet"]["frames"], 6)

    def test_copy_video_and_drop_audio_keep_video_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "mute.mp4"
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cleaner.main([str(SAMPLE), "-o", str(output),
                                               "--copy-video", "--drop-audio", "--report",
                                               str(output.with_suffix(".audit.json"))]), 0)
            result = json.loads(output.with_suffix(".audit.json").read_text())
            self.assertEqual([s["codec_type"] for s in result["output"]["streams"]],
                             ["video"])
            self.assertEqual(result["comparison"]["source_stream_hashes"],
                             result["comparison"]["output_stream_hashes"])

    def test_requested_reencode_changes_video_and_preserves_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "reencoded.mp4"
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cleaner.main([str(SAMPLE), "-o", str(output),
                                               "--reencode", "--report",
                                               str(output.with_suffix(".audit.json"))]), 0)
            result = json.loads(output.with_suffix(".audit.json").read_text())
            self.assertEqual(result["steps"], ["reencode_requested", "color_boost"])
            self.assertEqual(result["content_change"], "video_reencoded")
            self.assertEqual(result["comparison"]["status"], "ok")
            before = result["comparison"]["source_stream_hashes"]
            after = result["comparison"]["output_stream_hashes"]
            self.assertNotEqual(before[0], after[0])
            self.assertEqual(before[1:], after[1:])

    def test_default_color_boost_changes_decoded_image_and_preserves_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            baseline = directory / "baseline.mp4"
            boosted = directory / "boosted.mp4"
            report = directory / "boosted.audit.json"
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(SAMPLE),
                            "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264",
                            "-crf", "18", "-preset", "slow", "-c:a", "copy",
                            "-map_metadata", "-1", "-map_metadata:s", "-1",
                            "-map_chapters", "-1", "-sn", "-dn", "-f", "mp4",
                            str(baseline)], check=True, capture_output=True)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cleaner.main([str(SAMPLE), "-o", str(boosted), "--reencode",
                                               "--report", str(report)]), 0)
            result = json.loads(report.read_text())
            self.assertEqual(result["steps"], ["reencode_requested", "color_boost"])
            self.assertEqual(result["visual_adjustments"], {"saturation_multiplier": 1.04})
            self.assertEqual(result["comparison"]["status"], "ok")
            self.assertEqual(cleaner.stream_hashes(SAMPLE)[1:],
                             cleaner.stream_hashes(boosted)[1:])

            def first_frame(path):
                return subprocess.run(["ffmpeg", "-v", "error", "-i", str(path),
                                       "-frames:v", "1", "-pix_fmt", "rgb24",
                                       "-f", "rawvideo", "-"], check=True,
                                      capture_output=True).stdout

            self.assertNotEqual(first_frame(baseline), first_frame(boosted))

    def test_visible_option_does_not_edit_when_no_stable_mark(self):
        original_run = cleaner.run

        def run_with_no_mark(command):
            if command[:3] == ["remove-ai-watermarks", "video", "visible"]:
                return subprocess.CompletedProcess(command, 2,
                                                   "No stable seedance watermark detected", "")
            return original_run(command)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.mp4"
            with mock.patch.object(cleaner, "inspect_known_provenance",
                                   return_value=(cleaner.signal("absent"),
                                                 cleaner.signal("absent"))), \
                    mock.patch.object(cleaner, "run", side_effect=run_with_no_mark), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cleaner.main([str(SAMPLE), "-o", str(output),
                                               "--remove-visible-seedance", "--report",
                                               str(output.with_suffix(".audit.json"))]), 0)
            result = json.loads(output.with_suffix(".audit.json").read_text())
            self.assertIn("visible_seedance_not_found", result["steps"])
            self.assertEqual(result["comparison"]["status"], "ok")

    def test_explicit_metadata_is_reported_then_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            tagged = directory / "tagged.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(SAMPLE),
                            "-map", "0:v:0", "-c", "copy", "-metadata",
                            "comment=Dreamina export", "-y", str(tagged)],
                           check=True, capture_output=True)
            output = directory / "result.mp4"
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cleaner.main([str(tagged), "-o", str(output),
                                               "--report",
                                               str(output.with_suffix(".audit.json"))]), 0)
            result = json.loads(output.with_suffix(".audit.json").read_text())
            self.assertEqual(result["source"]["signals"]["metadata"]["status"], "present")
            self.assertEqual(result["output"]["signals"]["metadata"]["status"], "absent")
            self.assertEqual(result["comparison"]["signal_changes"]["metadata"],
                             {"source": "present", "output": "absent"})

    def test_persistent_signal_in_copy_mode_does_not_trigger_reencode(self):
        original_audit = cleaner.audit
        original_which = shutil.which

        def audit_with_persistent_marker(path, display_path=None, check_visible=False):
            result = original_audit(path, display_path, check_visible)
            if display_path is not None:
                result["signals"]["byte_markers"] = cleaner.signal(
                    "present", {"test_marker": 1})
                result["verdict"] = cleaner.verdict(result["signals"], result["tools"])
            return result

        def which_without_optional_tool(name):
            return None if name == "remove-ai-watermarks" else original_which(name)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.mp4"
            report = Path(directory) / "result.audit.json"
            with mock.patch.object(cleaner, "audit", side_effect=audit_with_persistent_marker), \
                    mock.patch.object(cleaner.shutil, "which",
                                      side_effect=which_without_optional_tool), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(cleaner.CleanerError):
                    cleaner.main([str(SAMPLE), "-o", str(output), "--copy-video",
                                  "--report", str(report)])
            result = json.loads(report.read_text())
            self.assertEqual(result["steps"], ["remux_copy"])
            self.assertEqual(result["verdict"], "signals_present")
            self.assertFalse(output.exists())

    def test_visible_branch_marks_attempt_and_requires_review(self):
        original_run = cleaner.run

        def provenance(path, visible=False):
            if visible and Path(path) == SAMPLE:
                return cleaner.signal("absent"), cleaner.signal("present", ["seedance"])
            return cleaner.signal("absent"), cleaner.signal(
                "absent" if visible else "unverified")

        def run_with_local_edit(command):
            if command[:3] == ["remove-ai-watermarks", "video", "visible"]:
                cleaner.ffmpeg_output(Path(command[3]), Path(command[-1]), False,
                                      None, reencode=True)
                return subprocess.CompletedProcess(command, 0,
                                                   "Removed seedance watermark from 1/145 frames", "")
            return original_run(command)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.mp4"
            with mock.patch.object(cleaner, "inspect_known_provenance",
                                   side_effect=provenance), \
                    mock.patch.object(cleaner, "run", side_effect=run_with_local_edit), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cleaner.main([str(SAMPLE), "-o", str(output),
                                               "--remove-visible-seedance", "--report",
                                               str(output.with_suffix(".audit.json"))]), 0)
            result = json.loads(output.with_suffix(".audit.json").read_text())
            self.assertIn("visible_seedance_removed", result["steps"])
            self.assertEqual(result["content_change"], "visible_correction_attempted")
            self.assertEqual(result["comparison"]["status"], "ok")
            self.assertEqual(result["visible_action"]["manual_region_review"], "required")


if __name__ == "__main__":
    unittest.main()
