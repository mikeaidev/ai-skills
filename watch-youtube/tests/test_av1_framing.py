# SPDX-License-Identifier: MIT
"""Narrow AV1 rewrite, evidence binding and native-clock regressions."""
import copy
from fractions import Fraction
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import av1_framing as av1


def dump(data):
    lines = []
    for start in range(0, len(data), 16):
        chunk = data[start:start + 16]
        text = " ".join(chunk[index:index + 2].hex() for index in range(0, len(chunk), 2))
        lines.append(f"{start:08x}: {text:<39}  {'a' * len(chunk)}")
    return "\n" + "\n".join(lines) + ("\n" if lines else "")


def probe(payloads, base="1/60000", step=1001, extra=b"\x81\x00\x0c\x00\x0a\x01\x01"):
    stream = {"index": 0, "codec_type": "video", "codec_name": "av1", "profile": "Main",
              "level": 0, "width": 64, "height": 48, "pix_fmt": "yuv420p",
              "time_base": base, "avg_frame_rate": str(1 / (step * Fraction(base))),
              "extradata": dump(extra), "extradata_size": len(extra),
              "extradata_hash": "SHA256:" + hashlib.sha256(extra).hexdigest()}
    packets = [{"stream_index": 0, "pts": index * step, "dts": index * step,
                "duration": step, "size": str(len(value)), "data": dump(value),
                "data_hash": "SHA256:" + hashlib.sha256(value).hexdigest()}
               for index, value in enumerate(payloads)]
    return {"streams": [stream], "packets": packets}


class ObuTests(unittest.TestCase):
    def test_only_exact_empty_leading_delimiter_is_normalized(self):
        body = b"\x0a\x01\x01\x32\x02\xaa\xbb"
        self.assertEqual(av1.canonical_packet(b"\x12\x00" + body), (body, True))
        self.assertEqual(av1.canonical_packet(body), (body, False))
        self.assertEqual([item["type"] for item in av1.parse_obus(body)], [1, 6])

    def test_malformed_reserved_and_nonleading_delimiters_are_refused(self):
        body = b"\x32\x01\xaa"
        bad = (b"", b"\x12", b"\x12\x01\x00", b"\x12\x00",
               b"\x12\x00\x12\x00" + body, body + b"\x12\x00",
               b"\xb2\x01\xaa", b"\x33\x01\xaa", b"\x36\x01\x00",
               b"\x32\x05\xaa", b"\x32" + b"\x80" * 8,
               b"\x4a\x00", b"\x12\x80\x00" + body)
        for value in bad:
            with self.subTest(packet=value):
                with self.assertRaises(av1.FramingError):
                    av1.canonical_packet(value)

    def test_last_implicit_size_and_valid_extension_are_bounded(self):
        self.assertEqual(av1.canonical_packet(b"\x30\xaa\xbb"), (b"\x30\xaa\xbb", False))
        self.assertEqual(av1.parse_obus(b"\x36\x00\x01\xaa")[0]["payload_bytes"], 1)
        with self.assertRaises(av1.DiagnosticLimit):
            av1.parse_obus(b"\x7a\x00" * 3, max_obus=2)

    def test_hex_dump_offsets_sizes_and_ascii_are_checked(self):
        value = bytes(range(37))
        self.assertEqual(av1.decode_hexdump(dump(value), len(value)), value)
        # Hex-looking ASCII after the column separator is never payload.
        self.assertEqual(av1.decode_hexdump("00000000: 1200  1234", 2), b"\x12\x00")
        for text, size in (("00000001: 1200  aa", 2), (dump(value), 36),
                           ("00000000: 12xz  text", 2), ("not a byte dump", 0)):
            with self.subTest(text=text):
                with self.assertRaises(av1.FramingError):
                    av1.decode_hexdump(text, size)


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.mp4"
        self.output = self.root / "output.mkv"
        self.source.write_bytes(b"mock source file")
        self.output.write_bytes(b"mock output file")
        self.payloads = [b"\x12\x00\x0a\x01\x01\x32\x01\xaa", b"\x12\x00\x32\x01\xbb"]
        self.a = probe(self.payloads)
        self.b = probe([value[2:] for value in self.payloads], "1/1000", 17)

    def compare(self, a=None, b=None, fresh_a=None, fresh_b=None, **options):
        a = copy.deepcopy(a if a is not None else self.a)
        b = copy.deepcopy(b if b is not None else self.b)
        fresh = [copy.deepcopy(fresh_a if fresh_a is not None else a),
                 copy.deepcopy(fresh_b if fresh_b is not None else b)]
        def provide(run, path, label):
            value = fresh[0 if label == "source" else 1]
            return value["streams"][0], value["packets"]
        with patch.object(av1._Run, "probe", provide):
            return av1.compare_av1_framing(self.source, self.output, a, b,
                                           options.get("directory", self.root / "diagnostics"),
                                           options.get("timeout", 60), options.get("max_bytes", 1024 * 1024))

    def test_delimiter_rewrite_aligns_packets_without_claiming_strict_identity(self):
        result = self.compare()
        self.assertEqual(result["status"], "passed", result)
        self.assertTrue(result["canonical_payloads_identical"])
        self.assertEqual(result["strict_encoded_identity"], "failed")
        self.assertEqual(result["normalization_counts"], {"source_leading_td_removed": 2})
        self.assertEqual(result["timestamp_preservation"], "passed")
        self.assertEqual(result["duration_preservation"], "passed")
        self.assertEqual(result["timestamps"]["pts"]["compared"], 2)
        self.assertTrue(result["inputs_unchanged"])

    def test_insertion_and_exact_identity_are_distinct_relations(self):
        for a, b, relation, strict in ((self.b, self.a, "output_leading_td_removed", "failed"),
                                       (self.a, self.a, "strict_identical", "passed")):
            with self.subTest(relation=relation):
                destination = self.root / relation
                result = self.compare(a, b, directory=destination)
                self.assertEqual(result["status"], "passed", result)
                self.assertEqual(result["strict_encoded_identity"], strict)
                self.assertEqual(result["normalization_counts"], {relation: 2})

    def test_changed_payload_padding_and_reordered_packets_do_not_align(self):
        body = [value[2:] for value in self.payloads]
        alternatives = ([body[0][:-1] + b"\xab", body[1]],
                        [body[0] + b"\x7a\x00", body[1]], list(reversed(body)), body[:1])
        for index, values in enumerate(alternatives):
            with self.subTest(change=index):
                result = self.compare(b=probe(values, "1/1000", 17), directory=self.root / str(index))
                self.assertNotEqual(result["status"], "passed", result)
                self.assertFalse(result["canonical_payloads_identical"])
                self.assertEqual(result["packet_alignment"], "not_established")
                self.assertEqual(result["timestamp_preservation"], "not_compared")

    def test_caller_hash_and_timestamp_must_match_fresh_extraction(self):
        for field, value in (("data_hash", "SHA256:" + "0" * 64), ("pts", 1000), ("size", "99")):
            a = copy.deepcopy(self.a)
            a["packets"][0][field] = value
            result = self.compare(a=a, fresh_a=self.a, directory=self.root / field)
            self.assertNotEqual(result["status"], "passed", result)
            self.assertFalse(result["canonical_payloads_identical"])

    def test_config_change_and_malformed_packets_are_not_blessed(self):
        a = copy.deepcopy(self.a)
        b = probe([value[2:] for value in self.payloads], extra=b"\x81\x01\x0c\x00")
        first = self.compare(a=a, b=b, directory=self.root / "config")
        self.assertNotEqual(first["status"], "passed")
        bad = probe([b"\x32\x05\xaa", self.payloads[1][2:]])
        second = self.compare(b=bad, directory=self.root / "bad")
        self.assertFalse(second["canonical_payloads_identical"])

    def test_timing_failure_stays_separate_from_framing_equality(self):
        b = copy.deepcopy(self.b)
        b["packets"][1]["pts"] += 100
        b["packets"][1]["dts"] += 100
        result = self.compare(b=b)
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["canonical_payloads_identical"])
        self.assertEqual(result["timestamp_preservation"], "failed")
        self.assertEqual(result["timing_status"], "failed")
        self.assertEqual(result["timestamps"]["pts"]["outside_allowance_packet_indices"], [1])

    def test_missing_timing_requires_review_without_synthesizing_values(self):
        a = copy.deepcopy(self.a)
        del a["packets"][0]["pts"]
        del a["packets"][0]["duration"]
        result = self.compare(a=a)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["timestamp_preservation"], "needs_review")
        self.assertEqual(result["duration_preservation"], "needs_review")
        self.assertEqual(result["timestamps"]["duration"]["source_missing"], 1)

    def test_bound_mp4_duration_evidence_can_fill_probe_omission(self):
        caller, fresh = copy.deepcopy(self.a), copy.deepcopy(self.a)
        del fresh["packets"][0]["duration"]
        caller["packets"][0]["duration_evidence"] = {
            "basis": "verified_mp4_fragment_sample", "source_sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
            "track_id": 1, "sample_index": 0, "native_duration": 1001, "time_base": "1/60000"}
        result = self.compare(a=caller, fresh_a=fresh, directory=self.root / "verified")
        self.assertEqual(result["duration_preservation"], "passed", result)
        caller["packets"][0]["duration_evidence"]["source_sha256"] = "0" * 64
        refused = self.compare(a=caller, fresh_a=fresh, directory=self.root / "unbound")
        self.assertNotEqual(refused["status"], "passed")

    def test_existing_destination_is_untouched_and_other_codecs_are_not_applicable(self):
        directory = self.root / "occupied" / "av1-framing"
        directory.mkdir(parents=True)
        marker = directory / "old.txt"
        marker.write_bytes(b"keep")
        result = self.compare(directory=directory.parent)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(marker.read_bytes(), b"keep")
        a, b = copy.deepcopy(self.a), copy.deepcopy(self.b)
        a["streams"][0]["codec_name"] = b["streams"][0]["codec_name"] = "vp9"
        other = self.compare(a=a, b=b, directory=self.root / "nonav1")
        self.assertEqual(other["status"], "not_applicable")
        self.assertFalse((self.root / "nonav1").exists())

    def test_unbound_duration_override_and_malformed_probe_are_refused(self):
        fresh = copy.deepcopy(self.a)
        del fresh["packets"][0]["duration"]
        result = self.compare(fresh_a=fresh)
        self.assertNotEqual(result["status"], "passed")
        for invalid in (None, [], {"streams": [None], "packets": []}):
            with self.subTest(probe=invalid):
                value = av1.compare_av1_framing(self.source, self.output, invalid, self.b,
                                               self.root / "unused")
                self.assertEqual(value["status"], "unsupported")

    def test_input_change_revokes_framing_and_timing_conclusions(self):
        def provide(run, path, label):
            data = self.a if label == "source" else self.b
            if label == "output":
                self.source.write_bytes(b"changed while comparing")
            return data["streams"][0], data["packets"]
        with patch.object(av1._Run, "probe", provide):
            result = av1.compare_av1_framing(self.source, self.output, self.a, self.b,
                                           self.root / "modified")
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["inputs_unchanged"])
        self.assertFalse(result["canonical_payloads_identical"])
        self.assertEqual(result["timestamp_preservation"], "not_compared")
        self.assertEqual(result["timestamps"], {})

    def test_invalid_resource_arguments_refuse_before_artifacts_or_probe(self):
        invalid_timeouts = (float("nan"), float("inf"), float("-inf"), 0, -1, True, "60", None)
        invalid_budgets = (float("nan"), float("inf"), 0, -1, True, 1.5, "1024", None)
        before = (self.source.read_bytes(), self.output.read_bytes())
        for field, values in (("timeout", invalid_timeouts), ("max_bytes", invalid_budgets)):
            for index, value in enumerate(values):
                with self.subTest(field=field, value=value):
                    destination = self.root / (field + str(index))
                    with patch.object(av1._Run, "probe") as mocked:
                        result = av1.compare_av1_framing(self.source, self.output, self.a, self.b,
                                                        destination, **{field: value})
                    self.assertEqual(result["status"], "unsupported")
                    mocked.assert_not_called()
                    self.assertFalse(destination.exists())
                    self.assertEqual((self.source.read_bytes(), self.output.read_bytes()), before)

    def test_caller_inventory_limit_is_checked_before_selection_or_artifacts(self):
        destination = self.root / "too_many_packets"
        with patch.object(av1, "MAX_PACKETS", 1), patch.object(av1, "_selected") as selected:
            result = av1.compare_av1_framing(self.source, self.output, self.a, self.b, destination)
        self.assertEqual(result["status"], "needs_review")
        selected.assert_not_called()
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
