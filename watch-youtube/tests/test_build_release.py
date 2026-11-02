# SPDX-License-Identifier: MIT
"""Reproducible allowlisted packaging and privacy refusal behavior."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from build_release import build_release, privacy_issues


class BuildReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "skill"
        self.root.mkdir()
        (self.root / "LICENSE").write_text("MIT License\nCopyright contributors\n", encoding="utf-8")
        (self.root / "SKILL.md").write_text("---\nname: sample-skill\ndescription: Inspect media.\n---\n", encoding="utf-8")
        self.paths = ["LICENSE", "SKILL.md", "release-files.json"]
        self.configure()

    def configure(self):
        (self.root / "release-files.json").write_text(json.dumps({"archive_root": "sample-skill", "files": self.paths}), encoding="utf-8")

    def test_reproducible_and_manifest_covers_every_other_member(self):
        a, b = self.base / "one.zip", self.base / "two.zip"
        result = build_release(self.root, a)
        build_release(self.root, b)
        self.assertEqual(a.read_bytes(), b.read_bytes())
        self.assertEqual(result["sha256"], hashlib.sha256(a.read_bytes()).hexdigest())
        with zipfile.ZipFile(a) as archive:
            self.assertIsNone(archive.testzip())
            manifest = json.loads(archive.read("sample-skill/RELEASE_MANIFEST.json"))
            self.assertNotIn("version", manifest)
            self.assertEqual(len(manifest["files"]), len(archive.namelist()) - 1)
            for entry in manifest["files"]:
                data = archive.read("sample-skill/" + entry["path"])
                self.assertEqual(entry["bytes"], len(data))
                self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())
            self.assertTrue(all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist()))

    def test_unknown_runtime_file_is_refused(self):
        (self.root / "runtime-report.json").write_text("{}", encoding="utf-8")
        output = self.base / "release.zip"
        with self.assertRaisesRegex(ValueError, "Unallowlisted"):
            build_release(self.root, output)
        self.assertFalse(output.exists())

    def test_extracted_release_can_rebuild_itself_identically(self):
        original = self.base / "original.zip"
        rebuilt = self.base / "rebuilt.zip"
        build_release(self.root, original)
        destination = self.base / "extracted"
        with zipfile.ZipFile(original) as archive:
            archive.extractall(destination)
        build_release(destination / "sample-skill", rebuilt)
        self.assertEqual(original.read_bytes(), rebuilt.read_bytes())

    def test_cache_is_never_archived(self):
        cache = self.root / "__pycache__"
        cache.mkdir()
        (cache / "module.pyc").write_bytes(b"not source")
        output = self.base / "release.zip"
        build_release(self.root, output)
        with zipfile.ZipFile(output) as archive:
            self.assertFalse(any("__pycache__" in p for p in archive.namelist()))

    def test_symlink_is_refused_even_under_cache(self):
        cache = self.root / "__pycache__"
        cache.mkdir()
        (cache / "link").symlink_to(self.root / "LICENSE")
        with self.assertRaisesRegex(ValueError, "Symlink"):
            build_release(self.root, self.base / "release.zip")

    def test_path_escape_media_and_duplicate_allowlist_are_refused(self):
        for value in ("../outside", "/absolute", "a/./b", "movie.webm", "audio.mka", "LICENSE"):
            with self.subTest(value=value):
                self.paths = ["LICENSE", "SKILL.md", "release-files.json", value]
                self.configure()
                with self.assertRaises(ValueError):
                    build_release(self.root, self.base / "release.zip")

    def test_required_license_and_skill_cannot_be_omitted(self):
        for missing in ("LICENSE", "SKILL.md"):
            with self.subTest(missing=missing):
                self.paths = [p for p in ("LICENSE", "SKILL.md", "release-files.json") if p != missing]
                self.configure()
                source = self.root / missing
                saved = source.read_bytes()
                source.unlink()
                try:
                    with self.assertRaisesRegex(ValueError, "must include LICENSE and SKILL.md"):
                        build_release(self.root, self.base / "release.zip")
                finally:
                    source.write_bytes(saved)

    def test_existing_destination_and_in_tree_destination_are_preserved(self):
        output = self.base / "release.zip"
        output.write_bytes(b"existing")
        with self.assertRaisesRegex(ValueError, "already exists"):
            build_release(self.root, output)
        self.assertEqual(output.read_bytes(), b"existing")
        with self.assertRaisesRegex(ValueError, "outside"):
            build_release(self.root, self.root / "new.zip")

    def test_signed_url_secret_private_path_and_local_literal_are_refused(self):
        samples = [
            "https://cdn.example.org/video?" + "sig=" + "abcdef0123456789",
            "api_key = '" + "aB0123456789AbcdEFghijk" + "'",
            "/" + "workspace/private/record.json",
            "private-project-identifier",
        ]
        for value in samples:
            with self.subTest(category=value.split(" ")[0]):
                (self.root / "SKILL.md").write_text(value, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "Privacy"):
                    build_release(self.root, self.base / "release.zip", forbidden_literals=["private-project-identifier"])

    def test_standard_public_urls_and_documented_placeholders_are_allowed(self):
        text = "https://ffmpeg.org/ffprobe.html\nhttps://example.org/file?token=YOUR_TOKEN\napi_key = 'YOUR_API_KEY_PLACEHOLDER'"
        self.assertEqual(privacy_issues(text, []), [])

    def test_utf8_and_resource_budget_are_enforced(self):
        (self.root / "SKILL.md").write_bytes(b"\xff")
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            build_release(self.root, self.base / "release.zip")
        (self.root / "SKILL.md").write_text("valid", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "budget"):
            build_release(self.root, self.base / "release.zip", max_source_bytes=1)

    def test_cli_runs_without_project_context(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "build_release.py"
        result = subprocess.run([sys.executable, str(script), "--root", str(self.root), "--output", str(self.base / "cli.zip")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "built")

    def test_documented_parent_relative_output_is_outside_source(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "build_release.py"
        result = subprocess.run(
            [sys.executable, str(script), "--root", str(self.root), "--output", "../relative.zip"],
            cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.base / "relative.zip").is_file())

    def test_output_symlink_does_not_modify_its_target(self):
        target = self.base / "existing.zip"
        target.write_bytes(b"preserve")
        output = self.base / "link.zip"
        output.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "already exists"):
            build_release(self.root, output)
        self.assertEqual(target.read_bytes(), b"preserve")
        self.assertTrue(output.is_symlink())


if __name__ == "__main__":
    unittest.main()
