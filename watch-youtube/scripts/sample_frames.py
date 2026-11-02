#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Extract bounded native-frame samples with timing evidence; never claim review."""
import argparse
from datetime import datetime, timezone
from fractions import Fraction
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from safe_outputs import digest, prepare_run, save_json as save


def stamp(seconds):
    sign = "-" if seconds < 0 else ""
    h, rem = divmod(abs(seconds), 3600)
    m, sec = divmod(rem, 60)
    return f"{sign}{int(h):02}:{int(m):02}:{sec:06.3f}"


def directory_bytes(path):
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def check_output_budget(out, limit):
    if directory_bytes(out) > limit:
        raise ValueError("Output byte budget exceeded; retain this run and request a smaller section or a larger explicit budget")


def stop_process(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def spooled_run(argv, stdout_path, stderr_path, timeout, log_limit, out, output_limit, stop_stdout=None):
    """Spool output, enforce budgets, optionally stop at an observed record boundary."""
    started, pending = time.monotonic(), b""
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
        with stdout_path.open("rb") as progress:
            try:
                while True:
                    code = proc.poll()
                    if stdout_path.stat().st_size + stderr_path.stat().st_size > log_limit:
                        raise ValueError("Process log byte budget exceeded")
                    check_output_budget(out, output_limit)
                    reached = False
                    if stop_stdout is not None:
                        while not reached:
                            chunk = progress.read(65536)
                            if not chunk:
                                break
                            pending += chunk
                            while b"\n" in pending:
                                line, pending = pending.split(b"\n", 1)
                                if stop_stdout(line.decode("utf-8", "replace")):
                                    reached = True
                                    break
                    if reached:
                        interrupted = code is None
                        stop_process(proc)
                        actual = proc.wait()
                        return {"returncode": 0 if interrupted else actual,
                                "process_returncode": actual,
                                "stopped_after_decoded_boundary": interrupted,
                                "decoded_boundary_reached": True}
                    if code is not None:
                        return {"returncode": code, "process_returncode": code,
                                "stopped_after_decoded_boundary": False,
                                "decoded_boundary_reached": False}
                    if time.monotonic() - started > timeout:
                        raise ValueError("Media process timed out")
                    time.sleep(0.1)
            finally:
                stop_process(proc)


def compact_record(line):
    result = {}
    for field in line.rstrip().split("|"):
        if "=" in field:
            key, value = field.split("=", 1)
            result[key] = value
    return result


def integer(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def frame_timings(path, time_base, start, end, max_frames):
    """Keep only the boundary predecessor and requested decoded-frame records."""
    before, relevant, count = None, [], 0
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            fields = compact_record(line)
            pts = integer(fields.get("pts"))
            if pts is None:
                pts = integer(fields.get("best_effort_timestamp"))
            if pts is None:
                continue
            count += 1
            if count > max_frames:
                raise ValueError("Decoded-frame metadata budget exceeded; request a smaller section or raise --max-probe-frames")
            duration = integer(fields.get("duration"))
            if duration is None or duration <= 0:
                duration = integer(fields.get("pkt_duration"))
            frame = {"pts": pts, "time": pts * time_base,
                     "duration": duration * time_base if duration and duration > 0 else None}
            # Decoded frames arrive in presentation order. The first successor
            # establishes that delayed frames before the requested end were flushed.
            if frame["time"] >= end:
                break
            if frame["time"] < start:
                if before is None or frame["time"] > before["time"]:
                    before = frame
            elif frame["time"] < end:
                relevant.append(frame)
    frames = ([before] if before is not None else []) + relevant
    frames.sort(key=lambda frame: frame["time"])
    for index, frame in enumerate(frames):
        if frame["duration"] is not None:
            frame["display_end"] = frame["time"] + frame["duration"]
            frame["duration_basis"] = "native_decoded_frame_duration"
        elif index + 1 < len(frames):
            frame["display_end"] = frames[index + 1]["time"]
            frame["duration_basis"] = "successor_pts_inferred_requires_review"
        else:
            frame["display_end"] = None
            frame["duration_basis"] = "unknown"
    return frames, count


def media_coverage(frames, start, end):
    """Describe evidenced frame display intervals, independently of sampling."""
    intervals, uncertain = [], []
    for frame in frames:
        a, b = frame["time"], frame["display_end"]
        if frame["duration_basis"] != "native_decoded_frame_duration":
            if b is None or (a < end and b > start):
                uncertain.append(float(a))
            continue
        if a < end and b > start:
            intervals.append([max(a, start), min(b, end)])
    merged = []
    for a, b in sorted(intervals):
        if not merged or a > merged[-1][1]:
            merged.append([a, b])
        else:
            merged[-1][1] = max(merged[-1][1], b)
    gaps, cursor = [], start
    for a, b in merged:
        if a > cursor:
            gaps.append([float(cursor), float(a)])
        cursor = max(cursor, b)
    if cursor < end:
        gaps.append([float(cursor), float(end)])
    status = "covered_by_native_frame_durations" if not gaps else "needs_review" if uncertain else "partial"
    return {"status": status, "requested_start": float(start), "requested_end": float(end),
            "native_display_intervals": [[float(a), float(b)] for a, b in merged],
            "uncovered_or_unestablished_intervals": gaps, "uncertain_frame_pts": uncertain,
            "source_completeness": "not_established_by_decoded_interval_checks",
            "note": "These are captured media display intervals, not sampled-image inspection or proof of hosted-source completeness."}


def sheets(frames, out, output_limit):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return {"created": False, "reason": "Pillow unavailable; extracted frames and PTS manifest retained"}
    thumbs = out / "thumbnails"
    thumbs.mkdir()
    font = ImageFont.load_default()
    result = []
    for start in range(0, len(frames), 20):
        subset = frames[start:start + 20]
        sheet = Image.new("RGB", (1600, 224 * math.ceil(len(subset) / 5)), "#151515")
        draw = ImageDraw.Draw(sheet)
        for offset, frame in enumerate(subset):
            with Image.open(frame["path"]) as original:
                image = original.convert("RGB")
            image.thumbnail((308, 190))
            thumb = thumbs / Path(frame["path"]).name
            image.save(thumb, quality=85)
            frame["thumbnail"] = str(thumb)
            x, y = (offset % 5) * 320, (offset // 5) * 224
            sheet.paste(image, (x + (320 - image.width) // 2, y + 4))
            draw.text((x + 7, y + 197), stamp(frame["source_time_seconds"]), fill="white", font=font)
            check_output_budget(out, output_limit)
        path = out / f"contact_{start // 20:04}.jpg"
        sheet.save(path, quality=90)
        check_output_budget(out, output_limit)
        result.append({"path": str(path), "sha256": digest(path), "frame_count": len(subset),
                       "first_source_time": subset[0]["source_time_seconds"],
                       "last_source_time": subset[-1]["source_time_seconds"], "reviewed": False})
    return {"created": True, "sheets": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--start", required=True, type=float)
    parser.add_argument("--end", required=True, type=float)
    parser.add_argument("--fps", type=float, default=2.0, help="Approximate maximum native-frame sample density; boundary context is additional.")
    parser.add_argument("--max-width", type=int, default=960, help="Maximum image width; 0 retains native source resolution.")
    parser.add_argument("--timeout", type=int, default=1800, help="Per media-process timeout in seconds.")
    parser.add_argument("--max-frames", type=int, default=2000, help="Maximum extracted images, including boundary context.")
    parser.add_argument("--max-probe-frames", type=int, default=100000, help="Maximum decoded-frame metadata records for one section.")
    parser.add_argument("--max-log-bytes", type=int, default=64 * 1024 * 1024, help="Maximum combined stdout/stderr bytes per process.")
    parser.add_argument("--max-output-bytes", type=int, default=512 * 1024 * 1024, help="Output directory byte budget, checked between writes and while processes run.")
    args = parser.parse_args()
    try:
        if not all(math.isfinite(x) for x in (args.start, args.end, args.fps)):
            raise ValueError("Start/end/fps must be finite")
        if args.start < 0 or args.end <= args.start or args.fps <= 0 or args.max_width < 0:
            raise ValueError("Require 0 <= start < end, fps > 0, max-width >= 0")
        if min(args.timeout, args.max_frames, args.max_probe_frames, args.max_log_bytes, args.max_output_bytes) <= 0:
            raise ValueError("Timeout and resource budgets must be positive")
        if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
            raise ValueError("FFmpeg and ffprobe must be available on PATH")
        out, snapshots = prepare_run(args.output_dir, {"input": args.input})
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "refused", "errors": [str(error)], "output_directory_modified": False}), file=sys.stderr)
        return 1
    source = Path(snapshots["input"]["path"])
    manifest, checkpoint = out / "frames_manifest.json", out / "checkpoint.json"
    report = {
        "schema_version": 3, "created_utc": datetime.now(timezone.utc).isoformat(),
        "status": "running", "source": str(source),
        "requested": {"start": args.start, "end": args.end, "fps": args.fps},
        "reviewed": False, "visual_review": {"performed": False, "reviewer": None, "observations": None},
        "extraction": {"performed": False}, "frames": [], "errors": [], "warnings": [],
        "source_sha256": snapshots["input"]["sha256"], "source_bytes": snapshots["input"]["bytes"],
        "sampling_coverage": {"status": "not_established", "continuous_coverage": False},
        "media_coverage": {"status": "not_established"},
        "budgets": {"max_frames": args.max_frames, "max_probe_frames": args.max_probe_frames,
                    "max_log_bytes": args.max_log_bytes, "max_output_bytes": args.max_output_bytes},
        "method": "Native decoded frames and optional preceding boundary context; original PTS, passthrough output, no synthetic frames or implied visual review.",
    }

    def check(stage):
        save(manifest, report)
        save(checkpoint, {"stage": stage, "status": report["status"], "manifest": str(manifest), "reviewed": False})

    def run(argv, name, stop_stdout=None):
        stdout, stderr = out / (name + ".stdout.log"), out / (name + ".stderr.log")
        result = spooled_run(argv, stdout, stderr, args.timeout, args.max_log_bytes, out, args.max_output_bytes, stop_stdout)
        return result, stdout, stderr

    try:
        start, end, step = Fraction(str(args.start)), Fraction(str(args.end)), Fraction(1) / Fraction(str(args.fps))
        frames_dir = out / "frames"
        frames_dir.mkdir()
        metadata_command = ["ffprobe", "-v", "error", "-show_entries",
                            "format=start_time,duration:stream=index,codec_type,start_time,time_base,width,height,avg_frame_rate",
                            "-of", "json", str(source)]
        process, meta_stdout, meta_stderr = run(metadata_command, "source_probe")
        if process["returncode"] != 0:
            raise ValueError("Source timestamp metadata probe failed; see source_probe.stderr.log")
        source_info = json.loads(meta_stdout.read_text(encoding="utf-8"))
        save(out / "source_probe.json", source_info)
        video_info = next((s for s in source_info.get("streams", []) if s.get("codec_type") == "video"), None)
        if video_info is None:
            raise ValueError("Input contains no video stream")
        source_tb = Fraction(video_info["time_base"])
        if source_tb <= 0:
            raise ValueError("Video time base must be positive")
        origin_text = source_info.get("format", {}).get("start_time")
        origin_assumed = origin_text is None or origin_text == "N/A"
        origin = Fraction(0) if origin_assumed else Fraction(origin_text)
        if origin >= end:
            raise ValueError("Requested interval precedes source start time")
        # Seeking to the preceding random-access point retains old held frames;
        # a fixed two-second preroll would miss a hold beginning farther back.
        timing_command = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals",
                          f"{args.start:.12g}%", "-show_frames", "-show_entries",
                          "frame=pts,best_effort_timestamp,duration,pkt_duration", "-of", "compact=p=0", str(source)]
        def reached_presentation_end(line):
            fields = compact_record(line)
            pts = integer(fields.get("pts"))
            if pts is None:
                pts = integer(fields.get("best_effort_timestamp"))
            return pts is not None and pts * source_tb >= end

        process, timing_stdout, timing_stderr = run(timing_command, "frame_timing", reached_presentation_end)
        report["extraction"]["timing_probe"] = {"command": timing_command, **process,
            "stdout_log": str(timing_stdout), "stderr_log": str(timing_stderr),
            "end_policy": "Observe decoded presentation PTS reaching the requested end, or true EOF; do not cut packet input before reordered frames are flushed."}
        if process["returncode"] != 0:
            raise ValueError("Native decoded-frame timing probe failed; see frame_timing.stderr.log")
        timings, decoded_count = frame_timings(timing_stdout, source_tb, start, end, args.max_probe_frames)
        report["media_coverage"] = media_coverage(timings, start, end)
        predecessor = next((f for f in reversed(timings) if f["time"] < start and (f["display_end"] is None or f["display_end"] > start)), None)
        candidates = [f for f in timings if start <= f["time"] < end]
        planned_times, previous = ([predecessor["time"]] if predecessor else []), None
        for frame in candidates:
            if previous is None or frame["time"] - previous >= step:
                planned_times.append(frame["time"])
                previous = frame["time"]
        if len(planned_times) > args.max_frames:
            raise ValueError("Image count budget would be exceeded; request a smaller section/lower density or raise --max-frames")
        if not timings:
            report["sampling_coverage"] = {"status": "no_frames", "requested_start": args.start, "requested_end": args.end, "continuous_coverage": False}
            raise ValueError("No native decoded frames were found for the requested section")
        seek = predecessor["time"] if predecessor else max(origin, start)
        relative_seek = max(Fraction(0), seek - origin)
        duration_limit = end - seek
        # Context is separately selected and does not consume the requested-interval
        # sampling cadence. Selection works in source seconds; exact PTS is retained.
        spacing = f"if(isnan(prev_selected_t),1,if(lt(prev_selected_t,{args.start:.12g}),1,gte(t-prev_selected_t,{float(step):.12g}-0.000000001)))"
        predicate = f"gte(t,{args.start:.12g})*lt(t,{args.end:.12g})*({spacing})"
        if predecessor:
            boundary = float(predecessor["time"])
            predicate = f"lt(abs(t-{boundary:.12g}),0.000000001)+({predicate})"
        scaling = f"scale=w='min({args.max_width},iw)':h=-2," if args.max_width else ""
        expression = "showinfo@decode,select='" + predicate + "'," + scaling + "showinfo@sample"
        argv = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "info", "-n", "-copyts",
                "-ss", f"{float(relative_seek):.12g}", "-t", f"{float(duration_limit):.12g}",
                "-i", str(source), "-map", "0:v:0", "-an", "-vf", expression,
                "-fps_mode", "passthrough", "-frames:v", str(args.max_frames), "-q:v", "3",
                "-start_number", "0", str(frames_dir / "frame_%06d.jpg")]
        report["extraction"].update({"command": argv,
            "scaling": {"max_width": args.max_width, "native_source_resolution": args.max_width == 0,
                        "filter": scaling.rstrip(",") or None, "contact_sheets_are_separate_thumbnails": True},
            "bounded_decode": {"native_seek_target": float(seek), "input_relative_seek_seconds": float(relative_seek),
                "container_start_time": float(origin), "container_start_assumed_zero": origin_assumed,
                "input_duration_limit_seconds": float(duration_limit), "preroll_seconds": float(start - seek),
                "timing_probe_frame_count": decoded_count}})
        check("extracting")
        process, _, stderr_log = run(argv, "extraction")
        report["extraction"].update({"returncode": process["returncode"], "performed": True, "stderr_log": str(stderr_log)})
        tb, records, count, first_decoded, last_decoded = None, [], 0, None, None
        with stderr_log.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if "showinfo@sample" in line:
                    config = re.search(r"config in time_base:\s*([0-9]+/[0-9]+)", line)
                    if config:
                        value = Fraction(config.group(1))
                        if tb is not None and tb != value:
                            raise ValueError("Time base changed during extraction; split into separate runs")
                        tb = value
                    match = re.search(r"\bn:\s*(\d+)\s+pts:\s*(-?\d+)\s+pts_time:\s*([^\s]+)", line)
                    if match:
                        dimensions = re.search(r"\bs:(\d+)x(\d+)", line)
                        records.append((int(match.group(1)), int(match.group(2)), match.group(3),
                                        int(dimensions.group(1)) if dimensions else None, int(dimensions.group(2)) if dimensions else None))
                elif "showinfo@decode" in line:
                    match = re.search(r"\bpts_time:\s*([^\s]+)", line)
                    if match:
                        value = float(match.group(1)); count += 1
                        first_decoded = value if first_decoded is None else min(first_decoded, value)
                        last_decoded = value if last_decoded is None else max(last_decoded, value)
        report["extraction"]["bounded_decode"].update({"observed_filter_frame_count": count,
            "first_decoded_pts_time": first_decoded, "last_decoded_pts_time": last_decoded})
        if process["returncode"] != 0:
            raise ValueError("FFmpeg extraction failed; see retained stderr")
        images = sorted(frames_dir.glob("frame_*.jpg"))
        if not images:
            report["sampling_coverage"] = {"status": "no_frames", "requested_start": args.start, "requested_end": args.end, "continuous_coverage": False}
            raise ValueError("No frames exist in the requested interval or its boundary context")
        if tb is None or len(images) != len(records):
            raise ValueError("Extracted image count does not match actual showinfo PTS evidence")
        if [record[1] * tb for record in records] != planned_times:
            raise ValueError("Extracted native PTS do not match the planned bounded selection; inspect logs instead of treating a shortened run as complete")
        timing_map = {f["time"]: f for f in timings}
        for image, (ordinal, pts, printed, width, height) in zip(images, records):
            exact_time = pts * tb
            native = timing_map.get(exact_time)
            if native is None:
                raise ValueError("Extracted PTS has no matching decoded-frame timing record")
            context = exact_time < start
            display_end = native["display_end"]
            report["frames"].append({"path": str(image), "sha256": digest(image), "selected_frame_index": ordinal,
                "source_pts": pts, "source_time_base": str(tb), "source_time_seconds": float(exact_time),
                "source_time_exact": str(exact_time), "showinfo_pts_time": printed,
                "timestamp_label": stamp(float(exact_time)), "reviewed": False, "width": width, "height": height,
                "role": "boundary_context" if context else "interval_sample",
                "display_interval": {"start": float(exact_time), "end": float(display_end) if display_end is not None else None,
                    "duration_basis": native["duration_basis"],
                    "overlaps_requested_interval": (exact_time < end and display_end > start)
                        if display_end is not None and native["duration_basis"] == "native_decoded_frame_duration" else None}})
        interval_samples = [f for f in report["frames"] if f["role"] == "interval_sample"]
        selected_times = [f["source_time_seconds"] for f in interval_samples]
        selected_steps = [b - a for a, b in zip(selected_times, selected_times[1:]) if b > a]
        first = selected_times[0] if selected_times else None
        last = selected_times[-1] if selected_times else None
        edge_allowance = float(step)
        partial = first is None or first > args.start + edge_allowance or last + edge_allowance < args.end
        report["sampling_coverage"] = {"status": "partial" if partial else "sample_positions_span_requested_interval",
            "requested_start": args.start, "requested_end": args.end, "first_sample_pts": first, "last_sample_pts": last,
            "sample_count": len(interval_samples), "boundary_context_count": len(report["frames"]) - len(interval_samples),
            "requested_spacing_seconds": float(step), "edge_allowance_seconds": edge_allowance,
            "largest_observed_sample_spacing": max(selected_steps, default=None), "continuous_coverage": False,
            "source_completeness": "not_established_by_sampling",
            "note": "Sample positions and boundary context are separate from media display coverage and actual visual inspection."}
        if partial:
            report["warnings"].append("Sample positions alone do not span the requested interval; inspect media_coverage and boundary-context evidence separately.")
        if report["media_coverage"]["status"] != "covered_by_native_frame_durations":
            report["warnings"].append("Native frame durations do not establish the whole requested media interval; see media_coverage.")
        report["extraction"].update({"frame_count": len(images), "first_actual_pts": report["frames"][0]["source_time_seconds"],
            "last_actual_pts": report["frames"][-1]["source_time_seconds"], "source_time_base": str(tb),
            "selection": "Retain preceding boundary context when available, then greedily sample native frames inside the interval. No interpolated frames."})
        report["contact_sheets"] = sheets(report["frames"], out, args.max_output_bytes)
        check_output_budget(out, args.max_output_bytes)
        report["status"] = "extracted_unreviewed"
    except (OSError, ValueError, ZeroDivisionError, KeyError, subprocess.SubprocessError) as error:
        report["status"] = "failed"
        report["errors"].append(str(error))
    finally:
        try:
            report["input_bytes_unchanged"] = source.is_file() and digest(source) == snapshots["input"]["sha256"]
        except OSError:
            report["input_bytes_unchanged"] = False
        if not report["input_bytes_unchanged"]:
            report["status"] = "failed"
            report["errors"].append("Input bytes changed during extraction")
        try:
            check("finished")
        except OSError as error:
            print(json.dumps({"status": "failed", "errors": ["Could not save this run's checkpoint: " + str(error)]}), file=sys.stderr)
            return 1
    print(json.dumps({"status": report["status"], "manifest": str(manifest), "frame_count": len(report["frames"]),
                      "reviewed": False, "sampling_coverage": report["sampling_coverage"]["status"],
                      "media_coverage": report["media_coverage"]["status"], "warnings": report["warnings"], "errors": report["errors"]}, indent=2))
    return 1 if report["status"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
