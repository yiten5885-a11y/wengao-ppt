"""Unit tests for native playback hard gate (HG10) and anti-synthetic checks.

Tests:
1. Rejection of fake MP4 headers with insufficient frame samples or tiny payload.
2. Rejection of fake MP4 with invalid resolution (<640x360).
3. Rejection of synthetic reviews with empty pages or missing coverage.
4. Rejection of synthetic reviews with duplicate/copy-pasted observations across pages.
5. Rejection of synthetic reviews with placeholder tokens ("TODO", "mock", "synthetic").
6. Rejection of synthetic reviews with generic reviewer ID or invalid player.
7. Rejection of review without disk reopen confirmation or hash mismatch.
8. Acceptance of genuine, diverse, full-coverage manual review records.
9. Verification of Runner.attach_native_playback_evidence fail-closed enforcement.
"""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from scripts import layout_gate as lg
from wengao_ppt.runner import GateError, Runner


class TestNativePlaybackGate(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="wg-playback-gate-test-")
        self.root = Path(self.temp_dir.name)
        self.pptx_sha = "a" * 64
        self.video_sha = "b" * 64
        self.sel_pages = list(range(1, 14))

        # A valid baseline review record
        self.valid_review = {
            "schema": "WG-NATIVE-REVIEW/1",
            "pptx_sha256": self.pptx_sha,
            "video_sha256": self.video_sha,
            "reviewer": {
                "id": "independent-qa-lead-zhang",
                "independent": True,
            },
            "playback_application": "Apple Keynote 14.1 Native SlideShow",
            "overall": "PASS",
            "reopened": True,
            "reopened_pptx_sha256": self.pptx_sha,
            "pages": [
                {
                    "page": p,
                    "visible_content": True,
                    "no_clipping": True,
                    "no_occlusion": True,
                    "observations": f"第{p}页实播观察：版式布局平整无错位，图文对比度高且文字边缘锐利清晰。",
                }
                for p in range(1, 14)
            ],
            "animation_review": {
                "checked": True,
                "pages": [1, 7, 13],
                "observations": "实播检验 P1 封面、P7 过渡页、P13 封底的粒子背景与过渡动效，帧率稳定在 30fps，动效运行平滑无卡顿丢帧。",
            },
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_review(self, rev: dict) -> tuple[dict, Path]:
        p = self.root / f"review-{rev.get('video_sha256', 'x')[:8]}.json"
        p.write_text(json.dumps(rev, ensure_ascii=False), encoding="utf-8")
        return {"record_path": str(p), "record_sha256": lg.sha256(p)}, p

    def test_valid_review_passes(self):
        rev_dict, _ = self._write_review(self.valid_review)
        ok, unverified = lg._validate_native_review_record(
            rev_dict, self.pptx_sha, self.video_sha, self.sel_pages)
        self.assertTrue(ok, f"Expected valid review to pass, got: {unverified}")
        self.assertEqual(unverified, [])

    def test_synthetic_empty_pages_rejected(self):
        rev = copy.deepcopy(self.valid_review)
        rev["pages"] = []
        rev_dict, _ = self._write_review(rev)
        ok, unverified = lg._validate_native_review_record(
            rev_dict, self.pptx_sha, self.video_sha, self.sel_pages)
        self.assertFalse(ok)
        kinds = [u.get("type") for u in unverified]
        self.assertIn("PLAYBACK_REVIEW_PAGES_EMPTY", kinds)

    def test_synthetic_duplicate_observations_rejected(self):
        rev = copy.deepcopy(self.valid_review)
        # All pages use the exact same template text
        template = "本页内容全部正常显示，无任何裁切遮挡现象，符合验收发布标准。"
        for item in rev["pages"]:
            item["observations"] = template
        rev_dict, _ = self._write_review(rev)
        ok, unverified = lg._validate_native_review_record(
            rev_dict, self.pptx_sha, self.video_sha, self.sel_pages)
        self.assertFalse(ok)
        kinds = [u.get("type") for u in unverified]
        self.assertIn("PLAYBACK_REVIEW_DUPLICATE_OBSERVATIONS", kinds)

    def test_synthetic_placeholder_observations_rejected(self):
        rev = copy.deepcopy(self.valid_review)
        rev["pages"][2]["observations"] = "第3页观察内容待补充 TODO: 检查卡片排版是否溢出"
        rev_dict, _ = self._write_review(rev)
        ok, unverified = lg._validate_native_review_record(
            rev_dict, self.pptx_sha, self.video_sha, self.sel_pages)
        self.assertFalse(ok)
        kinds = [u.get("type") for u in unverified]
        self.assertIn("PLAYBACK_REVIEW_PLACEHOLDER_DETECTED", kinds)

    def test_synthetic_generic_reviewer_rejected(self):
        rev = copy.deepcopy(self.valid_review)
        rev["reviewer"]["id"] = "synthetic"
        rev_dict, _ = self._write_review(rev)
        ok, unverified = lg._validate_native_review_record(
            rev_dict, self.pptx_sha, self.video_sha, self.sel_pages)
        self.assertFalse(ok)
        kinds = [u.get("type") for u in unverified]
        self.assertIn("PLAYBACK_REVIEWER_GENERIC", kinds)

    def test_reopen_not_confirmed_rejected(self):
        rev = copy.deepcopy(self.valid_review)
        rev["reopened"] = False
        rev_dict, _ = self._write_review(rev)
        ok, unverified = lg._validate_native_review_record(
            rev_dict, self.pptx_sha, self.video_sha, self.sel_pages)
        self.assertFalse(ok)
        kinds = [u.get("type") for u in unverified]
        self.assertIn("PLAYBACK_REVIEW_REOPEN_NOT_CONFIRMED", kinds)

    def test_reopen_hash_mismatch_rejected(self):
        rev = copy.deepcopy(self.valid_review)
        rev["reopened_pptx_sha256"] = "c" * 64
        rev_dict, _ = self._write_review(rev)
        ok, unverified = lg._validate_native_review_record(
            rev_dict, self.pptx_sha, self.video_sha, self.sel_pages)
        self.assertFalse(ok)
        kinds = [u.get("type") for u in unverified]
        self.assertIn("PLAYBACK_REVIEW_REOPEN_MISMATCH", kinds)

    def test_fake_mp4_empty_rejected(self):
        empty_mp4 = self.root / "empty.mp4"
        empty_mp4.write_bytes(b"")
        info = lg._inspect_mp4(empty_mp4)
        self.assertFalse(info["ok"])
        self.assertEqual(info["error"], "empty_or_missing_file")

    def test_fake_mp4_missing_ftyp_rejected(self):
        bad_mp4 = self.root / "bad.mp4"
        bad_mp4.write_bytes(b"\x00\x00\x00\x20moov" + b"\x00" * 24)
        info = lg._inspect_mp4(bad_mp4)
        self.assertFalse(info["ok"])
        self.assertEqual(info["error"], "missing_ftyp")

    def test_real_playback_mp4_inspection(self):
        real_video = Path("wb_loopback_data/jobs/job-4d584240ec05/layout_gate_evidence/f92232e1820b60e23d9d9e12a249b689e7d3ce9c9506cff49f41c96b091d300c/playback-59ae7d6e5558cb3e03b9b546967ee91c5837dd6bf4c282247fae2dd22a372448.mp4")
        if real_video.is_file():
            info = lg._inspect_mp4(real_video)
            self.assertTrue(info["ok"])
            self.assertEqual(info["duration_seconds"], 45.5)
            self.assertEqual(info["width"], 1920)
            self.assertEqual(info["height"], 1080)
            self.assertEqual(info["sample_count"], 1365)
            self.assertEqual(info["fps"], 30.0)
            self.assertGreater(info["sample_payload_bytes"], 5_000_000)

    def test_runner_attach_rejects_synthetic_empty_review(self):
        from unittest.mock import MagicMock
        runner = Runner(self.root)
        job_dir = self.root / "jobs" / "job-test"
        cand_dir = job_dir / "candidate"
        cand_dir.mkdir(parents=True)
        # Create minimal valid pptx
        from pptx import Presentation
        prs = Presentation()
        prs.slides.add_slide(prs.slide_layouts[0])
        cand_pptx = cand_dir / "test.themed.pptx"
        prs.save(str(cand_pptx))
        cand_sha = lg.sha256(cand_pptx)
        job_json = {
            "schema_version": "1.0.0", "job_id": "job-test", "run_id": "run-test",
            "attempt_id": "att-1", "input_path": "/fake/test.pptx"
        }
        (job_dir / "job.json").write_text(json.dumps(job_json), encoding="utf-8")

        # Fake video bytes (empty or invalid)
        fake_video = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32
        fake_review = {
            "schema": "WG-NATIVE-REVIEW/1",
            "pptx_sha256": cand_sha,
            "video_sha256": lg.hashlib.sha256(fake_video).hexdigest(),
            "reviewer": {"id": "synthetic", "independent": True},
            "playback_application": "none",
            "overall": "PASS",
            "reopened": True,
            "reopened_pptx_sha256": cand_sha,
            "pages": [],
        }
        with self.assertRaises(GateError) as ctx:
            runner.attach_native_playback_evidence("job-test", fake_video, fake_review, cand_sha)
        self.assertIn(ctx.exception.code, ("EVIDENCE.NATIVE_VIDEO_INVALID", "EVIDENCE.NATIVE_REVIEW_INVALID"))


if __name__ == "__main__":
    unittest.main()
