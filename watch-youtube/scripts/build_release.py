#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Build an allowlisted, privacy-checked, reproducible source-only skill ZIP."""
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path, PurePosixPath
import re
import sys
from urllib.parse import parse_qsl, urlsplit
import zipfile


MANIFEST_NAME = "RELEASE_MANIFEST.json"
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
IGNORED_PARTS = {".git", "__pycache__", ".pytest_cache", ".venv"}
IGNORED_NAMES = {".DS_Store", ".coverage", "coverage.xml"}
MEDIA_SUFFIXES = {".mp4", ".mkv", ".mka", ".webm", ".mov", ".avi", ".wav", ".mp3", ".ogg", ".zip", ".tar", ".gz", ".exe", ".dll", ".so", ".apk"}
SENSITIVE_QUERY_KEYS = {"sig", "signature", "token", "access_token", "authorization", "credential", "x-amz-signature", "x-amz-credential", "x-goog-signature", "x-goog-credential"}
URL_PATTERN = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
PRIVATE_PATH = re.compile(r"(?<![\w])/(?:root|workspace)/[^\s\"'`]+|(?<![\w])/(?:home)/[^/\s\"'`]+/|\b[A-Za-z]:\\Users\\[^\\\s]+\\")
PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----")
KNOWN_TOKEN = re.compile(r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
CREDENTIAL_ASSIGNMENT = re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|password|client[_-]?secret)\b[\"']?\s*[:=]\s*[\"']([^\"'\r\n]{16,})[\"']")
PLACEHOLDERS = {"example", "placeholder", "redacted", "your_", "your-", "insert_", "replace_"}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def ignored(path):
    return bool(set(path.parts) & IGNORED_PARTS) or path.name in IGNORED_NAMES or path.suffix in {".pyc", ".pyo"}


def safe_relative(value):
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise ValueError("Allowlist paths must be nonempty POSIX relative paths")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value or value == ".":
        raise ValueError("Noncanonical or escaping allowlist path")
    return path


def placeholder(value):
    lower = value.lower()
    return any(word in lower for word in PLACEHOLDERS) or set(value) <= {"*", ".", "x", "X", "_", "-", "<", ">"}


def privacy_issues(text, forbidden_literals):
    """Return categories only; never echo a detected private value."""
    issues = set()
    if PRIVATE_PATH.search(text):
        issues.add("private absolute filesystem path")
    if PRIVATE_KEY.search(text) or KNOWN_TOKEN.search(text):
        issues.add("credential or private key")
    if any(not placeholder(m.group(1)) for m in CREDENTIAL_ASSIGNMENT.finditer(text)):
        issues.add("credential assignment")
    if any(literal.casefold() in text.casefold() for literal in forbidden_literals):
        issues.add("forbidden literal")
    for match in URL_PATTERN.finditer(text):
        try:
            url = urlsplit(match.group(0).rstrip(".,);]}"))
            if url.username or url.password:
                issues.add("URL with embedded credentials")
            host = url.hostname or ""
            if host.lower() in {"localhost", "localhost.localdomain"}:
                issues.add("private network URL")
            try:
                if not ipaddress.ip_address(host).is_global:
                    issues.add("private network URL")
            except ValueError:
                pass
            if any(key.casefold() in SENSITIVE_QUERY_KEYS and value and not placeholder(value)
                   for key, value in parse_qsl(url.query, keep_blank_values=True)):
                issues.add("signed or credential-bearing URL")
        except ValueError:
            issues.add("malformed URL requires review")
    return sorted(issues)


def read_config(path):
    config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or set(config) != {"archive_root", "files"}:
        raise ValueError("Allowlist requires exactly archive_root and files")
    name, values = config["archive_root"], config["files"]
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
        raise ValueError("archive_root must be a lowercase hyphenated name")
    if not isinstance(values, list) or not values or not all(isinstance(x, str) for x in values) or len(values) != len(set(values)):
        raise ValueError("files must be a nonempty list of unique relative paths")
    paths = [safe_relative(value) for value in values]
    if MANIFEST_NAME in values or any(ignored(path) or path.suffix.lower() in MEDIA_SUFFIXES for path in paths):
        raise ValueError("Allowlist includes reserved, cached, binary or media content")
    return name, sorted(paths, key=str)


def collect_files(root, paths, forbidden_literals, max_source_bytes):
    allowed = {str(path) for path in paths}
    # Refuse any symlink, including a symlink that an ignore rule would omit.
    for child in root.rglob("*"):
        relative = child.relative_to(root)
        if child.is_symlink():
            raise ValueError("Symlink in release tree: " + relative.as_posix())
        if child.is_file() and not ignored(relative) and relative.as_posix() != MANIFEST_NAME and relative.as_posix() not in allowed:
            raise ValueError("Unallowlisted file in release tree: " + relative.as_posix())
    contents, total = {}, 0
    for relative in paths:
        path = root.joinpath(*relative.parts)
        if not path.is_file():
            raise ValueError("Missing allowlisted file: " + str(relative))
        before = path.stat()
        if before.st_size > max_source_bytes - total:
            raise ValueError("Source byte budget exceeded")
        with path.open("rb") as handle:
            data = handle.read(max_source_bytes - total + 1)
        total += len(data)
        if total > max_source_bytes:
            raise ValueError("Source byte budget exceeded")
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(data) != after.st_size:
            raise ValueError("Source changed while packaging: " + str(relative))
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("Non-UTF-8 content is outside this source-only release contract: " + str(relative)) from error
        issues = privacy_issues(text, forbidden_literals)
        if issues:
            raise ValueError("Privacy check failed for " + str(relative) + ": " + "; ".join(issues))
        contents[str(relative)] = data
    return contents


def build_release(root, output, allowlist=None, forbidden_literals=(), max_source_bytes=16 * 1024 * 1024):
    root, output = Path(root).resolve(), Path(output).absolute()
    # Resolve parent segments while retaining the final component so an
    # existing output symlink is still detected and never followed for writing.
    output = output.parent.resolve() / output.name
    if not root.is_dir() or max_source_bytes <= 0:
        raise ValueError("Require a skill directory and a positive source byte budget")
    if output.is_relative_to(root) or output.resolve().is_relative_to(root):
        raise ValueError("Release output must be outside the skill directory")
    if output.exists() or output.is_symlink():
        raise ValueError("Release output already exists")
    if not output.parent.is_dir():
        raise ValueError("Release output parent does not exist")
    if any(not isinstance(x, str) or not x.strip() for x in forbidden_literals):
        raise ValueError("Forbidden literals must be nonempty strings")
    archive_root, paths = read_config(Path(allowlist) if allowlist else root / "release-files.json")
    contents = collect_files(root, paths, forbidden_literals, max_source_bytes)
    if "LICENSE" not in contents or "SKILL.md" not in contents:
        raise ValueError("Release must include LICENSE and SKILL.md")
    manifest = {
        "schema_version": 1, "archive_root": archive_root,
        "files": [{"path": path, "bytes": len(data), "sha256": sha256(data)} for path, data in sorted(contents.items())],
        "manifest_scope": "This manifest covers every other archive member; it does not hash itself.",
    }
    # A previously extracted manifest is never copied or trusted; regenerate it
    # from the current allowlisted bytes so a distributed ZIP can rebuild itself.
    contents[MANIFEST_NAME] = (json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    # ZIP_STORED avoids compressor-version variability. Exclusive creation
    # preserves any destination another process claims after the preflight.
    owned = False
    try:
        with output.open("xb") as handle:
            owned = True
            with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_STORED) as archive:
                for path, data in sorted(contents.items()):
                    info = zipfile.ZipInfo(archive_root + "/" + path, FIXED_ZIP_TIME)
                    info.create_system = 3
                    info.external_attr = 0o100644 << 16
                    archive.writestr(info, data)
        with zipfile.ZipFile(output) as archive:
            if archive.testzip() is not None:
                raise ValueError("Built archive failed its CRC check")
        return {"status": "built", "output": str(output), "archive_root": archive_root,
                "members": len(contents), "bytes": output.stat().st_size,
                "sha256": sha256(output.read_bytes()), "manifest": MANIFEST_NAME}
    except Exception:
        if owned:
            output.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--allowlist", type=Path)
    parser.add_argument("--forbidden-literals", type=Path, help="Local JSON list; never copied into the release")
    parser.add_argument("--max-source-bytes", type=int, default=16 * 1024 * 1024)
    args = parser.parse_args()
    try:
        literals = json.loads(args.forbidden_literals.read_text(encoding="utf-8")) if args.forbidden_literals else []
        if not isinstance(literals, list):
            raise ValueError("Forbidden-literal file must contain a JSON list")
        result = build_release(args.root, args.output, args.allowlist, literals, args.max_source_bytes)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, TypeError, zipfile.BadZipFile) as error:
        print(json.dumps({"status": "refused", "error": str(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
