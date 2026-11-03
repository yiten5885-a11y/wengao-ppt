from __future__ import annotations

import unittest

from wengao_ppt.semantic_scenes import (
    CAPACITY_UNRESOLVED_REASONS,
    split_unresolved_scenes,
)


class SplitUnresolvedScenesTests(unittest.TestCase):
    def test_capacity_reasons_are_reported_not_blocking(self):
        rows = [{"scene_key": "a", "reason": "SCENE.NO_SAFE_PHOTO_AREA"},
                {"scene_key": "b", "reason": "SCENE.PAGE_SCENE_CAPACITY_EXCEEDED"}]
        blocking, capacity = split_unresolved_scenes(rows)
        self.assertEqual(blocking, [])
        self.assertEqual(capacity, rows)

    def test_integrity_reasons_still_block(self):
        for reason in ("SCENE.LICENSE_METADATA_MISSING",
                       "SCENE.CONTENT_SAFETY_METADATA_BLOCK",
                       "SCENE.PHOTO_REUSED_OR_UNVERIFIED",
                       "SCENE.NEAR_DUPLICATE_PHOTO",
                       "SCENE.IMAGE_DIMENSIONS_INVALID",
                       "SCENE.VISUAL_REVIEW_UNAVAILABLE",
                       "SCENE.VISUAL_CONTENT_MISMATCH",
                       "SCENE.PHOTO_STYLE_MISMATCH",
                       "SCENE.IMAGE_CONTEXT_UNVERIFIED",
                       "SCENE.IMAGE_RELEVANCE_UNVERIFIED",
                       "SCENE.NO_SCENES_PLANNED"):
            with self.subTest(reason=reason):
                blocking, capacity = split_unresolved_scenes([{"reason": reason}])
                self.assertEqual(capacity, [])
                self.assertEqual(blocking, [{"reason": reason}])

    def test_mixed_rows_keep_the_blocking_ones(self):
        rows = [{"scene_key": "a", "reason": "SCENE.NO_SAFE_PHOTO_AREA"},
                {"scene_key": "b", "reason": "SCENE.LICENSE_METADATA_MISSING"}]
        blocking, capacity = split_unresolved_scenes(rows)
        self.assertEqual([r["scene_key"] for r in blocking], ["b"])
        self.assertEqual([r["scene_key"] for r in capacity], ["a"])

    def test_unknown_reason_is_treated_as_blocking(self):
        blocking, capacity = split_unresolved_scenes([{"reason": "SCENE.SOMETHING_NEW"}])
        self.assertEqual(capacity, [])
        self.assertEqual(len(blocking), 1)

    def test_empty_input(self):
        self.assertEqual(split_unresolved_scenes(None), ([], []))
        self.assertEqual(split_unresolved_scenes([]), ([], []))

    def test_capacity_set_is_geometry_plus_explicit_photo_skip(self):
        # 2026-10-29 user decision: with the text-card fallback on, a scene for
        # which no photo passed visual review is skipped (recorded, not faked).
        # Only that one explicit reason joins the geometry reasons; raw "no
        # photo" reasons still block on their own.
        self.assertEqual(CAPACITY_UNRESOLVED_REASONS,
                         {"SCENE.NO_SAFE_PHOTO_AREA",
                          "SCENE.PAGE_SCENE_CAPACITY_EXCEEDED",
                          "SCENE.NO_VERIFIED_PHOTO_SKIPPED"})
        for raw in ("SCENE.NO_LICENSED_PHOTO", "SCENE.PROVIDER_ROWS_INELIGIBLE",
                    "SCENE.GEN_IMAGE_UNAVAILABLE"):
            with self.subTest(raw=raw):
                blocking, capacity = split_unresolved_scenes([{"reason": raw}])
                self.assertEqual((len(blocking), capacity), (1, []))


if __name__ == "__main__":
    unittest.main()
