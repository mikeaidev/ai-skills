# SPDX-License-Identifier: MIT
"""Adversarial archive checks and no-skip release gate semantics."""
import hashlib
import json
from pathlib import Path
import sys
import stat
import tempfile
import unittest
import warnings
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_release import parse_node_summary, verify_archive


class CheckReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "release.zip"
        self.contents = {
            "LICENSE": b"MIT License\nCopyright contributors\n",
            "SKILL.md": b"---\nname: sample-skill\ndescription: Inspect actual media.\n---\n",
        }

    def write_archive(self, *, mutate=None, extra=None, duplicate=False):
        manifest = {"schema_version": 1, "archive_root": "sample-skill",
                    "files": [{"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                              for name, data in self.contents.items()]}
        if mutate:
            mutate(manifest)
        with zipfile.ZipFile(self.output, "w") as archive:
            for name, data in self.contents.items():
                archive.writestr("sample-skill/" + name, data)
            archive.writestr("sample-skill/RELEASE_MANIFEST.json", json.dumps(manifest))
            if extra:
                archive.writestr(*extra)
            if duplicate:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    archive.writestr("sample-skill/LICENSE", self.contents["LICENSE"])

    def test_valid_manifest_and_skill_structure(self):
        self.write_archive()
        result = verify_archive(self.output)
        self.assertNotIn("version", result)
        self.assertEqual(result["manifest_entries"], 2)

    def test_tampered_manifest_hash_is_refused(self):
        self.write_archive(mutate=lambda m: m["files"][0].update(sha256="0" * 64))
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            verify_archive(self.output)

    def test_extra_and_duplicate_members_are_refused(self):
        self.write_archive(extra=("sample-skill/unlisted.txt", b"unexpected"))
        with self.assertRaisesRegex(ValueError, "exactly match"):
            verify_archive(self.output)
        self.write_archive(duplicate=True)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            verify_archive(self.output)

    def test_traversal_is_refused_before_extraction(self):
        self.write_archive(extra=("sample-skill/../outside.txt", b"outside"))
        with self.assertRaisesRegex(ValueError, "escaping"):
            verify_archive(self.output)
        self.assertFalse((self.output.parent / "outside.txt").exists())

    def test_symlink_member_is_refused_before_extraction(self):
        self.write_archive()
        with zipfile.ZipFile(self.output, "a") as archive:
            link = zipfile.ZipInfo("sample-skill/link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, "LICENSE")
        with self.assertRaisesRegex(ValueError, "regular source files"):
            verify_archive(self.output)

    def test_inconsistent_skill_name_is_refused(self):
        self.contents["SKILL.md"] = b"---\nname: other-skill\ndescription: Inspect media.\n---\n"
        self.write_archive()
        with self.assertRaisesRegex(ValueError, "name/root"):
            verify_archive(self.output)

    def test_node_release_gate_requires_actual_unskipped_success(self):
        good = "\n".join(["# tests 30", "# pass 30", "# fail 0", "# cancelled 0", "# skipped 0", "# todo 0"])
        self.assertEqual(parse_node_summary(good)["pass"], 30)
        for changed in (good.replace("# pass 30", "# pass 29").replace("# skipped 0", "# skipped 1"),
                        good.replace("# cancelled 0", "# cancelled 1"),
                        good.replace("# todo 0", "# todo 1"),
                        good.replace("# tests 30", "# tests 0").replace("# pass 30", "# pass 0"),
                        good + "\n# pass 30", good.replace("# pass 30", "")):
            with self.subTest(summary=changed):
                with self.assertRaises(ValueError):
                    parse_node_summary(changed)


if __name__ == "__main__":
    unittest.main()
