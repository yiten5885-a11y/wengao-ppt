import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

PROJECT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "scripts" / "p2_build_release_copy.py"
PYTHON = "/Users/huangyingteng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"


class ReleaseCopyGateEnforcement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.src = self.root / "src"
        self.src.mkdir()
        (self.src / "README.md").write_text("fixture input\n", encoding="utf-8")
        self.out = self.root / "release-copy"
        self.report = self.root / "report"
        self.env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")

    def tearDown(self):
        self.tmp.cleanup()

    def run_copy(self, *extra):
        return subprocess.run(
            [PYTHON, str(SCRIPT), "--src", str(self.src), "--out", str(self.out),
             "--report", str(self.report), *extra],
            cwd=PROJECT, env=self.env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=15,
        )

    def test_apply_without_50_deck_evidence_refuses_before_writing(self):
        result = self.run_copy("--apply")
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertIn("RELEASE_GATE_EVIDENCE_REQUIRED", result.stderr)
        self.assertFalse(self.out.exists())
        self.assertFalse(self.report.exists())

    def test_unprovisioned_trust_store_blocks_apply_before_writing(self):
        manifest = self.root / "manifest.json"
        freeze = self.root / "freeze.json"
        manifest.write_text("{}", encoding="utf-8")
        freeze.write_text("{}", encoding="utf-8")
        result = self.run_copy(
            "--apply", "--deck-manifest", str(manifest),
            "--deck-freeze-manifest", str(freeze),
        )
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertIn("TRUST_STORE_UNPROVISIONED", result.stderr)
        self.assertFalse(self.out.exists())
        self.assertFalse(self.report.exists())

    def test_preview_is_explicitly_non_release_and_does_not_write_copy(self):
        result = self.run_copy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.out.exists())
        report = json.loads((self.report / "release_copy_build.json").read_text())
        self.assertEqual(report["mode"], "preview")
        self.assertEqual(report["prepublication_50_decks_gate"]["verdict"],
                         "PREVIEW_ONLY")
        self.assertEqual(report["prepublication_50_decks_gate"]["counted_real_decks"], 0)


if __name__ == "__main__":
    unittest.main()
