# SPDX-License-Identifier: MIT
"""Synthetic fragment-table evidence tests; fixtures are not codec/decode tests."""
import copy
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from mp4_evidence import recover_packet_durations


def box(kind, payload, extended=False):
    if extended:
        return struct.pack(">I4sQ", 1, kind, len(payload) + 16) + payload
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def full(kind, payload=b"", flags=0, version=0):
    return box(kind, bytes([version]) + flags.to_bytes(3, "big") + payload)


def track(identity, kind, scale, trex_duration, *, edit=False, external=False, encrypted=False):
    tkhd = full(b"tkhd", b"\0" * 8 + struct.pack(">III", identity, 0, 0))
    mdhd = full(b"mdhd", b"\0" * 8 + struct.pack(">III", scale, 0, 0))
    hdlr = full(b"hdlr", b"\0" * 4 + (b"vide" if kind == "video" else b"soun") + b"\0" * 12)
    reference = full(b"url ", b"" if not external else b"external\0", flags=0 if external else 1)
    dinf = box(b"dinf", full(b"dref", struct.pack(">I", 1) + reference))
    entry_type = b"encv" if encrypted else b"avc1" if kind == "video" else b"mp4a"
    entry = box(entry_type, b"\0" * 6 + struct.pack(">H", 1))
    stbl = box(b"stbl", full(b"stsd", struct.pack(">I", 1) + entry)
               + full(b"stts", b"\0" * 4) + full(b"stsc", b"\0" * 4)
               + full(b"stco", b"\0" * 4) + full(b"stsz", b"\0" * 8))
    mdia = box(b"mdia", mdhd + hdlr + box(b"minf", dinf + stbl))
    trak = box(b"trak", tkhd + (box(b"edts", full(b"elst", b"\0" * 4)) if edit else b"") + mdia)
    trex = full(b"trex", struct.pack(">IIIII", identity, 1, trex_duration, 0, 0))
    return trak, trex


def fixture(*, mode="tfhd", cto=(0, 0, 0), version=0, stream_ticks=1,
            missing=(0, 1), tracks=1, edit=False, external=False, encrypted=False,
            offset_adjust=0, omit_tfdt=False, split_runs=False, explicit_base=False):
    """Minimal self-contained tables plus hash-matched arbitrary sample payloads."""
    traks, defaults, definitions = [], [], []
    probe = {"streams": [], "packets": []}
    for number in range(tracks):
        identity = number + 1
        kind = "video" if number == 0 else "audio"
        trak, trex = track(identity, kind, 1000, 10 if mode == "trex" else 777,
                            edit=edit, external=external, encrypted=encrypted)
        traks.append(trak); defaults.append(trex)
        probe["streams"].append({"index": number, "id": hex(identity), "codec_type": kind,
                                  "time_base": "1/" + str(1000 * stream_ticks)})
        payloads = [bytes([65 + number, 97 + index]) * (index + 1) for index in range(3)]
        definitions.append((identity, payloads))
        for index, payload in enumerate(payloads):
            packet = {"stream_index": number, "pts": (100 + index * 10 + cto[index]) * stream_ticks,
                      "dts": (100 + index * 10) * stream_ticks,
                      "data_hash": "SHA256:" + hashlib.sha256(payload).hexdigest()}
            if index not in missing:
                packet["duration"] = 10 * stream_ticks
            probe["packets"].append(packet)
    start = box(b"ftyp", b"isom\0\0\0\0isom") + box(b"moov", box(b"mvex", b"".join(defaults)) + b"".join(traks))

    def fragment(offsets):
        trafs = []
        for identity, payloads in definitions:
            # Explicit base points to file start. Other fixtures use moof-relative addressing.
            tfhd_flags = 1 if explicit_base else 0x020000
            tfhd_data = struct.pack(">I", identity) + (b"\0" * 8 if explicit_base else b"")
            if mode in ("tfhd", "trun"):
                tfhd_flags |= 8
                tfhd_data += struct.pack(">I", 10 if mode == "tfhd" else 333)
            tfhd = full(b"tfhd", tfhd_data, flags=tfhd_flags)
            tfdt = b"" if omit_tfdt else full(b"tfdt", struct.pack(">Q", 100), version=1)
            groups = ((0, 1), (1, 3)) if split_runs else ((0, 3),)
            truns = []
            for run_number, (first, end) in enumerate(groups):
                flags = 0x200 | (0x100 if mode == "trun" else 0) | (0x800 if any(cto) else 0)
                # The second run deliberately exercises implicit continuation.
                if run_number == 0:
                    flags |= 1
                data = struct.pack(">I", end - first)
                if flags & 1:
                    position = offsets.get(identity, 0) + offset_adjust
                    data += struct.pack(">i", position)
                for index in range(first, end):
                    if flags & 0x100:
                        data += struct.pack(">I", 10)
                    data += struct.pack(">I", len(payloads[index]))
                    if flags & 0x800:
                        data += struct.pack(">i" if version else ">I", cto[index])
                truns.append(full(b"trun", data, flags=flags, version=version))
            trafs.append(box(b"traf", tfhd + tfdt + b"".join(truns)))
        return box(b"moof", full(b"mfhd", struct.pack(">I", 1)) + b"".join(trafs))

    empty = fragment({})
    offsets = {}
    cumulative = len(start) if explicit_base else 0
    cumulative += len(empty) + 8
    for identity, payloads in definitions:
        offsets[identity] = cumulative
        cumulative += sum(map(len, payloads))
    moof = fragment(offsets)
    return start + moof + box(b"mdat", b"".join(payload for _, samples in definitions for payload in samples)), probe


class MP4EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "input.mp4"

    def inspect(self, contents, probe, **kwargs):
        self.source.write_bytes(contents)
        before, original_probe = self.source.read_bytes(), copy.deepcopy(probe)
        result = recover_packet_durations(self.source, probe, **kwargs)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(probe, original_probe)
        json.dumps(result, allow_nan=False)
        return result

    def test_all_default_precedence_modes_recover_only_missing_values(self):
        for mode in ("tfhd", "trex", "trun"):
            with self.subTest(mode=mode):
                data, probe = fixture(mode=mode)
                result = self.inspect(data, probe)
                self.assertEqual(result["status"], "verified", result)
                stream = result["streams"][0]
                self.assertEqual(stream["updates"], [{"packet_index": 0, "duration": 10}, {"packet_index": 1, "duration": 10}])
                self.assertEqual([s["duration"] for s in stream["samples"]], [10] * 3)
                self.assertEqual(stream["verified_sample_count"], 3)
                self.assertTrue(all(s["duration_basis"].startswith(mode) for s in stream["samples"]))

    def test_native_tick_conversion_signed_offsets_and_multiple_tracks(self):
        data, probe = fixture(mode="trun", cto=(-5, 3, 0), version=1, stream_ticks=2, tracks=2)
        result = self.inspect(data, probe)
        self.assertEqual(result["status"], "verified", result)
        self.assertEqual(len(result["streams"]), 2)
        self.assertEqual(result["streams"][0]["updates"][0]["duration"], 20)
        self.assertEqual(result["streams"][0]["samples"][0]["pts"], 190)
        self.assertEqual(result["source"]["sha256"], hashlib.sha256(data).hexdigest())

    def test_multiple_runs_and_explicit_absolute_base(self):
        for explicit in (False, True):
            data, probe = fixture(split_runs=True, explicit_base=explicit)
            result = self.inspect(data, probe)
            self.assertEqual(result["status"], "verified", result)
            self.assertEqual([s["run_index"] for s in result["streams"][0]["samples"]], [0, 1, 1])

    def test_extended_mdat_header_preserves_real_payload_bounds(self):
        data, probe = fixture()
        start = data.index(b"mdat") - 4
        prefix = bytearray(data[:start])
        offset = data.index(b"trun") + 12
        previous = struct.unpack_from(">i", prefix, offset)[0]
        struct.pack_into(">i", prefix, offset, previous + 8)
        modified = bytes(prefix) + box(b"mdat", data[start + 8:], extended=True)
        result = self.inspect(modified, probe)
        self.assertEqual(result["status"], "verified", result)
        self.assertEqual(result["streams"][0]["samples"][0]["offset"], start + 16)

    def test_contradictions_withhold_every_update(self):
        mutations = [lambda p: p["packets"][-1].update(data_hash="SHA256:" + "0" * 64),
                     lambda p: p["packets"][-1].update(pts=999),
                     lambda p: p["packets"][-1].update(dts=999),
                     lambda p: p["packets"][-1].update(duration=11),
                     lambda p: p["packets"].pop()]
        for change in mutations:
            with self.subTest(change=change):
                data, probe = fixture(tracks=2)
                change(probe)
                result = self.inspect(data, probe)
                self.assertEqual(result["status"], "failed", result)
                self.assertEqual(result["streams"], [])

    def test_truncated_boxes_and_run_counts_are_refused(self):
        data, probe = fixture()
        bad_count = bytearray(data)
        position = data.index(b"trun") + 8
        struct.pack_into(">I", bad_count, position, 4)
        for modified in (data[:-1], data + b"bad", bytes(bad_count)):
            result = self.inspect(modified, probe)
            self.assertEqual(result["status"], "failed", result)
            self.assertEqual(result["streams"], [])

    def test_sample_outside_mdat_and_changed_payload_are_refused(self):
        outside, probe = fixture(offset_adjust=-1)
        self.assertEqual(self.inspect(outside, probe)["status"], "failed")
        data, probe = fixture()
        corrupt = data[:-1] + bytes([data[-1] ^ 1])
        self.assertEqual(self.inspect(corrupt, probe)["status"], "failed")

    def test_unimplemented_layouts_do_not_grant_evidence(self):
        for kwargs in ({"edit": True}, {"external": True}, {"encrypted": True}):
            data, probe = fixture(**kwargs)
            result = self.inspect(data, probe)
            self.assertEqual(result["status"], "unsupported", result)
            self.assertEqual(result["streams"], [])
        data, probe = fixture(omit_tfdt=True)
        self.assertEqual(self.inspect(data, probe)["status"], "failed")

    def test_limits_do_not_verify_a_prefix_or_partial_track_inventory(self):
        data, probe = fixture(tracks=2)
        for limits in ({"max_bytes": len(data) - 1}, {"max_samples": 5}):
            result = self.inspect(data, probe, **limits)
            self.assertEqual(result["status"], "needs_review", result)
            self.assertEqual(result["streams"], [])
        probe["streams"].pop()
        result = self.inspect(data, probe)
        self.assertNotEqual(result["status"], "verified")
        self.assertEqual(result["streams"], [])

    def test_non_fragmented_and_other_formats_are_not_applicable(self):
        for contents in (b"unrelated file", box(b"ftyp", b"isom") + box(b"moov", b"")):
            self.assertEqual(self.inspect(contents, {})["status"], "not_applicable")

    def test_open_ended_box_is_not_treated_as_complete(self):
        data, probe = fixture()
        modified = bytearray(data)
        struct.pack_into(">I", modified, data.index(b"mdat") - 4, 0)
        result = self.inspect(bytes(modified), probe)
        self.assertEqual(result["status"], "unsupported", result)
        self.assertEqual(result["streams"], [])

    def test_malformed_probe_and_numeric_budgets_fail_without_updates(self):
        data, probe = fixture()
        for malformed in (None, [], {"streams": [None], "packets": []},
                          {"streams": probe["streams"], "packets": [None]}):
            result = self.inspect(data, malformed)
            self.assertEqual(result["status"], "failed", result)
            self.assertEqual(result["streams"], [])
        for limits in ({"max_samples": True}, {"max_bytes": 0}, {"timeout": float("nan")}):
            self.assertEqual(self.inspect(data, probe, **limits)["status"], "failed")
        with patch("mp4_evidence.time.monotonic", side_effect=[0, 11]):
            result = self.inspect(data, probe)
        self.assertEqual(result["status"], "needs_review", result)
        self.assertEqual(result["streams"], [])


if __name__ == "__main__":
    unittest.main()
