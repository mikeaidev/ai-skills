# SPDX-License-Identifier: MIT
"""Verified audio derivatives preserve raw evidence and codec trimming."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from validate_media import create_audio_analysis_copy, decode, opus_context, probe


class AnalysisContextTests(unittest.TestCase):
    def test_unsupported_or_ambiguous_audio_context_is_refused(self):
        for structure in ({}, {"tracks": [{"kind": "audio", "codec_id": "A_AAC"}]},
                          {"tracks": [{"kind": "audio", "codec_id": "A_OPUS", "opus": {}},
                                      {"kind": "audio", "codec_id": "A_OPUS", "opus": {}}]}):
            with self.assertRaises(ValueError):
                opus_context(structure)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools required")
class AnalysisCopyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.webm"
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-n", "-f", "lavfi", "-i",
                        "sine=frequency=440:sample_rate=48000:duration=0.2", "-c:a", "libopus",
                        str(self.source)], check=True, capture_output=True)

    def inspect(self):
        directory = self.root / "probe"
        directory.mkdir()
        info, data = probe(self.source, directory, "source", 30, max_bytes=4 * 1024 * 1024)
        info["full_decode"] = decode(self.source, 30, directory, "source.decode", 4 * 1024 * 1024)
        return info, data

    def test_clean_analysis_copy_verifies_packets_context_pcm_and_raw_identity(self):
        original = self.source.read_bytes()
        info, data = self.inspect()
        result = create_audio_analysis_copy(self.source, data, info, self.root, 30, 4 * 1024 * 1024)
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["clean_container"], "passed")
        self.assertEqual(result["decoder_context"]["status"], "passed")
        self.assertEqual(result["decoded_comparison"]["content_equality"], "passed")
        self.assertEqual(result["hosted_original_completeness"], "not_established")
        self.assertEqual(self.source.read_bytes(), original)

    def test_incomplete_outer_envelope_can_get_clean_derivative_without_repairing_raw(self):
        original = bytearray(self.source.read_bytes())
        start = original.index(bytes.fromhex("18538067")) + 4
        first = original[start]
        width = next(n for n in range(1, 9) if first & (1 << (8 - n)))
        mask = (1 << (7 * width)) - 1
        value = int.from_bytes(original[start:start + width], "big") & mask
        encoded = (1 << (7 * width)) | (value + 7)
        original[start:start + width] = encoded.to_bytes(width, "big")
        self.source.write_bytes(original)
        info, data = self.inspect()
        self.assertEqual(info["structured_container"]["outer_envelope"]["status"], "incomplete")
        self.assertTrue(info["structured_container"]["observed_media_elements_complete"])
        self.assertTrue(info["container_warnings"])
        result = create_audio_analysis_copy(self.source, data, info, self.root, 30, 4 * 1024 * 1024)
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(self.source.read_bytes(), bytes(original))
        self.assertEqual(result["probe"]["structured_container"]["outer_envelope"]["status"], "complete")
        self.assertEqual(info["structured_container"]["outer_envelope"]["declared_bytes_remaining"], 7)

    def test_truncated_media_is_refused_before_creating_a_derivative(self):
        data = self.source.read_bytes()
        cluster = data.index(bytes.fromhex("1f43b675")) + 4
        width = next(n for n in range(1, 9) if data[cluster] & (1 << (8 - n)))
        length = int.from_bytes(data[cluster:cluster + width], "big") & ((1 << (7 * width)) - 1)
        self.source.write_bytes(data[:cluster + width + length - 1])
        info, data = self.inspect()
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = create_audio_analysis_copy(self.source, data, info, self.root, 30, 4 * 1024 * 1024)
        self.assertEqual(result["status"], "refused")
        self.assertFalse((self.root / "audio-analysis").exists())
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)

    def test_decoded_pcm_failure_cannot_become_a_verified_copy(self):
        info, data = self.inspect()
        before = self.source.read_bytes()
        with patch("decoded_equivalence.compare_decoded", return_value={"status": "failed", "content_equality": "failed", "timeline_equivalence": "passed"}):
            result = create_audio_analysis_copy(self.source, data, info, self.root, 30, 4 * 1024 * 1024)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.source.read_bytes(), before)

    def test_existing_analysis_directory_is_preserved(self):
        info, data = self.inspect()
        destination = self.root / "audio-analysis"
        destination.mkdir()
        sentinel = destination / "audio.mka"
        sentinel.write_bytes(b"previous analysis")
        result = create_audio_analysis_copy(self.source, data, info, self.root, 30, 4 * 1024 * 1024)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(sentinel.read_bytes(), b"previous analysis")


if __name__ == "__main__":
    unittest.main()
