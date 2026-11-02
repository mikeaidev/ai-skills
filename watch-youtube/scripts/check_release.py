#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Verify a reproducible source release and optionally run its tests in a clean extraction."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import zipfile

from build_release import MANIFEST_NAME, build_release, safe_relative


MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_MEMBERS = 1000
RUNTIME_MODULES = ("workflow", "validate_media", "sample_frames", "safe_outputs",
                   "media_timing", "decoded_equivalence", "review_ledger",
                   "mp4_evidence", "av1_framing", "webm_structure")


def verify_archive(path):
    """Verify every member before extraction; a manifest is integrity evidence, not a signature."""
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)) or not 1 <= len(names) <= MAX_MEMBERS:
            raise ValueError("Duplicate members or invalid release member count")
        if sum(info.file_size for info in infos) > MAX_ARCHIVE_BYTES:
            raise ValueError("Release uncompressed byte budget exceeded")
        for info in infos:
            safe_relative(info.filename)
            kind = stat.S_IFMT(info.external_attr >> 16)
            if info.is_dir() or kind not in (0, stat.S_IFREG):
                raise ValueError("Release members must be regular source files")
        manifests = [name for name in names if name.endswith("/" + MANIFEST_NAME)]
        if len(manifests) != 1:
            raise ValueError("Require one release manifest")
        manifest = json.loads(archive.read(manifests[0]))
        root = manifest.get("archive_root")
        if not isinstance(root, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", root):
            raise ValueError("Invalid archive root")
        if manifests[0] != root + "/" + MANIFEST_NAME:
            raise ValueError("Manifest is not at the release root")
        entries = manifest.get("files")
        if manifest.get("schema_version") != 1 or not isinstance(entries, list):
            raise ValueError("Unsupported release manifest")
        expected = {root + "/" + MANIFEST_NAME}
        for entry in entries:
            relative = safe_relative(entry["path"])
            name = root + "/" + str(relative)
            if name in expected:
                raise ValueError("Duplicate or reserved manifest entry")
            expected.add(name)
            if type(entry.get("bytes")) is not int or entry["bytes"] < 0:
                raise ValueError("Invalid manifest byte count")
            if not isinstance(entry.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
                raise ValueError("Invalid manifest SHA-256")
            data = archive.read(name)
            if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise ValueError("Release member size/hash mismatch: " + str(relative))
            data.decode("utf-8")
        if set(names) != expected:
            raise ValueError("Archive members do not exactly match the manifest")
        if archive.testzip() is not None:
            raise ValueError("Release CRC verification failed")
        skill = archive.read(root + "/SKILL.md").decode("utf-8")
        frontmatter = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", skill, re.DOTALL)
        if not frontmatter:
            raise ValueError("SKILL.md requires YAML frontmatter")
        name = re.search(r"^name:\s*([a-z0-9]+(?:-[a-z0-9]+)*)\s*$", frontmatter[1], re.MULTILINE)
        description = re.search(r"^description:\s*(\S[^\r\n]*)$", frontmatter[1], re.MULTILINE)
        if not name or name[1] != root or not description:
            raise ValueError("Skill name/root and nonempty description must agree")
        license_text = archive.read(root + "/LICENSE").decode("utf-8")
        if not license_text.startswith("MIT License\n"):
            raise ValueError("This release requires its MIT LICENSE")
        return {"archive_root": root, "members": len(names),
                "manifest_entries": len(entries), "crc": "passed", "member_hashes": "passed",
                "skill_structure": "basic_frontmatter_passed"}


def clean_environment():
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_command(args, cwd, env):
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=300)
    if result.stdout:
        print(result.stdout, end="", flush=True)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr, flush=True)
    if result.returncode != 0:
        raise ValueError("Verification command failed: " + Path(args[0]).name)
    return result.stdout


def parse_node_summary(output):
    values = {}
    for key in ("tests", "pass", "fail", "cancelled", "skipped", "todo"):
        matches = re.findall(r"^# " + key + r" (\d+)\s*$", output, re.MULTILINE)
        if len(matches) != 1:
            raise ValueError("Missing or ambiguous Node test summary: " + key)
        values[key] = int(matches[0])
    if values["tests"] <= 0 or values["pass"] != values["tests"] or any(values[key] for key in ("fail", "cancelled", "skipped", "todo")):
        raise ValueError("Node suite contains failure, skip, cancellation, TODO or no executed tests")
    return values


def run_regressions(root, env):
    runner = """import json,sys,unittest
suite=unittest.defaultTestLoader.discover('tests', pattern='test_*.py')
result=unittest.TextTestRunner(verbosity=2).run(suite)
summary={'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),'expected_failures':len(result.expectedFailures),'unexpected_successes':len(result.unexpectedSuccesses)}
print('PYTHON_TEST_SUMMARY='+json.dumps(summary))
sys.exit(0 if result.testsRun and result.wasSuccessful() and not result.skipped and not result.expectedFailures else 1)
"""
    python_output = run_command([sys.executable, "-c", runner], root, env)
    matches = re.findall(r"^PYTHON_TEST_SUMMARY=(.+)$", python_output, re.MULTILINE)
    if len(matches) != 1:
        raise ValueError("Python test summary was not returned")
    node_output = run_command(["node", "--test", "--test-reporter=tap", "tests/test_capture_mse.js"], root, env)
    return {"python": json.loads(matches[0]), "javascript": parse_node_summary(node_output)}


def check_release(root, run_tests=False):
    root = Path(root).resolve(strict=True)
    env = clean_environment()
    with tempfile.TemporaryDirectory(prefix="watch-youtube-release-") as temporary:
        base = Path(temporary)
        first, second = base / "source.zip", base / "rebuilt.zip"
        build = build_release(root, first)
        result = verify_archive(first)
        extraction = base / "extracted"
        with zipfile.ZipFile(first) as archive:
            archive.extractall(extraction)
        extracted = extraction / result["archive_root"]
        origin_code = """import importlib,json,pathlib,sys
root=pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0,str(root/'scripts'))
origins={name:str(pathlib.Path(importlib.import_module(name).__file__).resolve()) for name in sys.argv[2:]}
if not all(pathlib.Path(path).is_relative_to(root) for path in origins.values()):raise SystemExit('Module imported outside fresh extraction')
print(json.dumps(origins))
"""
        origins = json.loads(run_command([sys.executable, "-c", origin_code, str(extracted), *RUNTIME_MODULES], base, env))
        doctor = json.loads(run_command([sys.executable, str(extracted / "scripts/workflow.py"), "doctor"], base, env))
        if doctor.get("status") != "passed":
            raise ValueError("Dependency doctor did not pass")
        tests = run_regressions(extracted, env) if run_tests else {"status": "not_requested"}
        build_release(extracted, second)
        if first.read_bytes() != second.read_bytes():
            raise ValueError("Clean extracted release did not rebuild byte-identically")
        result.update({"status": "passed", "archive_sha256": build["sha256"],
                       "deterministic_rebuild": "byte_identical", "module_origins": origins,
                       "dependency_preflight": doctor["status"], "regressions": tests,
                       "scope": "Source integrity/local regression checks; no live retrieval, visual review or audio audition."})
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--run-tests", action="store_true", help="Run all Python/JavaScript regressions from the clean extraction; skips fail this release gate")
    args = parser.parse_args()
    try:
        print(json.dumps(check_release(args.root, args.run_tests), indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, subprocess.TimeoutExpired, zipfile.BadZipFile) as error:
        print(json.dumps({"status": "failed", "error": str(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
