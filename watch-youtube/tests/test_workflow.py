# SPDX-License-Identifier: MIT
"""Portable orchestration, explicit evidence contracts and real CLI behavior."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import workflow
from safe_outputs import digest


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.input = self.root / "video.bin"
        self.input.write_bytes(b"captured encoded video bytes")
        self.snapshot = {"input": {"path": str(self.input), "bytes": self.input.stat().st_size,
                                   "sha256": digest(self.input)}}

    def record(self):
        values = {"bytes": self.snapshot["input"]["bytes"], "sha256": self.snapshot["input"]["sha256"]}
        return {"schema_version": 1, "source": {"url": "https://example.invalid/watch"},
                "exports": {"files": [{"id": "video-track", "filename": "video.bin", **values}]},
                "tracks": [{"role": "video", "id": "video-track", "receipt": dict(values)}]}

    def arguments(self, *extra):
        return workflow.parser().parse_args(["run", "--input", str(self.input),
                "--output-dir", str(self.root / "run"), "--end", "1", *extra])

    def child_report(self, command, name, code=0):
        destination = Path(command[command.index("--output-dir") + 1])
        destination.mkdir()
        if name == "validation":
            (destination / "validation.json").write_text(json.dumps({"status": "needs_review" if code == 2 else "passed", "completeness": "unknown"}))
        else:
            (destination / "frames_manifest.json").write_text(json.dumps({"frames": [{"source_pts": 0}], "sampling_coverage": {"status": "sampled"}}))
        return {"returncode": code}

    def test_plan_bounded_sections(self):
        sections = workflow.section_plan(3, 70, 30, 2, 3)
        self.assertEqual([(x["start"], x["end"]) for x in sections], [(3, 33), (33, 63), (63, 70)])
        with self.assertRaises(ValueError):
            workflow.section_plan(3, 70, 30, 2, 2)

    def test_plan_nonfinite_or_empty_refused(self):
        for start, end, seconds, fps in [(0, float("nan"), 30, 2), (1, 1, 30, 2), (0, 1, 0, 2), (0, 1, 30, 0)]:
            with self.assertRaises(ValueError):
                workflow.section_plan(start, end, seconds, fps, 10)

    def test_unmodified_export_metadata_verified(self):
        record = self.record()
        before = json.dumps(record, sort_keys=True)
        result = workflow.verify_acquisition(record, self.snapshot, record["source"]["url"])
        self.assertEqual(result["status"], "passed")
        self.assertEqual(json.dumps(record, sort_keys=True), before)

    def test_direct_raw_file_declarations(self):
        record = self.record()
        record["tracks"][0]["export"] = dict(record["exports"]["files"][0])
        del record["tracks"][0]["id"]
        del record["exports"]
        self.assertEqual(workflow.verify_acquisition(record, self.snapshot, None)["status"], "passed")

    def test_bad_transfer_evidence_refused(self):
        for field, replacement in (("bytes", 1), ("sha256", "0" * 64)):
            record = self.record()
            record["tracks"][0]["receipt"][field] = replacement
            with self.assertRaises(ValueError):
                workflow.verify_acquisition(record, self.snapshot, None)
        with self.assertRaises(ValueError):
            workflow.verify_acquisition(self.record(), self.snapshot, "https://example.invalid/other")

    def test_duplicate_roles_and_export_ids_refused(self):
        record = self.record()
        record["tracks"].append(dict(record["tracks"][0]))
        with self.assertRaises(ValueError):
            workflow.verify_acquisition(record, self.snapshot, None)
        record = self.record()
        record["exports"]["files"].append(dict(record["exports"]["files"][0]))
        with self.assertRaises(ValueError):
            workflow.verify_acquisition(record, self.snapshot, None)

    def test_stale_export_keeps_transfer_and_capture_verdicts_separate(self):
        record = self.record()
        record["exports"]["files"][0]["stale"] = True
        result = workflow.verify_acquisition(record, self.snapshot, None)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["capture_evidence_status"], "needs_review")

    def test_explicit_uncovered_capture_cannot_hide_behind_eligible_flag(self):
        for state in ("uncovered", "unestablished", "unexpected", None):
            with self.subTest(status=state):
                record = self.record()
                exported = record["exports"]["files"][0]
                exported.update(eligibleForValidation=True, diagnostic=False, stale=False,
                                requestedCoverage={"status": state, "requestedInterval": {"start": 0, "end": 1}})
                before = json.dumps(record, sort_keys=True)
                result = workflow.verify_acquisition(record, self.snapshot, None)
                self.assertEqual(result["status"], "passed")
                self.assertEqual(result["capture_evidence_status"], "needs_review")
                self.assertEqual(json.dumps(record, sort_keys=True), before)

    def test_covered_browser_interval_stays_separate_from_native_completeness(self):
        for state in ("covered", "not_requested"):
            record = self.record()
            record["exports"]["files"][0]["requestedCoverage"] = {"status": state}
            result = workflow.verify_acquisition(record, self.snapshot, None)
            self.assertEqual(result["capture_evidence_status"], "not_established_by_transfer")
            self.assertEqual(result["requested_coverage"][0]["browser_observation"]["status"], state)

    def test_analysis_copy_flag_and_separate_readiness_are_preserved(self):
        commands = []
        def execute(command, directory, name):
            commands.append(command)
            result = self.child_report(command, name, 2 if name == "validation" else 0)
            if name == "validation":
                destination = Path(command[command.index("--output-dir") + 1]) / "validation.json"
                value = json.loads(destination.read_text())
                value.update(analysis_readiness={"status": "passed", "hosted_original_completeness": "not_established"},
                             analysis_copy={"status": "passed", "path": "analysis.mka"})
                destination.write_text(json.dumps(value))
            return result
        with patch.object(workflow, "doctor", return_value={"status": "passed"}), patch.object(workflow, "execute", side_effect=execute), redirect_stdout(io.StringIO()):
            status = workflow.run(self.arguments("--analysis-copy"))
        self.assertEqual(status, 2)
        self.assertIn("--analysis-copy", commands[0])
        report = json.loads((self.root / "run" / "workflow.json").read_text())
        self.assertEqual(report["analysis_readiness"]["status"], "passed")
        self.assertEqual(report["analysis_copy"]["status"], "passed")
        self.assertFalse(report["visual_review"]["performed"])

    def test_doctor_distinguishes_features_and_optional_tools(self):
        fake = subprocess.CompletedProcess([], 0, "-fps_mode mode", "")
        with patch.object(workflow.shutil, "which", side_effect=lambda name: "/tools/" + name if name != "node" else None), patch.object(workflow.subprocess, "run", return_value=fake):
            result = workflow.doctor()
        self.assertEqual(result["status"], "passed")
        self.assertIsNone(result["optional"]["node_for_javascript_tests"])
        fake.stdout = "old help without required option"
        with patch.object(workflow.shutil, "which", return_value="/tools/tool"), patch.object(workflow.subprocess, "run", return_value=fake):
            self.assertEqual(workflow.doctor()["status"], "unsupported")

    def test_occupied_output_leaves_previous_checkpoint_untouched(self):
        args = self.arguments()
        args.output_dir.mkdir()
        checkpoint = args.output_dir / "checkpoint.json"
        checkpoint.write_text('{"reviewed":true}')
        with patch.object(workflow, "doctor", return_value={"status": "passed"}):
            with self.assertRaises(ValueError):
                workflow.run(args)
        self.assertEqual(checkpoint.read_text(), '{"reviewed":true}')

    def test_review_needed_continues_sampling_without_inferred_expectation(self):
        commands = []
        def execute(command, directory, name):
            commands.append(command)
            return self.child_report(command, name, 2 if name == "validation" else 0)
        with patch.object(workflow, "doctor", return_value={"status": "passed"}), patch.object(workflow, "execute", side_effect=execute), redirect_stdout(io.StringIO()):
            status = workflow.run(self.arguments("--expected-duration", "1.1"))
        self.assertEqual(status, 2)
        self.assertEqual(len(commands), 2)
        self.assertNotIn("--expected-end", commands[0])
        self.assertIn("--expected-duration", commands[0])
        report = json.loads((self.root / "run" / "workflow.json").read_text())
        self.assertEqual(report["status"], "needs_review")
        self.assertFalse(report["visual_review"]["performed"])

    def test_confirmed_validation_failure_stops_sampling(self):
        with patch.object(workflow, "doctor", return_value={"status": "passed"}), patch.object(workflow, "execute", return_value={"returncode": 1}) as child, redirect_stdout(io.StringIO()):
            self.assertEqual(workflow.run(self.arguments()), 1)
        self.assertEqual(child.call_count, 1)

    def test_success_exit_without_child_report_is_failure(self):
        with patch.object(workflow, "doctor", return_value={"status": "passed"}), patch.object(workflow, "execute", return_value={"returncode": 0}), redirect_stdout(io.StringIO()):
            self.assertEqual(workflow.run(self.arguments()), 1)
        report = json.loads((self.root / "run" / "workflow.json").read_text())
        self.assertEqual(report["stage_results"][-1]["status"], "failed")

    def test_partial_media_coverage_is_review_condition_despite_successful_extraction(self):
        def execute(command, directory, name):
            result = self.child_report(command, name)
            if name != "validation":
                destination = Path(command[command.index("--output-dir") + 1]) / "frames_manifest.json"
                value = json.loads(destination.read_text())
                value["media_coverage"] = {"status": "partial"}
                destination.write_text(json.dumps(value))
            return result
        with patch.object(workflow, "doctor", return_value={"status": "passed"}), patch.object(workflow, "execute", side_effect=execute), redirect_stdout(io.StringIO()):
            self.assertEqual(workflow.run(self.arguments()), 2)
        report = json.loads((self.root / "run" / "workflow.json").read_text())
        self.assertEqual(report["stage_results"][-1]["status"], "passed")
        self.assertEqual(report["stage_results"][-1]["coverage_status"], "needs_review")

    def test_source_change_is_recorded(self):
        def execute(command, directory, name):
            self.input.write_bytes(b"changed input")
            return self.child_report(command, name)
        with patch.object(workflow, "doctor", return_value={"status": "passed"}), patch.object(workflow, "execute", side_effect=execute), redirect_stdout(io.StringIO()):
            self.assertEqual(workflow.run(self.arguments()), 1)
        report = json.loads((self.root / "run" / "workflow.json").read_text())
        self.assertFalse(report["inputs"]["input"]["unchanged"])

    def test_plan_cli_in_fresh_process(self):
        result = subprocess.run([sys.executable, str(SCRIPTS / "workflow.py"), "plan", "--end", "31"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)["sections"]), 2)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg integration dependency unavailable")
    def test_real_local_workflow_cli(self):
        video = self.root / "clip.webm"
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-n", "-f", "lavfi", "-i", "testsrc2=s=64x64:r=10:d=1",
                        "-c:v", "libvpx-vp9", "-deadline", "realtime", str(video)], check=True, capture_output=True)
        result = subprocess.run([sys.executable, str(SCRIPTS / "workflow.py"), "run", "--input", str(video),
                    "--output-dir", str(self.root / "real-run"), "--end", "1", "--section-seconds", "0.5", "--fps", "2"], capture_output=True, text=True)
        self.assertIn(result.returncode, (0, 2), result.stderr + result.stdout)
        report = json.loads((self.root / "real-run" / "workflow.json").read_text())
        sections = [x for x in report["stage_results"] if x["stage"] == "frame_extraction"]
        self.assertEqual(len(sections), 2)
        self.assertTrue(all(Path(x["manifest"]).is_file() for x in sections))
        self.assertTrue(report["inputs"]["input"]["unchanged"])
        partial = subprocess.run([sys.executable, str(SCRIPTS / "workflow.py"), "run", "--input", str(video),
                    "--output-dir", str(self.root / "partial-run"), "--end", "1.5", "--fps", "2"], capture_output=True, text=True)
        self.assertEqual(partial.returncode, 2, partial.stderr + partial.stdout)
        report = json.loads((self.root / "partial-run" / "workflow.json").read_text())
        self.assertEqual(report["status"], "needs_review")
        self.assertEqual(report["stage_results"][-1]["media_coverage"]["status"], "partial")


if __name__ == "__main__":
    unittest.main()
