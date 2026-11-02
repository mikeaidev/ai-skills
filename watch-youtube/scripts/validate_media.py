#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Probe/decode native media; optionally stream-copy separate video/audio to MKV.

Exit 0: requested technical checks passed (source completeness can be unknown).
Exit 1: refused request or a confirmed technical/declared-interval failure.
Exit 2: checks completed but an ambiguity, warning or resource limit needs review.
Presentation duration, native packet coverage and decoded playback are distinct.
"""
import argparse
from collections import Counter
import copy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import subprocess
import sys
import time
import tempfile


from safe_outputs import prepare_run, save_json as save, digest
from media_timing import packet_time, timing_policy, fraction


DEFAULT_PROBE_BYTES = 64 * 1024 * 1024


class ReviewRequired(ValueError):
    """A bounded or inconclusive check is not evidence of missing media."""


def run_spooled(argv, stdout_path, stderr_path, timeout, max_bytes):
    """Keep child output on disk and stop before loading an oversized report.

    The limit bounds each subprocess's combined stdout/stderr bytes, not the
    expanded Python object size. Polling can overshoot the file limit briefly;
    an oversized or timed-out result is never parsed or certified as complete.
    """
    reason = None
    started = time.monotonic()
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = subprocess.Popen(argv, stdout=stdout, stderr=stderr)
        while process.poll() is None:
            if stdout_path.stat().st_size + stderr_path.stat().st_size > max_bytes:
                reason = "output_budget_exceeded"
                break
            if time.monotonic() - started > timeout:
                reason = "timeout"
                break
            time.sleep(0.02)
        if reason:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process.wait()
    total = stdout_path.stat().st_size + stderr_path.stat().st_size
    if total > max_bytes:
        reason = reason or "output_budget_exceeded"
    with stderr_path.open("rb") as handle:
        diagnostic = handle.read(min(max_bytes, 256 * 1024)).decode("utf-8", "replace")
    return {
        "command": argv, "returncode": process.returncode, "stderr": diagnostic,
        "stderr_log": str(stderr_path), "stdout_log": str(stdout_path),
        "output_bytes": total, "output_budget_bytes": max_bytes,
        "resource_limit": reason,
        "stderr_excerpt_truncated": stderr_path.stat().st_size > len(diagnostic.encode("utf-8")),
    }


def num(value):
    try:
        x = float(value)
        return x if x == x and abs(x) != float("inf") else None
    except (TypeError, ValueError):
        return None


def run(argv, timeout, out=None, label="process", max_bytes=DEFAULT_PROBE_BYTES):
    if out is None:
        with tempfile.TemporaryDirectory(prefix="media-check-") as temporary:
            return run(argv, timeout, Path(temporary), label, max_bytes)
    stdout_path, stderr_path = out / (label + ".stdout.log"), out / (label + ".stderr.log")
    result = run_spooled(argv, stdout_path, stderr_path, timeout, max_bytes)
    return result, "" if result["resource_limit"] else stdout_path.read_text(encoding="utf-8")


def packets_for(data, stream):
    return [p for p in data.get("packets", []) if p.get("stream_index") == stream["index"]]


def apply_mp4_duration_evidence(data, evidence, source_hash):
    """Create an effective probe copy; never rewrite the raw ffprobe record.

    The bounded MP4 reader binds all source sample bytes and ordered rational
    timestamps before returning verified evidence. Only absent/nonpositive
    native duration fields can be filled, with provenance on each packet.
    """
    if evidence.get("status") != "verified":
        return data
    if evidence.get("source", {}).get("sha256") != source_hash:
        raise ValueError("MP4 timing evidence does not match the input SHA-256")
    effective = copy.deepcopy(data)
    for selected in evidence.get("streams", []):
        matches = [s for s in effective.get("streams", []) if s.get("index") == selected.get("stream_index")]
        if len(matches) != 1 or fraction(matches[0].get("time_base")) != fraction(selected.get("time_base")):
            raise ValueError("MP4 timing evidence does not identify the same native stream clock")
        stream = matches[0]
        packets = packets_for(effective, stream)
        samples = selected.get("samples", [])
        if len(packets) != len(samples) or len(packets) != selected.get("verified_sample_count"):
            raise ValueError("MP4 timing evidence does not cover the entire packet sequence")
        for update in selected.get("updates", []):
            index, duration = update.get("packet_index"), update.get("duration")
            if type(index) is not int or not 0 <= index < len(packets) or type(duration) is not int or duration <= 0:
                raise ValueError("Invalid verified MP4 duration update")
            packet = packets[index]
            old = packet_time(packet, "duration", stream)
            if old is not None and old > 0:
                raise ValueError("MP4 duration recovery cannot overwrite an existing positive duration")
            sample = samples[index]
            if sample.get("packet_index") != index or sample.get("duration") != duration:
                raise ValueError("MP4 duration update contradicts its verified sample")
            packet["duration"] = duration
            packet["duration_evidence"] = {"basis": "verified_mp4_fragment_sample",
                "source_sha256": source_hash, "track_id": selected.get("track_id"),
                "sample_index": index, "native_duration": duration, "time_base": selected["time_base"]}
    effective["container_duration_evidence"] = evidence
    return effective


def packet_summary(stream, packets):
    pts = [packet_time(p, "pts", stream) for p in packets]
    dts = [packet_time(p, "dts", stream) for p in packets]
    durations = [packet_time(p, "duration", stream) for p in packets]
    unique = sorted(set(p for p in pts if p is not None))
    policy = timing_policy(stream, packets)
    typical = policy["nominal"] or fraction(0)
    tolerance = policy["allowance"]
    intervals, estimated, estimated_intervals = [], 0, []
    for start, duration in zip(pts, durations):
        if start is None:
            continue
        if duration is None or duration <= 0:
            duration = typical
            estimated += 1
            estimated_intervals.append([str(start), str(start + duration)])
        intervals.append([start, start + duration])
    intervals.sort()
    merged, gaps = [], []
    for start, end in intervals:
        if not merged or start > merged[-1][1] + tolerance:
            if merged:
                gaps.append({"start": float(merged[-1][1]), "end": float(start),
                             "seconds": float(start - merged[-1][1])})
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)

    def regressions(values):
        previous, result = None, []
        for i, value in enumerate(values):
            if value is None:
                continue
            if previous is not None and value < previous - tolerance:
                result.append({"packet": i, "previous": float(previous), "current": float(value),
                               "pts": float(pts[i]) if pts[i] is not None else None})
            previous = value
        return result

    exact, duplicate_positions = Counter(), []
    for packet in packets:
        if not packet.get("data_hash"):
            continue
        record = (packet.get("pts"), packet.get("dts"), packet.get("duration"), packet["data_hash"])
        if exact[record]:
            position = packet_time(packet, "pts", stream)
            duplicate_positions.append(str(position) if position is not None else None)
        exact[record] += 1
    hashes = Counter(p["data_hash"] for p in packets if p.get("data_hash"))
    padding = []
    rate = num(stream.get("sample_rate")) or 0
    for packet_index, p in enumerate(packets):
        for side in p.get("side_data_list", []):
            if side.get("side_data_type") == "Skip Samples":
                padding.append({**side, "packet_index": packet_index,
                                "pts": p.get("pts"), "pts_time": p.get("pts_time"),
                                "pts_seconds_exact": str(pts[packet_index]) if pts[packet_index] is not None else None})
    # initial_padding and a first-packet Skip Samples entry can describe the
    # same priming. Report both, using their maximum instead of summing them.
    # A later packet can also carry Skip Samples; its first occurrence is not
    # evidence of stream priming. Indices retain the demuxed stream order.
    first_skip = max((num(x.get("skip_samples")) or 0 for x in padding
                      if x["packet_index"] == 0), default=0)
    initial_samples = max(num(stream.get("initial_padding")) or 0, first_skip)
    initial = initial_samples / rate if rate else 0
    final = max((num(x.get("discard_padding")) or 0 for x in padding), default=0) / rate if rate else 0
    uncertainty = []
    if typical <= 0:
        uncertainty.append("native packet/frame duration cannot be established")
    if estimated:
        uncertainty.append("packet durations estimated; native end/hold coverage requires review")
    return {
        "stream_index": stream["index"], "codec_type": stream.get("codec_type"),
        "codec_name": stream.get("codec_name"), "time_base": stream.get("time_base"),
        "packet_count": len(packets), "missing_pts": sum(p is None for p in pts),
        "missing_dts": sum(d is None for d in dts),
        "first_pts": float(min(unique)) if unique else None,
        "first_pts_exact": str(min(unique)) if unique else None,
        "last_pts": float(max(unique)) if unique else None,
        "packet_end": float(max(x[1] for x in intervals)) if intervals else None,
        "packet_end_exact": str(max(x[1] for x in intervals)) if intervals else None,
        "packet_span": float(max(x[1] for x in intervals) - min(unique)) if intervals else None,
        "typical_packet_duration": float(typical), "estimated_duration_packets": estimated,
        "container_verified_duration_packets": sum(bool(p.get("duration_evidence")) for p in packets),
        "duration_basis": policy["nominal_basis"], "timing_requires_review": uncertainty,
        "gap_rounding_tolerance": float(tolerance),
        "rounding_policy": "rational native ticks; allowance capped at 1/16 nominal interval",
        "packet_intervals": [[float(a), float(b)] for a, b in merged], "gaps": gaps,
        "packet_intervals_exact": [[str(a), str(b)] for a, b in merged],
        "estimated_intervals_exact": estimated_intervals,
        "duplicate_packet_records": sum(n - 1 for n in exact.values()),
        "duplicate_packet_positions_exact": duplicate_positions,
        "repeated_payload_hashes": sum(n - 1 for n in hashes.values()),
        "dts_regressions": regressions(dts), "pts_regressions": regressions(pts),
        "initial_padding_seconds": initial, "discard_padding_seconds": final,
        "audio_padding": {
            "sample_rate": rate or None,
            "initial_padding_samples": num(stream.get("initial_padding")) or 0,
            "first_packet_skip_samples": first_skip,
            "priming_samples_without_double_counting": initial_samples,
            "native_packet_durations_modified": False,
            "interpretation": "Demuxed packet durations may already reflect trimming. Padding is reported separately, never added to video coverage or automatically subtracted twice. Decoded-sample checks establish the decoder's retained samples.",
        },
        "timestamp_coordinate": "native demuxed packet PTS, before any caller-specified mapping",
        "skip_sample_side_data": padding,
        "notes": "PTS may reorder; repeated payloads can be holds. Potential packet gaps require source review for intentional VFR. Coarse ticks never excuse whole missing frames. Padding is retained, not trimmed or rebased.",
    }


def probe(path, out, label, timeout, baseline=None, max_bytes=DEFAULT_PROBE_BYTES):
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError("Missing or empty input: " + str(path))
    with path.open("rb") as handle:
        prefix = handle.read(2048).lstrip().lower()
    if prefix.startswith((b"<!doctype html", b"<html", b"<?xml")):
        raise ValueError("Text/HTML/XML response is not playable media: " + str(path))
    original = baseline or {"path": str(path), "bytes": path.stat().st_size, "sha256": digest(path)}
    raw = out / (label + ".ffprobe.json")
    result = run_spooled([
        "ffprobe", "-hide_banner", "-v", "warning", "-show_error", "-show_format",
        "-show_streams", "-show_packets", "-show_data_hash", "sha256", "-show_entries",
        "packet=stream_index,pts,dts,duration,data_hash,flags:packet_side_data=side_data_type,skip_samples,discard_padding,skip_reason,discard_reason",
        "-of", "json", str(path)
    ], raw, out / (label + ".ffprobe.stderr.log"), timeout, max_bytes)
    if result["resource_limit"]:
        raise ReviewRequired(label + ": probe " + result["resource_limit"] + "; retained bounded-run diagnostics at " + str(raw))
    try:
        with raw.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except ValueError as e:
        data = {}
        result["json_error"] = str(e)
    from mp4_evidence import recover_packet_durations
    from webm_structure import inspect_webm
    timing_evidence = recover_packet_durations(path, data, max_bytes=max_bytes, timeout=min(float(timeout), 10.0))
    data = apply_mp4_duration_evidence(data, timing_evidence, original["sha256"])
    container_evidence = inspect_webm(path, max_bytes=max_bytes)
    result.update({
        "path": str(path), "bytes": original["bytes"], "sha256": original["sha256"],
        "raw_probe_json": str(raw), "format": data.get("format", {}),
        "streams": data.get("streams", []), "probe_error": data.get("error"),
        "container_warnings": result["stderr"].splitlines(),
        "container_duration_evidence": timing_evidence,
        "structured_container": container_evidence,
        "summaries": [packet_summary(s, packets_for(data, s)) for s in data.get("streams", [])
                      if s.get("codec_type") in ("video", "audio")],
    })
    result["success"] = result.get("returncode") == 0 and bool(result["summaries"]) and not data.get("error")
    return result, data


def decode(path, timeout, out=None, label="decode", max_bytes=DEFAULT_PROBE_BYTES):
    result, _ = run([
        "ffmpeg", "-hide_banner", "-nostdin", "-v", "warning", "-xerror",
        "-err_detect", "explode", "-i", str(path),
        "-map", "0:v?", "-map", "0:a?", "-f", "null", "-"
    ], timeout, out, label, max_bytes)
    result["success"] = result.get("returncode") == 0 and not result.get("resource_limit")
    result["status"] = "needs_review" if result.get("resource_limit") else "passed" if result["success"] else "failed"
    result["decode_diagnostics"] = result["stderr"].splitlines()
    return result


def stream_by_kind(data, kind):
    return next((s for s in data.get("streams", []) if s.get("codec_type") == kind), None)


def preservation(source, output, kind):
    a, b = stream_by_kind(source, kind), stream_by_kind(output, kind)
    if not a or not b:
        return {"kind": kind, "success": False, "reason": "missing mapped stream"}
    ap, bp = packets_for(source, a), packets_for(output, b)
    hashes_a, hashes_b = [p.get("data_hash") for p in ap], [p.get("data_hash") for p in bp]
    hashes_match = bool(ap) and all(hashes_a) and all(hashes_b) and hashes_a == hashes_b
    timing = {}
    tolerance = max(timing_policy(a, ap)["allowance"], timing_policy(b, bp)["allowance"])
    for key in ("pts", "dts"):
        deltas, missing_before, missing_after, changed_missing = [], 0, 0, 0
        if hashes_match:
            for p, q in zip(ap, bp):
                x, y = packet_time(p, key, a), packet_time(q, key, b)
                missing_before += x is None
                missing_after += y is None
                changed_missing += (x is None) != (y is None)
                if x is not None and y is not None:
                    deltas.append(y - x)
        timing[key + "_time"] = {
            "compared": len(deltas), "source_missing": missing_before, "output_missing": missing_after,
            "missingness_changed": changed_missing,
            "max_absolute_delta": float(max(map(abs, deltas))) if deltas else None,
            "known_values_preserved": bool(hashes_match and deltas and not changed_missing and all(abs(d) <= tolerance for d in deltas)),
            "status": "not_compared" if not hashes_match else
                      "failed" if changed_missing or any(abs(d) > tolerance for d in deltas) else
                      "passed" if deltas else "unknown",
        }
    duration_changes, padding_changes = [], []
    if hashes_match:
        for index, (p, q) in enumerate(zip(ap, bp)):
            x, y = packet_time(p, "duration", a), packet_time(q, "duration", b)
            if (x is None) != (y is None) or (x is not None and y is not None and abs(x - y) > tolerance):
                duration_changes.append(index)
            if p.get("side_data_list", []) != q.get("side_data_list", []):
                padding_changes.append(index)
    codec_match = a.get("codec_name") == b.get("codec_name")
    timestamps_match = all(x["known_values_preserved"] for x in timing.values())
    timestamp_status = "not_compared" if not hashes_match else "failed" if any(
        x["status"] == "failed" for x in timing.values()) else "passed" if timestamps_match else "unknown"
    return {
        "kind": kind, "source_codec": a.get("codec_name"), "output_codec": b.get("codec_name"),
        "source_packet_count": len(ap), "output_packet_count": len(bp),
        "ordered_payload_hashes_identical": bool(hashes_match), "timestamp_rounding_tolerance": float(tolerance),
        "timestamps": timing,
        "encoded_packet_preservation": "passed" if hashes_match and codec_match else "failed",
        "timestamp_preservation": timestamp_status,
        "packet_alignment": "ordered_identical_payloads" if hashes_match else "not_established",
        "auxiliary_timing": {"duration_changed_packet_indices": duration_changes,
                             "side_data_changed_packet_indices": padding_changes,
                             "scope": "Reported separately; strict payload/PTS/DTS result does not establish audio priming/padding or decoded-sample equivalence."},
        "success": bool(hashes_match and codec_match and timestamps_match),
    }


def expectation(args):
    for value in (args.expected_start, args.expected_end, args.expected_duration):
        if value is not None and not math.isfinite(value):
            raise ValueError("Expected start/end/duration must be finite")
    start = args.expected_start if args.expected_start is not None else 0.0
    end = args.expected_end
    if args.expected_duration is not None and args.expected_duration <= 0:
        raise ValueError("--expected-duration must be positive")
    if end is not None and end <= start:
        raise ValueError("Expected end must be greater than expected start")
    return {
        "start": start, "end": end,
        "status": "specified" if end is not None else "unknown",
        "source": "explicit CLI selected interval" if end is not None else "no selected interval end supplied",
        "coordinate": "native packet PTS unless an explicit track mapping is supplied",
        "presentation_duration": args.expected_duration,
        "presentation_duration_scope": "Overall player/source metadata; never automatically assigned to every track",
    }


def load_track_expectations(path):
    """Read declared track extents and explicit native-to-source mappings.

    The caller supplies provenance. This function validates the declaration's
    shape, not the truth of an external source or a claimed observation.
    """
    if path is None:
        return {}
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Track expectations exceed the 1 MiB declaration budget")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) - {"video", "audio"}:
        raise ValueError("Track expectations must be an object keyed by video/audio")
    result = {}
    for kind, entry in data.items():
        if not isinstance(entry, dict) or set(entry) - {"start", "end", "timestamp_offset", "source", "coordinate"}:
            raise ValueError(kind + ": invalid track expectation fields")
        source = entry.get("source")
        coordinate = entry.get("coordinate", "native_packet")
        if not isinstance(source, str) or not source.strip():
            raise ValueError(kind + ": a nonempty expectation source is required")
        if not isinstance(coordinate, str) or not coordinate.strip():
            raise ValueError(kind + ": coordinate must be a nonempty label")
        values = {key: fraction(entry.get(key, default)) for key, default in
                  (("start", "0"), ("end", None), ("timestamp_offset", "0"))}
        if any(v is None for v in values.values()) or values["end"] <= values["start"]:
            raise ValueError(kind + ": finite rational start/end/offset and end > start are required")
        result[kind] = {**{key: str(value) for key, value in values.items()},
                        "source": source, "coordinate": coordinate,
                        "provenance_verification": "caller_supplied; not independently verified by this validator"}
    return result


def _intersects(a, b, start, end):
    return b > start and (end is None or a < end)


def coverage(info, expected, opus_delay=0, selected_ids=None, track_expectations=None):
    """Check native packet intervals in an explicitly identified coordinate.

    opus_delay remains an ignored positional argument for callers of v1.1.
    Priming/padding never supplies video coverage. A container-level duration
    is diagnostic only: it may refer to another track or another coordinate.
    """
    details = []
    declarations = track_expectations or {}
    for summary in info["summaries"]:
        if selected_ids is not None and summary["stream_index"] not in selected_ids:
            continue
        kind = summary["codec_type"]
        declaration = declarations.get(kind)
        offset = fraction(declaration["timestamp_offset"]) if declaration else fraction(0)
        start, end = fraction(expected.get("start", 0)), fraction(expected.get("end"))
        if declaration:
            declared_start, declared_end = fraction(declaration["start"]), fraction(declaration["end"])
            if end is None:
                start, end = declared_start, declared_end
            else:
                start, end = max(start, declared_start), min(end, declared_end)
        tolerance = fraction(summary["gap_rounding_tolerance"])
        native_intervals = summary.get("packet_intervals_exact", summary.get("packet_intervals", []))
        intervals = [(fraction(a) + offset, fraction(b) + offset) for a, b in native_intervals]
        first = fraction(summary.get("first_pts_exact", summary.get("first_pts")))
        finish = fraction(summary.get("packet_end_exact", summary.get("packet_end")))
        first = first + offset if first is not None else None
        finish = finish + offset if finish is not None else None
        failures, review = [], []
        relevant_gaps = [gap for gap in summary["gaps"] if _intersects(
            fraction(gap["start"]) + offset, fraction(gap["end"]) + offset, start, end)]
        estimates = summary.get("estimated_intervals_exact", [])
        estimated_in_scope = any(_intersects(fraction(a) + offset, fraction(b) + offset, start, end)
                                 for a, b in estimates)
        if not estimates and summary.get("estimated_duration_packets"):
            estimated_in_scope = True  # Compatibility with older saved summaries.
        if end is not None and end <= start:
            status = "not_applicable"
        else:
            if not summary["packet_count"] or first is None:
                failures.append("no timestamped media packets")
            if summary["missing_pts"]:
                review.append("packets without PTS cannot be assigned confidently to the requested interval")
            duplicate_positions = summary.get("duplicate_packet_positions_exact", [])
            if any(position is None or (fraction(position) + offset >= start and (end is None or fraction(position) + offset < end))
                   for position in duplicate_positions) or (summary["duplicate_packet_records"] and not duplicate_positions):
                review.append("duplicate packet records occur within, or cannot be scoped outside, the requested interval")
            if any(regression.get("pts") is None or (fraction(regression["pts"]) + offset >= start and
                   (end is None or fraction(regression["pts"]) + offset < end)) for regression in summary["dts_regressions"]):
                review.append("DTS regression occurs within, or cannot be scoped outside, the requested interval")
            if estimated_in_scope or (summary["typical_packet_duration"] <= 0):
                review.append("native packet duration is unresolved within the requested extent")
            if relevant_gaps:
                (failures if end is not None else review).append("native packet intervals contain an uncovered gap in scope")
            if end is not None and first is not None:
                if first > start + tolerance:
                    failures.append("declared/requested interval start is uncovered")
                if finish < end - tolerance:
                    (review if estimated_in_scope else failures).append("declared/requested interval end is uncovered")
            status = "failed" if failures else "needs_review" if review else "covered_with_stated_tolerance" if end is not None else "unknown"
        global_issues = []
        if summary["gaps"]:
            global_issues.append("potential full-file packet gaps")
        if summary["duplicate_packet_records"]:
            global_issues.append("duplicate packet records")
        if summary["dts_regressions"]:
            global_issues.append("DTS regressions")
        if summary["timing_requires_review"]:
            global_issues.extend(summary["timing_requires_review"])
        header = num(info.get("format", {}).get("duration"))
        details.append({
            "stream_index": summary["stream_index"], "kind": kind,
            "first_pts": float(first) if first is not None else None,
            "packet_end": float(finish) if finish is not None else None,
            "native_packet_end": summary["packet_end"],
            "expected_interval": {"start": float(start), "end": float(end) if end is not None else None},
            "track_expectation": declaration,
            "timestamp_mapping": {"offset_exact": str(offset), "automatic_fitting": False,
                                  "coordinate": declaration["coordinate"] if declaration else "native_packet"},
            "expected_end_shortfall_seconds": float(max(fraction(0), end - finish)) if end is not None and finish is not None else None,
            "tail_tolerance_seconds": float(tolerance),
            "tail_tolerance_basis": {"container_rounding": float(tolerance), "observed_opus_padding_allowance": 0},
            "header_duration_minus_packet_end": header - summary["packet_end"] if header is not None and summary["packet_end"] is not None else None,
            "header_comparison": "diagnostic_only; container duration may use another track or coordinate",
            "gaps_in_requested_interval": relevant_gaps,
            "global_integrity": {"status": "needs_review" if global_issues else "no_detected_issues", "issues": global_issues},
            "issues": failures + review, "failures": failures, "review_reasons": review, "status": status,
        })
    statuses = {row["status"] for row in details}
    overall = "failed" if "failed" in statuses else "needs_review" if "needs_review" in statuses else "unknown" if "unknown" in statuses or not statuses else "not_applicable" if statuses == {"not_applicable"} else "covered_with_stated_tolerance"
    return {"expected_interval": expected, "streams": details, "status": overall,
            "limitation": "Coverage evaluates declared/requested native packet intervals. It does not independently authenticate source extents, identify content, prove audible-sample coverage or establish visual review."}


def combined_status(statuses):
    values = set(statuses)
    if "failed" in values:
        return "failed"
    if values & {"needs_review", "unsupported", "refused"}:
        return "needs_review"
    if "not_compared" in values:
        return "not_compared"
    if "unknown" in values:
        return "unknown"
    if values and values <= {"passed", "not_applicable"}:
        return "passed" if "passed" in values else "not_applicable"
    return "not_tested"


def opus_context(structure):
    """Return the selected audio decoder context, without index/container IDs."""
    tracks = [track for track in structure.get("tracks", []) if track.get("kind") == "audio"]
    if len(tracks) != 1 or tracks[0].get("codec_id") != "A_OPUS" or not isinstance(tracks[0].get("opus"), dict):
        raise ValueError("Analysis-copy verification currently requires one parsed Opus audio track")
    track = tracks[0]
    return {"codec_id": track["codec_id"], "codec_private_sha256": track.get("codec_private_sha256"),
            "codec_delay_ns": track.get("codec_delay_ns"), "seek_preroll_ns": track.get("seek_preroll_ns"),
            "opus": track["opus"],
            "discard_padding_ns": [item.get("value_ns") for item in structure.get("discard_padding", [])
                                   if item.get("track_number") == track.get("track_number")]}


def create_audio_analysis_copy(source, data, info, out, timeout, max_bytes):
    """Stream-copy into a fresh container, keeping raw evidence unchanged.

    A valid derivative is not a repaired or authenticated original. Structural
    media boundaries, mapped packet identity/timestamps, Opus priming metadata
    and decoded retained samples must all verify before this returns passed.
    """
    result = {"status": "not_tested", "kind": "audio", "errors": [],
              "source_sha256": info.get("sha256"), "hosted_original_completeness": "not_established",
              "scope": "New normalized analysis derivative; the original bytes, warnings and completeness remain separate."}
    structure = info.get("structured_container", {})
    if info.get("full_decode", {}).get("status") != "passed" or structure.get("observed_media_elements_complete") is not True:
        result.update(status="refused", errors=["Source decoding and complete observed container media elements are required"])
        return result
    try:
        before_context = opus_context(structure)
    except ValueError as error:
        result.update(status="unsupported", errors=[str(error)])
        return result
    directory = out / "audio-analysis"
    try:
        directory.mkdir(exist_ok=False)
        target = directory / "audio.mka"
        mux, _ = run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "warning", "-n", "-copyts",
                      "-i", str(source), "-map", "0:a:0", "-c", "copy", "-avoid_negative_ts", "disabled",
                      "-f", "matroska", str(target)], timeout, directory, "stream-copy", max_bytes)
        result.update(path=str(target), stream_copy=mux)
        if mux.get("resource_limit"):
            raise ReviewRequired("Analysis audio stream copy exceeded its resource budget")
        if mux.get("returncode") != 0:
            raise ValueError("Analysis audio stream copy failed")
        copied, copied_data = probe(target, directory, "analysis", timeout, max_bytes=max_bytes)
        copied["full_decode"] = decode(target, timeout, directory, "analysis.decode", max_bytes)
        result["probe"] = copied
        if not copied.get("success") or copied["full_decode"]["status"] != "passed":
            raise ValueError("Analysis derivative probing or full decode failed")
        after_structure = copied.get("structured_container", {})
        clean = (after_structure.get("status") == "passed" and
                 after_structure.get("outer_envelope", {}).get("status") == "complete" and
                 after_structure.get("observed_media_elements_complete") is True and
                 not copied.get("container_warnings") and not copied["full_decode"].get("decode_diagnostics"))
        result["clean_container"] = "passed" if clean else "needs_review"
        comparison = preservation(data, copied_data, "audio")
        result["preservation"] = comparison
        after_context = opus_context(after_structure)
        result["decoder_context"] = {"source": before_context, "output": after_context,
                                     "status": "passed" if before_context == after_context else "needs_review"}
        from decoded_equivalence import compare_decoded
        decoded = compare_decoded(source, target, "audio", directory, timeout, max_bytes)
        result["decoded_comparison"] = decoded
        if decoded.get("content_equality") == "failed" or decoded.get("timeline_equivalence") == "failed":
            raise ValueError("Analysis derivative decoded audio or timeline differs")
        packet_padding = not comparison.get("auxiliary_timing", {}).get("side_data_changed_packet_indices")
        result["packet_padding_preservation"] = "passed" if packet_padding else "needs_review"
        verified = clean and comparison.get("success") is True and before_context == after_context and packet_padding and decoded.get("status") == "passed"
        result["status"] = "passed" if verified else "needs_review"
    except ReviewRequired as error:
        result.update(status="needs_review", errors=[str(error)])
    except (OSError, ValueError, KeyError, TypeError) as error:
        result.update(status="failed", errors=[str(error)])
    return result


def analysis_readiness(report, stages, analysis_copy_requested=False, separate_audio=False, decoded_requested=False):
    """Assess technical analysis inputs/derivatives without changing raw verdicts."""
    analysis = report.get("analysis_copy", {})
    containers_ready = all(info.get("container_duration_evidence", {}).get("status") != "failed" and
                           (not info.get("container_warnings") and
                            info.get("structured_container", {}).get("status") not in ("failed", "needs_review")
                            or (analysis.get("status") == "passed" and label == analysis.get("source_label")))
                           for label, info in report["inputs"].items())
    output = report.get("output") or {}
    remux_ready = (not separate_audio or (stages["output_decode"]["status"] == "passed" and
                   stages["timestamp_preservation"]["status"] == "passed" and
                   (stages["encoded_preservation"]["status"] == "passed" or
                    (stages["framing_equivalence"]["status"] == "passed" and
                     output.get("framing_equivalence", {}).get("canonical_payloads_identical") is True)) and
                   (not decoded_requested or stages["decoded_equivalence"]["status"] == "passed")))
    ready = (not report["errors"] and stages["probe"]["status"] == "passed" and
             stages["decode"]["status"] == "passed" and stages["native_coverage"]["status"] == "passed" and
             containers_ready and remux_ready and
             (not analysis_copy_requested or analysis.get("status") == "passed" or stages["analysis_copy"]["status"] == "not_applicable"))
    return {"status": "passed" if ready else "needs_review" if not report["errors"] else "failed",
            "scope": "Technically decoded and interval-covered captured inputs, plus verified requested audio derivative; raw container/encoded findings and source completeness remain separate.",
            "visual_inspection": "not_established", "audio_audition": "not_established",
            "hosted_original_completeness": "not_established"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--expected-duration", type=float, help="Overall presentation duration metadata, not a shared track endpoint.")
    parser.add_argument("--expected-start", type=float, help="Selected interval start in the declared coordinate; default 0.")
    parser.add_argument("--expected-end", type=float, help="Explicit selected interval end; intersected with declared track extents.")
    parser.add_argument("--track-expectations", type=Path, help="JSON video/audio declarations with start/end, source, optional timestamp_offset and coordinate.")
    parser.add_argument("--decoded-equivalence", action="store_true", help="Compare decoded pictures/samples and timelines after separate-track remuxing.")
    parser.add_argument("--analysis-copy", action="store_true", help="Create and verify a separate clean Opus audio analysis container; raw findings remain unchanged.")
    parser.add_argument("--max-probe-bytes", type=int, default=DEFAULT_PROBE_BYTES, help="Per-process output budget before probe JSON is parsed; expanded objects use additional memory.")
    parser.add_argument("--timeout", type=int, default=1800, help="Per subprocess timeout in seconds.")
    args = parser.parse_args()
    try:
        if args.timeout <= 0 or args.max_probe_bytes <= 0:
            raise ValueError("Timeout and output budget must be positive")
        expected = expectation(args)
        declared = load_track_expectations(args.track_expectations)
        out, snapshots = prepare_run(args.output_dir, {"input": args.input, "audio": args.audio})
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "refused", "errors": [str(error)]}), file=sys.stderr)
        return 1
    report_path, checkpoint_path = out / "validation.json", out / "checkpoint.json"
    stage_names = ("probe", "decode", "container_structure", "container_timing_evidence", "native_coverage", "remux", "output_probe", "output_decode", "encoded_preservation", "framing_equivalence", "timestamp_preservation", "decoded_equivalence", "analysis_copy")
    report = {
        "schema_version": 3, "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "running", "inputs": {label: dict(value) for label, value in snapshots.items()},
        "output": None, "errors": [], "warnings": [], "review_reasons": [],
        "stage_results": {name: {"status": "not_tested"} for name in stage_names},
        "expected_interval": expected, "track_expectations": declared,
        "audio_presence": "unknown", "technical_decode": "not_tested",
        "source_identity": "not_tested", "visual_review": {"performed": False},
        "policy": "Original inputs unchanged; bounded subprocess output; no shortest/rebase/interpolation/repair. Overall presentation duration is separate from declared source-track extents.",
    }
    stages = report["stage_results"]

    def checkpoint(stage):
        save(report_path, report)
        save(checkpoint_path, {"stage": stage, "status": report["status"], "report": str(report_path),
                               "updated_utc": datetime.now(timezone.utc).isoformat()})

    def review(reason):
        if reason not in report["review_reasons"]:
            report["review_reasons"].append(reason)

    def coverage_check(label, info, selected):
        result = coverage(info, expected, selected_ids=selected, track_expectations=declared)
        info["coverage"] = result
        if result["status"] == "failed":
            report["errors"].append(label + ": declared/requested native packet interval is uncovered")
        elif result["status"] == "needs_review":
            review(label + ": native interval timing requires review")
        for row in result["streams"]:
            if row["global_integrity"]["issues"]:
                report["warnings"].append(label + ": global integrity diagnostics retained separately from selected-interval coverage")
        return result["status"]

    try:
        raw, probe_statuses, decode_statuses = {}, [], []
        checkpoint("preflight")
        for label, baseline in snapshots.items():
            path = Path(baseline["path"])
            try:
                info, data = probe(path, out, label, args.timeout, baseline, args.max_probe_bytes)
                report["inputs"][label] = info
                required_kind = "video" if label == "input" else "audio"
                required_stream = stream_by_kind(data, required_kind)
                if not info["success"] or required_stream is None or not packets_for(data, required_stream):
                    raise ValueError(label + ": probing did not find the required playable " + required_kind + " stream")
                raw[label] = data
                probe_statuses.append("passed")
                info["full_decode"] = decode(path, args.timeout, out, label + ".decode", args.max_probe_bytes)
                decode_statuses.append(info["full_decode"]["status"])
                if info["full_decode"]["status"] == "failed":
                    report["errors"].append(label + ": full decode failed")
                elif info["full_decode"]["status"] == "needs_review":
                    review(label + ": full decode could not complete within its resource budget")
                if info["container_warnings"] or info["full_decode"]["decode_diagnostics"]:
                    report["warnings"].append(label + ": container/decode diagnostics retained in process logs")
                    review(label + ": container/decode diagnostics require inspection")
                timing_evidence = info.get("container_duration_evidence", {})
                if timing_evidence.get("status") == "failed":
                    review(label + ": container sample timing evidence could not be verified")
                container_evidence = info.get("structured_container", {})
                if container_evidence.get("status") == "failed":
                    report["errors"].append(label + ": structured container media is malformed or truncated")
                elif container_evidence.get("status") == "needs_review":
                    review(label + ": structured container findings require inspection")
            except ReviewRequired as error:
                probe_statuses.append("needs_review")
                report["inputs"][label]["probe_status"] = "needs_review"
                report["inputs"][label]["diagnostic"] = str(error)
                review(str(error))
            except (OSError, ValueError, KeyError, TypeError) as error:
                if label in raw:
                    decode_statuses.append("failed")
                    report["inputs"][label]["full_decode"] = {"status": "failed", "success": False, "error": str(error)}
                else:
                    probe_statuses.append("failed")
                    report["inputs"][label]["probe_status"] = "failed"
                report["errors"].append(str(error))
            stages["probe"]["status"] = combined_status(probe_statuses)
            stages["decode"]["status"] = combined_status(decode_statuses)
            report["technical_decode"] = stages["decode"]["status"]
            checkpoint("inspected_" + label)

        has_audio = stream_by_kind(raw.get("audio", raw.get("input", {})), "audio") is not None
        timing_states = [info.get("container_duration_evidence", {}).get("status") for info in report["inputs"].values()]
        stages["container_timing_evidence"]["status"] = "needs_review" if "failed" in timing_states else "passed" if "verified" in timing_states else "not_applicable"
        report["audio_presence"] = "present" if has_audio else "absent_video_only" if "input" in raw else "unknown"
        if report["audio_presence"] == "absent_video_only":
            report["warnings"].append("No audio stream: checks concern video only, not full audiovisual media.")
        coverage_statuses = []
        for label, data in raw.items():
            kinds = ("video",) if args.audio and label == "input" else ("audio",) if label == "audio" else ("video", "audio")
            selected = [stream_by_kind(data, kind)["index"] for kind in kinds if stream_by_kind(data, kind)]
            info = report["inputs"][label]
            info["coverage_stream_indices"] = selected
            info["unmapped_media_streams"] = [s["stream_index"] for s in info["summaries"] if s["stream_index"] not in selected]
            coverage_statuses.append(coverage_check(label, info, selected))
        mapped_status = {"covered_with_stated_tolerance": "passed"}
        stages["native_coverage"]["status"] = combined_status(mapped_status.get(value, value) for value in coverage_statuses)
        stages["container_structure"]["status"] = "failed" if any(
            info.get("structured_container", {}).get("status") == "failed" for info in report["inputs"].values()) else "needs_review" if any(
            info.get("container_warnings") or info.get("structured_container", {}).get("status") in ("failed", "needs_review")
            for info in report["inputs"].values()) else "no_detected_issues" if raw else "not_tested"
        checkpoint("checked_native_coverage")

        if args.analysis_copy and has_audio:
            audio_label = "audio" if "audio" in raw else "input"
            report["analysis_copy"] = create_audio_analysis_copy(Path(snapshots[audio_label]["path"]), raw[audio_label],
                    report["inputs"][audio_label], out, args.timeout, args.max_probe_bytes)
            report["analysis_copy"]["source_label"] = audio_label
            stages["analysis_copy"]["status"] = report["analysis_copy"]["status"]
            if report["analysis_copy"]["status"] == "failed":
                report["errors"].append("Audio analysis derivative verification failed; original source evidence is retained")
            elif report["analysis_copy"]["status"] != "passed":
                review("Audio analysis derivative could not be fully verified")
            checkpoint("created_analysis_copy")
        elif args.analysis_copy:
            stages["analysis_copy"] = {"status": "not_applicable", "reason": "No audio stream is present"}

        can_remux = args.audio and {"input", "audio"} <= raw.keys() and all(
            report["inputs"][label].get("full_decode", {}).get("status") == "passed" for label in ("input", "audio"))
        if can_remux:
            target = out / "native.mkv"
            mux, _ = run([
                "ffmpeg", "-hide_banner", "-nostdin", "-v", "warning", "-n", "-copyts",
                "-i", snapshots["input"]["path"], "-i", snapshots["audio"]["path"],
                "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-avoid_negative_ts", "disabled", str(target)
            ], args.timeout, out, "remux", args.max_probe_bytes)
            report["output"] = {"path": str(target), "stream_copy": mux, "decoded_av_equivalence": "not_tested"}
            stages["remux"]["status"] = "needs_review" if mux["resource_limit"] else "passed" if mux["returncode"] == 0 else "failed"
            checkpoint("stream_copy")
            if stages["remux"]["status"] == "failed":
                report["errors"].append("Native MKV stream copy failed")
            elif stages["remux"]["status"] == "needs_review":
                review("Native MKV stream copy exceeded a resource budget")
            else:
                try:
                    info, data = probe(target, out, "native", args.timeout, max_bytes=args.max_probe_bytes)
                    stages["output_probe"]["status"] = "passed" if info["success"] else "failed"
                except ReviewRequired:
                    stages["output_probe"]["status"] = "needs_review"
                    raise
                except (OSError, ValueError, KeyError, TypeError):
                    stages["output_probe"]["status"] = "failed"
                    raise
                output = report["output"]
                output["probe"] = info
                if not info["success"]:
                    raise ValueError("Native MKV probe failed")
                info["full_decode"] = decode(target, args.timeout, out, "native.decode", args.max_probe_bytes)
                output["technical_decode"] = info["full_decode"]["status"]
                stages["output_decode"]["status"] = output["technical_decode"]
                if output["technical_decode"] == "failed":
                    report["errors"].append("Native MKV full decode failed")
                elif output["technical_decode"] == "needs_review":
                    review("Native MKV decoding exceeded a resource budget")
                comparisons = [preservation(raw["input"], data, "video"), preservation(raw["audio"], data, "audio")]
                output["preservation"] = comparisons
                from av1_framing import compare_av1_framing
                framing = compare_av1_framing(Path(snapshots["input"]["path"]), target, raw["input"], data,
                                              out, args.timeout, args.max_probe_bytes)
                output["framing_equivalence"] = framing
                stages["framing_equivalence"]["status"] = framing.get("status", "not_tested")
                for field, stage in (("encoded_packet_preservation", "encoded_preservation"), ("timestamp_preservation", "timestamp_preservation")):
                    output[field] = combined_status(item[field] for item in comparisons)
                    stages[stage]["status"] = output[field]
                output["strict_timestamp_preservation"] = output["timestamp_preservation"]
                if framing.get("status") == "passed" and framing.get("canonical_payloads_identical") is True:
                    output["framing_aligned_timestamp_preservation"] = framing.get("timestamp_preservation", "unknown")
                    canonical_timing = combined_status([framing.get("timestamp_preservation", "unknown"), comparisons[1]["timestamp_preservation"]])
                    stages["timestamp_preservation"]["status"] = canonical_timing
                    output["timestamp_preservation"] = canonical_timing
                    output["timestamp_alignment_basis"] = "AV1 empty-temporal-delimiter equivalence for video; ordered strict payload identity for audio"
                    output["explained_changes"] = ["AV1 packet framing equivalence verified independently; strict encoded-byte identity remains separate"]
                elif framing.get("status") not in ("not_applicable", "not_tested"):
                    review("AV1 framing equivalence was not established")
                if output["encoded_packet_preservation"] == "failed":
                    review("Strict encoded packet identity changed during remuxing; decoded equivalence is a separate check")
                if output["timestamp_preservation"] == "failed":
                    report["errors"].append("Aligned native packet timestamps changed beyond the stated rounding allowance")
                elif output["timestamp_preservation"] in ("not_compared", "unknown"):
                    review("Native packet timestamp preservation was not established")
                if any(item["auxiliary_timing"]["side_data_changed_packet_indices"] for item in comparisons):
                    review("Remux changed packet side data; inspect priming/padding or decoded samples")
                output["interpretation"] = "Strict encoded identity, aligned timestamp preservation, decoded content and decoded timing have independent results. A strict preservation failure alone does not prove corrupted pictures or samples."
                output_coverage = coverage_check("native output", info, None)
                stages["native_coverage"]["status"] = combined_status([
                    stages["native_coverage"]["status"], mapped_status.get(output_coverage, output_coverage)])
                if mux["stderr"] or info["container_warnings"] or info["full_decode"]["stderr"]:
                    report["warnings"].append("Native mux/output diagnostics retained separately")
                    review("Native mux/output emitted diagnostics")
                if args.decoded_equivalence:
                    from decoded_equivalence import compare_decoded
                    decoded = [compare_decoded(Path(snapshots[label]["path"]), target, kind, out,
                                               args.timeout, args.max_probe_bytes,
                                               source_duration_evidence=report["inputs"][label].get("container_duration_evidence")
                                                   if kind == "video" and report["inputs"][label].get("container_duration_evidence", {}).get("status") == "verified" else None)
                               for label, kind in (("input", "video"), ("audio", "audio"))]
                    output["decoded_comparisons"] = decoded
                    output["decoded_av_equivalence"] = combined_status(item["status"] for item in decoded)
                    stages["decoded_equivalence"]["status"] = output["decoded_av_equivalence"]
                    if output["decoded_av_equivalence"] == "failed":
                        report["errors"].append("Decoded remux content or timeline equivalence failed")
                    elif output["decoded_av_equivalence"] != "passed":
                        review("Decoded remux equivalence requires review")
                checkpoint("verified_output")
        elif not args.audio:
            for name in ("remux", "output_probe", "output_decode", "encoded_preservation", "framing_equivalence", "timestamp_preservation", "decoded_equivalence"):
                stages[name] = {"status": "not_applicable", "reason": "No separate-track remux requested"}
        else:
            stages["remux"]["reason"] = "Required source probe/decode did not complete successfully"
    except ReviewRequired as error:
        review(str(error))
    except (OSError, ValueError, KeyError, TypeError) as error:
        report["errors"].append(str(error))
    finally:
        for label, baseline in snapshots.items():
            path = Path(baseline["path"])
            try:
                unchanged = path.is_file() and digest(path) == baseline["sha256"]
            except OSError:
                unchanged = False
            report["inputs"][label]["input_bytes_unchanged"] = unchanged
            if not unchanged:
                report["errors"].append("Input bytes changed during validation: " + str(path))
        report["analysis_readiness"] = analysis_readiness(report, stages, args.analysis_copy, bool(args.audio), args.decoded_equivalence)
        if report["errors"]:
            report["status"], report["completeness"] = "failed", "not_established"
        elif report["review_reasons"]:
            report["status"], report["completeness"] = "needs_review", "not_established"
        else:
            report["completeness"] = "declared_or_requested_interval_covered" if stages["native_coverage"]["status"] in ("passed", "not_applicable") else "unknown"
            report["status"] = "validated_completeness_unknown" if report["completeness"] == "unknown" else "passed_with_warnings" if report["warnings"] else "passed"
        checkpoint("finished")
    print(json.dumps({"status": report["status"], "report": str(report_path), "checkpoint": str(checkpoint_path),
                      "output": report["output"]["path"] if report["output"] else None,
                      "errors": report["errors"], "review_reasons": report["review_reasons"]}, indent=2))
    return 1 if report["status"] == "failed" else 2 if report["status"] == "needs_review" else 0


if __name__ == "__main__":
    sys.exit(main())
