# SPDX-License-Identifier: MIT
"""Offline validator regressions; synthesize disposable media with FFmpeg."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from validate_media import packet_summary, preservation, coverage, load_track_expectations, apply_mp4_duration_evidence, analysis_readiness


def stream(base="1/10"):
    return {"index": 0, "codec_type": "video", "codec_name": "vp8", "time_base": base,
            "avg_frame_rate": "10/1"}


def packets(ticks, duration=1):
    return [{"stream_index": 0, "pts": t, "dts": t, "duration": duration,
             "data_hash": "hash-" + str(t)} for t in ticks]


class TimingTests(unittest.TestCase):
    def test_container_timing_contradiction_blocks_analysis_readiness(self):
        stages = {name: {"status": "passed"} for name in ("probe", "decode", "native_coverage")}
        report = {"errors": [], "inputs": {"input": {"container_duration_evidence": {"status": "failed"}},
                                            "audio": {"container_duration_evidence": {"status": "verified"}}}}
        self.assertEqual(analysis_readiness(report, stages)["status"], "needs_review")
        for state in ("unsupported", "not_applicable"):
            report["inputs"]["input"]["container_duration_evidence"]["status"] = state
            self.assertEqual(analysis_readiness(report, stages)["status"], "passed")
        report["errors"].append("input changed")
        self.assertEqual(analysis_readiness(report, stages)["status"], "failed")

    def test_verified_mp4_fills_missing_duration_without_mutating_raw_probe(self):
        data = {"streams": [stream()], "packets": packets([0, 1])}
        del data["packets"][0]["duration"]
        original = json.dumps(data, sort_keys=True)
        proof = {"status": "verified", "source": {"sha256": "a" * 64}, "streams": [
            {"stream_index": 0, "track_id": 1, "time_base": "1/10", "verified_sample_count": 2,
             "updates": [{"packet_index": 0, "duration": 1}],
             "samples": [{"packet_index": 0, "pts": 0, "dts": 0, "duration": 1},
                         {"packet_index": 1, "pts": 1, "dts": 1, "duration": 1}]}]}
        effective = apply_mp4_duration_evidence(data, proof, "a" * 64)
        self.assertEqual(json.dumps(data, sort_keys=True), original)
        self.assertEqual(effective["packets"][0]["duration"], 1)
        self.assertEqual(effective["packets"][0]["duration_evidence"]["source_sha256"], "a" * 64)
        summary = packet_summary(effective["streams"][0], effective["packets"])
        self.assertEqual(summary["estimated_duration_packets"], 0)
        self.assertEqual(summary["container_verified_duration_packets"], 1)
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            apply_mp4_duration_evidence(data, proof, "b" * 64)
        data["packets"][0]["duration"] = 1
        with self.assertRaisesRegex(ValueError, "overwrite"):
            apply_mp4_duration_evidence(data, proof, "a" * 64)

    def test_unverified_container_evidence_never_changes_probe(self):
        data = {"streams": [stream()], "packets": packets([0])}
        del data["packets"][0]["duration"]
        for state in ("not_applicable", "unsupported", "needs_review", "failed"):
            self.assertIs(apply_mp4_duration_evidence(data, {"status": state}, "a" * 64), data)
            self.assertNotIn("duration", data["packets"][0])

    def test_audio_padding_never_excuses_a_missing_high_fps_video_frame(self):
        for rate in (60, 120):
            with self.subTest(rate=rate):
                video = {**stream("1/" + str(rate * 100)), "avg_frame_rate": str(rate) + "/1"}
                short = packet_summary(video, packets([n * 100 for n in range(rate - 1)], 100))
                result = coverage({"summaries": [short], "format": {}}, {"start": 0, "end": 1}, 0.020)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["streams"][0]["tail_tolerance_basis"]["observed_opus_padding_allowance"], 0)

    def test_estimated_durations_outside_requested_interval_remain_global(self):
        data = packets(range(40))
        del data[0]["duration"]
        summary = packet_summary(stream(), data)
        result = coverage({"summaries": [summary], "format": {}}, {"start": 2, "end": 3})
        self.assertEqual(result["status"], "covered_with_stated_tolerance")
        self.assertEqual(result["streams"][0]["global_integrity"]["status"], "needs_review")

    def test_gap_outside_requested_interval_is_not_an_interval_failure(self):
        summary = packet_summary(stream(), packets([n for n in range(40) if n not in (10, 11)]))
        result = coverage({"summaries": [summary], "format": {}}, {"start": 2, "end": 3})
        self.assertEqual(result["status"], "covered_with_stated_tolerance")
        self.assertEqual(result["streams"][0]["gaps_in_requested_interval"], [])
        self.assertEqual(result["streams"][0]["global_integrity"]["status"], "needs_review")

    def test_unequal_declared_track_extents_and_explicit_delay_mapping(self):
        video = packet_summary(stream(), packets(range(40)))
        audio_stream = {"index": 1, "codec_type": "audio", "codec_name": "opus", "time_base": "1/48000", "sample_rate": "48000", "initial_padding": 312}
        audio_packets = [{**p, "stream_index": 1} for p in packets([-312 + n * 960 for n in range(201)], 960)]
        audio = packet_summary(audio_stream, audio_packets)
        declarations = {
            "video": {"start": "0", "end": "4", "timestamp_offset": "0", "source": "declared source-track extent", "coordinate": "source_packet"},
            "audio": {"start": "0", "end": "201/50", "timestamp_offset": "13/2000", "source": "declared extent and codec-delay mapping", "coordinate": "source_packet"},
        }
        result = coverage({"summaries": [video, audio], "format": {"duration": "4.02"}},
                          {"start": 0, "end": 4.02}, track_expectations=declarations)
        self.assertEqual(result["status"], "covered_with_stated_tolerance")
        self.assertEqual(result["streams"][0]["expected_interval"]["end"], 4)
        self.assertEqual(result["streams"][1]["timestamp_mapping"]["offset_exact"], "13/2000")

    def test_priming_metadata_and_first_packet_skip_are_not_added_twice(self):
        audio_stream = {"index": 0, "codec_type": "audio", "codec_name": "opus", "time_base": "1/48000", "sample_rate": "48000", "initial_padding": 312}
        data = packets([-312, 648], 960)
        data[0]["side_data_list"] = [{"side_data_type": "Skip Samples", "skip_samples": 312, "discard_padding": 0}]
        data[-1]["side_data_list"] = [{"side_data_type": "Skip Samples", "skip_samples": 0, "discard_padding": 3}]
        summary = packet_summary(audio_stream, data)
        self.assertEqual(summary["initial_padding_seconds"], 0.0065)
        self.assertEqual(summary["audio_padding"]["priming_samples_without_double_counting"], 312)
        self.assertEqual(summary["discard_padding_seconds"], 3 / 48000)
        self.assertFalse(summary["audio_padding"]["native_packet_durations_modified"])

    def test_later_packet_skip_is_not_initial_priming(self):
        audio_stream = {"index": 0, "codec_type": "audio", "codec_name": "opus",
                        "time_base": "1/48000", "sample_rate": "48000"}
        data = packets([0, 960, 1920], 960)
        baseline = packet_summary(audio_stream, data)
        data[1]["side_data_list"] = [{"side_data_type": "Skip Samples", "skip_samples": 480,
                                      "discard_padding": 0}]
        data[2]["side_data_list"] = [{"side_data_type": "Skip Samples", "skip_samples": 0,
                                      "discard_padding": 240}]
        summary = packet_summary(audio_stream, data)
        self.assertEqual(summary["audio_padding"]["first_packet_skip_samples"], 0)
        self.assertEqual(summary["audio_padding"]["priming_samples_without_double_counting"], 0)
        self.assertEqual(summary["initial_padding_seconds"], 0)
        self.assertEqual(summary["discard_padding_seconds"], 240 / 48000)
        self.assertEqual(summary["packet_intervals_exact"], baseline["packet_intervals_exact"])
        self.assertEqual(summary["skip_sample_side_data"], [
            {"side_data_type": "Skip Samples", "skip_samples": 480, "discard_padding": 0,
             "packet_index": 1, "pts": 960, "pts_time": None, "pts_seconds_exact": "1/50"},
            {"side_data_type": "Skip Samples", "skip_samples": 0, "discard_padding": 240,
             "packet_index": 2, "pts": 1920, "pts_time": None, "pts_seconds_exact": "1/25"},
        ])

    def test_first_packet_skip_and_declared_priming_use_maximum_not_sum(self):
        for declared, expected in ((0, 312), (312, 312), (624, 624)):
            with self.subTest(declared=declared):
                audio_stream = {"index": 0, "codec_type": "audio", "codec_name": "opus",
                                "time_base": "1/48000", "sample_rate": "48000",
                                "initial_padding": declared}
                data = packets([-312, 648, 1608], 960)
                data[0]["side_data_list"] = [
                    {"side_data_type": "Skip Samples", "skip_samples": 312, "discard_padding": 0},
                    {"side_data_type": "Skip Samples", "skip_samples": 120, "discard_padding": 0},
                ]
                data[1]["side_data_list"] = [{"side_data_type": "Skip Samples", "skip_samples": 960,
                                              "discard_padding": 0}]
                data[2]["side_data_list"] = [{"side_data_type": "Skip Samples", "skip_samples": 0,
                                              "discard_padding": 3}]
                summary = packet_summary(audio_stream, data)
                self.assertEqual(summary["audio_padding"]["first_packet_skip_samples"], 312)
                self.assertEqual(summary["audio_padding"]["initial_padding_samples"], declared)
                self.assertEqual(summary["audio_padding"]["priming_samples_without_double_counting"], expected)
                self.assertEqual(summary["initial_padding_seconds"], expected / 48000)
                self.assertEqual(summary["discard_padding_seconds"], 3 / 48000)
                self.assertEqual([x["packet_index"] for x in summary["skip_sample_side_data"]], [0, 0, 1, 2])
                self.assertFalse(summary["audio_padding"]["native_packet_durations_modified"])

    def test_first_packet_priming_uses_demux_order_not_lowest_pts(self):
        audio_stream = {"index": 0, "codec_type": "audio", "codec_name": "opus",
                        "time_base": "1/48000", "sample_rate": "48000"}
        data = packets([1920, 0], 960)
        data[1]["side_data_list"] = [{"side_data_type": "Skip Samples", "skip_samples": 480,
                                      "discard_padding": 0}]
        summary = packet_summary(audio_stream, data)
        self.assertEqual(summary["audio_padding"]["first_packet_skip_samples"], 0)
        self.assertEqual(summary["initial_padding_seconds"], 0)
        self.assertEqual(summary["skip_sample_side_data"][0]["packet_index"], 1)
        self.assertEqual(summary["skip_sample_side_data"][0]["pts_seconds_exact"], "0")

    def test_container_duration_is_not_a_shared_track_endpoint(self):
        summary = packet_summary(stream(), packets(range(40)))
        result = coverage({"summaries": [summary], "format": {"duration": "4.011"}}, {"start": 0, "end": None})
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["streams"][0]["issues"], [])

    def test_interval_beyond_declared_track_extent_is_not_applicable(self):
        summary = packet_summary(stream(), packets(range(40)))
        declaration = {"video": {"start": "0", "end": "4", "timestamp_offset": "0", "source": "track index", "coordinate": "native_packet"}}
        result = coverage({"summaries": [summary], "format": {}}, {"start": 5, "end": 6}, track_expectations=declaration)
        self.assertEqual(result["status"], "not_applicable")

    def test_payload_mismatch_marks_timestamps_not_compared(self):
        a = {"streams": [stream()], "packets": packets([0, 1, 2])}
        b = json.loads(json.dumps(a))
        b["packets"][1]["data_hash"] = "different-framing"
        result = preservation(a, b, "video")
        self.assertEqual(result["encoded_packet_preservation"], "failed")
        self.assertEqual(result["timestamp_preservation"], "not_compared")
        self.assertEqual(result["timestamps"]["pts_time"]["compared"], 0)

    def test_b_frame_pts_reordering_is_not_a_coverage_gap(self):
        data = packets([0, 3, 1, 2])
        for index, packet in enumerate(data):
            packet["dts"] = index - 2
        summary = packet_summary(stream(), data)
        result = coverage({"summaries": [summary], "format": {}}, {"start": 0, "end": 0.4})
        self.assertTrue(summary["pts_regressions"])
        self.assertEqual(result["status"], "covered_with_stated_tolerance")

    def test_duplicate_records_in_requested_scope_require_review(self):
        data = packets(range(40))
        data.insert(25, dict(data[24]))
        summary = packet_summary(stream(), data)
        result = coverage({"summaries": [summary], "format": {}}, {"start": 2, "end": 3})
        self.assertEqual(result["status"], "needs_review")
        outside = coverage({"summaries": [summary], "format": {}}, {"start": 0, "end": 1})
        self.assertEqual(outside["status"], "covered_with_stated_tolerance")

    def test_dts_regression_in_requested_scope_requires_review(self):
        data = packets(range(40))
        data[25]["dts"] = 23
        summary = packet_summary(stream(), data)
        result = coverage({"summaries": [summary], "format": {}}, {"start": 2, "end": 3})
        self.assertEqual(result["status"], "needs_review")
        outside = coverage({"summaries": [summary], "format": {}}, {"start": 0, "end": 1})
        self.assertEqual(outside["status"], "covered_with_stated_tolerance")

    def test_track_declarations_require_finite_values_and_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "expectations.json"
            for entry in ({"start": 0, "end": 1}, {"start": 0, "end": "NaN", "source": "index"}, {"end": 1, "source": " "}):
                path.write_text(json.dumps({"video": entry}))
                with self.assertRaises(ValueError):
                    load_track_expectations(path)
            path.write_text(json.dumps({"video": {"start": "0", "end": "1001/1000", "source": "source index"}}))
            result = load_track_expectations(path)
            self.assertEqual(result["video"]["end"], "1001/1000")
            self.assertIn("caller_supplied", result["video"]["provenance_verification"])

    def test_coarse_clock_does_not_hide_removed_frames(self):
        result = packet_summary(stream(), packets([0, 1, 2, 5, 6]))
        self.assertAlmostEqual(result["gaps"][0]["seconds"], 0.2)
        self.assertLess(result["gap_rounding_tolerance"], 0.01)

    def test_fine_clock_detects_the_same_gap(self):
        result = packet_summary(stream("1/1000"), packets([0, 100, 200, 500], 100))
        self.assertAlmostEqual(result["gaps"][0]["seconds"], 0.2)

    def test_integer_ticks_avoid_decimal_timestamp_rounding(self):
        data = packets([0, 1, 2])
        for p in data:
            p["pts_time"] = "999999.0"
        result = packet_summary(stream(), data)
        self.assertEqual(result["first_pts"], 0)
        self.assertEqual(result["gaps"], [])

    def test_valid_coarse_cfr_is_not_rejected(self):
        result = packet_summary(stream(), packets(range(40)))
        self.assertEqual(result["gaps"], [])
        self.assertEqual(result["timing_requires_review"], [])

    def test_explicit_vfr_hold_is_coverage(self):
        data = packets([0, 20], 20)
        result = packet_summary(stream(), data)
        self.assertEqual(result["gaps"], [])
        self.assertEqual(result["packet_end"], 4)

    def test_fine_rounding_gap_within_subframe_bound(self):
        result = packet_summary(stream("1/1000"), packets([0, 33, 67, 100], 33))
        self.assertEqual(result["gaps"], [])

    def test_unknown_duration_requires_review(self):
        s = stream()
        del s["avg_frame_rate"]
        data = packets([0, 1, 2])
        for p in data:
            del p["duration"]
        result = packet_summary(s, data)
        self.assertTrue(result["timing_requires_review"])

    def test_nominal_rate_does_not_turn_estimated_durations_into_proof(self):
        data = packets([0, 1, 2])
        for p in data:
            del p["duration"]
        result = packet_summary(stream(), data)
        self.assertTrue(result["timing_requires_review"])

    def test_preservation_uses_the_same_subframe_policy(self):
        a = {"streams": [stream()], "packets": packets([0, 1, 2])}
        b = json.loads(json.dumps(a))
        b["packets"][1]["pts"] += 1
        result = preservation(a, b, "video")
        self.assertTrue(result["ordered_payload_hashes_identical"])
        self.assertEqual(result["timestamp_preservation"], "failed")
        self.assertFalse(result["success"])

    def test_single_missing_tail_frame_not_excused(self):
        s = packet_summary(stream(), packets(range(39)))
        result = coverage({"summaries": [s], "format": {}}, {"start": 0, "end": 4}, 0)
        self.assertEqual(result["status"], "failed")

    def test_nonzero_start_kept(self):
        s = packet_summary(stream(), packets(range(100, 140)))
        result = coverage({"summaries": [s], "format": {}}, {"start": 10, "end": 14}, 0)
        self.assertEqual(result["status"], "covered_with_stated_tolerance")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg/ffprobe required")
class ValidatorIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="watch-youtube-validation-tests-")
        cls.root = Path(cls.temp.name)
        cls.movie = cls.root / "fixture.mp4"
        cls.ffmpeg("-f", "lavfi", "-i", "testsrc2=s=64x64:r=10:d=4", "-c:v", "libx264", "-bf", "0", str(cls.movie))
        cls.gap = cls.root / "gap.ivf"
        cls.ffmpeg("-f", "lavfi", "-i", "testsrc2=s=64x64:r=10:d=4", "-vf", "select=not(between(n\\,10\\,11))",
                   "-fps_mode", "passthrough", "-c:v", "libvpx", "-deadline", "realtime", str(cls.gap))
        cls.webm = cls.root / "gap.webm"
        cls.ffmpeg("-i", str(cls.gap), "-c", "copy", str(cls.webm))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    @classmethod
    def ffmpeg(cls, *args):
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n", *args],
                       check=True, capture_output=True, timeout=60)

    def run_validator(self, source=None, output=None, extra=()):
        output = output or self.root / (self.id().split(".")[-1] + "-run")
        proc = subprocess.run([sys.executable, str(SCRIPTS / "validate_media.py"),
                               "--input", str(source or self.movie), "--output-dir", str(output), *extra],
                              capture_output=True, text=True, timeout=90,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        report = json.loads((output / "validation.json").read_text()) if (output / "validation.json").is_file() else None
        return proc, report, output

    def test_ordinary_video(self):
        p, r, _ = self.run_validator(extra=("--expected-duration", "4"))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(r["technical_decode"], "passed")
        self.assertTrue(r["inputs"]["input"]["input_bytes_unchanged"])

    def test_verified_fragment_tables_do_not_invent_an_uncaptured_tail(self):
        source = self.root / "fragmented-short.mp4"
        self.ffmpeg("-f", "lavfi", "-i", "testsrc2=s=64x48:r=24:d=0.5", "-c:v", "libx264", "-bf", "0",
                    "-movflags", "empty_moov+frag_keyframe+default_base_moof", str(source))
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        p, r, _ = self.run_validator(source, extra=("--expected-start", "0", "--expected-end", "1"))
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(r["inputs"]["input"]["container_duration_evidence"]["status"], "verified")
        self.assertEqual(r["stage_results"]["native_coverage"]["status"], "failed")
        self.assertEqual(r["analysis_readiness"]["status"], "failed")
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    def test_unknown_completeness_stays_unknown(self):
        p, r, _ = self.run_validator()
        self.assertEqual(p.returncode, 0)
        self.assertEqual(r["completeness"], "unknown")

    def test_presentation_duration_does_not_create_a_video_tail_failure(self):
        p, r, _ = self.run_validator(extra=("--expected-duration", "4.011"))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(r["completeness"], "unknown")
        self.assertEqual(r["expected_interval"]["presentation_duration"], 4.011)
        self.assertIsNone(r["expected_interval"]["end"])

    def test_selected_interval_passes_despite_earlier_global_gap(self):
        p, r, _ = self.run_validator(self.gap, extra=("--expected-start", "2", "--expected-end", "3"))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(r["stage_results"]["native_coverage"]["status"], "passed")
        row = r["inputs"]["input"]["coverage"]["streams"][0]
        self.assertEqual(row["global_integrity"]["status"], "needs_review")
        self.assertEqual(row["gaps_in_requested_interval"], [])

    def test_probe_budget_is_inconclusive_and_does_not_parse_partial_json(self):
        p, r, out = self.run_validator(extra=("--max-probe-bytes", "128"))
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertEqual(r["status"], "needs_review")
        self.assertEqual(r["stage_results"]["probe"]["status"], "needs_review")
        self.assertEqual(r["stage_results"]["decode"]["status"], "not_tested")
        self.assertTrue((out / "input.ffprobe.json").is_file())
        self.assertTrue(r["inputs"]["input"]["input_bytes_unchanged"])

    def test_opus_does_not_hide_a_high_fps_missing_final_video_frame(self):
        for rate in (60, 120):
            with self.subTest(rate=rate):
                prefix = self.root / ("tail-loss-" + str(rate))
                video, audio, combined = prefix.with_suffix(".video.webm"), prefix.with_suffix(".ogg"), prefix.with_suffix(".webm")
                self.ffmpeg("-f", "lavfi", "-i", "testsrc2=s=64x64:r=" + str(rate) + ":d=1",
                            "-vf", "select=lt(n\\," + str(rate - 1) + ")", "-fps_mode", "passthrough",
                            "-c:v", "libvpx-vp9", "-deadline", "realtime", str(video))
                self.ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1", "-c:a", "libopus", str(audio))
                self.ffmpeg("-copyts", "-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
                            "-c", "copy", "-avoid_negative_ts", "disabled", "-live", "1", str(combined))
                p, r, _ = self.run_validator(combined, output=prefix.with_suffix(".run"), extra=("--expected-end", "1"))
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                video_coverage = next(row for row in r["inputs"]["input"]["coverage"]["streams"] if row["kind"] == "video")
                self.assertEqual(video_coverage["status"], "failed")
                self.assertEqual(video_coverage["tail_tolerance_basis"]["observed_opus_padding_allowance"], 0)

    def test_separate_legitimate_track_ends_use_their_declared_extents(self):
        video, audio = self.root / "unequal.video.webm", self.root / "unequal.audio.ogg"
        self.ffmpeg("-f", "lavfi", "-i", "testsrc2=s=64x64:r=25:d=1", "-c:v", "libvpx-vp9", "-deadline", "realtime", str(video))
        self.ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.02", "-c:a", "libopus", str(audio))
        declaration = self.root / "unequal.expectations.json"
        declaration.write_text(json.dumps({
            "video": {"start": "0", "end": "1", "source": "synthetic source declared duration"},
            "audio": {"start": "0", "end": "51/50", "source": "synthetic source declared duration"},
        }))
        p, r, _ = self.run_validator(video, extra=("--audio", str(audio), "--expected-duration", "1.02", "--expected-end", "1.02", "--track-expectations", str(declaration)))
        self.assertIn(p.returncode, (0, 2), p.stdout + p.stderr)
        self.assertEqual(r["stage_results"]["native_coverage"]["status"], "passed")
        self.assertEqual(r["inputs"]["input"]["coverage"]["streams"][0]["expected_interval"]["end"], 1)

    def test_decoded_equivalence_is_a_separate_packaged_stage(self):
        video, audio = self.root / "equivalence.video.webm", self.root / "equivalence.audio.ogg"
        self.ffmpeg("-f", "lavfi", "-i", "testsrc2=s=64x64:r=10:d=1", "-c:v", "libvpx-vp9", "-deadline", "realtime", str(video))
        self.ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1", "-c:a", "libopus", str(audio))
        p, r, _ = self.run_validator(video, extra=("--audio", str(audio), "--decoded-equivalence"))
        self.assertIn(p.returncode, (0, 2), p.stdout + p.stderr)
        self.assertEqual(r["stage_results"]["decoded_equivalence"]["status"], "passed")
        self.assertEqual(len(r["output"]["decoded_comparisons"]), 2)

    def test_ivf_gap_is_not_accepted(self):
        p, r, _ = self.run_validator(self.gap, extra=("--expected-end", "4"))
        self.assertEqual(p.returncode, 1)
        self.assertAlmostEqual(r["inputs"]["input"]["summaries"][0]["gaps"][0]["seconds"], 0.2)

    def test_webm_gap_is_not_accepted(self):
        p, r, _ = self.run_validator(self.webm, extra=("--expected-end", "4"))
        self.assertEqual(p.returncode, 1)
        self.assertTrue(r["inputs"]["input"]["summaries"][0]["gaps"])

    def test_expected_longer_than_source_fails(self):
        p, r, _ = self.run_validator(extra=("--expected-end", "6"))
        self.assertEqual(p.returncode, 1)
        self.assertEqual(r["completeness"], "not_established")

    def test_reserved_name_collisions_leave_inputs_untouched(self):
        for name in ("input.ffprobe.json", "validation.json", "checkpoint.json", "validation.json.tmp", "native.mkv"):
            with self.subTest(name=name):
                out = self.root / ("collision-" + name)
                out.mkdir()
                source = out / name
                source.write_bytes(self.movie.read_bytes())
                before = source.read_bytes()
                proc = subprocess.run([sys.executable, str(SCRIPTS / "validate_media.py"), "--input", str(source),
                                       "--output-dir", str(out)], capture_output=True, text=True, timeout=30)
                self.assertEqual(proc.returncode, 1)
                self.assertEqual(source.read_bytes(), before)
                self.assertEqual(list(out.iterdir()), [source])
                self.assertIn("already exists", proc.stderr)

    def test_empty_existing_directory_is_refused(self):
        out = self.root / "empty-output"
        out.mkdir()
        p, _, _ = self.run_validator(output=out)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(list(out.iterdir()), [])

    def test_invalid_request_leaves_old_checkpoint_untouched(self):
        out = self.root / "annotated-output"
        out.mkdir()
        checkpoint = out / "checkpoint.json"
        checkpoint.write_text('{"reviewed": true, "notes": "keep this annotation"}')
        before = checkpoint.read_bytes()
        p, _, _ = self.run_validator(output=out, extra=("--expected-duration", "nan"))
        self.assertEqual(p.returncode, 1)
        self.assertEqual(checkpoint.read_bytes(), before)
        self.assertEqual(list(out.iterdir()), [checkpoint])

    def test_symlink_output_alias_is_refused(self):
        target = self.root / "symlink-target"
        target.mkdir()
        alias = self.root / "output-alias"
        alias.symlink_to(target, target_is_directory=True)
        p, _, _ = self.run_validator(output=alias)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(list(target.iterdir()), [])

    def test_hardlink_collision_is_refused(self):
        out = self.root / "hardlink-output"
        out.mkdir()
        alias = out / "checkpoint.json"
        os.link(self.movie, alias)
        before = self.movie.read_bytes()
        p, _, _ = self.run_validator(output=out)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(self.movie.read_bytes(), before)
        self.assertEqual(alias.read_bytes(), before)

    def test_missing_input_does_not_claim_directory(self):
        p, r, out = self.run_validator(source=self.root / "absent.mp4")
        self.assertEqual(p.returncode, 1)
        self.assertFalse(out.exists())

    def test_failed_probe_still_reports_original_hash(self):
        source = self.root / "html.mp4"
        source.write_text("<html>not media</html>")
        original = hashlib.sha256(source.read_bytes()).hexdigest()
        p, r, _ = self.run_validator(source)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(r["inputs"]["input"]["sha256"], original)
        self.assertTrue(r["inputs"]["input"]["input_bytes_unchanged"])

    def test_combined_h264_aac(self):
        source = self.root / "combined.mp4"
        self.ffmpeg("-i", str(self.movie), "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
                    "-c:v", "copy", "-c:a", "aac", str(source))
        p, r, _ = self.run_validator(source, extra=("--expected-duration", "4"))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(r["audio_presence"], "present")

    def test_native_nonzero_start_integration(self):
        source = self.root / "nonzero.mp4"
        self.ffmpeg("-i", str(self.movie), "-vf", "setpts=PTS+10/TB", "-c:v", "libx264", "-bf", "0",
                    "-fps_mode", "passthrough", str(source))
        p, r, _ = self.run_validator(source, extra=("--expected-start", "10", "--expected-end", "14"))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(r["inputs"]["input"]["summaries"][0]["first_pts"], 10)

    def test_vp9_opus_remux_statuses(self):
        video, audio = self.root / "vp9.webm", self.root / "opus.ogg"
        self.ffmpeg("-f", "lavfi", "-i", "testsrc2=s=64x64:r=10:d=4", "-c:v", "libvpx-vp9", "-deadline", "realtime", str(video))
        self.ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4", "-c:a", "libopus", str(audio))
        p, r, _ = self.run_validator(video, extra=("--audio", str(audio), "--expected-duration", "4"))
        self.assertIn(p.returncode, (0, 2), p.stdout + p.stderr)
        self.assertEqual(r["output"]["technical_decode"], "passed")
        self.assertEqual(r["output"]["encoded_packet_preservation"], "passed")
        self.assertEqual(r["output"]["timestamp_preservation"], "passed")
        self.assertEqual(r["output"]["decoded_av_equivalence"], "not_tested")

    def test_h264_bframe_remux_failure_is_not_corruption_claim(self):
        video, audio = self.root / "bframes.mp4", self.root / "aac.m4a"
        self.ffmpeg("-f", "lavfi", "-i", "testsrc2=s=64x64:r=10:d=4", "-c:v", "libx264", "-bf", "2", str(video))
        self.ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4", "-c:a", "aac", str(audio))
        p, r, _ = self.run_validator(video, extra=("--audio", str(audio), "--expected-duration", "4"))
        self.assertEqual(r["output"]["technical_decode"], "passed")
        self.assertEqual(r["output"]["encoded_packet_preservation"], "passed")
        self.assertEqual(r["output"]["decoded_av_equivalence"], "not_tested")
        if r["output"]["timestamp_preservation"] == "failed":
            self.assertEqual(p.returncode, 1)
        self.assertIn("does not prove corrupted", r["output"]["interpretation"])


if __name__ == "__main__":
    unittest.main()
