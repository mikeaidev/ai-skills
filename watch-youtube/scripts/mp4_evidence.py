#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Recover missing packet durations from verified, bounded MP4 fragment tables.

This is a restricted evidence reader, not a general MP4 demuxer. It accepts
self-contained fragmented audio/video tracks without edit lists or encryption.
It grants no duration updates until every fragment sample matches the supplied
probe's ordered payload hash, PTS/DTS and any already-known positive duration.
It never changes a caller's probe, fits a timestamp offset, or proves that all
hosted-source fragments were captured. All results are JSON serializable.
"""
from dataclasses import dataclass
from bisect import bisect_right
from fractions import Fraction
import hashlib
import math
from pathlib import Path
import re
import stat
import struct
import time


class Unsupported(ValueError):
    """A valid layout may require features this restricted reader does not implement."""


class LimitReached(ValueError):
    """A prefix or resource-limited check cannot grant duration evidence."""


@dataclass(frozen=True)
class _Box:
    kind: bytes
    start: int
    payload: int
    end: int


class _Reader:
    def __init__(self, data, max_samples, deadline):
        self.data = data
        self.max_samples = max_samples
        self.box_count = 0
        self.sample_count = 0
        self.box_limit = max(1000, min(200000, max_samples * 16))
        self.deadline = deadline

    def check(self):
        if time.monotonic() >= self.deadline:
            raise LimitReached("MP4 evidence time budget exceeded")

    def boxes(self, start, end):
        result = []
        while start < end:
            self.check()
            if end - start < 8:
                raise ValueError("Truncated MP4 box header")
            size, kind = struct.unpack_from(">I4s", self.data, start)
            header = 8
            if size == 1:
                if end - start < 16:
                    raise ValueError("Truncated extended MP4 box header")
                size = struct.unpack_from(">Q", self.data, start + 8)[0]
                header = 16
            elif size == 0:
                # Open-ended boxes require semantics beyond this evidence reader.
                raise Unsupported("Open-ended MP4 box requires independent review")
            if size < header or size > end - start:
                raise ValueError("MP4 box length exceeds its containing bytes")
            self.box_count += 1
            if self.box_count > self.box_limit:
                raise LimitReached("MP4 box-count budget exceeded")
            result.append(_Box(kind, start, start + header, start + size))
            start += size
        return result

    def children(self, box):
        return self.boxes(box.payload, box.end)

    def uint(self, offset, end, width=4, signed=False):
        if offset < 0 or offset + width > end:
            raise ValueError("Truncated MP4 table field")
        return int.from_bytes(self.data[offset:offset + width], "big", signed=signed)

    def full(self, box, versions=(0,), flags=None):
        value = self.uint(box.payload, box.end)
        version, found = value >> 24, value & 0xffffff
        if version not in versions or (flags is not None and found & ~flags):
            raise Unsupported("Unsupported " + box.kind.decode("ascii", "replace") + " version/flags")
        return version, found, box.payload + 4


def _one(boxes, kind, required=True):
    found = [box for box in boxes if box.kind == kind]
    if len(found) > 1 or (required and len(found) != 1):
        raise ValueError("Require one " + kind.decode("ascii", "replace") + " box")
    return found[0] if found else None


def _integer(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and len(value) <= 80 and re.fullmatch(r"[+-]?[0-9]+", value):
        return int(value)
    raise ValueError("Probe requires exact integer packet timestamps/durations")


def _track_id(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and len(value) <= 18 and re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)", value):
        return int(value, 16 if value.lower().startswith("0x") else 10)
    raise Unsupported("Probe stream lacks an explicit MP4 track ID")


def _tracks(reader, moov):
    children = reader.children(moov)
    mvex = _one(children, b"mvex", False)
    if mvex is None:
        raise Unsupported("Movie is not a supported fragmented MP4 initialization")
    defaults = {}
    for box in reader.children(mvex):
        if box.kind != b"trex":
            continue
        _, _, offset = reader.full(box, flags=0)
        values = [reader.uint(offset + i * 4, box.end) for i in range(5)]
        if offset + 20 != box.end or not values[0] or values[0] in defaults:
            raise ValueError("Invalid or repeated track defaults")
        defaults[values[0]] = {"description": values[1], "duration": values[2], "size": values[3]}
    tracks = {}
    for trak in (box for box in children if box.kind == b"trak"):
        inner = reader.children(trak)
        if any(box.kind == b"edts" for box in inner):
            raise Unsupported("Edit-list timeline mapping is not implemented")
        tkhd = _one(inner, b"tkhd")
        version, _, offset = reader.full(tkhd, versions=(0, 1))
        identity = reader.uint(offset + (16 if version else 8), tkhd.end)
        if not identity or identity in tracks:
            raise ValueError("Invalid or repeated MP4 track ID")
        mdia = reader.children(_one(inner, b"mdia"))
        mdhd = _one(mdia, b"mdhd")
        version, _, offset = reader.full(mdhd, versions=(0, 1), flags=0)
        scale = reader.uint(offset + (16 if version else 8), mdhd.end)
        if scale <= 0:
            raise ValueError("MP4 media timescale must be positive")
        hdlr = _one(mdia, b"hdlr")
        _, _, offset = reader.full(hdlr, flags=0)
        reader.uint(offset + 4, hdlr.end)
        handler = reader.data[offset + 4:offset + 8]
        if handler not in (b"vide", b"soun"):
            raise Unsupported("Only fragmented audio/video tracks are supported")
        minf = reader.children(_one(mdia, b"minf"))
        dref = _one(reader.children(_one(minf, b"dinf")), b"dref")
        _, _, offset = reader.full(dref, flags=0)
        count = reader.uint(offset, dref.end)
        refs = reader.boxes(offset + 4, dref.end)
        if count != len(refs) or count == 0:
            raise ValueError("Invalid MP4 data-reference count")
        contained = set()
        for index, ref in enumerate(refs, 1):
            if ref.kind not in (b"url ", b"urn "):
                raise Unsupported("Unknown MP4 data-reference type")
            _, flags, _ = reader.full(ref, flags=1)
            if flags == 1:
                contained.add(index)
        stbl = reader.children(_one(minf, b"stbl"))
        for box in stbl:
            if box.kind in (b"stts", b"ctts", b"stsc", b"stco", b"co64", b"stss"):
                _, _, offset = reader.full(box)
                if reader.uint(offset, box.end):
                    raise Unsupported("Mixed initialized/fragment sample tables are not supported")
            elif box.kind in (b"stsz", b"stz2"):
                _, _, offset = reader.full(box)
                if reader.uint(offset + 4, box.end):
                    raise Unsupported("Mixed initialized/fragment sample sizes are not supported")
        stsd = _one(stbl, b"stsd")
        _, _, offset = reader.full(stsd, flags=0)
        count = reader.uint(offset, stsd.end)
        entries = reader.boxes(offset + 4, stsd.end)
        if count != len(entries) or count == 0:
            raise ValueError("Invalid MP4 sample-description count")
        for entry in entries:
            if entry.kind in (b"encv", b"enca"):
                raise Unsupported("Encrypted MP4 sample entries are not supported")
            if reader.uint(entry.payload + 6, entry.end, 2) not in contained:
                raise Unsupported("External MP4 sample data references are not supported")
        if identity not in defaults:
            raise Unsupported("Fragment track has no trex defaults")
        tracks[identity] = {"timescale": scale, "kind": "video" if handler == b"vide" else "audio",
                            "descriptions": count, "defaults": defaults[identity], "samples": []}
    if not tracks:
        raise ValueError("No audio/video tracks in fragmented movie")
    return tracks


def _samples(reader, top, tracks):
    mdats = [(box.payload, box.end) for box in top if box.kind == b"mdat"]
    mdat_starts = [start for start, _ in mdats]
    moofs = [box for box in top if box.kind == b"moof"]
    if not moofs or not mdats:
        raise Unsupported("No complete fragmented MP4 media samples")
    ranges = []
    for fragment_index, moof in enumerate(moofs):
        boxes = reader.children(moof)
        mfhd = _one(boxes, b"mfhd")
        _, _, offset = reader.full(mfhd, flags=0)
        reader.uint(offset, mfhd.end)
        if offset + 4 != mfhd.end:
            raise ValueError("Invalid movie-fragment header length")
        trafs = [box for box in boxes if box.kind == b"traf"]
        if not trafs:
            raise ValueError("Movie fragment has no track fragments")
        for traf in trafs:
            entries = reader.children(traf)
            if any(box.kind in (b"senc", b"saiz", b"saio") for box in entries):
                raise Unsupported("Auxiliary/encrypted fragment data is not supported")
            tfhd = _one(entries, b"tfhd")
            _, flags, offset = reader.full(tfhd, flags=0x03003b)
            track_id = reader.uint(offset, tfhd.end)
            offset += 4
            if track_id not in tracks:
                raise ValueError("Fragment references an unknown track")
            track = tracks[track_id]
            if flags & 1 and flags & 0x020000:
                raise ValueError("Contradictory MP4 fragment base-offset flags")
            if flags & 1:
                base = reader.uint(offset, tfhd.end, 8)
                offset += 8
            elif flags & 0x020000 or len(trafs) == 1:
                base = moof.start
            else:
                raise Unsupported("Implicit offsets across multiple track fragments are not supported")
            description = track["defaults"]["description"]
            if flags & 2:
                description = reader.uint(offset, tfhd.end)
                offset += 4
            if not 1 <= description <= track["descriptions"]:
                raise ValueError("Fragment references an unknown sample description")
            default_duration = track["defaults"]["duration"]
            duration_basis = "trex_default_sample_duration"
            if flags & 8:
                default_duration = reader.uint(offset, tfhd.end)
                duration_basis = "tfhd_default_sample_duration"
                offset += 4
            default_size = track["defaults"]["size"]
            if flags & 16:
                default_size = reader.uint(offset, tfhd.end)
                offset += 4
            if flags & 32:
                reader.uint(offset, tfhd.end)
                offset += 4
            if offset != tfhd.end:
                raise ValueError("Unparsed track-fragment header bytes")
            tfdt = _one(entries, b"tfdt")
            version, _, offset = reader.full(tfdt, versions=(0, 1), flags=0)
            dts = reader.uint(offset, tfdt.end, 8 if version else 4)
            if offset + (8 if version else 4) != tfdt.end:
                raise ValueError("Invalid track-fragment decode-time length")
            truns = [box for box in entries if box.kind == b"trun"]
            if not truns:
                raise ValueError("Track fragment has no sample runs")
            run_end = None
            for run_index, trun in enumerate(truns):
                version, run_flags, offset = reader.full(trun, versions=(0, 1), flags=0x000f05)
                count = reader.uint(offset, trun.end)
                offset += 4
                if count > reader.max_samples - reader.sample_count:
                    raise LimitReached("MP4 sample-count budget exceeded")
                if flags & 0x010000 and count:
                    raise ValueError("Empty-duration track fragment declares samples")
                if run_flags & 1:
                    position = base + reader.uint(offset, trun.end, signed=True)
                    offset += 4
                else:
                    position = run_end if run_end is not None else base
                if run_flags & 4:
                    reader.uint(offset, trun.end)
                    offset += 4
                    if run_flags & 0x400:
                        raise ValueError("Both first-sample and per-sample flags are present")
                fields = sum(bool(run_flags & flag) for flag in (0x100, 0x200, 0x400, 0x800))
                if count * fields * 4 != trun.end - offset:
                    raise ValueError("Sample count disagrees with complete MP4 run table")
                for _ in range(count):
                    reader.check()
                    duration, basis = default_duration, duration_basis
                    if run_flags & 0x100:
                        duration = reader.uint(offset, trun.end)
                        basis = "trun_sample_duration"
                        offset += 4
                    size = default_size
                    if run_flags & 0x200:
                        size = reader.uint(offset, trun.end)
                        offset += 4
                    if run_flags & 0x400:
                        reader.uint(offset, trun.end)
                        offset += 4
                    cto = 0
                    if run_flags & 0x800:
                        cto = reader.uint(offset, trun.end, signed=version == 1)
                        offset += 4
                    if duration <= 0 or size <= 0:
                        raise Unsupported("A sample lacks a positive explicit/default duration or payload size")
                    finish = position + size
                    containing = bisect_right(mdat_starts, position) - 1
                    if containing < 0 or finish > mdats[containing][1]:
                        raise ValueError("Referenced sample is outside captured mdat payload bytes")
                    sample = {"pts": dts + cto, "dts": dts, "duration": duration,
                              "offset": position, "size": size,
                              "fragment_index": fragment_index, "run_index": run_index,
                              "duration_basis": basis}
                    track["samples"].append(sample)
                    ranges.append((position, finish))
                    reader.sample_count += 1
                    position, dts = finish, dts + duration
                run_end = position
    ranges.sort()
    if any(start < previous_end for (_, previous_end), (start, _) in zip(ranges, ranges[1:])):
        raise ValueError("MP4 samples alias overlapping payload bytes")
    # Only hash after checking overlap. Thus adversarial references cannot make
    # us repeatedly hash the same large payload despite a small source budget.
    view = memoryview(reader.data)
    for track in tracks.values():
        for sample in track["samples"]:
            reader.check()
            start = sample["offset"]
            sample["sha256"] = hashlib.sha256(view[start:start + sample["size"]]).hexdigest()


def _verified_streams(reader, tracks, probe_data):
    if not isinstance(probe_data, dict):
        raise ValueError("Require a complete probe object")
    streams = probe_data.get("streams")
    packets = probe_data.get("packets")
    if not isinstance(streams, list) or not isinstance(packets, list):
        raise ValueError("Require complete probe streams and ordered packets")
    if len(streams) > reader.max_samples or len(packets) > reader.max_samples:
        raise LimitReached("Probe inventory exceeds MP4 sample-count budget")
    matched = {}
    indices = set()
    for stream in streams:
        reader.check()
        if not isinstance(stream, dict):
            raise ValueError("Probe streams must be objects")
        if stream.get("codec_type") not in ("audio", "video"):
            raise Unsupported("Probe contains non-audio/video streams")
        index = _integer(stream["index"])
        identity = _track_id(stream.get("id"))
        if index in indices or identity in matched:
            raise ValueError("Ambiguous probe stream/track mapping")
        indices.add(index)
        if identity not in tracks or stream["codec_type"] != tracks[identity]["kind"]:
            raise ValueError("Probe stream identity disagrees with MP4 track")
        matched[identity] = {**stream, "index": index}
    if set(matched) != set(tracks):
        raise Unsupported("Probe and fragmented MP4 track inventory differ")
    grouped = {index: [] for index in indices}
    for packet in packets:
        reader.check()
        if not isinstance(packet, dict) or packet.get("stream_index") not in indices:
            raise ValueError("Probe packet references an unknown stream")
        grouped[packet["stream_index"]].append(packet)
    verified = []
    for identity, track in tracks.items():
        stream = matched[identity]
        base_text = stream["time_base"]
        if not isinstance(base_text, str) or not re.fullmatch(r"[0-9]{1,20}/[0-9]{1,20}", base_text):
            raise ValueError("Probe requires a bounded rational time base")
        base = Fraction(base_text)
        if base <= 0:
            raise ValueError("Probe time base must be positive")
        selected = grouped[stream["index"]]
        if len(selected) != len(track["samples"]) or not selected:
            raise ValueError("Probe packet count differs from referenced fragment samples")
        samples, updates = [], []
        for index, (sample, packet) in enumerate(zip(track["samples"], selected)):
            reader.check()
            converted = {}
            for field in ("pts", "dts", "duration"):
                value = Fraction(sample[field], track["timescale"]) / base
                if value.denominator != 1:
                    raise Unsupported("Container timing cannot be represented exactly in probe ticks")
                converted[field] = value.numerator
            sha = packet.get("data_hash")
            if not isinstance(sha, str) or sha.lower() != "sha256:" + sample["sha256"]:
                raise ValueError("Ordered sample payload hash differs from probe packet")
            for field in ("pts", "dts"):
                if _integer(packet.get(field)) != converted[field]:
                    raise ValueError("Probe " + field.upper() + " differs from explicit container timing")
            known_duration = packet.get("duration")
            if known_duration is None or _integer(known_duration) <= 0:
                updates.append({"packet_index": index, "duration": converted["duration"]})
            elif _integer(known_duration) != converted["duration"]:
                raise ValueError("Positive probe duration contradicts explicit container timing")
            samples.append({**sample, **converted, "packet_index": index})
        verified.append({"stream_index": stream["index"], "track_id": identity,
                         "time_base": str(base), "container_timescale": track["timescale"],
                         "updates": updates, "samples": samples,
                         "verified_sample_count": len(samples),
                         "duration_basis": "explicit trun sample duration, otherwise tfhd then trex default",
                         "captured_pts_end_exact": str(max(Fraction(s["pts"] + s["duration"]) * base for s in samples))})
    return verified


def recover_packet_durations(path, probe_data, *, max_bytes=64 * 1024 * 1024, max_samples=100000, timeout=10):
    """Return all-or-nothing verified evidence without mutating inputs or probe.

    ``max_bytes`` bounds the complete source read; oversized sources receive
    needs_review, never a prefix-based verification. ``max_samples`` bounds all
    tracks together. ``timeout`` bounds parsing/verification wall time; regular
    file reads are bounded in bytes and checked immediately afterward. Updates use stream-local packet indices and native integer
    probe ticks. A verified result concerns the captured samples only.
    """
    result = {"schema_version": 1, "status": "not_applicable", "streams": [],
              "source": None, "diagnostics": [],
              "scope": "Verified captured fragment samples, not hosted-original completeness; no timeline fitting."}
    try:
        if type(max_bytes) is not int or max_bytes <= 0 or type(max_samples) is not int or max_samples <= 0:
            raise ValueError("MP4 byte/sample budgets must be positive integers")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("MP4 evidence time budget must be finite and positive")
        deadline = time.monotonic() + timeout
        path = Path(path)
        before = path.stat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("MP4 evidence requires a regular source file")
        if before.st_size > max_bytes:
            raise LimitReached("Complete source exceeds MP4 evidence byte budget")
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
        after = path.stat()
        if len(data) > max_bytes:
            raise LimitReached("Complete source exceeds MP4 evidence byte budget")
        if time.monotonic() >= deadline:
            raise LimitReached("MP4 evidence time budget exceeded")
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino) or len(data) != before.st_size:
            raise ValueError("Source changed during bounded MP4 evidence read")
        result["source"] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        if len(data) < 8 or data[4:8] not in (b"ftyp", b"styp", b"moov"):
            result["diagnostics"].append("Not a recognized MP4 initialization")
            return result
        reader = _Reader(data, max_samples, deadline)
        top = reader.boxes(0, len(data))
        if not any(box.kind == b"moof" for box in top):
            result["diagnostics"].append("No fragmented MP4 sample evidence requested")
            return result
        tracks = _tracks(reader, _one(top, b"moov"))
        _samples(reader, top, tracks)
        verified = _verified_streams(reader, tracks, probe_data)
        result.update({"status": "verified", "streams": verified, "box_count": reader.box_count,
                       "verified_sample_count": reader.sample_count,
                       "all_referenced_payloads_inside_mdat": True,
                       "all_payload_hashes_and_native_timestamps_match": True})
    except LimitReached as error:
        result.update(status="needs_review", streams=[], diagnostics=[str(error)])
    except Unsupported as error:
        result.update(status="unsupported", streams=[], diagnostics=[str(error)])
    except (OSError, ValueError, KeyError, TypeError, OverflowError, ZeroDivisionError, struct.error) as error:
        result.update(status="failed", streams=[], diagnostics=[str(error)])
    return result
