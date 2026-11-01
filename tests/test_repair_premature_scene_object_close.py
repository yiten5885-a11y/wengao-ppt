"""Regression test for a real pipeline failure found live on job-mt-2c018d86 /
job-719ddcfdc576 (2026-11-01): a genuine end-to-end material-generation run
through the real workbench API hit SCENE.MODEL_JSON_INVALID on the semantic
scene-planning batch for pages 5-8.

Captured the raw model response (both the first attempt and its automatic
same-model retry — two independent samples, same defect) and found the exact
cause: for the LAST scene object in the batch, the model closed the object
with a stray `}` immediately after its top-level "setting" field — the same
key name as the nested photo_search_terms.{subject,action,setting} triplet a
few lines later, which the model appears to pattern-complete reflexively. It
then skipped label/lighting/composition_camera/style/quality/
photo_search_terms entirely and continued straight into visual_query /
visual_query_alternatives / page_candidates, which the premature close turned
into invalid bare array elements instead of the same object's own fields.

The fix (_repair_premature_scene_object_close in wengao_ppt/scene_model.py)
detects this precisely: the stray `}` sits at the SAME indentation as its own
"setting" field, whereas a legitimate close of the nested photo_search_terms
sub-object sits one indentation level shallower than its own "setting" field.
That exact-indentation-match signal is what distinguishes the two without a
full nesting-depth parse — this test pins both cases down: the real premature
close gets repaired, and a legitimate nested photo_search_terms object sitting
right next to it is left untouched.
"""
import json
import unittest

from wengao_ppt.scene_model import _repair_premature_scene_object_close


# Mirrors the exact shape captured from the real failing responses: scene 1 is
# well-formed (including its own correctly-nested, correctly-closed
# photo_search_terms object, whose "setting" sits one indent level deeper than
# its own closing brace); scene 2 reproduces the real defect.
MALFORMED_BATCH_RESPONSE = """{
  "schema_version": "2.2.0",
  "scenes": [
    {
      "scene_key": "ops_monitoring_dashboard",
      "canonical_scene_id": "engineer_monitoring_ops_dashboard",
      "topic": "t1",
      "depiction_type": "illustrative",
      "illustration_rationale": "r1",
      "subject": "engineer",
      "action": "monitoring dashboard",
      "setting": "data center",
      "label": "l1",
      "lighting": "cool",
      "composition_camera": "medium shot",
      "style": "photo",
      "quality": "hd",
      "photo_search_terms": {
        "subject": "engineer",
        "action": "monitoring dashboard",
        "setting": "data center"
      },
      "visual_query": "engineer monitoring dashboard data center",
      "visual_query_alternatives": ["technician checking servers"],
      "page_candidates": [
        {"page": 6, "relevance": 0.95, "evidence": ["evidence a"]}
      ]
    },
    {
      "scene_key": "developer_coding_community",
      "canonical_scene_id": "developer_coding_workstation",
      "topic": "t2",
      "depiction_type": "illustrative",
      "illustration_rationale": "r2",
      "subject": "developer",
      "action": "writing code on laptop",
      "setting": "office desk"
      },
      "visual_query": "developer writing code on laptop office desk",
      "visual_query_alternatives": ["programmer coding at computer"],
      "page_candidates": [
        {"page": 8, "relevance": 0.9, "evidence": ["evidence b"]}
      ]
    }
  ],
  "page_coverage": [
    {"page": 6, "scene_keys": ["ops_monitoring_dashboard"], "no_scene_reason": null, "evidence": ["evidence a"]},
    {"page": 8, "scene_keys": ["developer_coding_community"], "no_scene_reason": null, "evidence": ["evidence b"]}
  ]
}"""


class TestRepairPrematureSceneObjectClose(unittest.TestCase):

    def test_real_malformed_shape_is_rejected_by_plain_json_loads(self):
        with self.assertRaises(json.JSONDecodeError):
            json.loads(MALFORMED_BATCH_RESPONSE)

    def test_repairs_the_real_premature_close_shape(self):
        repaired = _repair_premature_scene_object_close(MALFORMED_BATCH_RESPONSE)
        self.assertIsNotNone(repaired)
        scene_keys = [s.get("scene_key") for s in repaired["scenes"]]
        self.assertEqual(scene_keys, ["ops_monitoring_dashboard", "developer_coding_community"])

    def test_repaired_scene_regains_its_dangling_fields(self):
        repaired = _repair_premature_scene_object_close(MALFORMED_BATCH_RESPONSE)
        broken_scene = repaired["scenes"][1]
        self.assertEqual(broken_scene["visual_query"],
                         "developer writing code on laptop office desk")
        self.assertEqual(broken_scene["page_candidates"][0]["page"], 8)

    def test_well_formed_nested_photo_search_terms_is_left_untouched(self):
        # The thing that actually makes this repair safe: it must never touch
        # a legitimate close of the nested photo_search_terms sub-object,
        # whose own "setting" field sits one indent level deeper than its
        # closing brace — distinct from the bug's same-indent close.
        repaired = _repair_premature_scene_object_close(MALFORMED_BATCH_RESPONSE)
        good_scene = repaired["scenes"][0]
        self.assertEqual(good_scene["photo_search_terms"],
                         {"subject": "engineer", "action": "monitoring dashboard",
                          "setting": "data center"})

    def test_well_formed_json_is_not_altered_or_misdetected(self):
        well_formed = json.dumps({"scenes": [], "page_coverage": []})
        self.assertIsNone(_repair_premature_scene_object_close(well_formed))

    def test_unrelated_garbage_returns_none(self):
        self.assertIsNone(_repair_premature_scene_object_close("not json at all"))


if __name__ == "__main__":
    unittest.main()
