"""Regression test for a real pipeline failure found on job-mt-91a41bab /
job-8bc4208b778f (2026-11-02), hit immediately after fixing the stray-brace
JSON defect and the mixed-language photo-anchor repair: a genuine end-to-end
material-generation run reached the same affected scene
("developer_coding_community") and hard-failed with
SCENE.IMAGE_PROMPT_SCHEMA_MISSING ("场景图提示缺少必填字段：lighting,
composition_camera, style, quality。") — the model never emitted these
fields for this one scene (the same root symptom as the stray-brace defect
and the missing photo_search_terms), and there was no repair path for them
at all, unlike photo_search_terms.

Two real fixes landed for this:

1. "lighting" should never have been in the hard-required list:
   resolve_scene_lighting() (wengao_ppt/scene_images.py) already derives a
   scene-accurate lighting value from environment/action alone, ending in a
   safe "balanced ambient room lighting" default, even when no preferred
   lighting is given. Confirmed directly: resolve_scene_lighting("office
   desk", "writing code on laptop", "") returns "warm focused desk lamp
   lighting" with no model call at all.

2. composition_camera/style/quality are genuinely missing (no deterministic
   resolver exists for them), so _repair_missing_image_prompt_fields adds a
   same-model, source-scoped, strictly-validated repair — mirroring
   _repair_invalid_photo_search_terms's existing, proven pattern. Verified
   live against the real local model with the exact scene data before
   writing this test: it reliably returns valid, sensible values (e.g.
   composition_camera="medium shot, eye-level angle",
   style="realistic photography, natural lighting",
   quality="high-definition, sharp focus, 8k").

A third real failure followed on job-mt-f7e70608/job-20b57848df6d: with both
fixes above in place, the job progressed much further (well into rendering)
before hard-failing with SCENE.MODEL_SCHEMA_INVALID ("场景
developer_coding_community 缺少主体、动作/关系、环境、标签或照片查询。") — a
SEPARATE validation in semantic_scenes.py's _normalize_semantics that also
requires "label" (a short display caption, not a search anchor), which
_repair_missing_image_prompt_fields never covered. "label" was folded into
the same repair, since every real well-formed scene in this codebase writes
label/composition_camera/style/quality in the source language (e.g.
"智能运维监控"/"中景"/"写实摄影"/"高清真实质感"), unlike photo_search_terms
and visual_query which must stay neutral English search anchors — so the
repair prompt now asks for source-language values, matching the scene's own
subject field, instead of the English wording used by the first two fixes.
"""
import unittest

from wengao_ppt.scene_images import resolve_scene_lighting
from wengao_ppt.scene_model import _repair_missing_image_prompt_fields, SceneModelError


REAL_AFFECTED_SCENE = {
    "scene_key": "developer_coding_community",
    "canonical_scene_id": "developer_coding_workstation",
    "subject": "开发者",
    "action": "writing code on laptop",
    "setting": "office desk",
}


def _plan_with(scene):
    return {"scenes": [dict(scene)]}


class TestResolveSceneLightingNeedsNoModelCall(unittest.TestCase):

    def test_empty_preferred_lighting_still_resolves_a_sensible_value(self):
        self.assertEqual(
            resolve_scene_lighting("office desk", "writing code on laptop", ""),
            "warm focused desk lamp lighting")

    def test_missing_preferred_lighting_falls_back_to_a_safe_default(self):
        self.assertEqual(resolve_scene_lighting("", "", None),
                         "balanced ambient room lighting")


class TestRepairMissingImagePromptFields(unittest.TestCase):

    def test_no_repair_needed_when_fields_already_present(self):
        complete_scene = dict(REAL_AFFECTED_SCENE, label="开发者编程",
                              composition_camera="中景", style="写实摄影", quality="高清真实质感")
        result = _repair_missing_image_prompt_fields(
            _plan_with(complete_scene), model="test-model",
            call=lambda model, prompt: self.fail("should not call the model"))
        self.assertIsNone(result)

    def test_valid_repair_response_is_accepted_and_applied(self):
        def stub_call(model, prompt):
            return {"response": '{"repairs":[{"scene_key":"developer_coding_community",'
                                 '"label":"开发者编程",'
                                 '"composition_camera":"中景",'
                                 '"style":"写实摄影",'
                                 '"quality":"高清真实质感"}]}',
                   "model": model, "done_reason": "stop"}

        plan = _plan_with(REAL_AFFECTED_SCENE)
        result = _repair_missing_image_prompt_fields(plan, model="test-model", call=stub_call)
        self.assertEqual(result["status"], "APPLIED_AND_VALIDATED")
        scene = plan["scenes"][0]
        self.assertEqual(scene["label"], "开发者编程")
        self.assertEqual(scene["composition_camera"], "中景")
        self.assertEqual(scene["style"], "写实摄影")
        self.assertEqual(scene["quality"], "高清真实质感")

    def test_two_consecutive_empty_repairs_raises(self):
        def stub_call(model, prompt):
            return {"response": '{"repairs":[]}', "model": model, "done_reason": "stop"}

        with self.assertRaises(SceneModelError) as ctx:
            _repair_missing_image_prompt_fields(
                _plan_with(REAL_AFFECTED_SCENE), model="test-model", call=stub_call)
        self.assertEqual(ctx.exception.code, "SCENE.IMAGE_PROMPT_SCHEMA_MISSING")

    def test_lighting_is_not_in_the_repair_scope(self):
        # Confirms lighting stays out of this repair's field set: it is
        # handled deterministically by resolve_scene_lighting instead, with
        # no model call at all.
        from wengao_ppt.scene_model import _IMAGE_PROMPT_REPAIR_FIELDS
        self.assertNotIn("lighting", _IMAGE_PROMPT_REPAIR_FIELDS)

    def test_label_is_in_the_repair_scope(self):
        # Real failure on job-mt-f7e70608/job-20b57848df6d: semantic_scenes.py
        # separately requires "label", which composition_camera/style/quality
        # alone never covered.
        from wengao_ppt.scene_model import _IMAGE_PROMPT_REPAIR_FIELDS
        self.assertIn("label", _IMAGE_PROMPT_REPAIR_FIELDS)

    def test_label_only_missing_still_triggers_repair(self):
        scene_missing_only_label = dict(
            REAL_AFFECTED_SCENE, composition_camera="中景", style="写实摄影",
            quality="高清真实质感")

        def stub_call(model, prompt):
            self.assertIn("label", prompt)
            return {"response": '{"repairs":[{"scene_key":"developer_coding_community",'
                                 '"label":"开发者编程",'
                                 '"composition_camera":"中景",'
                                 '"style":"写实摄影",'
                                 '"quality":"高清真实质感"}]}',
                   "model": model, "done_reason": "stop"}

        plan = _plan_with(scene_missing_only_label)
        result = _repair_missing_image_prompt_fields(plan, model="test-model", call=stub_call)
        self.assertEqual(result["status"], "APPLIED_AND_VALIDATED")
        self.assertEqual(plan["scenes"][0]["label"], "开发者编程")

    def test_prompt_asks_for_source_language_not_english(self):
        captured = {}

        def stub_call(model, prompt):
            captured["prompt"] = prompt
            return {"response": '{"repairs":[{"scene_key":"developer_coding_community",'
                                 '"label":"开发者编程","composition_camera":"中景",'
                                 '"style":"写实摄影","quality":"高清真实质感"}]}',
                   "model": model, "done_reason": "stop"}

        _repair_missing_image_prompt_fields(
            _plan_with(REAL_AFFECTED_SCENE), model="test-model", call=stub_call)
        self.assertIn("not English", captured["prompt"])


if __name__ == "__main__":
    unittest.main()
