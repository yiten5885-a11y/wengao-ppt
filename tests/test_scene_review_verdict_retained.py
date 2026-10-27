from __future__ import annotations

import hashlib
import json
import unittest

from wengao_ppt import semantic_scenes
from wengao_ppt.scene_images import SceneImageError
from wengao_ppt.scene_model import (
    SCENE_REVIEW_VERDICT_FIELDS,
    scene_image_review_prompt,
    scene_review_payload_sha256,
    scene_review_result_sha256,
    scene_review_verdict_digest,
    scene_visual_identity_sha256,
)

BLOCKED_REVIEW = {
    "schema_version": "WG-LOCAL-SCENE-IMAGE-REVIEW/1",
    "status": "BLOCKED",
    "image_sha256": "ab" * 32,
    "visual_input_sha256": "cd" * 32,
    "scene_identity_sha256": "ef" * 32,
    "prompt_sha256": "11" * 32,
    "review_result_sha256": "22" * 32,
    "answered_prompt_sha256": "33" * 32,
    "model": "gemini-3.8-flash",
    "response_sha256": "44" * 32,
    "elapsed_seconds": 1.5,
    "format_repair_retries": 1,
    "scene_match": False,
    "subject_visible": True,
    "action_visible": False,
    "setting_visible": False,
    "photographic": True,
    "confidence": "high",
    "reason_code": "ACTION_NOT_VISIBLE",
}


class VerdictDigestTests(unittest.TestCase):
    def test_digest_keeps_every_field_needed_to_explain_a_rejection(self):
        digest = scene_review_verdict_digest(BLOCKED_REVIEW)
        self.assertEqual(digest["status"], "BLOCKED")
        self.assertEqual(digest["reason_code"], "ACTION_NOT_VISIBLE")
        self.assertEqual(digest["confidence"], "high")
        for field in ("scene_match", "subject_visible", "action_visible",
                      "setting_visible", "photographic"):
            self.assertEqual(digest[field], BLOCKED_REVIEW[field], field)
        self.assertEqual(digest["model"], "gemini-3.8-flash")
        self.assertEqual(digest["format_repair_retries"], 1)
        self.assertEqual(digest["image_sha256"], "ab" * 32)

    def test_digest_is_metadata_only(self):
        # The job record is shared; it must not accumulate prompt or response
        # identities beyond the image the verdict is about.
        digest = scene_review_verdict_digest(BLOCKED_REVIEW)
        for leaked in ("prompt_sha256", "answered_prompt_sha256",
                       "review_result_sha256", "response_sha256",
                       "scene_identity_sha256", "visual_input_sha256"):
            self.assertNotIn(leaked, digest, leaked)

    def test_digest_handles_missing_or_partial_reviews(self):
        self.assertIsNone(scene_review_verdict_digest(None))
        self.assertIsNone(scene_review_verdict_digest("not a dict"))
        self.assertIsNone(scene_review_verdict_digest({}))
        self.assertEqual(scene_review_verdict_digest({"status": "BLOCKED"}),
                         {"status": "BLOCKED"})

    def test_digest_fields_are_declared(self):
        self.assertIn("reason_code", SCENE_REVIEW_VERDICT_FIELDS)
        self.assertIn("confidence", SCENE_REVIEW_VERDICT_FIELDS)


class SceneImageErrorDiagnosticsTests(unittest.TestCase):
    def test_error_carries_the_verdict(self):
        exc = SceneImageError("SCENE.VISUAL_CONTENT_MISMATCH", "blocked",
                              diagnostics={"visual_review":
                                           scene_review_verdict_digest(BLOCKED_REVIEW)})
        record = exc.as_record()
        self.assertEqual(record["code"], "SCENE.VISUAL_CONTENT_MISMATCH")
        self.assertEqual(
            record["diagnostics"]["visual_review"]["reason_code"],
            "ACTION_NOT_VISIBLE")

    def test_error_without_diagnostics_is_unchanged(self):
        exc = SceneImageError("SCENE.NO_LICENSED_PHOTO", "none found")
        self.assertEqual(exc.as_record(), {"code": "SCENE.NO_LICENSED_PHOTO",
                                           "detail": "none found"})
        self.assertEqual(exc.diagnostics, {})


class RejectedVerdictReachesThePlanTests(unittest.TestCase):
    """A rejected image is discarded; its verdict must survive in the plan."""

    def setUp(self):
        import test_semantic_scenes as helpers
        self.helpers = helpers
        self.case = helpers.SemanticSceneTests(
            "test_full_slide_card_blocks_photo_placement")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    @staticmethod
    def _blocked_review(model, scene, image_bytes):
        result = {"scene_match": False, "subject_visible": True,
                  "action_visible": False, "setting_visible": False,
                  "photographic": True, "confidence": "high",
                  "reason_code": "ACTION_NOT_VISIBLE"}
        return {"schema_version": "WG-LOCAL-SCENE-IMAGE-REVIEW/1",
                "status": "BLOCKED",
                "image_sha256": hashlib.sha256(image_bytes).hexdigest(),
                "visual_input_sha256": scene_review_payload_sha256(image_bytes),
                "scene_identity_sha256": scene_visual_identity_sha256(scene),
                "prompt_sha256": hashlib.sha256(
                    scene_image_review_prompt(scene).encode("utf-8")).hexdigest(),
                "review_result_sha256": scene_review_result_sha256(result),
                "model": model, "response_sha256": "0" * 64,
                "elapsed_seconds": 0.01, "format_repair_retries": 0, **result}

    def _model_generated_plan(self):
        helpers = self.helpers
        pages = semantic_scenes._source_pages(self.case.source)
        resolution = helpers.semantic_fixture()
        audit = {"schema_version": "1.0.0", "review_status": "NO_GAPS",
                 "page_reviews": [
                     {"page": row["page"], "coverage_status": "SCENE_COVERED",
                      "scene_keys": row["scene_keys"],
                      "reason": "已逐页复核并覆盖该页所有具体场景语义。",
                      "evidence": [pages[row["page"] - 1]["text"].split("\n")[0]]}
                     for row in resolution["page_coverage"]],
                 "missing_scenes": [], "merged_distinct_scenes": []}
        calls = []

        def model_call(model, prompt):
            calls.append(prompt)
            payload = resolution if len(calls) == 1 else audit
            return {"response": json.dumps(payload, ensure_ascii=False),
                    "model": "fixture-local-model", "elapsed_seconds": 0.01}

        return semantic_scenes.build_plan(
            self.case.source, provider=helpers.FakePhotoProvider(self.case.images),
            model_call=model_call, visual_review_call=self._blocked_review)

    def test_rejected_scene_keeps_its_verdict(self):
        plan = self._model_generated_plan()
        rejected = [r for r in plan["unresolved_scenes"]
                    if r["reason"] == "SCENE.VISUAL_CONTENT_MISMATCH"]
        self.assertTrue(rejected, "expected at least one rejected scene")
        for row in rejected:
            verdict = row.get("visual_review")
            self.assertIsNotNone(verdict, row.get("scene_key"))
            self.assertEqual(verdict["status"], "BLOCKED")
            self.assertEqual(verdict["reason_code"], "ACTION_NOT_VISIBLE")
            self.assertEqual(verdict["confidence"], "high")
            self.assertFalse(verdict["action_visible"])
            self.assertTrue(verdict["subject_visible"])

    def test_the_verdict_survives_a_json_round_trip(self):
        # The plan is what actually lands on disk as scene_visual_plan.json.
        plan = self._model_generated_plan()
        reloaded = json.loads(json.dumps(plan, ensure_ascii=False))
        rejected = [r for r in reloaded["unresolved_scenes"]
                    if r["reason"] == "SCENE.VISUAL_CONTENT_MISMATCH"]
        self.assertTrue(rejected)
        self.assertEqual(rejected[0]["visual_review"]["reason_code"],
                         "ACTION_NOT_VISIBLE")


if __name__ == "__main__":
    unittest.main()
