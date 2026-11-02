#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Compare losslessly decoded content and its timeline without changing inputs.

This checks one selected track in two local files, not hosted-source identity or
encoded bitstream identity. FFmpeg/ffprobe are the only external dependencies.
"""
import argparse
from collections import Counter
from fractions import Fraction
import hashlib
import itertools
import json
from pathlib import Path
import re
import subprocess
import time


PCM_FORMATS = {
    "u8": ("pcm_u8", 1), "s16": ("pcm_s16le", 2),
    "s32": ("pcm_s32le", 4), "s64": ("pcm_s64le", 8),
    "flt": ("pcm_f32le", 4), "dbl": ("pcm_f64le", 8),
}


class DiagnosticLimit(Exception):
    """A bounded check could not finish; this is not a media corruption verdict."""


class ProcessFailure(Exception):
    pass


def _fraction(value):
    try:
        return Fraction(str(value))
    except (ValueError, TypeError, ZeroDivisionError):
        return None


def _digest(path):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


class _Run:
    def __init__(self, directory, timeout, budget):
        self.directory = directory
        self.deadline = time.monotonic() + timeout
        self.budget = budget
        self.commands = []

    def check(self):
        if time.monotonic() >= self.deadline:
            raise DiagnosticLimit("decoded comparison timeout")
        size = sum(p.stat().st_size for p in self.directory.iterdir() if p.is_file())
        if size > self.budget:
            raise DiagnosticLimit("decoded diagnostic output exceeded byte budget")

    def execute(self, command, label):
        self.check()
        stdout = self.directory / (label + ".stdout")
        stderr = self.directory / (label + ".stderr")
        record = {"command": command, "stdout": str(stdout), "stderr": str(stderr)}
        self.commands.append(record)
        with stdout.open("xb") as output, stderr.open("xb") as errors:
            process = subprocess.Popen(command, stdout=output, stderr=errors, stdin=subprocess.DEVNULL)
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
            raise ProcessFailure(label + " exited with status " + str(process.returncode))
        return stdout

    def frame_timing(self, path, label, kind):
        selector = "v:0" if kind == "video" else "a:0"
        fields = "frame=pts,pkt_duration" if kind == "video" else "frame=pts,nb_samples"
        return self.execute([
            "ffprobe", "-v", "warning", "-threads", "1", "-select_streams", selector, "-show_frames",
            "-show_entries", fields, "-of", "compact=p=0:nk=0", str(path),
        ], label + "-frames")

    def probe(self, path, kind, label):
        selector = "v:0" if kind == "video" else "a:0"
        output = self.execute([
            "ffprobe", "-v", "warning", "-select_streams", selector,
            "-show_streams", "-of", "json", str(path),
        ], label + "-probe")
        data = json.loads(output.read_text(encoding="utf-8"))
        streams = data.get("streams", [])
        if len(streams) != 1:
            raise ValueError("expected one selected " + kind + " stream in " + label)
        return streams[0]


def _frames(path):
    """Yield bounded framehash records; retain rational output clock metadata."""
    base = None
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("#tb 0:"):
                base = _fraction(line.split(":", 1)[1].strip())
            if not line.strip() or line.startswith("#"):
                continue
            values = [part.strip() for part in line.split(",")]
            if len(values) != 6 or base is None or base <= 0:
                raise ValueError("unrecognized framehash record")
            if not re.fullmatch(r"[0-9a-fA-F]{64}", values[5]):
                raise ValueError("unrecognized SHA-256 framehash")
            pts = _fraction(values[2])
            dts = _fraction(values[1])
            duration = _fraction(values[3])
            size = int(values[4])
            if size < 0:
                raise ValueError("negative decoded frame size")
            yield {"pts": pts * base if pts is not None else None,
                   "dts": dts * base if dts is not None else None,
                   "duration": duration * base if duration is not None else None,
                   "size": size, "hash": values[5].lower(), "time_base": base}


def _rounding_tolerance(source, output, nominal):
    """Use the sum of native ticks for unknown floor/nearest quantizers.

    A clock as coarse as half the decoded interval cannot establish alignment;
    do not silently turn that uncertainty into a whole-frame allowance.
    """
    clocks = [_fraction(s.get("time_base")) for s in (source, output)]
    if any(base is None or base <= 0 for base in clocks) or nominal is None or nominal <= 0:
        return None, False
    tolerance = sum(clocks)
    return tolerance, tolerance < nominal / 2


def _native_frames(path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            fields = dict(part.split("=", 1) for part in line.strip().split("|") if "=" in part)
            if not any(key in fields for key in ("pts", "pkt_duration", "nb_samples")):
                continue
            yield fields


def _verified_frame_durations(evidence, stream, timing, expected_hash):
    """Bind sample durations to the complete unique decoded presentation timeline.

    Container evidence cannot be assigned by approximate timestamps or by a
    prefix. Every verified sample must correspond one-to-one to a decoded frame
    with the same rational PTS. This supports the bounded fragmented-MP4 reader;
    it does not infer a display duration from a nominal frame rate.
    """
    if evidence is None:
        return {}, {"status": "not_requested", "filled_values": 0}
    if not isinstance(evidence, dict) or evidence.get("status") != "verified" or evidence.get("source", {}).get("sha256") != expected_hash:
        return {}, {"status": "rejected", "filled_values": 0, "reason": "Container evidence is not verified for these exact source bytes"}
    matches = [item for item in evidence.get("streams", []) if item.get("stream_index") == stream.get("index")]
    if len(matches) != 1:
        return {}, {"status": "rejected", "filled_values": 0, "reason": "Container evidence has no unique selected stream"}
    selected = matches[0]
    base, native_base = _fraction(selected.get("time_base")), _fraction(stream.get("time_base"))
    samples = selected.get("samples")
    if base is None or base <= 0 or native_base is None or native_base <= 0 or not isinstance(samples, list) or not samples:
        return {}, {"status": "rejected", "filled_values": 0, "reason": "Missing container sample timing"}
    durations, missing = {}, 0
    for sample, frame in itertools.zip_longest(samples, _native_frames(timing)):
        if not isinstance(sample, dict) or frame is None:
            return {}, {"status": "rejected", "filled_values": 0, "reason": "Container samples and decoded-frame count do not agree"}
        ticks, length = sample.get("pts"), sample.get("duration")
        decoded_pts = _fraction(frame.get("pts"))
        if type(ticks) is not int or type(length) is not int or length <= 0 or decoded_pts is None:
            return {}, {"status": "rejected", "filled_values": 0, "reason": "Unusable sample or decoded timestamp"}
        position, duration = ticks * base, length * base
        if decoded_pts * native_base != position or position in durations:
            return {}, {"status": "rejected", "filled_values": 0, "reason": "Container samples do not align one-to-one to unique decoded PTS"}
        observed = _fraction(frame.get("pkt_duration"))
        if observed is not None and observed > 0 and observed * native_base != duration:
            return {}, {"status": "rejected", "filled_values": 0, "reason": "Existing decoded timing contradicts the container sample duration"}
        missing += observed is None or observed <= 0
        durations[position] = duration
    return durations, {"status": "applied", "filled_values": missing, "aligned_sample_count": len(durations),
                       "source_sha256": expected_hash, "basis": "Verified container samples aligned to the entire unique decoded-frame PTS sequence"}


def _video_records(framehash, timing, stream, duration_evidence=None):
    base = _fraction(stream.get("time_base"))
    if base is None or base <= 0:
        raise ValueError("missing native video time base")
    for frame, times in itertools.zip_longest(_frames(framehash), _native_frames(timing)):
        if frame is None or times is None:
            raise ValueError("decoded hash and native timing frame counts disagree")
        pts, duration = _fraction(times.get("pts")), _fraction(times.get("pkt_duration"))
        frame["pts"] = pts * base if pts is not None else None
        frame["duration"] = duration * base if duration is not None else None
        if (frame["duration"] is None or frame["duration"] <= 0) and duration_evidence:
            frame["duration"] = duration_evidence.get(frame["pts"])
        yield frame


def _video_comparison(paths, streams, timings, duration_evidence=None):
    count = Counter()
    mismatches, missing_timestamps = [], 0
    maximum = Fraction(0)
    timing_failure, timing_unknown = False, False
    first, end = [{}, {}], [{}, {}]
    duration_evidence = duration_evidence or ({}, {})
    for index, pair in enumerate(itertools.zip_longest(_video_records(paths[0], timings[0], streams[0], duration_evidence[0]),
                                                       _video_records(paths[1], timings[1], streams[1], duration_evidence[1]))):
        a, b = pair
        for label, frame in zip(("source", "output"), pair):
            if frame is not None:
                count[label] += 1
        if a is None or b is None:
            if len(mismatches) < 10:
                mismatches.append(index)
            continue
        if (a["size"], a["hash"]) != (b["size"], b["hash"]):
            if len(mismatches) < 10:
                mismatches.append(index)
        durations = [x["duration"] for x in pair if x["duration"] is not None and x["duration"] > 0]
        nominal = min(durations) if durations else None
        tolerance, resolved = _rounding_tolerance(*streams, nominal)
        if tolerance is None or not resolved:
            timing_unknown = True
        for column in ("pts", "duration"):
            x, y = a[column], b[column]
            if x is None or y is None or (column == "duration" and (x <= 0 or y <= 0)):
                missing_timestamps += 1
                timing_unknown = True
                continue
            delta = abs(y - x)
            maximum = max(maximum, delta)
            if tolerance is not None and delta > tolerance:
                timing_failure = True
        for side, frame in enumerate(pair):
            if frame["pts"] is not None:
                first[side].setdefault("pts", frame["pts"])
                if frame["duration"] is not None:
                    end[side]["pts"] = frame["pts"] + frame["duration"]
    equality = "passed" if count["source"] and count["source"] == count["output"] and not mismatches else "failed"
    timeline = "failed" if timing_failure else "needs_review" if timing_unknown or count["source"] != count["output"] else "passed"
    return {"content_equality": equality, "timeline_equivalence": timeline,
            "decoded_frame_counts": dict(count), "first_mismatch_indices": mismatches,
            "max_timestamp_or_duration_delta_seconds": float(maximum),
            "missing_or_unusable_timing_values": missing_timestamps,
            "first_pts_seconds": [float(x["pts"]) if x else None for x in first],
            "end_seconds": [float(x["pts"]) if x else None for x in end],
            "timeline_policy": "Ordered native decoded-frame PTS and durations, with hash-bound container evidence only after complete unique-PTS alignment; timing and framehash decoders use one thread; sum-of-native-ticks quantization bound; clocks coarser than half a frame require review; no fitted shift."}


def _audio_segments(path, stream, rate):
    base = _fraction(stream.get("time_base"))
    if base is None or base <= 0:
        raise ValueError("missing native audio time base")
    cursor = 0
    for frame in _native_frames(path):
        samples = int(frame["nb_samples"])
        if samples < 0:
            raise ValueError("negative decoded sample count")
        if samples == 0:
            continue
        pts = _fraction(frame.get("pts"))
        yield {"start": cursor, "end": cursor + samples,
               "pts": pts * base if pts is not None else None,
               "duration": Fraction(samples, rate)}
        cursor += samples


def _audio_comparison(paths, hashes, streams, bytes_per_sample_frame, rate, timings):
    totals, frames, starts, ends = [], [], [], []
    for index, path in enumerate(timings):
        count, samples, first, end = 0, 0, None, None
        for segment in _audio_segments(path, streams[index], rate):
            count += 1
            samples = segment["end"]
            if first is None:
                first = segment["pts"]
            if segment["pts"] is not None:
                end = segment["pts"] + segment["duration"]
        hashed_bytes = sum(frame["size"] for frame in _frames(paths[index]))
        if hashed_bytes % bytes_per_sample_frame or hashed_bytes // bytes_per_sample_frame != samples:
            raise ValueError("canonical PCM and native decoded timing sample counts disagree")
        totals.append(samples)
        frames.append(count)
        starts.append(first)
        ends.append(end)
    raw_hashes = []
    for path in hashes:
        text = path.read_text(encoding="ascii").strip()
        if not re.fullmatch(r"SHA256=[0-9a-fA-F]{64}", text):
            raise ValueError("unrecognized decoded PCM hash")
        raw_hashes.append(text.split("=", 1)[1].lower())
    content = "passed" if totals[0] > 0 and totals[0] == totals[1] and raw_hashes[0] == raw_hashes[1] else "failed"
    generators = [_audio_segments(p, streams[i], rate) for i, p in enumerate(timings)]
    a, b = [next(generator, None) for generator in generators]
    maximum = Fraction(0)
    compared, timing_unknown, timing_failure = 0, False, False
    while a is not None and b is not None:
        position = max(a["start"], b["start"])
        overlap_end = min(a["end"], b["end"])
        if position < overlap_end:
            if a["pts"] is None or b["pts"] is None:
                timing_unknown = True
            else:
                x = a["pts"] + Fraction(position - a["start"], rate)
                y = b["pts"] + Fraction(position - b["start"], rate)
                delta = abs(y - x)
                maximum = max(maximum, delta)
                compared += 1
                tolerance, resolved = _rounding_tolerance(*streams, min(a["duration"], b["duration"]))
                if tolerance is None or not resolved:
                    timing_unknown = True
                if tolerance is not None and delta > tolerance:
                    timing_failure = True
        stop_a, stop_b = a["end"], b["end"]
        if stop_a <= stop_b:
            a = next(generators[0], None)
        if stop_b <= stop_a:
            b = next(generators[1], None)
    timeline = "failed" if timing_failure else "needs_review" if timing_unknown or totals[0] != totals[1] or not compared else "passed"
    return {"content_equality": content, "timeline_equivalence": timeline,
            "decoded_sample_frames": {"source": totals[0], "output": totals[1]},
            "decoded_chunk_counts": {"source": frames[0], "output": frames[1]},
            "pcm_sha256": {"source": raw_hashes[0], "output": raw_hashes[1]},
            "decoded_duration_seconds": [float(Fraction(n, rate)) for n in totals],
            "first_pts_seconds": [float(x) if x is not None else None for x in starts],
            "end_seconds": [float(x) if x is not None else None for x in ends],
            "timeline_segments_compared": compared,
            "max_timestamp_delta_seconds": float(maximum),
            "timeline_policy": "Match cumulative decoded sample positions across chunk boundaries; sum-of-native-ticks quantization bound; clocks coarser than half a chunk require review; no fitted shift."}


def compare_decoded(source, output, kind, out_dir, timeout=1800, max_output_bytes=64 * 1024 * 1024,
                    source_duration_evidence=None, output_duration_evidence=None):
    """Return independent decoded-content/timing results for one mapped track.

    Write diagnostics in a new ``decoded-video`` or ``decoded-audio`` child of
    out_dir. Refuse an existing child. ``max_output_bytes`` budgets diagnostic
    files, not decoded media (PCM is hashed in FFmpeg without retaining it).
    """
    result = {"kind": kind, "status": "not_tested", "content_equality": "not_tested",
              "timeline_equivalence": "not_tested", "errors": [], "warnings": [],
              "scope": "Losslessly decoded local-track comparison; not encoded bitstream or hosted-source identity."}
    if kind not in ("video", "audio") or timeout <= 0 or max_output_bytes <= 0:
        result.update(status="unsupported", errors=["require video/audio, positive timeout and byte budget"])
        return result
    source, output = Path(source).resolve(), Path(output).resolve()
    if not source.is_file() or not output.is_file():
        result.update(status="failed", errors=["missing source or output media"])
        return result
    directory = Path(out_dir).resolve() / ("decoded-" + kind)
    try:
        baseline = [_digest(path) for path in (source, output)]
    except OSError as error:
        result.update(status="failed", errors=[str(error)])
        return result
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        result.update(status="refused", errors=[str(error)])
        return result
    run = _Run(directory, timeout, max_output_bytes)
    try:
        streams = [run.probe(path, kind, label) for path, label in zip((source, output), ("source", "output"))]
        result["stream_metadata"] = streams
        if kind == "video":
            fields = ("width", "height", "pix_fmt")
            if not all(streams[0].get(field) is not None and streams[0].get(field) == streams[1].get(field) for field in fields):
                result.update(status="unsupported", errors=["video dimensions/native pixel formats differ; conversion would change comparison scope"])
                return result
            presentation_fields = ("sample_aspect_ratio", "color_range", "color_space", "color_transfer", "color_primaries", "field_order")
            result["presentation_metadata_differences"] = [
                {"field": field, "source": streams[0].get(field), "output": streams[1].get(field)}
                for field in presentation_fields if streams[0].get(field) != streams[1].get(field)
            ]
            pixel_format = streams[0]["pix_fmt"]
            codec_options = ["-c:v", "rawvideo", "-pix_fmt", "+" + pixel_format, "-fps_mode", "passthrough"]
        else:
            fields = ("sample_rate", "channels", "channel_layout", "sample_fmt")
            if not all(streams[0].get(field) == streams[1].get(field) for field in fields):
                result.update(status="unsupported", errors=["audio native sample formats, rates or layouts differ; resampling/conversion is outside this check"])
                return result
            fmt = streams[0].get("sample_fmt", "")
            fmt = fmt[:-1] if fmt.endswith("p") else fmt
            if fmt not in PCM_FORMATS:
                result.update(status="unsupported", errors=["unsupported lossless native PCM format"])
                return result
            codec, sample_bytes = PCM_FORMATS[fmt]
            rate, channels = int(streams[0]["sample_rate"]), int(streams[0]["channels"])
            if rate <= 0 or channels <= 0:
                raise ValueError("invalid audio sample rate or channel count")
            codec_options = ["-c:a", codec]
            result["canonical_pcm"] = {"codec": codec, "sample_rate": rate, "channels": channels,
                                       "bytes_per_sample_frame": channels * sample_bytes,
                                       "padding_policy": "Use decoder-retained samples after codec pre-skip/discard; retain absolute decoded timestamps; do not independently rebase tracks."}
        paths, hashes, timings = [], [], []
        for index, (path, label) in enumerate(zip((source, output), ("source", "output"))):
            framehash = directory / (label + ".framehash")
            selector = "0:v:0" if kind == "video" else "0:a:0"
            options = list(codec_options)
            timings.append(run.frame_timing(path, label, kind))
            if kind == "video":
                base = _fraction(streams[index].get("time_base"))
                if base is None or base <= 0:
                    raise ValueError("missing native video clock")
                options.extend(["-enc_time_base:v", str(base)])
            command = ["ffmpeg", "-hide_banner", "-v", "warning", "-nostdin", "-n", "-copyts",
                       "-threads", "1", "-i", str(path), "-map", selector, *options,
                       "-f", "framehash", "-hash", "sha256", str(framehash)]
            if kind == "audio":
                pcm_hash = directory / (label + ".pcm.sha256")
                command.extend(["-map", selector, *codec_options, "-f", "hash", "-hash", "sha256", str(pcm_hash)])
                hashes.append(pcm_hash)
            run.execute(command, label + "-decode")
            paths.append(framehash)
        if kind == "video":
            verified = [_verified_frame_durations(evidence, stream, timing, expected_hash)
                        for evidence, stream, timing, expected_hash in
                        zip((source_duration_evidence, output_duration_evidence), streams, timings, baseline)]
            result["container_duration_evidence"] = [item[1] for item in verified]
            result.update(_video_comparison(paths, streams, timings, [item[0] for item in verified]))
        else:
            result.update(_audio_comparison(paths, hashes, streams, channels * sample_bytes, rate, timings))
        if result["content_equality"] == "failed" or result["timeline_equivalence"] == "failed":
            result["status"] = "failed"
        elif result["timeline_equivalence"] != "passed" or result.get("presentation_metadata_differences") or any(
                item.get("status") == "rejected" for item in result.get("container_duration_evidence", [])):
            result["status"] = "needs_review"
        else:
            result["status"] = "passed"
        for record in run.commands:
            stderr = Path(record["stderr"])
            if stderr.stat().st_size:
                result["warnings"].append("diagnostics retained in " + str(stderr))
    except DiagnosticLimit as error:
        result.update(status="needs_review", errors=[str(error)])
    except FileNotFoundError as error:
        result.update(status="unsupported", errors=[str(error)])
    except (OSError, ValueError, KeyError, ProcessFailure, json.JSONDecodeError) as error:
        result.update(status="failed", errors=[str(error)])
    finally:
        result["commands"] = run.commands
        result["artifact_directory"] = str(directory)
        try:
            result["inputs_unchanged"] = all(path.is_file() and _digest(path) == original for path, original in zip((source, output), baseline))
        except OSError:
            result["inputs_unchanged"] = False
        if not result["inputs_unchanged"]:
            result["status"] = "failed"
            result["errors"].append("media input bytes changed during decoded comparison")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--kind", choices=("video", "audio"), required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--max-output-bytes", type=int, default=64 * 1024 * 1024)
    args = parser.parse_args()
    result = compare_decoded(args.source, args.output, args.kind, args.output_dir, args.timeout, args.max_output_bytes)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "passed" else 2 if result["status"] in ("needs_review", "unsupported", "not_tested") else 1


if __name__ == "__main__":
    raise SystemExit(main())
