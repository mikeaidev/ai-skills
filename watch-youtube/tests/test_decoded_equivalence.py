# SPDX-License-Identifier: MIT
"""Synthesize disposable inputs for decoded-content and timing regressions."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from decoded_equivalence import compare_decoded, _verified_frame_durations


class ContainerFrameTimingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.timing = Path(self.temp.name) / "frames.txt"
        self.timing.write_text("pts=0|pkt_duration=N/A\npts=1|pkt_duration=1\n")
        self.stream = {"index": 0, "time_base": "1/10"}
        self.proof = {"status": "verified", "source": {"sha256": "a" * 64}, "streams": [
            {"stream_index": 0, "time_base": "1/10", "samples": [
                {"pts": 0, "duration": 1}, {"pts": 1, "duration": 1}]}]}

    def test_duration_evidence_requires_complete_one_to_one_native_pts(self):
        values, result = _verified_frame_durations(self.proof, self.stream, self.timing, "a" * 64)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["filled_values"], 1)
        self.assertEqual(len(values), 2)
        self.timing.write_text("pts=0|pkt_duration=N/A\n")
        values, result = _verified_frame_durations(self.proof, self.stream, self.timing, "a" * 64)
        self.assertEqual(values, {})
        self.assertEqual(result["status"], "rejected")

    def test_conflicting_duration_duplicate_pts_or_wrong_source_are_rejected(self):
        for text in ("pts=0|pkt_duration=2\npts=1|pkt_duration=1\n",
                     "pts=0|pkt_duration=N/A\npts=0|pkt_duration=1\n",
                     "pts=0|pkt_duration=N/A\npts=2|pkt_duration=1\n"):
            self.timing.write_text(text)
            values, result = _verified_frame_durations(self.proof, self.stream, self.timing, "a" * 64)
            self.assertEqual(values, {})
            self.assertEqual(result["status"], "rejected")
        self.assertEqual(_verified_frame_durations(self.proof, self.stream, self.timing, "b" * 64)[1]["status"], "rejected")

    def test_no_optional_evidence_does_not_claim_recovery(self):
        values, result = _verified_frame_durations(None, self.stream, self.timing, "a" * 64)
        self.assertEqual(values, {})
        self.assertEqual(result["status"], "not_requested")


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-nostdin", "-n", *map(str, args)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools required")
class DecodedTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def video(self, name="source.mkv"):
        path = self.root / name
        ffmpeg("-f", "lavfi", "-i", "testsrc2=s=64x48:r=24:d=0.5", "-c:v", "ffv1", path)
        return path

    def audio(self, name="source.wav"):
        path = self.root / name
        ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.2", "-c:a", "pcm_s16le", path)
        return path

    def compare(self, source, output, kind, **options):
        return compare_decoded(source, output, kind, self.root / "diagnostics", 60,
                               options.get("max_output_bytes", 2 * 1024 * 1024))

    def test_video_remux_preserves_native_pictures_and_timeline(self):
        source = self.video()
        output = self.root / "copy.mkv"
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        ffmpeg("-copyts", "-i", source, "-map", "0:v:0", "-c", "copy", output)
        result = self.compare(source, output, "video")
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["decoded_frame_counts"], {"source": 12, "output": 12})
        self.assertTrue(result["inputs_unchanged"])
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    def test_av1_fragmented_decode_retains_all_delayed_frame_timings(self):
        source = self.root / "fragmented-av1.mp4"
        output = self.root / "copied-av1.mkv"
        ffmpeg("-f", "lavfi", "-i", "testsrc2=s=64x48:r=24:d=1", "-c:v", "libaom-av1", "-cpu-used", "8",
               "-crf", "45", "-b:v", "0", "-movflags", "empty_moov+frag_keyframe+default_base_moof", source)
        ffmpeg("-copyts", "-i", source, "-map", "0:v:0", "-c", "copy", output)
        result = self.compare(source, output, "video")
        self.assertEqual(result["decoded_frame_counts"], {"source": 24, "output": 24}, result)
        self.assertEqual(result["content_equality"], "passed", result)
        self.assertIn(result["status"], ("passed", "needs_review"), result)
        timing = self.root / "diagnostics" / "decoded-video" / "source-frames.stdout"
        self.assertEqual(sum("pts=" in line for line in timing.read_text().splitlines()), 24)

    def test_changed_video_picture_fails(self):
        source = self.video()
        output = self.root / "changed.mkv"
        ffmpeg("-i", source, "-vf", "negate", "-c:v", "ffv1", output)
        result = self.compare(source, output, "video")
        self.assertEqual(result["content_equality"], "failed", result)
        self.assertEqual(result["status"], "failed")

    def test_video_timestamp_shift_does_not_hide_behind_equal_pictures(self):
        source = self.video()
        output = self.root / "shifted.mkv"
        ffmpeg("-copyts", "-itsoffset", "0.1", "-i", source, "-map", "0:v:0", "-c", "copy", output)
        result = self.compare(source, output, "video")
        self.assertEqual(result["content_equality"], "passed", result)
        self.assertEqual(result["timeline_equivalence"], "failed", result)
        self.assertEqual(result["status"], "failed")

    def test_missing_video_frame_fails(self):
        source = self.video()
        output = self.root / "short.mkv"
        ffmpeg("-i", source, "-frames:v", "11", "-c", "copy", output)
        result = self.compare(source, output, "video")
        self.assertEqual(result["content_equality"], "failed", result)
        self.assertEqual(result["decoded_frame_counts"], {"source": 12, "output": 11})

    def test_native_pcm_with_different_decode_chunk_boundaries(self):
        source = self.audio()
        output = self.root / "copy.mka"
        ffmpeg("-copyts", "-i", source, "-map", "0:a:0", "-c", "copy", output)
        result = self.compare(source, output, "audio")
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["decoded_sample_frames"], {"source": 9600, "output": 9600})
        self.assertEqual(result["pcm_sha256"]["source"], result["pcm_sha256"]["output"])

    def test_pcm_identity_survives_different_packet_boundaries(self):
        source = self.audio()
        output = self.root / "packetized.mka"
        ffmpeg("-copyts", "-i", source, "-af", "asetnsamples=n=257:p=0", "-c:a", "pcm_s16le", output)
        result = self.compare(source, output, "audio")
        self.assertEqual(result["content_equality"], "passed", result)
        self.assertIn(result["status"], ("passed", "needs_review"), result)
        self.assertNotEqual(result["decoded_chunk_counts"]["source"], result["decoded_chunk_counts"]["output"])
        self.assertEqual(result["decoded_sample_frames"], {"source": 9600, "output": 9600})

    def test_opus_decoder_preskip_and_final_discard_are_retained(self):
        source = self.root / "opus.webm"
        output = self.root / "opus-copy.mka"
        ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.2", "-c:a", "libopus", source)
        ffmpeg("-copyts", "-i", source, "-map", "0:a:0", "-c", "copy", "-avoid_negative_ts", "disabled", output)
        result = self.compare(source, output, "audio")
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["decoded_sample_frames"], {"source": 9600, "output": 9600})
        self.assertGreater(result["stream_metadata"][0]["initial_padding"], 0)

    def test_audio_timestamp_shift_is_independent_of_sample_identity(self):
        source = self.audio()
        output = self.root / "shifted.mka"
        ffmpeg("-copyts", "-itsoffset", "0.1", "-i", source, "-map", "0:a:0", "-c", "copy", output)
        result = self.compare(source, output, "audio")
        self.assertEqual(result["content_equality"], "passed", result)
        self.assertEqual(result["timeline_equivalence"], "failed", result)

    def test_changed_audio_samples_fail(self):
        source = self.audio()
        output = self.root / "changed.wav"
        ffmpeg("-i", source, "-af", "volume=0.5", "-c:a", "pcm_s16le", output)
        result = self.compare(source, output, "audio")
        self.assertEqual(result["content_equality"], "failed", result)
        self.assertEqual(result["status"], "failed")

    def test_float_pcm_keeps_native_float_representation(self):
        source = self.root / "float.wav"
        output = self.root / "float-copy.wav"
        ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.2", "-c:a", "pcm_f32le", source)
        ffmpeg("-i", source, "-c", "copy", output)
        result = self.compare(source, output, "audio")
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["canonical_pcm"]["codec"], "pcm_f32le")

    def test_diagnostic_budget_is_review_not_corruption(self):
        source = self.video()
        result = self.compare(source, source, "video", max_output_bytes=16)
        self.assertEqual(result["status"], "needs_review", result)
        self.assertIn("budget", result["errors"][0])

    def test_existing_diagnostics_are_not_overwritten(self):
        source = self.video()
        first = self.compare(source, source, "video")
        self.assertEqual(first["status"], "passed", first)
        directory = self.root / "diagnostics" / "decoded-video"
        before = {p.name: p.read_bytes() for p in directory.iterdir()}
        result = self.compare(source, source, "video")
        self.assertEqual(result["status"], "refused", result)
        self.assertEqual({p.name: p.read_bytes() for p in directory.iterdir()}, before)

    def test_cli_runs_without_session_specific_imports(self):
        source = self.audio()
        process = subprocess.run([
            sys.executable, "-B", str(SCRIPTS / "decoded_equivalence.py"),
            "--source", str(source), "--output", str(source), "--kind", "audio",
            "--output-dir", str(self.root / "cli-results"),
        ], capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        self.assertIn('"status": "passed"', process.stdout)

    def test_missing_inputs_report_failure(self):
        result = self.compare(self.root / "missing", self.root / "missing2", "audio")
        self.assertEqual(result["status"], "failed")


if __name__ == "__main__":
    unittest.main()
