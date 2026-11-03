"""Theme color better than the original but still below the bar (2026-10-29).

job-3afdd5d88bd9 slide 9: theme 3.14 vs original 3.04, both under the 4.5
normal-text bar. The classifier called that "UNSAFE: the conversion makes it
worse" and blocked the deck, while actually keeping the (worse) original color.
"""
import unittest

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Inches, Pt

from wengao_ppt import backdrop as bd

WHITE = (255, 255, 255)


def _plan(original, theme):
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    tb = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
    tb.text_frame.text = "正文"
    run = tb.text_frame.paragraphs[0].runs[0]
    run.font.size = Pt(12)
    run.font.color.rgb = RGBColor(*original)
    info = bd.scan_slide(slide, prs.slide_width, prs.slide_height, 1)
    return bd.plan_slide(info, (WHITE,), {"body": theme}, lambda t: "body",
                         orig_color_of=lambda t: original)


class ImprovedBelowThresholdTests(unittest.TestCase):
    def test_better_theme_color_is_improved_not_unsafe(self):
        row = _plan(original=(0x94,) * 3, theme=(0x90,) * 3)["rows"][0]  # 3.03 -> 3.19
        self.assertLess(row["ratio"], row["required"])
        self.assertGreater(row["ratio"], row["orig_ratio"])
        self.assertEqual(row["status"], "IMPROVED_BELOW")

    def test_worse_or_equal_theme_color_keeps_original(self):
        row = _plan(original=(0x90,) * 3, theme=(0x94,) * 3)["rows"][0]  # 3.19 -> 3.03
        self.assertEqual(row["status"], "PREEXISTING_LOW")

    def test_unsafe_is_never_produced(self):
        for orig, theme in (((0x94,) * 3, (0x90,) * 3), ((0x90,) * 3, (0x94,) * 3)):
            with self.subTest(orig=orig, theme=theme):
                self.assertNotEqual(_plan(orig, theme)["rows"][0]["status"], "UNSAFE")


class AutoContrastAdjustTests(unittest.TestCase):
    """Below-bar text is fixed by the agent instead of becoming a REVIEW item."""

    def test_dark_backdrop_gets_lighter_shade_that_clears_the_bar(self):
        from wengao_ppt import theme
        row = {"status": "IMPROVED_BELOW", "backdrop_rgb": "#142935",
               "required": 4.5, "ratio": 3.14, "orig_ratio": 3.04}
        theme._auto_contrast_adjust(row, "5A5A5A")
        self.assertEqual(row["status"], "AUTO_ADJUSTED")
        self.assertGreaterEqual(row["ratio"], 4.5)
        self.assertGreaterEqual(bd.contrast_ratio(bd.hex_to_rgb(row["auto_color"]),
                                                  bd.hex_to_rgb("#142935")), 4.5)
        self.assertEqual(row["auto_from"]["theme_ratio"], 3.14)

    def test_light_backdrop_gets_darker_shade(self):
        from wengao_ppt import theme
        row = {"status": "PREEXISTING_LOW", "backdrop_rgb": "#FFFFFF",
               "required": 4.5, "ratio": 3.0, "orig_ratio": 3.68}
        theme._auto_contrast_adjust(row, "959595")
        self.assertEqual(row["status"], "AUTO_ADJUSTED")
        self.assertLess(sum(bd.hex_to_rgb(row["auto_color"])), 3 * 0x95)

    def test_unknown_backdrop_and_passing_rows_are_untouched(self):
        from wengao_ppt import theme
        for row in ({"status": "UNKNOWN", "backdrop_rgb": None, "required": 4.5},
                    {"status": "OK", "backdrop_rgb": "#FFFFFF", "required": 4.5, "ratio": 7.0}):
            before = dict(row)
            theme._auto_contrast_adjust(row, "959595")
            self.assertEqual(row, before)


class ClampOversizedTests(unittest.TestCase):
    """After the visual upgrade: shrink over-max runs only, never move boxes."""

    def test_only_shrinks_and_keeps_geometry(self):
        from wengao_ppt import theme

        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        boxes = []
        for top, size in ((0.3, 40), (3.0, 9)):
            tb = slide.shapes.add_textbox(Inches(1), Inches(top), Inches(8), Inches(1))
            tb.text_frame.text = "字号测试文本"
            tb.text_frame.paragraphs[0].runs[0].font.size = Pt(size)
            boxes.append(tb)
        before = [(s.left, s.top, s.width, s.height) for s in slide.shapes]
        changes = theme.clamp_oversized_to_role_max(prs)
        self.assertEqual(before, [(s.left, s.top, s.width, s.height) for s in slide.shapes])
        sizes = [b.text_frame.paragraphs[0].runs[0].font.size.pt for b in boxes]
        self.assertLessEqual(sizes[0], 40)            # big text clamped (or left) at/below max
        self.assertEqual(sizes[1], 9)                 # small text is never raised here
        self.assertTrue(all(c["to"] < c["from"] for c in changes))


if __name__ == "__main__":
    unittest.main()
