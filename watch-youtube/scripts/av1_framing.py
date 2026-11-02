#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Compare AV1 packets allowing only one empty leading temporal delimiter.

This is a narrow container-framing comparison, not a decoder, source-completeness
check, or replacement for exact encoded identity. Caller probe packets are bound
to fresh payload hashes and native timestamps before their timing is compared.
"""
from collections import Counter
from fractions import Fraction
import hashlib
import json
import math
from numbers import Real
from pathlib import Path
import re
import subprocess
import time

from media_timing import packet_time, timing_policy


MAX_OBUS_PER_PACKET = 4096
MAX_PACKETS = 100000
_HEX = re.compile(r"^[0-9a-fA-F]+$")
_SHA = re.compile(r"^(?:SHA256:)?([0-9a-f]{64})$", re.IGNORECASE)


class FramingError(ValueError):
    """Packet framing or provenance is not established under this narrow rule."""


class DiagnosticLimit(Exception):
    pass


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _hash(value):
    match = _SHA.fullmatch(str(value or ""))
    return match[1].lower() if match else None


def _digest(path, deadline):
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            if time.monotonic() > deadline:
                raise DiagnosticLimit("AV1 comparison timeout while hashing input")
            result.update(block)
    return result.hexdigest()


def decode_hexdump(text, expected_size):
    """Read ffprobe's offset/hex/ASCII layout without interpreting ASCII as hex."""
    if type(expected_size) is not int or expected_size < 0 or not isinstance(text, str):
        raise FramingError("Invalid ffprobe byte dump size or data")
    result = bytearray()
    for line in text.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"([0-9a-fA-F]{8,}): (.*)", line)
        if not match or int(match[1], 16) != len(result):
            raise FramingError("Noncontiguous or malformed ffprobe byte dump")
        # ffprobe pads sixteen bytes into eight four-hex-digit groups, then
        # separates the ASCII rendering with two spaces. A short final group
        # can contain two hex digits; only this left hex column is decoded.
        column = match[2].split("  ", 1)[0]
        groups = column.split()
        if not groups or any(len(group) not in (2, 4) or not _HEX.fullmatch(group) for group in groups):
            raise FramingError("Malformed ffprobe hexadecimal column")
        block = bytes.fromhex("".join(groups))
        if len(block) > 16 or len(result) + len(block) > expected_size:
            raise FramingError("ffprobe byte dump exceeds declared size")
        result.extend(block)
    if len(result) != expected_size:
        raise FramingError("ffprobe byte dump length differs from declared size")
    return bytes(result)


def parse_obus(data, max_obus=MAX_OBUS_PER_PACKET):
    """Parse bounded low-overhead OBU framing; payload syntax is not decoded.

    An OBU without a size field consumes the remaining packet bytes and is
    therefore necessarily last. Reserved OBU types remain unsupported here.
    """
    if not isinstance(data, bytes) or not data or max_obus <= 0:
        raise FramingError("Require a nonempty AV1 packet and positive OBU limit")
    result, position = [], 0
    while position < len(data):
        if len(result) >= max_obus:
            raise DiagnosticLimit("AV1 per-packet OBU count budget exceeded")
        start = position
        header = data[position]
        position += 1
        if header & 0x81:
            raise FramingError("AV1 forbidden or reserved header bit is set")
        kind = (header >> 3) & 15
        if kind == 0 or 9 <= kind <= 14:
            raise FramingError("Reserved AV1 OBU type is outside this comparison")
        extension = bool(header & 4)
        if extension:
            if position >= len(data):
                raise FramingError("Truncated AV1 extension header")
            if data[position] & 7:
                raise FramingError("AV1 extension reserved bits are set")
            position += 1
        has_size = bool(header & 2)
        if has_size:
            size = 0
            for index in range(8):
                if position >= len(data):
                    raise FramingError("Truncated AV1 OBU length")
                value = data[position]
                position += 1
                size |= (value & 127) << (7 * index)
                if not value & 128:
                    break
            else:
                raise FramingError("AV1 OBU length exceeds eight LEB128 bytes")
        else:
            size = len(data) - position
        if size > len(data) - position:
            raise FramingError("AV1 OBU payload exceeds packet boundary")
        position += size
        if kind == 2 and size != 0:
            raise FramingError("Temporal delimiter OBU must have an empty payload")
        result.append({"type": kind, "offset": start, "end": position,
                       "payload_bytes": size, "extension": extension, "has_size": has_size})
    return result


def canonical_packet(data):
    """Return bytes after only a validated, exact leading ``12 00`` OBU."""
    obus = parse_obus(data)
    delimiters = [index for index, obu in enumerate(obus) if obu["type"] == 2]
    if delimiters and delimiters != [0]:
        raise FramingError("Only one temporal delimiter at packet start is supported")
    leading = bool(delimiters)
    if leading and (obus[0]["end"] != 2 or data[:2] != b"\x12\x00"):
        raise FramingError("Only the exact empty leading delimiter encoding 12 00 is supported")
    canonical = data[2:] if leading else data
    if not canonical:
        raise FramingError("A delimiter-only packet cannot establish coded-unit equivalence")
    return canonical, leading


class _Run:
    def __init__(self, directory, deadline, budget):
        self.directory, self.deadline, self.budget = directory, deadline, budget
        self.commands = []

    def check(self):
        if time.monotonic() > self.deadline:
            raise DiagnosticLimit("AV1 framing diagnostic timeout")
        if sum(path.stat().st_size for path in self.directory.iterdir() if path.is_file()) > self.budget:
            raise DiagnosticLimit("AV1 framing diagnostic byte budget exceeded")

    def probe(self, path, label):
        stdout = self.directory / (label + ".packets.json")
        stderr = self.directory / (label + ".stderr.log")
        command = ["ffprobe", "-v", "warning", "-select_streams", "v:0", "-show_streams",
                   "-show_packets", "-show_data", "-show_data_hash", "sha256", "-show_entries",
                   "stream=index,codec_name,profile,level,width,height,pix_fmt,time_base,avg_frame_rate,r_frame_rate,extradata_size,extradata,extradata_hash:packet=stream_index,pts,dts,duration,size,data,data_hash",
                   "-of", "json", str(path)]
        record = {"command": command, "stdout": str(stdout), "stderr": str(stderr)}
        self.commands.append(record)
        self.check()
        with stdout.open("xb") as output, stderr.open("xb") as errors:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=errors)
            try:
                while process.poll() is None:
                    self.check()
                    time.sleep(0.02)
                self.check()
            except BaseException:
                process.kill()
                process.wait()
                raise
        record["returncode"] = process.returncode
        if process.returncode:
            raise FramingError("ffprobe could not extract AV1 packet evidence")
        data = json.loads(stdout.read_text(encoding="utf-8"))
        streams, packets = data.get("streams", []), data.get("packets", [])
        if len(streams) != 1 or streams[0].get("codec_name") != "av1":
            raise FramingError("Require exactly one selected AV1 stream")
        if not isinstance(packets, list) or not 0 < len(packets) <= MAX_PACKETS:
            raise DiagnosticLimit("No selected packets or AV1 packet count budget exceeded")
        return streams[0], packets


def _selected(probe):
    if not isinstance(probe, dict) or not isinstance(probe.get("streams"), list) or not isinstance(probe.get("packets"), list):
        return None, []
    if len(probe["packets"]) > MAX_PACKETS:
        return None, []
    stream = next((s for s in probe["streams"] if isinstance(s, dict) and s.get("codec_type") == "video"), None)
    packets = [p for p in probe["packets"] if isinstance(p, dict) and stream and p.get("stream_index") == stream.get("index")]
    return stream, packets


def _bind(caller_stream, caller_packets, fresh_stream, fresh_packets, source_sha):
    """Bind caller timing metadata to independently extracted ordered bytes."""
    if caller_stream.get("index") != fresh_stream.get("index") or Fraction(caller_stream["time_base"]) != Fraction(fresh_stream["time_base"]):
        raise FramingError("Caller and fresh stream index/clock disagree")
    if len(caller_packets) != len(fresh_packets) or not caller_packets:
        raise FramingError("Caller and fresh packet counts disagree")
    payloads, effective = [], []
    for index, (declared, observed) in enumerate(zip(caller_packets, fresh_packets)):
        size = int(observed.get("size", -1))
        payload = decode_hexdump(observed.get("data"), size)
        hashed = _sha(payload)
        if _hash(observed.get("data_hash")) != hashed or _hash(declared.get("data_hash")) != hashed:
            raise FramingError("AV1 packet payload hash disagrees with caller or fresh evidence at index " + str(index))
        if "size" in declared and int(declared["size"]) != size:
            raise FramingError("Caller AV1 packet size disagrees with fresh evidence")
        for field in ("pts", "dts"):
            if packet_time(declared, field, caller_stream) != packet_time(observed, field, fresh_stream):
                raise FramingError("Caller and fresh AV1 packet timestamps disagree")
        packet = dict(observed)
        old = packet_time(observed, "duration", fresh_stream)
        supplied = packet_time(declared, "duration", caller_stream)
        if old is not None and old > 0:
            if supplied is not None and supplied > 0 and supplied != old:
                raise FramingError("Caller duration contradicts fresh native packet evidence")
        elif supplied is not None and supplied > 0:
            evidence = declared.get("duration_evidence", {})
            if not isinstance(evidence, dict) or evidence.get("basis") != "verified_mp4_fragment_sample" or evidence.get("source_sha256") != source_sha or evidence.get("sample_index") != index or evidence.get("native_duration") != declared.get("duration") or Fraction(evidence.get("time_base", "0")) != Fraction(caller_stream["time_base"]):
                raise FramingError("Caller duration override lacks required validator MP4 provenance")
            packet["duration"] = declared["duration"]
            packet["duration_evidence"] = dict(evidence)
        effective.append(packet)
        payloads.append(payload)
    return payloads, effective


def _timing(source_stream, output_stream, source_packets, output_packets):
    allowance = max(timing_policy(source_stream, source_packets)["allowance"],
                    timing_policy(output_stream, output_packets)["allowance"])
    results = {}
    for field in ("pts", "dts", "duration"):
        deltas, source_missing, output_missing, outside = [], 0, 0, []
        for index, (a, b) in enumerate(zip(source_packets, output_packets)):
            x, y = packet_time(a, field, source_stream), packet_time(b, field, output_stream)
            if field == "duration":
                x = x if x is not None and x > 0 else None
                y = y if y is not None and y > 0 else None
            source_missing += x is None
            output_missing += y is None
            if x is not None and y is not None:
                delta = y - x
                deltas.append(delta)
                if abs(delta) > allowance:
                    outside.append(index)
        status = "failed" if outside else "needs_review" if source_missing or output_missing else "passed" if deltas else "unknown"
        results[field] = {"status": status, "compared": len(deltas),
                          "source_missing": source_missing, "output_missing": output_missing,
                          "max_absolute_delta": float(max(map(abs, deltas))) if deltas else None,
                          "max_absolute_delta_exact": str(max(map(abs, deltas))) if deltas else None,
                          "outside_allowance_packet_indices": outside,
                          "rounding_allowance": float(allowance), "rounding_allowance_exact": str(allowance),
                          "automatic_offset_fitting": False}
    combined = "failed" if any(results[k]["status"] == "failed" for k in ("pts", "dts")) else "passed" if all(results[k]["status"] == "passed" for k in ("pts", "dts")) else "needs_review"
    return results, combined


def compare_av1_framing(source_path, output_path, source_probe, output_probe, directory,
                        timeout=1800, max_bytes=64 * 1024 * 1024):
    """Verify ordered packet equivalence under the one permitted framing rule.

    Diagnostics use a new ``av1-framing`` child of directory. A caller may
    supply missing durations recovered by the MP4 reader only with the bound
    ``duration_evidence`` records added by the validator. Framing comparison
    does not independently parse MP4 timing tables or perform decoding. These
    markers are trusted internal validator provenance, not authenticated proof
    supplied by arbitrary callers or an external timing-evidence CLI.
    """
    result = {"status": "not_tested", "canonical_payloads_identical": False,
              "packet_alignment": "not_established", "strict_encoded_identity": "not_tested",
              "timestamp_preservation": "not_compared", "duration_preservation": "not_compared",
              "timestamps": {}, "errors": [], "warnings": [],
              "scope": "Local AV1 framing/ordered packet comparison; no decoded-content, source-completeness or perceptual claim.",
              "allowed_rewrite": "Only insertion/removal of one exact empty leading temporal-delimiter OBU 12 00 per packet"}
    try:
        valid_timeout = isinstance(timeout, Real) and not isinstance(timeout, bool) and math.isfinite(timeout) and timeout > 0
    except (OverflowError, TypeError, ValueError):
        valid_timeout = False
    if not valid_timeout or type(max_bytes) is not int or max_bytes <= 0:
        result.update(status="unsupported", errors=["Require finite positive numeric timeout and positive integer diagnostic byte budget"])
        return result
    if any(isinstance(probe, dict) and isinstance(probe.get("packets"), list) and len(probe["packets"]) > MAX_PACKETS
           for probe in (source_probe, output_probe)):
        result.update(status="needs_review", errors=["Caller packet inventory exceeds AV1 packet count budget"])
        return result
    source_stream, source_packets = _selected(source_probe)
    output_stream, output_packets = _selected(output_probe)
    if not source_stream or not output_stream:
        result.update(status="unsupported", errors=["Missing selected video stream metadata"])
        return result
    if source_stream.get("codec_name") != "av1" or output_stream.get("codec_name") != "av1":
        result.update(status="not_applicable" if source_stream.get("codec_name") != "av1" and output_stream.get("codec_name") != "av1" else "unsupported",
                      reason="Narrow framing rule requires AV1 in both selected streams")
        return result
    source, output = Path(source_path).resolve(), Path(output_path).resolve()
    deadline, baseline = time.monotonic() + timeout, []
    child, run = Path(directory).resolve() / "av1-framing", None
    try:
        if not source.is_file() or not output.is_file():
            raise FramingError("Missing source or output media")
        baseline = [_digest(path, deadline) for path in (source, output)]
        result["inputs"] = [{"path": str(path), "bytes": path.stat().st_size, "sha256": sha}
                            for path, sha in zip((source, output), baseline)]
        try:
            child.mkdir(parents=True, exist_ok=False)
        except OSError as error:
            result.update(status="refused", errors=[str(error)])
            return result
        run = _Run(child, deadline, max_bytes)
        streams, packets, payloads = [], [], []
        for path, label, caller_stream, caller_packets, sha in zip(
                (source, output), ("source", "output"), (source_stream, output_stream),
                (source_packets, output_packets), baseline):
            stream, fresh = run.probe(path, label)
            values, effective = _bind(caller_stream, caller_packets, stream, fresh, sha)
            streams.append(stream)
            packets.append(effective)
            payloads.append(values)
        result["source_packet_count"], result["output_packet_count"] = map(len, packets)
        if len(packets[0]) != len(packets[1]):
            raise FramingError("AV1 packet counts differ; ordered alignment is not established")
        result["strict_encoded_identity"] = "passed" if payloads[0] == payloads[1] else "failed"
        fields = ("width", "height", "pix_fmt", "profile", "level")
        differences = [name for name in fields if streams[0].get(name) != streams[1].get(name)]
        extra = [decode_hexdump(s.get("extradata", ""), int(s.get("extradata_size", 0))) for s in streams]
        if differences or extra[0] != extra[1]:
            raise FramingError("AV1 stream configuration/extradata differs")
        for stream, config in zip(streams, extra):
            if config:
                if len(config) < 4 or config[0] != 0x81 or config[3] & 0xe0 or (not config[3] & 0x10 and config[3] & 0x0f):
                    raise FramingError("Unsupported or malformed AV1 codec configuration record")
                if len(config) > 4:
                    parse_obus(config[4:])
            if stream.get("extradata_hash") and _hash(stream["extradata_hash"]) != _sha(config):
                raise FramingError("AV1 codec configuration hash differs from extracted bytes")
        result["codec_configuration"] = {"status": "identical", "extradata_bytes": len(extra[0]),
                                          "extradata_sha256": _sha(extra[0]), "compared_fields": list(fields)}
        relations, all_types = Counter(), []
        for index, (a, b) in enumerate(zip(*payloads)):
            run.check()
            x, leading_a = canonical_packet(a)
            y, leading_b = canonical_packet(b)
            if x != y:
                raise FramingError("AV1 packet differs beyond the allowed empty leading delimiter at index " + str(index))
            if a == b:
                relations["strict_identical"] += 1
            elif leading_a and not leading_b:
                relations["source_leading_td_removed"] += 1
            elif leading_b and not leading_a:
                relations["output_leading_td_removed"] += 1
            else:
                raise FramingError("Unexplained AV1 packet rewrite")
            if index == 0:
                all_types = [obu["type"] for obu in parse_obus(x)]
        if not extra[0] and 1 not in all_types:
            raise FramingError("No matching out-of-band or initial in-band AV1 sequence configuration")
        result["canonical_payloads_identical"] = True
        result["packet_alignment"] = "ordered_packets_equal_under_empty_leading_td_rule"
        result["normalization_counts"] = dict(relations)
        timing, combined = _timing(streams[0], streams[1], packets[0], packets[1])
        result.update(timestamps=timing, timestamp_preservation=combined,
                      duration_preservation=timing["duration"]["status"], status="passed")
        result["timing_status"] = "failed" if any(row["status"] == "failed" for row in timing.values()) else "needs_review" if any(row["status"] != "passed" for row in timing.values()) else "passed"
        result["interpretation"] = "Framing status describes canonical packet correspondence only; timestamp/duration results remain independent, and strict encoded identity is unchanged."
        if result["timing_status"] != "passed":
            result["warnings"].append("Canonical packet correspondence is established; inspect independent timing results")
    except DiagnosticLimit as error:
        result.update(status="needs_review", errors=[str(error)])
    except FileNotFoundError as error:
        result.update(status="unsupported", errors=[str(error)])
    except (OSError, ValueError, KeyError, TypeError, OverflowError, ZeroDivisionError) as error:
        result.update(status="needs_review", errors=[str(error)])
    finally:
        if run is not None:
            result["commands"] = run.commands
            result["artifact_directory"] = str(child)
        if len(baseline) == 2:
            try:
                # The data comparison has its own deadline. Allow the final
                # integrity hashes to finish without turning a timeout into a
                # false claim that media changed.
                check_deadline = time.monotonic() + max(timeout, 1)
                result["inputs_unchanged"] = all(_digest(path, check_deadline) == sha for path, sha in zip((source, output), baseline))
            except (OSError, DiagnosticLimit):
                result["inputs_unchanged"] = None
                result["warnings"].append("Final input integrity could not be re-established")
                if result["status"] == "passed":
                    result["status"] = "needs_review"
            if result.get("inputs_unchanged") is False:
                result.update(status="failed", canonical_payloads_identical=False,
                              packet_alignment="not_established", timestamp_preservation="not_compared",
                              duration_preservation="not_compared", timing_status="not_compared", timestamps={})
                result["errors"].append("Media input bytes changed during AV1 comparison")
    return result
