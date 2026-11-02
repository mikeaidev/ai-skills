# SPDX-License-Identifier: MIT
"""Bounded structural proofs stay separate from envelope/source completeness."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from webm_structure import inspect_webm


def size_vint(value, width=None):
    if width is None:
        width = next(n for n in range(1, 9) if value < (1 << (7 * n)) - 1)
    return ((1 << (7 * width)) | value).to_bytes(width, "big")


def element(eid, payload, *, unknown=False, declared=None):
    identity = eid.to_bytes((eid.bit_length() + 7) // 8, "big")
    length = b"\xff" if unknown else size_vint(len(payload) if declared is None else declared)
    return identity + length + payload


def uint(eid, value, width=None):
    return element(eid, value.to_bytes(width or max(1, (value.bit_length() + 7) // 8), "big"))


def header(doctype=b"webm"):
    return element(0x1A45DFA3, uint(0x4286, 1) + uint(0x42F7, 1) +
                   uint(0x42F2, 4) + uint(0x42F3, 8) + element(0x4282, doctype))


def tracks(*, codec=b"A_OPUS", track_type=2, track_number=1, mapping=0):
    opus = b"OpusHead" + bytes([1, 2]) + (312).to_bytes(2, "little") + \
           (48000).to_bytes(4, "little") + b"\0\0" + bytes([mapping])
    private = element(0x63A2, opus) if codec == b"A_OPUS" else b""
    entry = uint(0xD7, track_number) + uint(0x73C5, 1) + uint(0x83, track_type) + \
            element(0x86, codec) + private + uint(0x56AA, 6500000) + uint(0x56BB, 80000000)
    return element(0x1654AE6B, element(0xAE, entry))


def block(payload=b"ab", *, track=1, relative=0, flags=0x80, group=False):
    body = size_vint(track) + relative.to_bytes(2, "big", signed=True) + bytes([flags]) + payload
    return element(0xA1 if group else 0xA3, body)


def cluster(blocks=None, timestamp=0, *, unknown=False):
    return element(0x1F43B675, uint(0xE7, timestamp) + (blocks if blocks is not None else block()), unknown=unknown)


def document(*, media=None, outer_extra=0, unknown_segment=False, track_data=None, extra=b"", doctype=b"webm"):
    info = element(0x1549A966, uint(0x2AD7B1, 1000000))
    payload = info + (tracks() if track_data is None else track_data) + (cluster() if media is None else media) + extra
    return header(doctype) + element(0x18538067, payload, unknown=unknown_segment, declared=len(payload) + outer_extra)


class WebMStructureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "input.webm"

    def inspect(self, data, **kwargs):
        self.path.write_bytes(data)
        original = self.path.read_bytes()
        result = inspect_webm(self.path, **kwargs)
        self.assertEqual(self.path.read_bytes(), original)
        return result

    def test_complete_observed_media_has_no_original_completeness_claim(self):
        result = self.inspect(document())
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["container"], "webm")
        self.assertEqual(result["outer_envelope"]["status"], "complete")
        self.assertTrue(result["observed_media_elements_complete"])
        self.assertEqual(result["block_counts"], {"audio": 1, "video": 0, "other": 0, "total": 1})
        self.assertEqual(result["hosted_original_completeness"], "not_established")

    def test_outer_shortage_and_stale_cues_are_separate_from_complete_blocks(self):
        info = element(0x1549A966, uint(0x2AD7B1, 1000000))
        track_data = tracks()
        seek = lambda pos: element(0x114D9B74, element(0x4DBB,
            element(0x53AB, b"\x1c\x53\xbb\x6b") + uint(0x53AC, pos, 2)))
        offset = len(seek(0)) + len(info) + len(track_data)
        payload = seek(offset) + info + track_data + cluster()
        data = header() + element(0x18538067, payload, declared=len(payload) + 115)
        result = self.inspect(data)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["outer_envelope"]["declared_bytes_remaining"], 115)
        self.assertTrue(result["observed_media_elements_complete"])
        self.assertFalse(result["seek_index"]["cues_present"])
        self.assertEqual(result["seek_index"]["entries"][0]["actual_target_id"], 0x1F43B675)
        self.assertEqual(result["seek_index"]["entries"][0]["status"], "stale_or_non_element_target")

    def test_true_cluster_and_block_truncation_fail(self):
        for data in (document()[:-1], document(media=cluster(element(0xA3, b"\x81\0\0\x80x", declared=30)))):
            with self.subTest(size=len(data)):
                result = self.inspect(data)
                self.assertEqual(result["status"], "failed")
                self.assertFalse(result["observed_media_elements_complete"])

    def test_complete_cluster_omission_does_not_certify_source_completeness(self):
        # Whole-element deletion cannot be disproved from structure alone.
        result = self.inspect(document(media=cluster(timestamp=0) + cluster(timestamp=200)))
        self.assertTrue(result["observed_media_elements_complete"])
        self.assertEqual(result["hosted_original_completeness"], "not_established")
        self.assertEqual(result["cluster_count"], 2)

    def test_unknown_size_live_segment_and_clusters_remain_explicit(self):
        result = self.inspect(document(media=cluster(timestamp=0, unknown=True) + cluster(timestamp=20, unknown=True),
                                       unknown_segment=True))
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["outer_envelope"]["status"], "unknown")
        self.assertTrue(result["observed_media_elements_complete"])
        self.assertEqual(result["cluster_count"], 2)

    def test_audio_delay_and_discard_metadata_preserved(self):
        group = element(0xA0, block(group=True, flags=0) + element(0x75A2, (62500).to_bytes(3, "big", signed=True)))
        result = self.inspect(document(media=cluster(group)))
        self.assertEqual(result["status"], "passed")
        track = result["tracks"][0]
        self.assertEqual(track["codec_delay_ns"], 6500000)
        self.assertEqual(track["seek_preroll_ns"], 80000000)
        self.assertEqual(track["opus"]["pre_skip_samples"], 312)
        self.assertEqual(result["discard_padding"], [{"track_number": 1, "block_index": 0, "value_ns": 62500, "timestamp_ns": 0}])
        self.assertEqual(track["encoded_frame_count"], 1)

    def test_all_lacing_modes_validate_lengths_and_order(self):
        # Three identical two-byte frames represented using each supported lace.
        bodies = [(0, block(b"ab") + block(b"ab", relative=20) + block(b"ab", relative=40)),
                  (1, block(b"\x02\x02\x02ababab", flags=0x82)),
                  (2, block(b"\x02ababab", flags=0x84)),
                  (3, block(b"\x02\x82\xbfababab", flags=0x86))]
        digests = []
        for mode, body in bodies:
            with self.subTest(mode=mode):
                result = self.inspect(document(media=cluster(body)))
                self.assertEqual(result["status"], "passed", result)
                self.assertEqual(result["tracks"][0]["encoded_frame_count"], 3)
                digests.append(result["tracks"][0]["encoded_frame_sequence_sha256"])
        self.assertEqual(len(set(digests)), 1)

    def test_malformed_lacing_cannot_pass(self):
        for payload, flag in ((b"\0ab", 0x82), (b"\x02\xff", 0x82),
                              (b"\x02abcde", 0x84), (b"\x02\x82\xff", 0x86)):
            with self.subTest(payload=payload):
                self.assertEqual(self.inspect(document(media=cluster(block(payload, flags=flag))))["status"], "failed")

    def test_unknown_media_elements_and_channel_mapping_require_review(self):
        for data in (document(media=cluster(block() + element(0xA2, b"opaque"))),
                     document(track_data=tracks(mapping=1))):
            result = self.inspect(data)
            self.assertEqual(result["status"], "needs_review")
            self.assertIsNone(result["observed_media_elements_complete"])

    def test_invisible_flag_is_supported_and_reserved_flags_require_review(self):
        for simple, flags in ((True, 0x88), (False, 0x08)):
            media = block(flags=flags, group=not simple)
            if not simple:
                media = element(0xA0, media)
            with self.subTest(simple=simple):
                result = self.inspect(document(media=cluster(media)))
                self.assertEqual(result["status"], "passed", result)
        for simple, flags in ((True, 0x90), (False, 0x10)):
            media = block(flags=flags, group=not simple)
            if not simple:
                media = element(0xA0, media)
            with self.subTest(simple=simple):
                result = self.inspect(document(media=cluster(media)))
                self.assertEqual(result["status"], "needs_review")
                self.assertIsNone(result["observed_media_elements_complete"])

    def test_misplaced_known_elements_and_duplicate_timing_cannot_pass(self):
        for data in (document(media=cluster(block() + element(0x4282, b"webm"))),
                     document(media=cluster(element(0xA0, block(group=True, flags=0) + tracks())))):
            result = self.inspect(data)
            self.assertEqual(result["status"], "needs_review")
            self.assertIsNone(result["observed_media_elements_complete"])
        self.assertEqual(self.inspect(document(extra=element(0x1549A966, b"")))["status"], "failed")
        payload = element(0x1549A966, uint(0x2AD7B1, 1) + uint(0x2AD7B1, 2)) + tracks() + cluster()
        self.assertEqual(self.inspect(header() + element(0x18538067, payload))["status"], "failed")

    def test_unsupported_required_read_version_is_not_a_pass(self):
        ebml = element(0x1A45DFA3, element(0x4282, b"webm") + uint(0x42F7, 2))
        payload = element(0x1549A966, b"") + tracks() + cluster()
        result = self.inspect(ebml + element(0x18538067, payload))
        self.assertEqual(result["status"], "needs_review")
        self.assertIsNone(result["observed_media_elements_complete"])

    def test_invalid_track_reference_or_duplicate_track_fails(self):
        for data in (document(media=cluster(block(track=2))),
                     document(track_data=tracks() + tracks())):
            result = self.inspect(data)
            self.assertEqual(result["status"], "failed")

    def test_byte_element_nesting_and_vint_limits(self):
        data = document()
        for kwargs in ({"max_bytes": len(data) - 1}, {"max_elements": 1}):
            result = self.inspect(data, **kwargs)
            self.assertEqual(result["status"], "needs_review")
            self.assertIsNone(result["observed_media_elements_complete"])
        laced = document(media=cluster(block(bytes([99]) + b"ab" * 100, flags=0x84)))
        result = self.inspect(laced, max_elements=60)
        self.assertEqual(result["status"], "needs_review")
        self.assertIn("Encoded frame count budget exceeded", result["review_reasons"])
        nested = element(0x4487, b"x")
        for _ in range(20):
            nested = element(0x67C8, nested)
        self.assertEqual(self.inspect(document(extra=element(0x1254C367, nested)))["status"], "needs_review")
        self.assertEqual(self.inspect(header() + b"\x00")["status"], "failed")
        # A huge declared outer envelope does not allocate the declared size.
        payload = element(0x1549A966, b"") + tracks() + cluster()
        result = self.inspect(header() + element(0x18538067, payload, declared=(1 << 55)))
        self.assertEqual(result["status"], "needs_review")
        self.assertTrue(result["observed_media_elements_complete"])

    def test_trailing_segment_and_partial_header_cannot_pass(self):
        self.assertEqual(self.inspect(document() + b"extra")["status"], "failed")
        self.assertEqual(self.inspect(b"\x1a\x45\xdf")["status"], "failed")
        self.assertEqual(self.inspect(b"ordinary media")["status"], "not_applicable")

    def test_cue_cluster_targets_are_checked(self):
        info = element(0x1549A966, b"")
        track_data = tracks()
        position = len(info) + len(track_data)
        def cue(offset):
            return element(0x1C53BB6B, element(0xBB, uint(0xB3, 0) +
                element(0xB7, uint(0xF7, 1) + uint(0xF1, offset))))
        payload = info + track_data + cluster() + cue(position)
        good = self.inspect(header() + element(0x18538067, payload))
        self.assertEqual(good["status"], "passed")
        payload = info + track_data + cluster() + cue(position + 1)
        bad = self.inspect(header() + element(0x18538067, payload))
        self.assertEqual(bad["status"], "needs_review")
        self.assertTrue(bad["observed_media_elements_complete"])

    def test_empty_integer_defaults_and_video_count(self):
        info = element(0x1549A966, element(0x2AD7B1, b""))
        payload = info + tracks(codec=b"V_VP9", track_type=1) + cluster()
        result = self.inspect(header(b"matroska") + element(0x18538067, payload))
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["timestamp_scale_ns"], 1000000)
        self.assertEqual(result["block_counts"]["video"], 1)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg required for real mux fixture")
    def test_ffmpeg_generated_opus_matroska(self):
        source = Path(self.temp.name) / "generated.mka"
        q = subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-nostdin", "-n",
            "-f", "lavfi", "-i", "sine=frequency=400:duration=0.2", "-c:a", "libopus", str(source)],
            capture_output=True, text=True)
        self.assertEqual(q.returncode, 0, q.stderr)
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        result = inspect_webm(source)
        self.assertEqual(result["status"], "passed", result)
        self.assertTrue(result["observed_media_elements_complete"])
        self.assertGreater(result["block_counts"]["audio"], 0)
        self.assertEqual(result["tracks"][0]["codec_id"], "A_OPUS")
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)


if __name__ == "__main__":
    unittest.main()
