from __future__ import annotations

import unittest

from wengao_ppt.semantic_scenes import _audited_plan_view_drift


def _plan(canonical_id: str, *, subject: str = "teacher") -> dict:
    return {
        "page_coverage": [
            {"page": 1, "scene_keys": ["grading"], "no_scene_reason": None,
             "evidence": ["讲评材料"], "image_scene_keys": ["grading"]},
            {"page": 2, "scene_keys": [], "no_scene_reason": "统计表", "evidence": ["39"]},
        ],
        "scenes": [{
            "scene_key": "grading", "canonical_scene_id": canonical_id,
            "topic": "作文讲评", "depiction_type": "illustrative",
            "illustration_rationale": "r", "subject": subject, "action": "grading",
            "setting": "classroom", "photo_search_terms": {"subject": "teacher"},
            "page_candidates": [{"page": 1, "relevance": 0.9}],
            "visual_query": "added after the audit",
        }],
    }


class CoverageAuditBindingTests(unittest.TestCase):
    def test_canonical_id_normalization_after_audit_is_not_drift(self):
        audited = _plan("teacher_reviewing_student_essays_classroom")
        final = _plan("teacher_reviewing_student_essay_classroom")
        self.assertEqual(_audited_plan_view_drift(audited, final), [])

    def test_semantic_field_change_after_audit_is_drift(self):
        audited = _plan("teacher_grading", subject="teacher")
        final = _plan("teacher_grading", subject="student")
        self.assertEqual(_audited_plan_view_drift(audited, final), ["grading.subject"])

    def test_page_coverage_change_after_audit_is_drift(self):
        audited = _plan("teacher_grading")
        final = _plan("teacher_grading")
        final["page_coverage"][1]["scene_keys"] = ["grading"]
        self.assertIn("page_coverage", _audited_plan_view_drift(audited, final))


if __name__ == "__main__":
    unittest.main()
