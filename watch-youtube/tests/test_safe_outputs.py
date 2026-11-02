# SPDX-License-Identifier: MIT
"""Fresh-run ownership and atomic metadata writes, without external tools."""
import concurrent.futures
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from safe_outputs import digest, prepare_run, save_json


class SafeOutputsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.bin"
        self.source.write_bytes(b"immutable source")

    def test_snapshot_precedes_output_creation(self):
        output, snapshot = prepare_run(self.root / "run", {"input": self.source})
        self.assertTrue(output.is_dir())
        self.assertEqual(snapshot["input"]["sha256"], digest(self.source))
        self.assertEqual(snapshot["input"]["bytes"], 16)

    def test_occupied_run_is_untouched(self):
        output = self.root / "run"
        output.mkdir()
        marker = output / "checkpoint.json"
        marker.write_text('{"reviewed":true}')
        with self.assertRaises(ValueError):
            prepare_run(output, {"input": self.source})
        self.assertEqual(marker.read_text(), '{"reviewed":true}')

    def test_missing_input_does_not_claim_output(self):
        output = self.root / "run"
        with self.assertRaises(OSError):
            prepare_run(output, {"input": self.root / "missing.bin"})
        self.assertFalse(output.exists())

    def test_empty_input_does_not_claim_output(self):
        self.source.write_bytes(b"")
        output = self.root / "run"
        with self.assertRaises(ValueError):
            prepare_run(output, {"input": self.source})
        self.assertFalse(output.exists())

    def test_invalid_json_never_replaces_previous_checkpoint(self):
        output, _ = prepare_run(self.root / "run", {"input": self.source})
        report = output / "checkpoint.json"
        save_json(report, {"reviewed": True})
        before = report.read_bytes()
        with self.assertRaises(ValueError):
            save_json(report, {"value": float("nan")})
        self.assertEqual(report.read_bytes(), before)
        self.assertEqual(list(output.glob("*.tmp")), [])

    def test_temp_collision_does_not_overwrite_source_alias(self):
        output, _ = prepare_run(self.root / "run", {"input": self.source})
        old_style_temp = output / "checkpoint.json.tmp"
        old_style_temp.hardlink_to(self.source)
        before = self.source.read_bytes()
        save_json(output / "checkpoint.json", {"status": "ready"})
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(old_style_temp.read_bytes(), before)
        self.assertEqual(json.loads((output / "checkpoint.json").read_text()), {"status": "ready"})

    def test_concurrent_claims_have_only_one_owner(self):
        output = self.root / "run"
        def claim(_):
            try:
                prepare_run(output, {"input": self.source})
                return True
            except (OSError, ValueError):
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(claim, range(2))), 1)


if __name__ == "__main__":
    unittest.main()
