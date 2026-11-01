"""Regression test for a false positive found live on job-d239567879df.

assign_chars (scripts/layout_gate.py) maps each pdfium-rendered glyph to
its owning shape. Page 1 has a large rounded-rectangle text-card shape
(its own text contains "年" three times) sitting next to a small date
label "2026 年 11 月" in its bottom-right corner. A rounded shape's
rectangular bounding box is larger than its visible rounded outline, so
the card's bbox geometrically overlaps the date label's own small bbox
even though their rendered ink never touches (confirmed against the
actual rendered page). Two real failure modes were found from this one
overlap:

1. The "年" glyph's center falls inside BOTH shapes' bboxes, and both
   shapes' own text contains "年", so both got the old scoring's +1e9
   "character present" bonus — a tie the old code broke by iteration
   order (the card, listed first, always won).
2. The "月" glyph's center falls inside NEITHER shape's bbox (it is a
   hair outside both, by sub-pixel rendering/kerning), so matching fell
   to the "largest overlap area" fallback — the card and the date label
   happen to share the exact same right edge (both flush against the
   canvas margin), so the plain overlap area was an exact tie, again
   broken by iteration order in the card's favor.

Both were wrongly assigned to the card, which doesn't contain "月" at
all, producing a CHAR_MISSING/ORPHAN_CHAR_LINE pair on a page with no
real content defect.
"""
import unittest

from scripts.layout_gate import assign_chars


def _shape(name, text, left, top, right, bottom):
    return {"name": name, "text": text, "left": left, "top": top,
           "right": right, "bottom": bottom}


class TestAssignCharsRoundedCardTiebreak(unittest.TestCase):

    def test_inside_both_boxes_prefers_shape_actually_containing_the_text(self):
        # Mirrors job-d239567879df page 1 exactly: a big rounded card whose
        # bbox geometrically contains a small date-label shape's area, and
        # whose own text also happens to contain the ambiguous character.
        card = _shape("WENGAO_TEXTCARD|elderly_retirement_life",
                      "安度晚年\n老年人 · 安度晚年 · 社区/家庭",
                      7.4, 0.412, 12.933, 7.087)
        date_label = _shape("", "2026 年 11 月", 11.812, 6.698, 12.933, 7.132)
        shapes = [card, date_label]
        # "年" glyph center sits inside both boxes.
        chars = [{"ch": "年", "x0": 12.30, "x1": 12.40, "y0": 6.75, "y1": 6.90}]
        mapping, unmatched = assign_chars(chars, shapes)
        self.assertEqual(unmatched, [])
        self.assertEqual(mapping[0], 1)

    def test_tied_overlap_area_prefers_shape_whose_own_text_has_the_char(self):
        # Mirrors the "月" case: the glyph center is outside both boxes
        # (just past the shared right edge), so matching falls to the
        # overlap-area fallback, where both shapes' overlap area with the
        # glyph box is identical because they share the same right edge.
        card = _shape("WENGAO_TEXTCARD|elderly_retirement_life",
                      "安度晚年\n老年人 · 安度晚年 · 社区/家庭",
                      7.4, 0.412, 12.933, 7.087)
        date_label = _shape("", "2026 年 11 月", 11.812, 6.698, 12.933, 7.132)
        shapes = [card, date_label]
        chars = [{"ch": "月", "x0": 12.87, "x1": 13.00, "y0": 6.74, "y1": 6.90}]
        mapping, unmatched = assign_chars(chars, shapes)
        self.assertEqual(unmatched, [])
        self.assertEqual(mapping[0], 1)

    def test_unambiguous_containment_is_unaffected(self):
        a = _shape("A", "hello", 0.0, 0.0, 2.0, 1.0)
        b = _shape("B", "world", 3.0, 0.0, 5.0, 1.0)
        chars = [{"ch": "h", "x0": 0.1, "x1": 0.3, "y0": 0.1, "y1": 0.5}]
        mapping, unmatched = assign_chars(chars, [a, b])
        self.assertEqual(unmatched, [])
        self.assertEqual(mapping[0], 0)


if __name__ == "__main__":
    unittest.main()
