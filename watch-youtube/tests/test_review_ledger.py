# SPDX-License-Identifier: MIT
"""Review attestations validate evidence and preserve previous annotations."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import review_ledger as ledger
from safe_outputs import digest


class ReviewLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.bin"
        self.source.write_bytes(b"original encoded media")
        self.image = self.root / "frame.jpg"
        self.image.write_bytes(b"extracted image bytes")
        self.manifest = self.root / "frames_manifest.json"
        self.report = {"source": str(self.source), "source_sha256": digest(self.source), "reviewed": False,
                       "frames": [{"path": str(self.image), "sha256": digest(self.image), "source_pts": 3,
                                   "source_time_base": "1/2", "source_time_seconds": 1.5, "reviewed": False}]}
        self.manifest.write_text(json.dumps(self.report))
        self.target = self.root / "review.json"

    def args(self, **extra):
        values = {"manifest": self.manifest, "ledger": self.target, "source": None,
                  "frame_index": [0], "reviewer": "reviewer", "observation": "An observed frame detail",
                  "interpretation": None, "next_start": None, "next_end": None,
                  "audio_audition_status": "not_performed", "audio_observation": None}
        values.update(extra)
        return argparse.Namespace(**values)

    def test_init_is_unreviewed_and_refuses_existing_target(self):
        result = ledger.init(self.args())
        self.assertEqual(result["status"], "created_unreviewed")
        before = self.target.read_bytes()
        with self.assertRaises(ValueError):
            ledger.init(self.args())
        self.assertEqual(before, self.target.read_bytes())

    def test_append_preserves_manifest_and_prior_entries(self):
        before = self.manifest.read_bytes()
        ledger.init(self.args())
        ledger.record(self.args(next_start=2, next_end=3))
        first = json.loads(self.target.read_text())["entries"][0]
        ledger.record(self.args(observation="Additional observation"))
        saved = json.loads(self.target.read_text())
        ledger.validate_ledger(saved)
        self.assertEqual(saved["entries"][0], first)
        self.assertEqual(saved["entries"][1]["previous_sha256"], first["sha256"])
        self.assertEqual(saved["entries"][0]["inspected_frames"][0]["source_time_seconds_exact"], "3/2")
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_changed_source_or_image_refused_without_ledger_update(self):
        ledger.init(self.args())
        before = self.target.read_bytes()
        self.image.write_bytes(b"different frame")
        with self.assertRaises(ValueError):
            ledger.record(self.args())
        self.assertEqual(self.target.read_bytes(), before)
        self.source.write_bytes(b"different source")
        with self.assertRaises(ValueError):
            ledger.record(self.args())
        self.assertEqual(self.target.read_bytes(), before)

    def test_invalid_indices_and_timestamps_refused(self):
        ledger.init(self.args())
        for indices in ([-1], [1], [0, 0]):
            with self.assertRaises(ValueError):
                ledger.record(self.args(frame_index=indices))
        self.report["frames"][0]["source_time_seconds"] = 10
        self.manifest.write_text(json.dumps(self.report))
        with self.assertRaises(ValueError):
            ledger.record(self.args())

    def test_tampered_history_is_not_overwritten(self):
        ledger.init(self.args())
        ledger.record(self.args())
        value = json.loads(self.target.read_text())
        value["entries"][0]["observation"] = "silently changed"
        self.target.write_text(json.dumps(value))
        before = self.target.read_bytes()
        with self.assertRaises(ValueError):
            ledger.record(self.args())
        self.assertEqual(self.target.read_bytes(), before)

    def test_coarse_timebase_never_excuses_wrong_frame_timestamp(self):
        ledger.init(self.args())
        self.report["frames"][0]["source_time_seconds"] = 2.0
        self.manifest.write_text(json.dumps(self.report))
        with self.assertRaises(ValueError):
            ledger.record(self.args())
        self.report["frames"][0]["source_time_seconds"] = 1.5
        self.report["frames"][0]["source_time_seconds_exact"] = "2"
        self.manifest.write_text(json.dumps(self.report))
        with self.assertRaises(ValueError):
            ledger.record(self.args())

    def test_sampler_and_legacy_exact_timestamps_preserve_ledger_format(self):
        ledger.init(self.args())
        frame = self.report["frames"][0]
        for fields in ({}, {"source_time_exact": "3/2"},
                       {"source_time_seconds_exact": "1.5"},
                       {"source_time_exact": "3/2", "source_time_seconds_exact": "1.5"}):
            with self.subTest(fields=fields):
                frame.pop("source_time_exact", None)
                frame.pop("source_time_seconds_exact", None)
                frame.update(fields)
                self.manifest.write_text(json.dumps(self.report))
                before = self.manifest.read_bytes()
                ledger.record(self.args())
                saved = json.loads(self.target.read_text())
                ledger.validate_ledger(saved)
                attested = saved["entries"][-1]["inspected_frames"][0]
                self.assertEqual(attested["source_time_seconds_exact"], "3/2")
                self.assertEqual(attested["source_time_seconds"], 1.5)
                self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual(len(saved["entries"]), 4)

    def test_conflicting_exact_timestamps_refuse_without_changing_evidence(self):
        ledger.init(self.args())
        ledger.record(self.args())
        frame = self.report["frames"][0]
        cases = ({"source_time_exact": "999"},
                 {"source_time_seconds_exact": "999"},
                 {"source_time_exact": "999", "source_time_seconds_exact": "3/2"},
                 {"source_time_exact": "3/2", "source_time_seconds_exact": "999"})
        for fields in cases:
            with self.subTest(fields=fields):
                frame.pop("source_time_exact", None)
                frame.pop("source_time_seconds_exact", None)
                frame.update(fields)
                self.manifest.write_text(json.dumps(self.report))
                before = {path: path.read_bytes() for path in
                          (self.source, self.image, self.manifest, self.target)}
                with self.assertRaisesRegex(ValueError, "exact timestamp"):
                    ledger.record(self.args())
                for path, original in before.items():
                    self.assertEqual(path.read_bytes(), original)
                self.assertFalse(self.target.with_name(self.target.name + ".lock").exists())

    def test_malformed_present_exact_timestamps_refuse_without_writes(self):
        ledger.init(self.args())
        frame = self.report["frames"][0]
        for field in ("source_time_exact", "source_time_seconds_exact"):
            for value in (None, True, "not-a-time", "1/0", [], {}):
                with self.subTest(field=field, value=value):
                    frame.pop("source_time_exact", None)
                    frame.pop("source_time_seconds_exact", None)
                    frame[field] = value
                    self.manifest.write_text(json.dumps(self.report))
                    before = {path: path.read_bytes() for path in
                              (self.source, self.image, self.manifest, self.target)}
                    with self.assertRaisesRegex(ValueError, "invalid exact timestamp"):
                        ledger.record(self.args())
                    for path, original in before.items():
                        self.assertEqual(path.read_bytes(), original)

    def test_source_manifest_image_aliases_refused(self):
        for target in (self.source, self.manifest, self.image):
            before = target.read_bytes()
            with self.assertRaises(ValueError):
                ledger.init(self.args(ledger=target))
            self.assertEqual(target.read_bytes(), before)
        self.target.hardlink_to(self.source)
        with self.assertRaises(ValueError):
            ledger.init(self.args())

    def test_locked_ledger_refuses_concurrent_update(self):
        ledger.init(self.args())
        with ledger.locked(self.target):
            with self.assertRaises(FileExistsError):
                ledger.record(self.args())
        self.assertEqual(json.loads(self.target.read_text())["entries"], [])

    def test_audio_attestation_requires_observation(self):
        ledger.init(self.args())
        with self.assertRaises(ValueError):
            ledger.record(self.args(frame_index=[], audio_audition_status="performed"))
        result = ledger.record(self.args(frame_index=[], audio_audition_status="performed", audio_observation="Auditioned sound detail"))
        self.assertEqual(result["inspected_frames"], 0)
        saved = json.loads(self.target.read_text())["entries"][0]
        self.assertFalse(saved["visual_review"]["performed"])

    def test_relocated_source_hash_is_verified(self):
        relocated = self.root / "relocated.bin"
        self.source.rename(relocated)
        ledger.init(self.args(source=relocated))
        ledger.record(self.args(source=relocated))
        self.assertEqual(len(json.loads(self.target.read_text())["entries"]), 1)

    def test_cli_init_then_record_in_separate_processes(self):
        base = [sys.executable, str(SCRIPTS / "review_ledger.py")]
        common = ["--manifest", str(self.manifest), "--ledger", str(self.target)]
        first = subprocess.run(base + ["init"] + common, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        second = subprocess.run(base + ["record"] + common + ["--frame-index", "0", "--reviewer", "reviewer", "--observation", "Observed detail"], capture_output=True, text=True)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertFalse(json.loads(self.manifest.read_text())["reviewed"])


if __name__ == "__main__":
    unittest.main()
