# SPDX-License-Identifier: MIT
"""Claim fresh private run directories before writing any media-analysis output."""
import hashlib
import json
import os
from pathlib import Path
import tempfile


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def prepare_run(output_dir, inputs):
    """Read/hash inputs, then exclusively claim a previously absent destination.

    Existing destinations are always refused, including empty directories and
    symlink aliases. Refusal never writes reports or checkpoints into an old run.
    No input may live beneath the new destination. The caller must validate its
    CLI arguments before calling and write failure reports only after success.
    """
    requested = Path(output_dir).expanduser().absolute()
    if requested.exists() or requested.is_symlink():
        raise ValueError("Output directory already exists; use a new output directory")
    output = requested.resolve()
    snapshots = {}
    for label, supplied in inputs.items():
        if supplied is None:
            continue
        source = Path(supplied).expanduser().resolve(strict=True)
        if not source.is_file() or source.stat().st_size == 0:
            raise ValueError("Missing or empty input: " + str(source))
        if source == output or output in source.parents:
            raise ValueError("Input must not be inside the output directory")
        snapshots[label] = {"path": str(source), "bytes": source.stat().st_size,
                            "sha256": digest(source)}
    # mkdir(exist_ok=False) is the ownership decision, including concurrent runs.
    # Parent creation is allowed; the leaf itself must not already exist.
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    return output, snapshots


def save_json(path, data):
    """Atomically replace this run's JSON through an exclusively created temp file."""
    target = Path(path)
    # Serialize first: invalid data must not replace a previous valid checkpoint.
    encoded = json.dumps(data, indent=2, allow_nan=False) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix="." + target.name + ".",
                                            suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
