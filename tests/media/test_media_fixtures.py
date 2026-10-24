"""Validate media fixtures in fixtures/media."""

import hashlib
import json
import unittest
from pathlib import Path

from wengao_ppt.scene_images import perceptual_hash

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "media"


class MediaFixturesTests(unittest.TestCase):
    def test_sample_resolutions_integrity(self):
        res_dir = FIXTURES_DIR / "resolutions"
        self.assertTrue(res_dir.is_dir(), "resolutions dir must exist")

        json_files = list(res_dir.glob("*.json"))
        self.assertGreaterEqual(len(json_files), 6, "Must have at least 6 resolution fixtures")

        statuses = set()
        for f in sorted(json_files):
            data = json.loads(f.read_text(encoding="utf-8"))
            self.assertEqual(data.get("schema"), "rs.scene_resolution.v1")
            self.assertIn("scene_id", data)
            status = data.get("status")
            statuses.add(status)
            self.assertIn(status, ("RESOLVED_T1", "RESOLVED_T2", "RESOLVED_T3", "FALLBACK_T4"))

            attempts = data.get("attempts", [])
            self.assertIsInstance(attempts, list)
            self.assertGreater(len(attempts), 0)

            asset = data.get("asset")
            if status == "FALLBACK_T4":
                self.assertIsNone(asset)
                self.assertTrue(bool(data.get("reason")), "FALLBACK_T4 must have reason")
            else:
                self.assertIsNotNone(asset)
                self.assertIsNone(data.get("reason"))
                img_path = Path(asset["path"])
                self.assertTrue(img_path.is_file(), f"Asset image must exist: {img_path}")
                raw = img_path.read_bytes()
                sha = hashlib.sha256(raw).hexdigest()
                self.assertEqual(asset["sha256"], sha)
                ph = perceptual_hash(raw)
                self.assertEqual(asset["phash"], ph)
                self.assertGreater(asset["width"], 0)
                self.assertGreater(asset["height"], 0)
                self.assertTrue(bool(asset["license"]))
                self.assertTrue(bool(asset["attribution_text"]))

        # Must cover all 4 tiers
        self.assertIn("RESOLVED_T1", statuses)
        self.assertIn("RESOLVED_T2", statuses)
        self.assertIn("RESOLVED_T3", statuses)
        self.assertIn("FALLBACK_T4", statuses)

    def test_ledger_sample_integrity(self):
        ledger_path = FIXTURES_DIR / "ledger_sample.json"
        self.assertTrue(ledger_path.is_file())
        data = json.loads(ledger_path.read_text(encoding="utf-8"))
        self.assertEqual(data.get("schema"), "rs.image_ledger.v1")
        self.assertEqual(data.get("total_scenes"), 6)
        self.assertEqual(data.get("resolved_scenes"), 4)
        self.assertEqual(data.get("fallback_scenes"), 2)
        self.assertEqual(data.get("deck_delivery_status"), "DELIVERED")
        self.assertTrue(data.get("deck_success"))
        self.assertAlmostEqual(data.get("scene_coverage_rate"), 4 / 6)


if __name__ == "__main__":
    unittest.main()
