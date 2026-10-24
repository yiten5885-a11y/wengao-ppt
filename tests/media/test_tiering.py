"""Tests for multi-tier image resolution engine (RS-2)."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image
from pptx import Presentation
from pptx.util import Inches

from wengao_ppt.media.place import check_image_ledger, place_image
from wengao_ppt.media.tiering import resolve_scenes


class MediaTieringEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.job_dir = Path(self.tmp_dir) / "job_01"
        self.job_dir.mkdir(parents=True)

        # Create a sample user image for T1
        self.user_img = self.job_dir / "user_sample.jpg"
        im = Image.new("RGB", (800, 600), color=(10, 80, 160))
        im.save(str(self.user_img), "JPEG")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_t1_user_image_resolution(self):
        reqs = [
            {
                "schema": "rs.scene_request.v1",
                "scene_id": "p01-s1",
                "user_image": {"path": str(self.user_img)},
                "visual_query": "user uploaded photo",
                "requires_real_photo": False,
            }
        ]
        resolutions = resolve_scenes(reqs, options={}, job_dir=self.job_dir)
        self.assertEqual(len(resolutions), 1)
        res = resolutions[0]
        self.assertEqual(res["status"], "RESOLVED_T1")
        self.assertIsNotNone(res["asset"])
        self.assertEqual(res["asset"]["provider"], "user")
        self.assertTrue(Path(res["asset"]["path"]).is_file())

    def test_requires_real_photo_skips_t3(self):
        reqs = [
            {
                "schema": "rs.scene_request.v1",
                "scene_id": "p02-s1",
                "visual_query": "specific apple logo on flagship store entrance",
                "anchors": {"subject": "apple logo", "action": "display", "setting": "store"},
                "requires_real_photo": True,
            }
        ]
        # Pexels key is not set, ComfyUI should be skipped
        resolutions = resolve_scenes(reqs, options={}, job_dir=self.job_dir)
        self.assertEqual(len(resolutions), 1)
        res = resolutions[0]
        self.assertEqual(res["status"], "FALLBACK_T4")
        self.assertIsNone(res["asset"])
        self.assertTrue(bool(res["reason"]))

        # Verify T3 attempt was SKIPPED due to requires_real_photo
        t3_attempt = next((a for a in res["attempts"] if a["tier"] == "T3"), None)
        self.assertIsNotNone(t3_attempt)
        self.assertEqual(t3_attempt["outcome"], "SKIPPED")
        self.assertIn("requires_real_photo", t3_attempt["detail"])

    def test_t3_local_generation_resolution(self):
        reqs = [
            {
                "schema": "rs.scene_request.v1",
                "scene_id": "p03-s1",
                "visual_query": "modern sleek server datacenter room",
                "requires_real_photo": False,
            }
        ]

        # Mock ComfyUI client to return valid image bytes
        mock_im = Image.new("RGB", (512, 512), color=(90, 40, 120))
        tmp_img = self.job_dir / "gen_tmp.jpg"
        mock_im.save(str(tmp_img), "JPEG")
        fake_bytes = tmp_img.read_bytes()

        with patch("wengao_ppt.media.tiering.ComfyUIClient.generate_image") as mock_gen:
            mock_gen.return_value = {
                "image_bytes": fake_bytes,
                "model": "sd_xl_turbo",
                "seed": 12345,
                "prompt_digest": "realistic documentary photograph of modern sleek server datacenter room",
            }
            resolutions = resolve_scenes(reqs, options={}, job_dir=self.job_dir)

        self.assertEqual(len(resolutions), 1)
        res = resolutions[0]
        self.assertEqual(res["status"], "RESOLVED_T3")
        self.assertIsNotNone(res["asset"])
        self.assertEqual(res["asset"]["provider"], "ai_generated_local")
        self.assertEqual(res["asset"]["model"], "sd_xl_turbo")
        self.assertIn("AI 生成", res["asset"]["attribution_text"])

    def test_deduplication_rejects_duplicate_assets(self):
        # Request same user image for two different scenes
        reqs = [
            {
                "schema": "rs.scene_request.v1",
                "scene_id": "p04-s1",
                "user_image": {"path": str(self.user_img)},
            },
            {
                "schema": "rs.scene_request.v1",
                "scene_id": "p04-s2",
                "user_image": {"path": str(self.user_img)},
                "requires_real_photo": True,
            },
        ]
        resolutions = resolve_scenes(reqs, options={}, job_dir=self.job_dir)
        self.assertEqual(len(resolutions), 2)
        self.assertEqual(resolutions[0]["status"], "RESOLVED_T1")
        # Second scene must reject duplicate asset and fallback to T4
        self.assertEqual(resolutions[1]["status"], "FALLBACK_T4")
        t1_attempt_s2 = next((a for a in resolutions[1]["attempts"] if a["tier"] == "T1"), None)
        self.assertIsNotNone(t1_attempt_s2)
        self.assertEqual(t1_attempt_s2["outcome"], "REJECTED_DUPLICATE")

    def test_idempotency_reuses_ledger(self):
        reqs = [
            {
                "schema": "rs.scene_request.v1",
                "scene_id": "p05-s1",
                "user_image": {"path": str(self.user_img)},
            }
        ]
        res1 = resolve_scenes(reqs, options={}, job_dir=self.job_dir)
        # Re-run with identical requests
        res2 = resolve_scenes(reqs, options={}, job_dir=self.job_dir)
        self.assertEqual(res1[0]["asset"]["sha256"], res2[0]["asset"]["sha256"])

    def test_place_image_and_check_ledger(self):
        reqs = [
            {
                "schema": "rs.scene_request.v1",
                "scene_id": "p06-s1",
                "user_image": {"path": str(self.user_img)},
            }
        ]
        resolutions = resolve_scenes(reqs, options={}, job_dir=self.job_dir)
        res = resolutions[0]

        # Place image onto PPTX slide
        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[0])
        rect = [1.0, 1.0, 6.0, 4.0]
        shape_id = place_image(slide, rect, res["asset"])
        self.assertIsInstance(shape_id, int)

        # Check speaker notes attribution
        self.assertIn("用户材料", slide.notes_slide.notes_text_frame.text)

        # Save presentation in job dir
        deck_path = self.job_dir / "deck.pptx"
        prs.save(str(deck_path))

        # Check image ledger
        check = check_image_ledger(self.job_dir)
        self.assertEqual(check["status"], "PASS")
        self.assertEqual(check["deck_delivery_status"], "DELIVERED")
        self.assertEqual(check["total_scenes"], 1)
        self.assertEqual(check["resolved_scenes"], 1)
        self.assertEqual(check["scene_coverage_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
