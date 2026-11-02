#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run portable local validation and bounded frame extraction after acquisition.

Browser/raw-file acquisition belongs to the host's tools. This CLI never assumes
a provider session, downloads media, or equates extraction with visual review.
"""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys

from safe_outputs import digest, prepare_run, save_json


def now():
    return datetime.now(timezone.utc).isoformat()


def doctor():
    required = {name: shutil.which(name) for name in ("ffmpeg", "ffprobe")}
    features = {"fps_mode": "not_tested"}
    if required["ffmpeg"]:
        try:
            result = subprocess.run([required["ffmpeg"], "-hide_banner", "-h", "full"],
                                    capture_output=True, text=True, timeout=10)
            features["fps_mode"] = "supported" if result.returncode == 0 and "-fps_mode" in result.stdout + result.stderr else "unsupported"
        except (OSError, subprocess.TimeoutExpired):
            features["fps_mode"] = "unavailable"
    status = "unavailable" if not all(required.values()) else "passed" if features["fps_mode"] == "supported" else "unsupported"
    return {"status": status,
            "python": sys.version.split()[0], "required_tools": required,
            "required_features": features,
            "optional": {"pillow": importlib.util.find_spec("PIL") is not None,
                         "node_for_javascript_tests": shutil.which("node")},
            "host_capabilities": {"browser_page_evaluation": "discover_in_host",
                                  "raw_file_transfer": "discover_in_host",
                                  "image_inspection": "discover_in_host",
                                  "audio_audition": "discover_in_host"}}


def section_plan(start, end, seconds, fps, max_sections):
    if not all(math.isfinite(x) for x in (start, end, seconds, fps)):
        raise ValueError("Section times and sampling density must be finite")
    if start < 0 or end <= start or seconds <= 0 or fps <= 0 or max_sections <= 0:
        raise ValueError("Require 0 <= start < end and positive section/fps/budget values")
    count = math.ceil((end - start) / seconds)
    if count > max_sections:
        raise ValueError("Section count exceeds --max-sections; narrow the interval or increase the explicit budget")
    return [{"index": index, "start": start + index * seconds,
             "end": min(end, start + (index + 1) * seconds), "fps": fps}
            for index in range(count)]


def load_object(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object: " + str(path))
    return value


def verify_acquisition(record, snapshots, source_url):
    """Verify declared export and receipt hashes against local captured bytes.

    Schema 1 uses source:{url}, exports:{files:[...]}, tracks:[{role,id,
    receipt:{bytes,sha256}}]. The exports object is the unmodified browser helper
    result. Direct-file routes may instead supply export:{bytes,sha256} per track.
    Metadata proves declared transfer consistency,
    not identity with every byte of a hosted original.
    """
    if record.get("schema_version") != 1:
        raise ValueError("Acquisition record requires schema_version 1")
    source = record.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("url"), str) or not source["url"].strip():
        raise ValueError("Acquisition record requires source.url")
    if source_url and source["url"] != source_url:
        raise ValueError("Acquisition source URL differs from --source-url")
    tracks = record.get("tracks")
    if not isinstance(tracks, list):
        raise ValueError("Acquisition record requires tracks array")
    by_role = {}
    exports = record.get("exports")
    files = exports.get("files", []) if isinstance(exports, dict) else []
    if not isinstance(files, list):
        raise ValueError("Acquisition exports.files must be an array")
    by_id = {}
    for exported in files:
        if not isinstance(exported, dict) or not isinstance(exported.get("id"), str) or exported["id"] in by_id:
            raise ValueError("Export records require unique string IDs")
        by_id[exported["id"]] = exported
    for track in tracks:
        if not isinstance(track, dict) or track.get("role") not in ("video", "audio"):
            raise ValueError("Acquisition track role must be video or audio")
        role = track["role"]
        if role in by_role:
            raise ValueError("Duplicate acquisition track role: " + role)
        by_role[role] = track
    checked = []
    requested_coverage = []
    capture_issues = []
    for label, snapshot in snapshots.items():
        if label not in ("input", "audio"):
            continue
        role = "video" if label == "input" else "audio"
        track = by_role.get(role)
        if track is None:
            raise ValueError("Acquisition record lacks supplied " + role + " track")
        for kind in ("export", "receipt"):
            declaration = by_id.get(track.get("id")) if kind == "export" and "id" in track else track.get(kind)
            if not isinstance(declaration, dict):
                raise ValueError(role + " requires " + kind + " bytes and sha256")
            size = declaration.get("bytes")
            sha = declaration.get("sha256")
            if type(size) is not int or size != snapshot["bytes"]:
                raise ValueError(role + " " + kind + " size does not match local file")
            if not isinstance(sha, str) or sha.lower() != snapshot["sha256"]:
                raise ValueError(role + " " + kind + " SHA-256 does not match local file")
            if kind == "export" and (declaration.get("stale") is True or declaration.get("diagnostic") is True or declaration.get("eligibleForValidation") is False):
                capture_issues.append(role + ": export is stale or marked diagnostic/ineligible")
            if kind == "export" and "requestedCoverage" in declaration:
                coverage = declaration["requestedCoverage"]
                status = coverage.get("status") if isinstance(coverage, dict) else None
                requested_coverage.append({"role": role, "browser_observation": coverage})
                if status not in ("covered", "not_requested"):
                    capture_issues.append(role + ": requested browser capture coverage is " + str(status or "unestablished"))
        checked.append(role)
    return {"status": "passed", "verified_roles": checked, "source": source,
            "unverified_roles": sorted(set(by_role) - set(checked)),
            "capture_evidence_status": "needs_review" if capture_issues else "not_established_by_transfer",
            "capture_issues": capture_issues,
            "requested_coverage": requested_coverage,
            "requested_coverage_scope": "Host-declared SourceBuffer observations only; local bytes and native requested-interval validation remain independent",
            "scope": "Export/receipt/local-byte agreement; hosted-original completeness is not established"}


def execute(argv, directory, name):
    """Spool child output; each bundled helper applies its own timeout budget."""
    stdout_path, stderr_path = directory / (name + ".stdout.log"), directory / (name + ".stderr.log")
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
    return {"command": argv, "returncode": result.returncode,
            "stdout": str(stdout_path), "stderr": str(stderr_path)}


def run(args):
    sections = section_plan(args.start, args.end, args.section_seconds, args.fps, args.max_sections)
    budgets = (args.timeout, args.max_probe_bytes, args.max_frames, args.max_probe_frames,
               args.max_log_bytes, args.max_output_bytes)
    if any(value <= 0 for value in budgets) or args.max_width < 0:
        raise ValueError("Require positive timeout/probe budget and nonnegative image width")
    if args.expected_duration is not None and (not math.isfinite(args.expected_duration) or args.expected_duration <= 0):
        raise ValueError("Expected presentation duration must be finite and positive")
    capabilities = doctor()
    if capabilities["status"] != "passed":
        raise ValueError("Required FFmpeg/ffprobe capabilities unavailable; run workflow.py doctor")
    supplied = {"input": args.input, "audio": args.audio,
                "acquisition_record": args.acquisition_record,
                "track_expectations": args.track_expectations}
    # Reject malformed JSON declarations before claiming any output directory.
    acquisition = load_object(args.acquisition_record) if args.acquisition_record else None
    if args.track_expectations:
        load_object(args.track_expectations)
    output, snapshots = prepare_run(args.output_dir, supplied)
    report = {"schema_version": 1, "created_utc": now(), "status": "running",
              "source": {"url": args.source_url, "identity_verification": "host_declared" if args.source_url else "not_supplied"},
              "inputs": snapshots, "capabilities": capabilities, "requested_sections": sections,
              "stage_results": [], "visual_review": {"performed": False},
              "audio_audition": {"status": "not_performed"}, "errors": []}
    def checkpoint(stage):
        save_json(output / "workflow.json", report)
        save_json(output / "checkpoint.json", {"stage": stage, "status": report["status"],
                  "workflow": str(output / "workflow.json"), "updated_utc": now()})
    checkpoint("preflight")
    try:
        transfer = verify_acquisition(acquisition, snapshots, args.source_url) if acquisition else {
            "status": "not_tested", "reason": "No acquisition record supplied; local input is available"}
        report["stage_results"].append({"stage": "transfer_verification", **transfer})
        if acquisition:
            report["source"] = {**acquisition["source"], "identity_verification": "host_declared"}
        checkpoint("transfer_verification")
        scripts = Path(__file__).resolve().parent
        command = [sys.executable, str(scripts / "validate_media.py"), "--input", snapshots["input"]["path"],
                   "--output-dir", str(output / "validation"), "--timeout", str(args.timeout),
                   "--max-probe-bytes", str(args.max_probe_bytes)]
        if args.audio:
            command += ["--audio", snapshots["audio"]["path"]]
        if args.expected_duration is not None:
            command += ["--expected-duration", str(args.expected_duration)]
        if args.track_expectations:
            command += ["--track-expectations", snapshots["track_expectations"]["path"]]
        if args.validate_interval:
            command += ["--expected-start", str(args.start), "--expected-end", str(args.end)]
        if args.decoded_equivalence:
            command.append("--decoded-equivalence")
        if args.analysis_copy:
            command.append("--analysis-copy")
        result = execute(command, output, "validation")
        validation_path = output / "validation" / "validation.json"
        validation = load_object(validation_path) if validation_path.is_file() else None
        state = "passed" if result["returncode"] == 0 else "needs_review" if result["returncode"] == 2 else "failed"
        if validation is None:
            state = "failed"
        report["stage_results"].append({"stage": "validation", "status": state,
                "execution": result, "report": str(validation_path) if validation else None,
                "validation_status": validation.get("status") if validation else "not_reported",
                "completeness": validation.get("completeness") if validation else "unknown"})
        if validation:
            report["analysis_readiness"] = validation.get("analysis_readiness", {"status": "not_reported"})
            report["analysis_copy"] = validation.get("analysis_copy", {"status": "not_requested"})
        checkpoint("validation")
        if state == "failed":
            raise ValueError("Validation failed; inspect the preserved validation report and logs")
        section_review = False
        for section in sections:
            destination = output / ("section_%04d" % section["index"])
            command = [sys.executable, str(scripts / "sample_frames.py"), "--input", snapshots["input"]["path"],
                       "--output-dir", str(destination), "--start", str(section["start"]),
                       "--end", str(section["end"]), "--fps", str(section["fps"]),
                       "--max-width", str(args.max_width), "--timeout", str(args.timeout),
                       "--max-frames", str(args.max_frames), "--max-probe-frames", str(args.max_probe_frames),
                       "--max-log-bytes", str(args.max_log_bytes), "--max-output-bytes", str(args.max_output_bytes)]
            result = execute(command, output, destination.name)
            manifest_path = destination / "frames_manifest.json"
            manifest = load_object(manifest_path) if manifest_path.is_file() else None
            extracted = result["returncode"] == 0 and manifest is not None and bool(manifest.get("frames"))
            coverage = manifest.get("media_coverage", {}) if manifest else {}
            coverage_review = coverage.get("status") in ("partial", "needs_review", "not_established", "unknown", "failed")
            section_review = section_review or coverage_review
            report["stage_results"].append({"stage": "frame_extraction", "section": section,
                  "status": "passed" if extracted else "failed", "execution": result,
                  "manifest": str(manifest_path), "reviewed": False,
                  "sampling_coverage": manifest.get("sampling_coverage") if manifest else None,
                  "media_coverage": coverage, "coverage_status": "needs_review" if coverage_review else "reported"})
            checkpoint(destination.name)
            if not extracted:
                raise ValueError("Section extraction failed; earlier sections and review evidence are preserved")
        report["status"] = "needs_review" if state == "needs_review" or section_review or transfer.get("capture_evidence_status") == "needs_review" else "completed_unreviewed"
    except (OSError, ValueError) as error:
        report["status"] = "failed"
        report["errors"].append(str(error))
    finally:
        for snapshot in snapshots.values():
            try:
                snapshot["unchanged"] = digest(snapshot["path"]) == snapshot["sha256"]
            except OSError:
                snapshot["unchanged"] = False
            if not snapshot["unchanged"]:
                report["status"] = "failed"
                report["errors"].append("Input changed during workflow: " + snapshot["path"])
        checkpoint("finished")
    print(json.dumps({"status": report["status"], "workflow": str(output / "workflow.json"),
                      "checkpoint": str(output / "checkpoint.json"), "errors": report["errors"]}, indent=2))
    return 1 if report["status"] == "failed" else 2 if report["status"] == "needs_review" else 0


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("doctor", help="Inspect local dependencies; host tools require separate discovery")
    for action in ("plan", "run"):
        q = sub.add_parser(action)
        q.add_argument("--start", type=float, default=0)
        q.add_argument("--end", type=float, required=True)
        q.add_argument("--section-seconds", type=float, default=30)
        q.add_argument("--fps", type=float, default=2)
        q.add_argument("--max-sections", type=int, default=100)
        if action == "run":
            q.add_argument("--input", type=Path, required=True)
            q.add_argument("--audio", type=Path)
            q.add_argument("--output-dir", type=Path, required=True)
            q.add_argument("--source-url")
            q.add_argument("--acquisition-record", type=Path)
            q.add_argument("--track-expectations", type=Path)
            q.add_argument("--expected-duration", type=float, help="Overall presentation duration; not each track's endpoint")
            q.add_argument("--validate-interval", action="store_true", help="Explicitly require requested review interval coverage")
            q.add_argument("--decoded-equivalence", action="store_true")
            q.add_argument("--analysis-copy", action="store_true", help="Verify a new clean Opus audio analysis derivative; raw findings remain separate")
            q.add_argument("--max-probe-bytes", type=int, default=64 * 1024 * 1024)
            q.add_argument("--max-width", type=int, default=960)
            q.add_argument("--max-frames", type=int, default=2000, help="Per-section image count budget")
            q.add_argument("--max-probe-frames", type=int, default=100000, help="Per-section decoded metadata count budget")
            q.add_argument("--max-log-bytes", type=int, default=64 * 1024 * 1024, help="Per-section process-log budget")
            q.add_argument("--max-output-bytes", type=int, default=512 * 1024 * 1024, help="Per-section extraction directory budget")
            q.add_argument("--timeout", type=int, default=1800, help="Per child-helper operation timeout, seconds")
    return p


def main():
    args = parser().parse_args()
    try:
        if args.action == "doctor":
            result = doctor()
            print(json.dumps(result, indent=2))
            return 0 if result["status"] == "passed" else 1
        if args.action == "plan":
            print(json.dumps({"sections": section_plan(args.start, args.end, args.section_seconds, args.fps, args.max_sections),
                              "visual_review": "not_performed"}, indent=2))
            return 0
        return run(args)
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "refused", "errors": [str(error)]}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
