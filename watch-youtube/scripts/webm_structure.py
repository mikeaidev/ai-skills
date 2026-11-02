#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Bounded, read-only WebM/Matroska structure and index inspection.

This is a scoped structural reader, not a full codec/schema validator. It keeps
observed media-element boundaries, the outer Segment envelope, seek metadata
and hosted-original completeness as independent conclusions. Unknown schema
features require review instead of being silently certified.
"""
import hashlib
import math
from pathlib import Path
import struct


EBML, SEGMENT, CLUSTER = 0x1A45DFA3, 0x18538067, 0x1F43B675
SEEKHEAD, INFO, TRACKS, CUES, TAGS = 0x114D9B74, 0x1549A966, 0x1654AE6B, 0x1C53BB6B, 0x1254C367
VOID, CRC = 0xEC, 0xBF
SEGMENT_CHILDREN = {SEEKHEAD, INFO, TRACKS, CLUSTER, CUES, TAGS, 0x1941A469, 0x1043A770}
MAX_DEPTH = 16

# Finite metadata with known element boundaries. Unrecognized elements are
# retained as unsupported findings; only known masters are recursively walked.
MASTERS = {EBML, SEEKHEAD, 0x4DBB, INFO, TRACKS, 0xAE, 0xE0, 0xE1, 0x55B0, 0x55D0,
           CUES, 0xBB, 0xB7, 0xDB, TAGS, 0x7373, 0x63C0, 0x67C8,
           0x5854, 0x75A1, 0xA6}
KNOWN_LEAVES = {
    VOID, CRC, 0x4286, 0x42F7, 0x42F2, 0x42F3, 0x4282, 0x4287, 0x4285,
    0x53AB, 0x53AC, 0x2AD7B1, 0x4489, 0x4D80, 0x5741, 0x73A4, 0x4461, 0x7BA9,
    0xD7, 0x73C5, 0x83, 0xB9, 0x88, 0x55AA, 0x9C, 0x6DE7, 0x6DF8,
    0x536E, 0x22B59C, 0x22B59D, 0x86, 0x63A2, 0x56AA, 0x56BB, 0x23E383,
    0x234E7A, 0x258688, 0x55EE, 0xAA, 0xB0, 0xBA, 0x54B0, 0x54BA, 0x54B2, 0x9A,
    0x53B8, 0x53C0, 0x54AA, 0x54BB, 0x54CC, 0x54DD, 0x2EB524,
    0x55B1, 0x55B2, 0x55B3, 0x55B4, 0x55B5, 0x55B6, 0x55B7, 0x55B8,
    0x55B9, 0x55BA, 0x55BB, 0x55BC, 0x55BD, 0x55D1, 0x55D2, 0x55D3,
    0x55D4, 0x55D5, 0x55D6, 0x55D7, 0x55D8, 0x55D9, 0x55DA,
    0xB5, 0x78B5, 0x9F, 0x6264, 0xE7, 0xA7, 0xAB, 0x58D7,
    0xB3, 0xF7, 0xF1, 0xF0, 0xB2, 0x5378, 0xEA, 0x96, 0x97, 0x535F, 0xEB,
    0x68CA, 0x63CA, 0x63C5, 0x63C4, 0x63C6, 0x63C9, 0x45A3, 0x447A, 0x447B,
    0x4484, 0x4487, 0x4485, 0x9B, 0xFA, 0xFB, 0xA4, 0x75A2, 0xEE, 0xA5,
}
GLOBAL = {VOID, CRC}
HEADER_FIELDS = {0x4286, 0x42F7, 0x42F2, 0x42F3, 0x4282, 0x4287, 0x4285} | GLOBAL
INFO_FIELDS = {0x2AD7B1, 0x4489, 0x4D80, 0x5741, 0x73A4, 0x4461, 0x7BA9} | GLOBAL
TRACK_FIELDS = {0xD7, 0x73C5, 0x83, 0xB9, 0x88, 0x55AA, 0x9C, 0x6DE7, 0x6DF8,
    0x536E, 0x22B59C, 0x22B59D, 0x86, 0x63A2, 0x56AA, 0x56BB, 0x23E383,
    0x234E7A, 0x258688, 0x55EE, 0xAA, 0xE0, 0xE1} | GLOBAL
METADATA_CHILDREN = {
    EBML: HEADER_FIELDS, INFO: INFO_FIELDS, TRACKS: {0xAE} | GLOBAL, 0xAE: TRACK_FIELDS,
    SEEKHEAD: {0x4DBB} | GLOBAL, 0x4DBB: {0x53AB, 0x53AC} | GLOBAL,
    0xE0: {0xB0, 0xBA, 0x54B0, 0x54BA, 0x54B2, 0x9A, 0x53B8, 0x53C0,
           0x54AA, 0x54BB, 0x54CC, 0x54DD, 0x2EB524, 0x55B0} | GLOBAL,
    0xE1: {0xB5, 0x78B5, 0x9F, 0x6264} | GLOBAL,
    0x55B0: {0x55B1, 0x55B2, 0x55B3, 0x55B4, 0x55B5, 0x55B6, 0x55B7,
             0x55B8, 0x55B9, 0x55BA, 0x55BB, 0x55BC, 0x55BD, 0x55D0} | GLOBAL,
    0x55D0: set(range(0x55D1, 0x55DB)) | GLOBAL,
    CUES: {0xBB} | GLOBAL, 0xBB: {0xB3, 0xB7} | GLOBAL,
    0xB7: {0xF7, 0xF1, 0xF0, 0xB2, 0x5378, 0xEA, 0xDB} | GLOBAL,
    0xDB: {0x96, 0x97, 0x535F, 0xEB} | GLOBAL,
    TAGS: {0x7373} | GLOBAL, 0x7373: {0x63C0, 0x67C8} | GLOBAL,
    0x63C0: {0x68CA, 0x63CA, 0x63C5, 0x63C4, 0x63C6, 0x63C9} | GLOBAL,
    0x67C8: {0x45A3, 0x447A, 0x447B, 0x4484, 0x4487, 0x4485, 0x67C8} | GLOBAL,
    0x5854: {0x58D7} | GLOBAL, 0x75A1: {0xA6} | GLOBAL,
    0xA6: {0xEE, 0xA5} | GLOBAL,
}


class _Malformed(ValueError):
    pass


class _Limit(ValueError):
    pass


class _Reader:
    def __init__(self, data, max_elements):
        self.data = data
        self.limit = len(data)
        self.max_elements = max_elements
        self.elements = 0
        self.frame_count = 0
        self.review = []
        self.unknown_media = False
        self.blocks = []
        self.clusters = []
        self.tracks = []
        self.seek_entries = []
        self.cue_refs = []
        self.top = {}
        self.timestamp_scale = 1000000
        self.duration = None
        self.doctype = None

    def warn(self, reason, media=False):
        if reason not in self.review:
            self.review.append(reason)
        self.unknown_media = self.unknown_media or media

    def vint(self, offset, limit, *, element_id=False, track=False):
        if offset >= limit or offset >= self.limit:
            raise _Malformed("Truncated EBML variable-length integer")
        first = self.data[offset]
        width = next((n for n in range(1, 9) if first & (1 << (8 - n))), None)
        if width is None or (element_id and width > 4):
            raise _Malformed("Invalid EBML variable-length integer width")
        if offset + width > min(limit, self.limit):
            raise _Malformed("Truncated EBML variable-length integer")
        raw = int.from_bytes(self.data[offset:offset + width], "big")
        value = raw if element_id else raw & ((1 << (7 * width)) - 1)
        unknown = not element_id and not track and value == (1 << (7 * width)) - 1
        return value, width, unknown

    def header(self, offset, limit):
        element, ni, _ = self.vint(offset, limit, element_id=True)
        size, ns, unknown = self.vint(offset + ni, limit)
        self.elements += 1
        if self.elements > self.max_elements:
            raise _Limit("Element count budget exceeded")
        start = offset + ni + ns
        return {"id": element, "offset": offset, "payload": start,
                "size": None if unknown else size, "end": None if unknown else start + size}

    def raw(self, node):
        if node["end"] is None:
            raise _Malformed("Scalar element has unknown size")
        return self.data[node["payload"]:node["end"]]

    def integer(self, node, *, signed=False, default=0):
        value = self.raw(node)
        if not 0 <= len(value) <= 8:
            raise _Malformed("Invalid scalar integer size")
        if not value:
            return default
        return int.from_bytes(value, "big", signed=signed)

    def children(self, node, depth=0):
        if depth > MAX_DEPTH:
            raise _Limit("Metadata nesting budget exceeded")
        if node["end"] is None:
            raise _Malformed("Unknown-size metadata master is unsupported")
        if node["end"] > self.limit:
            raise _Malformed("Metadata element extends beyond captured bytes")
        position = node["payload"]
        values = []
        while position < node["end"]:
            child = self.header(position, node["end"])
            if child["end"] is None or child["end"] > node["end"]:
                raise _Malformed("Child exceeds finite parent or has unsupported unknown size")
            values.append(child)
            position = child["end"]
        return values

    def metadata(self, node, depth=0, *, media=False):
        if depth > MAX_DEPTH:
            raise _Limit("Metadata nesting budget exceeded")
        if node["id"] in MASTERS:
            for child in self.children(node, depth + 1):
                if node["id"] in METADATA_CHILDREN and child["id"] not in METADATA_CHILDREN[node["id"]]:
                    self.warn("Unsupported child 0x%x in master 0x%x" % (child["id"], node["id"]), media=media)
                self.metadata(child, depth + 1, media=media)
        elif node["id"] not in KNOWN_LEAVES:
            self.warn("Unsupported finite element 0x%x" % node["id"], media=media)
        if node["id"] == CRC and node["size"] != 4:
            raise _Malformed("CRC-32 element requires four bytes")

    def ebml_header(self, node):
        seen = set()
        for child in self.children(node):
            if child["id"] not in HEADER_FIELDS:
                self.warn("Unsupported EBML Header child", media=True)
            if child["id"] not in GLOBAL:
                if child["id"] in seen:
                    raise _Malformed("Repeated unique EBML Header field")
                seen.add(child["id"])
            if child["id"] == 0x4282:
                if self.doctype is not None:
                    raise _Malformed("Repeated DocType")
                self.doctype = self.raw(child).decode("ascii")
            elif child["id"] in (0x42F2, 0x42F3):
                maximum = self.integer(child)
                if maximum > (4 if child["id"] == 0x42F2 else 8) or maximum < 1:
                    self.warn("Unsupported EBML maximum ID/size length")
            elif child["id"] in (0x42F7, 0x4285):
                version = self.integer(child, default=1)
                if version < 1 or version > (1 if child["id"] == 0x42F7 else 4):
                    self.warn("Unsupported required EBML/DocType read version", media=True)
            self.metadata(child)
        if self.doctype not in ("webm", "matroska"):
            self.warn("Unsupported or missing DocType", media=True)

    def info(self, node):
        seen = set()
        for child in self.children(node):
            if child["id"] not in INFO_FIELDS:
                self.warn("Unsupported Info child", media=True)
            if child["id"] in (0x2AD7B1, 0x4489):
                if child["id"] in seen:
                    raise _Malformed("Repeated unique Info timing field")
                seen.add(child["id"])
            if child["id"] == 0x2AD7B1:
                self.timestamp_scale = self.integer(child, default=1000000)
                if self.timestamp_scale <= 0:
                    raise _Malformed("TimestampScale must be positive")
            elif child["id"] == 0x4489:
                raw = self.raw(child)
                if len(raw) not in (0, 4, 8):
                    raise _Malformed("Invalid Duration float size")
                self.duration = struct.unpack(">f" if len(raw) == 4 else ">d", raw)[0] if raw else 0.0
                if not math.isfinite(self.duration) or self.duration < 0:
                    raise _Malformed("Invalid Duration value")
            self.metadata(child)

    def track_entries(self, node):
        for entry in self.children(node):
            if entry["id"] != 0xAE:
                if entry["id"] not in GLOBAL:
                    self.warn("Unsupported Tracks child 0x%x" % entry["id"], media=True)
                self.metadata(entry, media=True)
                continue
            track = {"track_number": None, "track_type": None, "kind": "other", "codec_id": None,
                     "codec_delay_ns": 0, "seek_preroll_ns": 0,
                     "codec_delay_explicit": False, "seek_preroll_explicit": False,
                     "codec_private_sha256": None, "opus": None}
            seen = set()
            for child in self.children(entry):
                eid = child["id"]
                if eid not in TRACK_FIELDS:
                    self.warn("Unsupported TrackEntry child 0x%x" % eid, media=True)
                if eid in {0xD7, 0x83, 0x86, 0x56AA, 0x56BB, 0x63A2}:
                    if eid in seen:
                        raise _Malformed("Repeated unique TrackEntry field")
                    seen.add(eid)
                if eid == 0xD7:
                    track["track_number"] = self.integer(child)
                elif eid == 0x83:
                    track["track_type"] = self.integer(child)
                elif eid == 0x86:
                    track["codec_id"] = self.raw(child).decode("ascii")
                elif eid == 0x56AA:
                    track["codec_delay_ns"] = self.integer(child)
                    track["codec_delay_explicit"] = True
                elif eid == 0x56BB:
                    track["seek_preroll_ns"] = self.integer(child)
                    track["seek_preroll_explicit"] = True
                elif eid == 0x63A2:
                    raw = self.raw(child)
                    track["codec_private_sha256"] = hashlib.sha256(raw).hexdigest()
                    if raw.startswith(b"OpusHead"):
                        if len(raw) < 19:
                            raise _Malformed("Truncated OpusHead")
                        track["opus"] = {"version": raw[8], "channels": raw[9],
                            "pre_skip_samples": int.from_bytes(raw[10:12], "little"),
                            "input_sample_rate": int.from_bytes(raw[12:16], "little"),
                            "output_gain_q8": int.from_bytes(raw[16:18], "little", signed=True),
                            "channel_mapping_family": raw[18]}
                        if raw[8] > 15 or raw[9] == 0:
                            self.warn("Unsupported OpusHead version/channels", media=True)
                        if raw[18] != 0:
                            self.warn("Unsupported Opus channel mapping family", media=True)
                        elif len(raw) != 19 or raw[9] not in (1, 2):
                            raise _Malformed("Invalid mapping-family-zero OpusHead")
                self.metadata(child, media=True)
            if not track["track_number"] or track["track_type"] is None or not track["codec_id"]:
                raise _Malformed("TrackEntry lacks required identity/type/codec")
            if track["codec_id"] == "A_OPUS" and track["opus"] is None:
                raise _Malformed("Opus track lacks a supported OpusHead")
            track["kind"] = {1: "video", 2: "audio"}.get(track["track_type"], "other")
            if any(old["track_number"] == track["track_number"] for old in self.tracks):
                raise _Malformed("Duplicate track number")
            self.tracks.append(track)

    def seekhead(self, node):
        for entry in self.children(node):
            if entry["id"] != 0x4DBB:
                self.metadata(entry)
                continue
            values = {}
            for child in self.children(entry):
                if child["id"] in (0x53AB, 0x53AC):
                    if child["id"] in values:
                        raise _Malformed("Duplicate Seek field")
                    values[child["id"]] = self.integer(child)
                self.metadata(child)
            if set(values) != {0x53AB, 0x53AC}:
                raise _Malformed("Incomplete Seek entry")
            self.seek_entries.append({"target_id": values[0x53AB], "position": values[0x53AC]})

    def cues(self, node):
        # Cues are index metadata; inspect target offsets separately from media.
        self.metadata(node)
        for point in self.children(node):
            if point["id"] != 0xBB:
                continue
            for positions in self.children(point):
                if positions["id"] != 0xB7:
                    continue
                for child in self.children(positions):
                    if child["id"] == 0xF1:
                        self.cue_refs.append(self.integer(child))

    def frame_ranges(self, start, end, mode):
        if mode == 0:
            if start >= end:
                raise _Malformed("Empty Block frame payload")
            return [(start, end)]
        if start >= end:
            raise _Malformed("Missing lace count")
        count = self.data[start] + 1
        position = start + 1
        if count < 2:
            raise _Malformed("Lacing requires at least two frames")
        sizes = []
        if mode == 2:
            remaining = end - position
            if not remaining or remaining % count:
                raise _Malformed("Fixed lacing payload is not evenly divisible")
            sizes = [remaining // count] * count
        elif mode == 1:
            for _ in range(count - 1):
                size = 0
                while True:
                    if position >= end:
                        raise _Malformed("Truncated Xiph lace size")
                    value = self.data[position]
                    position += 1
                    size += value
                    if value != 255:
                        break
                sizes.append(size)
            sizes.append(end - position - sum(sizes))
        else:
            first, width, _ = self.vint(position, end, track=True)
            position += width
            sizes.append(first)
            for _ in range(count - 2):
                delta, width, _ = self.vint(position, end, track=True)
                position += width
                sizes.append(sizes[-1] + delta - ((1 << (7 * width - 1)) - 1))
            sizes.append(end - position - sum(sizes))
        ranges = []
        for size in sizes:
            if size <= 0 or size > end - position:
                raise _Malformed("Invalid or truncated laced frame")
            ranges.append((position, position + size))
            position += size
        if position != end:
            raise _Malformed("Lace sizes do not consume the Block")
        return ranges

    def block(self, node, cluster_timestamp, *, simple=True):
        track, width, _ = self.vint(node["payload"], node["end"], track=True)
        header_end = node["payload"] + width + 3
        if track <= 0 or header_end > node["end"]:
            raise _Malformed("Invalid or truncated Block header")
        relative = int.from_bytes(self.data[node["payload"] + width:header_end - 1], "big", signed=True)
        flags = self.data[header_end - 1]
        # INV is bit 0x08. SimpleBlock reserves 0x70; Block reserves 0xF0
        # and its unused low bit is kept conservative here.
        if flags & (0x70 if simple else 0xF1):
            self.warn("Unsupported reserved Block flags", media=True)
        frames = self.frame_ranges(header_end, node["end"], (flags >> 1) & 3)
        self.frame_count += len(frames)
        if self.frame_count > self.max_elements:
            raise _Limit("Encoded frame count budget exceeded")
        record = {"track_number": track, "block_index": len(self.blocks),
                  "offset": node["offset"], "end": node["end"],
                  "timestamp_ticks": cluster_timestamp + relative,
                  "frame_count": len(frames), "lacing": (flags >> 1) & 3,
                  "frame_payloads": [(b - a, hashlib.sha256(self.data[a:b]).hexdigest()) for a, b in frames],
                  "discard_padding_ns": None}
        self.blocks.append(record)
        return record

    def cluster(self, node):
        bound = self.limit if node["end"] is None else node["end"]
        if bound > self.limit:
            raise _Malformed("Cluster extends beyond captured bytes")
        position = node["payload"]
        children = []
        while position < bound:
            child = self.header(position, bound)
            if node["end"] is None and child["id"] in SEGMENT_CHILDREN:
                self.elements -= 1  # The Segment reader owns this next header.
                break
            if child["id"] in SEGMENT_CHILDREN:
                raise _Malformed("Segment-level element inside finite Cluster")
            if child["end"] is None or child["end"] > bound:
                raise _Malformed("Truncated Cluster child or unsupported unknown size")
            children.append(child)
            position = child["end"]
        timestamps = [self.integer(c) for c in children if c["id"] == 0xE7]
        if len(timestamps) != 1:
            raise _Malformed("Cluster requires exactly one Timestamp")
        before = len(self.blocks)
        for child in children:
            if child["id"] == 0xA3:
                self.block(child, timestamps[0])
            elif child["id"] == 0xA0:
                members = self.children(child)
                blocks = [c for c in members if c["id"] == 0xA1]
                padding = [c for c in members if c["id"] == 0x75A2]
                if len(blocks) != 1 or len(padding) > 1:
                    raise _Malformed("BlockGroup requires one Block and at most one DiscardPadding")
                record = self.block(blocks[0], timestamps[0], simple=False)
                if padding:
                    record["discard_padding_ns"] = self.integer(padding[0], signed=True)
                for member in members:
                    if member["id"] != 0xA1:
                        if member["id"] not in {0x9B, 0xFA, 0xFB, 0xA4, 0x75A2, 0x75A1, VOID, CRC}:
                            self.warn("Unsupported BlockGroup child 0x%x" % member["id"], media=True)
                        self.metadata(member, media=True)
            else:
                if child["id"] not in {0xE7, 0xA7, 0xAB, 0x5854, VOID, CRC}:
                    self.warn("Unsupported Cluster child 0x%x" % child["id"], media=True)
                self.metadata(child, media=True)
        self.clusters.append({"offset": node["offset"], "end": position,
                              "unknown_size": node["end"] is None,
                              "timestamp_ticks": timestamps[0], "block_count": len(self.blocks) - before})
        return position

    def segment(self, node):
        position = node["payload"]
        bound = min(node["end"], self.limit) if node["end"] is not None else self.limit
        while position < bound:
            child = self.header(position, bound)
            if child["id"] in (EBML, SEGMENT):
                raise _Malformed("Multiple/restarted EBML Segments are outside this inspection")
            if child["id"] != CLUSTER and (child["end"] is None or child["end"] > bound):
                raise _Malformed("Truncated Segment child or unsupported unknown size")
            self.top[child["offset"]] = child["id"]
            if child["id"] in (INFO, TRACKS) and list(self.top.values()).count(child["id"]) > 1:
                raise _Malformed("Repeated unique Segment Info/Tracks element")
            if child["id"] == CLUSTER:
                position = self.cluster(child)
                continue
            if child["id"] == INFO:
                self.info(child)
            elif child["id"] == TRACKS:
                self.track_entries(child)
            elif child["id"] == SEEKHEAD:
                self.seekhead(child)
            elif child["id"] == CUES:
                self.cues(child)
            else:
                self.metadata(child, media=child["id"] not in (TAGS, VOID, CRC))
            position = child["end"]
        return position


def _base():
    return {"status": "not_applicable", "container": None, "doctype": None,
            "outer_envelope": {"status": "unknown", "declared_bytes_remaining": None},
            "observed_media_elements_complete": None, "cluster_count": 0,
            "block_counts": {"audio": 0, "video": 0, "other": 0, "total": 0},
            "seek_index": {"status": "not_tested", "cues_present": False, "entries": [], "findings": []},
            "tracks": [], "discard_padding": [], "errors": [], "review_reasons": [],
            "hosted_original_completeness": "not_established",
            "scope": "Bounded structural boundaries/index/track metadata; no codec decode, CRC-32 verification, missing-content reconstruction or hosted-original identity proof."}


def inspect_webm(path, *, max_bytes=64 * 1024 * 1024, max_elements=100000):
    """Inspect one supported EBML Segment without changing the file.

    Finite omitted outer bytes and stale indexes require review, even when all
    observed media elements finish cleanly. The element budget also bounds the
    total laced frame count. Unknown-size Segment/Cluster forms
    have explicit unknown envelope status. Unknown schema features cannot pass.
    """
    if type(max_bytes) is not int or type(max_elements) is not int or min(max_bytes, max_elements) <= 0:
        raise ValueError("Require positive integer byte and element budgets")
    result = _base()
    reader = None
    try:
        path = Path(path)
        before = path.stat()
        if not path.is_file() or before.st_size <= 0:
            raise _Malformed("Missing or empty input")
        with path.open("rb") as handle:
            prefix = handle.read(4)
            if prefix != b"\x1a\x45\xdf\xa3":
                if prefix and b"\x1a\x45\xdf\xa3".startswith(prefix):
                    raise _Malformed("Truncated EBML header")
                return result
            if before.st_size > max_bytes:
                raise _Limit("Input byte budget exceeded")
            handle.seek(0)
            data = handle.read(max_bytes + 1)
        after = path.stat()
        if len(data) > max_bytes:
            raise _Limit("Input byte budget exceeded")
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(data) != after.st_size:
            raise _Malformed("Input changed during structural inspection")
        result.update({"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        reader = _Reader(data, max_elements)
        header = reader.header(0, len(data))
        if header["id"] != EBML or header["end"] is None or header["end"] > len(data):
            raise _Malformed("Invalid/truncated EBML header")
        reader.ebml_header(header)
        position = header["end"]
        while position < len(data):
            segment = reader.header(position, len(data))
            if segment["id"] not in (VOID, CRC):
                break
            if segment["end"] is None or segment["end"] > len(data):
                raise _Malformed("Truncated root global element")
            reader.metadata(segment)
            position = segment["end"]
        else:
            raise _Malformed("EBML header has no Segment")
        if segment["id"] != SEGMENT:
            raise _Malformed("Expected one Segment after EBML header")
        envelope = "unknown" if segment["end"] is None else "incomplete" if segment["end"] > len(data) else "complete"
        remaining = max(0, segment["end"] - len(data)) if segment["end"] is not None else None
        result["outer_envelope"] = {"status": envelope, "declared_bytes_remaining": remaining,
            "segment_offset": segment["offset"], "segment_payload_offset": segment["payload"],
            "declared_end": segment["end"], "captured_end": len(data)}
        observed_end = reader.segment(segment)
        if segment["end"] is not None and segment["end"] < len(data):
            raise _Malformed("Bytes follow the declared Segment; concatenation is unsupported")
        if observed_end != len(data):
            raise _Malformed("Observed Segment children do not end at captured EOF")
        if not reader.tracks or not reader.clusters or not reader.blocks:
            reader.warn("Tracks and nonempty observed media are required for media-boundary verification", media=True)
        if INFO not in reader.top.values():
            reader.warn("Segment Info is missing", media=True)
        tracks = {track["track_number"]: track for track in reader.tracks}
        for track in reader.tracks:
            track.update({"block_count": 0, "encoded_frame_count": 0,
                          "first_timestamp_ns": None, "last_block_timestamp_ns": None})
        chains = {number: hashlib.sha256() for number in tracks}
        for block in reader.blocks:
            if block["track_number"] not in tracks:
                raise _Malformed("Block references an undefined track")
            track = tracks[block["track_number"]]
            kind = track["kind"]
            result["block_counts"][kind] += 1
            result["block_counts"]["total"] += 1
            track["block_count"] += 1
            timestamp_ns = block["timestamp_ticks"] * reader.timestamp_scale
            if track["first_timestamp_ns"] is None:
                track["first_timestamp_ns"] = timestamp_ns
            track["last_block_timestamp_ns"] = timestamp_ns
            for size, digest in block["frame_payloads"]:
                chains[block["track_number"]].update(size.to_bytes(8, "big") + bytes.fromhex(digest))
                track["encoded_frame_count"] += 1
            if block["discard_padding_ns"] is not None:
                result["discard_padding"].append({"track_number": block["track_number"],
                    "block_index": block["block_index"], "value_ns": block["discard_padding_ns"],
                    "timestamp_ns": timestamp_ns})
        for track in reader.tracks:
            track["encoded_frame_sequence_sha256"] = chains[track["track_number"]].hexdigest()
        seek = result["seek_index"]
        seek["cues_present"] = CUES in reader.top.values()
        for entry in reader.seek_entries:
            absolute = segment["payload"] + entry["position"]
            actual = reader.top.get(absolute)
            state = "matched" if actual == entry["target_id"] else "out_of_bounds" if absolute >= len(data) else "stale_or_non_element_target"
            seek["entries"].append({**entry, "absolute_offset": absolute, "actual_target_id": actual, "status": state})
            if state != "matched":
                seek["findings"].append("Seek target does not identify the declared captured top-level element")
        for position in reader.cue_refs:
            if reader.top.get(segment["payload"] + position) != CLUSTER:
                seek["findings"].append("CueClusterPosition does not identify a captured Cluster")
        seek["findings"] = sorted(set(seek["findings"]))
        seek["status"] = "needs_review" if seek["findings"] else "no_detected_issues"
        if envelope != "complete":
            reader.warn("Outer Segment envelope is " + envelope + "; original completeness is not established")
        if seek["findings"]:
            reader.warn("Captured seek/index metadata is stale or unresolved")
        result.update({"container": reader.doctype, "doctype": reader.doctype,
            "observed_media_elements_complete": None if reader.unknown_media else True,
            "cluster_count": len(reader.clusters), "clusters": reader.clusters,
            "timestamp_scale_ns": reader.timestamp_scale, "duration_ticks": reader.duration,
            "tracks": reader.tracks, "elements_inspected": reader.elements,
            "review_reasons": reader.review, "status": "needs_review" if reader.review else "passed"})
    except _Limit as error:
        result["status"] = "needs_review"
        result["review_reasons"].append(str(error))
        result["observed_media_elements_complete"] = None
    except (OSError, ValueError, UnicodeError, OverflowError, struct.error) as error:
        result["status"] = "failed"
        result["errors"].append(str(error))
        result["observed_media_elements_complete"] = False
    return result
