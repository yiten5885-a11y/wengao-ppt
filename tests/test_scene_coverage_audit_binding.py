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
        self.assertIn("page_coverage[2]", _audited_plan_view_drift(audited, final))

    def test_evidence_only_change_regrounded_in_source_is_not_drift(self):
        # _normalize_semantics's own evidence-grounding repair
        # (_repair_page_coverage_evidence) replaces an ungrounded citation
        # with a newly re-grounded one for the *same* scene/no_scene
        # conclusion the audit already reviewed. That must not count as
        # drift when the replacement independently re-verifies against the
        # real source text.
        audited = _plan("teacher_grading")
        final = _plan("teacher_grading")
        final["page_coverage"][0]["evidence"] = ["不同但同样可核验的原文片段"]
        source_pages = [
            {"page": 1, "text": "这是第一页，含有不同但同样可核验的原文片段。"},
            {"page": 2, "text": "统计表 39"},
        ]
        self.assertEqual(
            _audited_plan_view_drift(audited, final, source_pages=source_pages), [])

    def test_evidence_only_change_not_grounded_in_source_is_still_drift(self):
        # The replacement evidence must actually be re-verified against the
        # real source, not merely accepted because only "evidence" differs —
        # otherwise this exception would let unverified text through.
        audited = _plan("teacher_grading")
        final = _plan("teacher_grading")
        final["page_coverage"][0]["evidence"] = ["这段话根本不在原文里"]
        source_pages = [
            {"page": 1, "text": "这是第一页，含有讲评材料。"},
            {"page": 2, "text": "统计表 39"},
        ]
        self.assertIn("page_coverage[1]",
                      _audited_plan_view_drift(audited, final, source_pages=source_pages))


if __name__ == "__main__":
    unittest.main()
