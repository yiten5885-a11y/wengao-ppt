from __future__ import annotations

import hashlib
import io
import json
import unittest

from PIL import Image

from wengao_ppt import scene_model
from wengao_ppt.scene_model import (
    review_semantic_scene_image,
    scene_image_review_prompt,
)

SCENE = {"scene_key": "teacher_grading", "subject": "teacher",
         "action": "grading student essays", "setting": "classroom",
         "depiction_type": "illustrative", "illustration_rationale": "r"}

PASSING = json.dumps({"scene_match": True, "subject_visible": True,
                      "action_visible": True, "setting_visible": True,
                      "photographic": True, "confidence": "high",
                      "reason_code": "NONE"})


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (128, 128, 128)).save(buf, format="PNG")
    return buf.getvalue()


class ReviewPromptBindingTests(unittest.TestCase):
    def setUp(self):
        self.image = _png_bytes()
        self.base_sha = hashlib.sha256(
            scene_image_review_prompt(SCENE).encode("utf-8")).hexdigest()
        self._real_call = scene_model.call_local_vision_model
        self.addCleanup(setattr, scene_model, "call_local_vision_model", self._real_call)

    def _review(self, replies):
        seen = []

        def fake(model, prompt, image_bytes, timeout_s=None):
            seen.append(prompt)
            return {"response": replies[len(seen) - 1], "model": model,
                    "response_sha256": "0" * 64}

        scene_model.call_local_vision_model = fake
        return review_semantic_scene_image("m", SCENE, self.image), seen

    def test_clean_reply_binds_the_base_prompt(self):
        result, seen = self._review([PASSING])
        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(len(seen), 1)
        self.assertEqual(result["prompt_sha256"], self.base_sha)
        self.assertEqual(result["format_repair_retries"], 0)
        self.assertEqual(result["answered_prompt_sha256"], self.base_sha)

    def test_format_repair_retry_still_binds_the_base_prompt(self):
        # A fenced first reply forces the format-repair retry. The recorded
        # prompt identity must stay the base prompt, or the export gate replays
        # a different prompt and blocks an image the model actually passed.
        fenced = "Sure:\n```json\n" + PASSING + "\n```\nHope this helps!"
        result, seen = self._review([fenced, PASSING])
        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(len(seen), 2)
        self.assertEqual(result["prompt_sha256"], self.base_sha)

    def test_the_retry_is_recorded_not_hidden(self):
        fenced = "Sure:\n```json\n" + PASSING + "\n```\nHope this helps!"
        result, seen = self._review([fenced, PASSING])
        self.assertEqual(result["format_repair_retries"], 1)
        self.assertNotEqual(result["answered_prompt_sha256"], self.base_sha)
        self.assertEqual(result["answered_prompt_sha256"],
                         hashlib.sha256(seen[1].encode("utf-8")).hexdigest())

    def test_blocked_verdict_also_binds_the_base_prompt(self):
        blocked = json.dumps({"scene_match": False, "subject_visible": False,
                              "action_visible": False, "setting_visible": False,
                              "photographic": True, "confidence": "high",
                              "reason_code": "SUBJECT_NOT_VISIBLE"})
        result, _ = self._review([blocked])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["prompt_sha256"], self.base_sha)


if __name__ == "__main__":
    unittest.main()
