#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Append explicit review attestations without modifying extraction manifests.

Frame indices are zero-based positions in the supplied manifest's frames array.
Recording is an agent/human attestation; this CLI does not inspect images or hear
audio. It verifies the source, manifest and selected image hashes before saving.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import sys

from safe_outputs import digest, save_json


def stamp():
    return datetime.now(timezone.utc).isoformat()


def object_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def read_object(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Expected JSON object: " + str(path))
    return value


def checked_manifest(path, source_override=None):
    path = Path(path).expanduser().resolve(strict=True)
    manifest = read_object(path)
    source = Path(source_override or manifest.get("source", "")).expanduser().resolve(strict=True)
    if not source.is_file() or digest(source) != manifest.get("source_sha256"):
        raise ValueError("Manifest source hash does not match the actual source file")
    frames = manifest.get("frames")
    if not isinstance(frames, list):
        raise ValueError("Manifest requires frames array")
    return path, manifest, source


@contextmanager
def locked(path):
    """An exclusive sidecar prevents lost updates; stale locks need explicit review."""
    lock = path.with_name(path.name + ".lock")
    descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()) + "\n")
        yield
    finally:
        lock.unlink()


def ledger_target(path, manifest_path, manifest, source):
    requested = Path(path).expanduser().absolute()
    if requested.is_symlink():
        raise ValueError("Ledger path must not be a symbolic link")
    target = requested.resolve()
    protected = {manifest_path, source}
    protected.update(Path(frame["path"]).expanduser().resolve() for frame in manifest["frames"]
                     if isinstance(frame, dict) and isinstance(frame.get("path"), str))
    if target in protected:
        raise ValueError("Ledger must be separate from source, manifest and extracted images")
    if target.exists() and any(target.samefile(item) for item in protected if item.exists()):
        raise ValueError("Ledger aliases protected evidence")
    if not target.parent.is_dir():
        raise ValueError("Ledger parent directory must exist")
    return target


def validate_ledger(ledger):
    if ledger.get("schema_version") != 1 or not isinstance(ledger.get("entries"), list):
        raise ValueError("Unsupported or malformed review ledger")
    previous = None
    for index, entry in enumerate(ledger["entries"]):
        if not isinstance(entry, dict):
            raise ValueError("Malformed ledger entry")
        payload = {key: value for key, value in entry.items() if key != "sha256"}
        if entry.get("index") != index or entry.get("previous_sha256") != previous or entry.get("sha256") != object_hash(payload):
            raise ValueError("Review ledger history failed integrity validation")
        previous = entry["sha256"]


def init(args):
    path, manifest, source = checked_manifest(args.manifest, args.source)
    target = ledger_target(args.ledger, path, manifest, source)
    with locked(target):
        if target.exists():
            raise ValueError("Ledger already exists; use record to append an attestation")
        ledger = {"schema_version": 1, "created_utc": stamp(),
                  "source_sha256": manifest["source_sha256"], "source_bytes": source.stat().st_size,
                  "entries": [], "scope": "Explicit attestations; hash integrity does not prove perceptual inspection"}
        save_json(target, ledger)
    return {"status": "created_unreviewed", "ledger": str(target), "entries": 0}


def selected_frames(manifest, indices):
    if len(indices) != len(set(indices)):
        raise ValueError("Frame indices must be unique within an entry")
    selected = []
    for index in indices:
        if index < 0 or index >= len(manifest["frames"]):
            raise ValueError("Frame index outside supplied manifest")
        frame = manifest["frames"][index]
        if not isinstance(frame, dict) or type(frame.get("source_pts")) is not int:
            raise ValueError("Frame requires integer source_pts")
        time_base = Fraction(frame["source_time_base"])
        if time_base <= 0:
            raise ValueError("Frame time base must be positive")
        exact = frame["source_pts"] * time_base
        observed = frame.get("source_time_seconds")
        expected = float(exact)
        if type(observed) not in (int, float) or not math.isfinite(observed) or abs(expected - observed) > 2 * max(math.ulp(expected), math.ulp(observed)):
            raise ValueError("Frame PTS disagrees with recorded timestamp")
        # The sampler emits source_time_exact; older callers may supply the
        # ledger's source_time_seconds_exact spelling. Check every supplied
        # value so a valid alias cannot conceal contradictory metadata.
        for field in ("source_time_exact", "source_time_seconds_exact"):
            if field not in frame:
                continue
            try:
                if isinstance(frame[field], bool):
                    raise ValueError("Boolean exact timestamp")
                recorded = Fraction(frame[field])
            except (ValueError, TypeError, ZeroDivisionError, OverflowError) as error:
                raise ValueError("Frame has an invalid exact timestamp: " + field) from error
            if recorded != exact:
                raise ValueError("Frame PTS disagrees with recorded exact timestamp: " + field)
        image = Path(frame["path"]).expanduser().resolve(strict=True)
        if not image.is_file() or digest(image) != frame.get("sha256"):
            raise ValueError("Selected image hash does not match manifest")
        selected.append({"manifest_frame_index": index, "source_pts": frame["source_pts"],
                         "source_time_base": str(time_base), "source_time_seconds_exact": str(exact),
                         "source_time_seconds": float(exact), "image_sha256": frame["sha256"]})
    return selected


def record(args):
    if not args.reviewer.strip() or not args.observation.strip():
        raise ValueError("Reviewer and observation must not be blank")
    if args.next_start is not None or args.next_end is not None:
        if args.next_start is None or args.next_end is None or not all(math.isfinite(x) for x in (args.next_start, args.next_end)) or not 0 <= args.next_start < args.next_end:
            raise ValueError("Next interval requires finite 0 <= start < end")
    if not args.frame_index and args.audio_audition_status != "performed":
        raise ValueError("Record at least one inspected frame or a performed audio audition")
    if args.audio_audition_status == "performed" and not (args.audio_observation or "").strip():
        raise ValueError("A performed audio audition requires --audio-observation")
    path, manifest, source = checked_manifest(args.manifest, args.source)
    target = ledger_target(args.ledger, path, manifest, source)
    selected = selected_frames(manifest, args.frame_index)
    with locked(target):
        ledger = read_object(target)
        validate_ledger(ledger)
        if ledger.get("source_sha256") != manifest["source_sha256"]:
            raise ValueError("Manifest belongs to a different source")
        entry = {"index": len(ledger["entries"]), "created_utc": stamp(), "reviewer": args.reviewer,
                 "previous_sha256": ledger["entries"][-1]["sha256"] if ledger["entries"] else None,
                 "manifest_sha256": digest(path), "source_sha256": manifest["source_sha256"],
                 "inspected_frames": selected, "visual_review": {"performed": bool(selected)},
                 "observation": args.observation, "interpretation": args.interpretation,
                 "audio_audition": {"status": args.audio_audition_status, "observation": args.audio_observation},
                 "next_interval": {"start": args.next_start, "end": args.next_end} if args.next_start is not None else None}
        entry["sha256"] = object_hash(entry)
        ledger["entries"].append(entry)
        save_json(target, ledger)
    return {"status": "attestation_appended", "ledger": str(target), "entry_index": entry["index"],
            "inspected_frames": len(selected), "source_sha256": manifest["source_sha256"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("init", "record"):
        p = sub.add_parser(action)
        p.add_argument("--manifest", type=Path, required=True)
        p.add_argument("--ledger", type=Path, required=True)
        p.add_argument("--source", type=Path, help="Relocated original source; its hash must match the manifest")
        if action == "record":
            p.add_argument("--frame-index", type=int, action="append", default=[], help="Zero-based manifest frames-array index; repeat for each image actually inspected")
            p.add_argument("--reviewer", required=True)
            p.add_argument("--observation", required=True)
            p.add_argument("--interpretation")
            p.add_argument("--next-start", type=float)
            p.add_argument("--next-end", type=float)
            p.add_argument("--audio-audition-status", choices=("not_performed", "performed", "unsupported"), default="not_performed")
            p.add_argument("--audio-observation")
    args = parser.parse_args()
    try:
        print(json.dumps(init(args) if args.action == "init" else record(args), indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "refused", "errors": [str(error)]}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
