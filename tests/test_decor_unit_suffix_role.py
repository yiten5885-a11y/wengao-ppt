"""Regression test for a role-misclassification bug found live on
job-d239567879df, page 4.

A stat display styled as a huge decorative number ("30", 99pt, role
"decor") plus a normal-sized unit-suffix word beside it ("倍", 24pt, split
into its own shape) left "倍" classified as plain "body" text — alone, its
own font is far under is_decor's 60pt floor. Because body-role shapes in
the upper half of the page compete to become the inferred page title by
"largest font size," "倍" (24pt) out-scored the real page title (already
shrunk to 11pt by this page's own content-density pressure), stealing the
title role outright and also tripping HG08 ORPHAN_CHAR_LINE (a single
character alone on its line, title role isn't exempted from that rule).

is_decor_unit_suffix (wengao_ppt/type_scale.py) recognizes a short word
immediately beside (same vertical band, small horizontal gap) an
independently-decor shape as part of that same decorative display.
"""
import unittest

from wengao_ppt.type_scale import is_decor_unit_suffix


def _box(left, top, width, height, text, decor=False):
    return {"left": left, "top": top, "width": width, "height": height,
           "text": text, "decor": decor}


class TestDecorUnitSuffix(unittest.TestCase):

    def test_unit_word_beside_decor_number_is_recognized(self):
        # Real geometry captured from job-d239567879df page 4.
        boxes = [
            _box(0.625, 2.604, 1.823, 1.845, "30", decor=True),
            _box(2.552, 3.656, 0.633, 0.729, "倍"),
        ]
        self.assertTrue(is_decor_unit_suffix(1, boxes))

    def test_unrelated_short_word_far_from_any_decor_shape_is_not(self):
        boxes = [
            _box(0.625, 2.604, 1.823, 1.845, "30", decor=True),
            _box(9.0, 0.5, 0.6, 0.3, "万"),
        ]
        self.assertFalse(is_decor_unit_suffix(1, boxes))

    def test_long_text_never_qualifies_even_if_adjacent(self):
        boxes = [
            _box(0.625, 2.604, 1.823, 1.845, "30", decor=True),
            _box(2.552, 3.656, 3.0, 0.729, "这是一段比较长的说明文字"),
        ]
        self.assertFalse(is_decor_unit_suffix(1, boxes))

    def test_vertically_disjoint_word_is_not_a_suffix(self):
        boxes = [
            _box(0.625, 2.604, 1.823, 1.845, "30", decor=True),
            _box(2.552, 5.0, 0.633, 0.3, "倍"),  # well below the decor shape
        ]
        self.assertFalse(is_decor_unit_suffix(1, boxes))


if __name__ == "__main__":
    unittest.main()
