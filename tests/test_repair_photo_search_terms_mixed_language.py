"""Regression test for a real pipeline failure found on job-mt-89d6a0de /
job-ece7d5fc87fb (2026-11-02), hit immediately after fixing the stray-brace
JSON defect (_repair_premature_scene_object_close): a genuine end-to-end
material-generation run reached one scene whose photo_search_terms had been
dropped by that earlier bug — "developer_coding_community", a plain office
scene (subject="开发者", action="writing code on laptop",
setting="office desk") grounded in "开发者社区注册开发者数量达到 27 万人，
上传的模板数量为 5400 个。" — and the SAME-model, source-scoped repair for it
(_repair_invalid_photo_search_terms) returned an empty {"repairs": []} on
both of its attempts, hard-failing the whole 15-page deck with
SCENE.PHOTO_SEARCH_ANCHORS_INVALID even though the scene is entirely mundane.

Reproduced directly against the live local model: the scene's own
action/setting fields were themselves already short English text (a
leftover of the earlier planning bug, which mixes languages across a
scene's fields), while subject was still Chinese. The repair prompt's
"translate the subject/action/place" instruction never told the model what
to do when part of that input is already English, and feeding it exactly
this mixed-language shape reliably reproduced the refusal; adding explicit
guidance for already-English fields (plus stating these are ordinary,
non-sensitive scenes) reliably produced a valid repair instead — verified
live, twice, before writing this test.

This test can't call the real local model, so it pins the two things that
are actually under this codebase's control: the prompt now contains the new
guidance, and the function's own repair/validation plumbing correctly
accepts a valid repair response shaped like what the live model now
reliably returns for this exact scene.
"""
import unittest

from wengao_ppt.scene_model import _repair_invalid_photo_search_terms


MIXED_LANGUAGE_SCENE = {
    "scene_key": "developer_coding_community",
    "canonical_scene_id": "developer_coding_workstation",
    "subject": "开发者",
    "action": "writing code on laptop",
    "setting": "office desk",
    "page_candidates": [
        {"page": 8, "relevance": 0.9,
         "evidence": ["开发者社区注册开发者数量达到 27 万人，上传的模板数量为 5400 个。"]},
    ],
}
PAGES = [{"page": 8, "text": "开发者社区注册开发者数量达到 27 万人，上传的模板数量为 5400 个。"}]


def _plan_with(scene):
    return {"scenes": [dict(scene)]}


class TestRepairPhotoSearchTermsMixedLanguage(unittest.TestCase):

    def test_prompt_explains_how_to_handle_already_english_fields(self):
        captured = {}

        def stub_call(model, prompt):
            captured["prompt"] = prompt
            return {"response": '{"repairs":[{"scene_key":"developer_coding_community",'
                                 '"photo_search_terms":{"subject":"developer",'
                                 '"action":"typing code","setting":"office desk"}}]}',
                   "model": model, "done_reason": "stop"}

        _repair_invalid_photo_search_terms(
            _plan_with(MIXED_LANGUAGE_SCENE), PAGES, model="test-model", call=stub_call)
        prompt = captured["prompt"]
        self.assertIn("already a short neutral English phrase", prompt)
        self.assertIn("no safety concern", prompt)

    def test_valid_repair_response_is_accepted_and_applied(self):
        def stub_call(model, prompt):
            return {"response": '{"repairs":[{"scene_key":"developer_coding_community",'
                                 '"photo_search_terms":{"subject":"developer",'
                                 '"action":"typing code","setting":"office desk"}}]}',
                   "model": model, "done_reason": "stop"}

        plan = _plan_with(MIXED_LANGUAGE_SCENE)
        result = _repair_invalid_photo_search_terms(
            plan, PAGES, model="test-model", call=stub_call)
        self.assertEqual(result["status"], "APPLIED_AND_STRICTLY_VALIDATED")
        self.assertEqual(plan["scenes"][0]["photo_search_terms"],
                         {"subject": "developer", "action": "typing code",
                          "setting": "office desk"})

    def test_two_consecutive_empty_repairs_still_raises_as_before(self):
        # Preserves the existing, intentional hard-block: if the model
        # genuinely declines twice, the caller must still block rather than
        # invent a scene — this test's job is only to confirm the prompt
        # change didn't accidentally weaken that gate.
        from wengao_ppt.scene_model import SceneModelError

        def stub_call(model, prompt):
            return {"response": '{"repairs":[]}', "model": model, "done_reason": "stop"}

        plan = _plan_with(MIXED_LANGUAGE_SCENE)
        with self.assertRaises(SceneModelError) as ctx:
            _repair_invalid_photo_search_terms(plan, PAGES, model="test-model", call=stub_call)
        self.assertEqual(ctx.exception.code, "SCENE.PHOTO_SEARCH_ANCHORS_INVALID")


if __name__ == "__main__":
    unittest.main()
