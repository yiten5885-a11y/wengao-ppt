"""Install packaging sanitization tests.

The candidate build directory doubles as the working directory: it contains
real job inputs (``jobs/``), QA reports (``reports/``), loopback workbench data
(``wb_loopback_data/``), scene caches and server logs. Installing used to copy
the whole tree, so private client material shipped inside
``<root>/versions/<label>/``. These tests pin the exclusion and the
fail-closed re-check.
"""
import tempfile
import unittest
from pathlib import Path

from wengao_ppt import install


def make_package(root: Path, *, with_private: bool) -> Path:
    (root / "wengao_ppt").mkdir(parents=True)
    (root / "wengao_ppt" / "__init__.py").write_text("VERSION = 'test'\n", encoding="utf-8")
    (root / "contracts").mkdir()
    (root / "contracts" / "schemas_v1.json").write_text("{}", encoding="utf-8")
    (root / "assets").mkdir()
    (root / "assets" / "logo.png").write_bytes(b"png")
    (root / "README.md").write_text("# readme\n", encoding="utf-8")
    if with_private:
        (root / "jobs" / "job-1").mkdir(parents=True)
        (root / "jobs" / "job-1" / "input.pptx").write_text("PRIVATE", encoding="utf-8")
        (root / "reports" / "r1").mkdir(parents=True)
        (root / "reports" / "r1" / "gate.json").write_text("{}", encoding="utf-8")
        (root / "wb_loopback_data" / "jobs" / "job-x").mkdir(parents=True)
        (root / "wb_loopback_data" / "jobs" / "job-x" / "job.json").write_text("{}", encoding="utf-8")
        (root / ".wengao_scene_assets").mkdir()
        (root / ".wengao_scene_assets" / "cached.png").write_bytes(b"png")
        (root / "workbench_server_8823.log").write_text("log line", encoding="utf-8")
        (root / "probe.txt").write_text("probe", encoding="utf-8")
        (root / "__pycache__").mkdir()
        (root / "__pycache__" / "x.pyc").write_bytes(b"pyc")
    return root


class PackageIgnoreTests(unittest.TestCase):
    def test_runtime_members_are_ignored(self):
        names = ["wengao_ppt", "contracts", "assets", "README.md",
                 "jobs", "reports", "wb_loopback_data", ".wengao_scene_assets",
                 "__pycache__", "workbench_server_8823.log", "probe.txt",
                 "SELFTEST_FAILED.json"]
        ignored = install._package_ignore("/pkg", names)
        for name in ("jobs", "reports", "wb_loopback_data", ".wengao_scene_assets",
                     "__pycache__", "workbench_server_8823.log", "probe.txt"):
            with self.subTest(name=name):
                self.assertIn(name, ignored)
        for name in ("wengao_ppt", "contracts", "assets", "README.md"):
            with self.subTest(name=name):
                self.assertNotIn(name, ignored)

    def test_suffix_rule_covers_logs_and_caches(self):
        ignored = install._package_ignore("/pkg", ["a.log", "b.pyc", "c.tmp",
                                                  "d.sqlite3", ".DS_Store", "keep.py"])
        self.assertEqual(ignored, {"a.log", "b.pyc", "c.tmp", "d.sqlite3", ".DS_Store"})


class CopyIntoSversionsTests(unittest.TestCase):
    def test_private_material_is_not_copied_into_a_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = make_package(Path(tmp) / "pkg", with_private=True)
            root = Path(tmp) / "inst"
            staging = install._copy_into_versions(pkg, root, "t1")
            for name in ("jobs", "reports", "wb_loopback_data",
                         ".wengao_scene_assets", "__pycache__"):
                with self.subTest(member=name):
                    self.assertFalse((staging / name).exists())
            self.assertFalse((staging / "workbench_server_8823.log").exists())
            self.assertFalse((staging / "probe.txt").exists())
            # Program members must still be installed.
            self.assertTrue((staging / "wengao_ppt" / "__init__.py").is_file())
            self.assertTrue((staging / "contracts" / "schemas_v1.json").is_file())
            self.assertTrue((staging / "assets" / "logo.png").is_file())
            self.assertTrue((staging / "README.md").is_file())

    def test_clean_package_is_unaffected(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = make_package(Path(tmp) / "pkg", with_private=False)
            staging = install._copy_into_versions(pkg, Path(tmp) / "inst", "t2")
            self.assertTrue((staging / "wengao_ppt").is_dir())
            self.assertEqual(install._forbidden_members(staging), [])


class ForbiddenMemberTests(unittest.TestCase):
    def test_planted_leak_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "wengao_ppt").mkdir()
            (root / "wengao_ppt" / "a.py").write_text("x", encoding="utf-8")
            self.assertEqual(install._forbidden_members(root), [])
            (root / "jobs").mkdir()
            (root / "jobs" / "input.pptx").write_text("PRIVATE", encoding="utf-8")
            (root / "server.log").write_text("log", encoding="utf-8")
            found = install._forbidden_members(root)
            self.assertTrue(any(item.startswith("jobs") for item in found), found)
            self.assertIn("server.log", found)


if __name__ == "__main__":
    unittest.main()
