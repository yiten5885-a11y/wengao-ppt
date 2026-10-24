"""Tests for wengao_ppt.media.similarity module and calibration dataset."""

import json
import unittest
from pathlib import Path

from wengao_ppt.media.similarity import (
    CALIBRATION_THRESHOLD,
    calculate_metadata_semantic_score,
    evaluate_candidate_photo,
)

CALIB_DIR = Path(__file__).resolve().parents[2] / "calibration"


class SimilarityAndCalibrationTests(unittest.TestCase):
    def test_calibration_dataset_integrity(self):
        dataset_path = CALIB_DIR / "dataset.json"
        contact_path = CALIB_DIR / "contact_sheet.json"

        self.assertTrue(dataset_path.is_file(), "calibration/dataset.json must exist")
        self.assertTrue(contact_path.is_file(), "calibration/contact_sheet.json must exist")

        data = json.loads(dataset_path.read_text(encoding="utf-8"))
        self.assertEqual(data.get("schema"), "rs.calibration_dataset.v1")
        self.assertGreaterEqual(data.get("total_samples", 0), 30)

        samples = data.get("samples", [])
        self.assertGreaterEqual(len(samples), 30)

        categories = set(s["category"] for s in samples)
        self.assertIn("QUALIFIED_REALISTIC_PHOTO", categories)
        self.assertIn("HISTORICAL_ENGRAVING_WOODCUT", categories)
        self.assertIn("LINE_DRAWING_VECTOR_CARTOON", categories)
        self.assertIn("UNRELATED_PHOTO", categories)

        for s in samples:
            self.assertEqual(s.get("labeler"), "agent", "Labeler must be explicitly marked as agent")
            self.assertIn(s.get("expected_outcome"), ("ACCEPT", "REJECT"))

    def test_semantic_score_evaluation(self):
        # 1. Matching realistic photo should pass threshold
        req = {
            "visual_query": "modern factory automated conveyor production line",
            "anchors": {
                "subject": "factory workers",
                "action": "operating line",
                "setting": "workshop",
            },
        }
        meta_match = {
            "title": "Factory workers assembling electronics",
            "description": "workers operating line inside modern automated workshop",
            "tags": ["factory workers", "production line", "workshop"],
        }
        score = calculate_metadata_semantic_score(meta_match, req)
        self.assertGreaterEqual(score, CALIBRATION_THRESHOLD)

        # 2. Irrelevant photo should have low score
        meta_irrelevant = {
            "title": "Cute puppy sleeping",
            "description": "golden retriever puppy on blanket",
            "tags": ["pets", "puppy", "dog"],
        }
        score_low = calculate_metadata_semantic_score(meta_irrelevant, req)
        self.assertLess(score_low, CALIBRATION_THRESHOLD)

    def test_evaluate_candidate_rejections(self):
        req = {
            "visual_query": "busy container seaport logistics terminal cranes cargo",
            "anchors": {"subject": "cargo port", "action": "loading container", "setting": "harbor"},
        }
        # Low similarity candidate
        unrelated_meta = {
            "title": "Spaghetti pasta dish",
            "description": "Italian spaghetti with tomato sauce",
            "tags": ["food", "pasta"],
        }
        res = evaluate_candidate_photo(b"\xff\xd8\xff\xe0dummy", unrelated_meta, req)
        self.assertFalse(res["accepted"])
        self.assertEqual(res["reason"], "SCENE.LOW_SIMILARITY")


if __name__ == "__main__":
    unittest.main()
