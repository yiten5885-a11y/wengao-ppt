"""Regression tests for the job-293501ae3698 acceptance failure (2026-11-04).

Two production defects, both first exposed by a real user deck uploaded
through the LOCAL STUDIO panel (beautify mode, wengao-business theme):

1. Bottom brand band misread as body (page 1 HG06 CONTENT_FOOTER_GAP).
   The user's deck carries a full-width brand strip「宏观观察 · 短视频配套
   文稿」at top=6.688, 11.25pt. The footer position rule (top >= 6.85 /
   sh_*0.91) missed it, so the readability floor raised it to the body
   target (18pt); the taller glyphs then reached 6.958, only 0.042in above
   the footer zone (HG06 requires 0.12) — and HG07 would also flag an
   11pt-sized footer against the body minimum of 13. Fix: both role
   classifiers (scripts/layout_gate.py:infer_roles and
   wengao_ppt/refine.py:infer_text_roles) recognize a bottom brand band
   (band [6.65, 6.85), bottom-most real text, <= 60 chars, <= 14pt, brand
   echo with an upper-half shape) as footer.

2. Declared box shrunk below its current width (page 3 HG02/HG05).
   refine.fit_page_type_scale's single-line width formula
   ``min(max(need_w + 0.12, s.W), avail_max_w)`` becomes a SHRINK when the
   same-row avoidance cap (avail_max_w) is below the current width: the
   「关键约束」label box (4.312in) was cut to 0.335in because「居民端」
   (0.03in above it) grew taller via fix_stacked_text_overlap and the
   borderline vertical-overlap test then counted it as a right-hand row
   neighbor. Shrinking a declared box never shortens the ink (renderers do
   not clip), it only turns HG02/HG05 "glyphs outside the owner box" from
   false to true. Fix: the avoidance cap can never pull the width below
   the current width — row clearance is the font-size search's job.
"""
import unittest

from pptx import Presentation
from pptx.util import Inches, Pt

from scripts.layout_gate import infer_roles


def _shape(text, max_size=None, font_size_pt=0.0, top=3.0, left=1.0, width=4.0):
    return {"text": text, "left": left, "top": top, "right": left + width,
            "bottom": top + 0.3, "max_size": max_size,
            "font_size_pt": font_size_pt}


class TestBottomBrandBandGate(unittest.TestCase):
    """scripts/layout_gate.py:infer_roles must recognize the brand band."""

    def test_user_deck_page1_geometry_band_is_footer(self):
        # Geometry mirrors job-293501ae3698 page 1 (themed candidate).
        shapes = [
            _shape("宏观观察", max_size=14.0, top=0.625, left=0.625, width=12.083),
            _shape("两种人民币", max_size=30.0, top=1.146, left=0.625, width=12.083),
            _shape("汇率、资本管制与新质生产力", max_size=18.0, top=2.635,
                   left=0.625, width=12.083),
            _shape("宏观观察 · 短视频配套文稿", max_size=11.25, top=6.688,
                   left=0.625, width=12.083),
        ]
        roles = infer_roles(shapes)
        self.assertEqual(roles[3], "footer")
        # The subtitle above stays body — the band rule must not leak upward.
        self.assertEqual(roles[2], "body")

    def test_band_with_page_number_below_still_footer(self):
        # A page number at 7.07 is footer furniture, not a content line:
        # the band above it must still qualify as bottom-most.
        shapes = [
            _shape("宏观观察", max_size=14.0, top=0.625, left=0.625, width=12.083),
            _shape("两种人民币", max_size=30.0, top=1.146, left=0.625, width=12.083),
            _shape("宏观观察 · 短视频配套文稿", max_size=11.25, top=6.688,
                   left=0.625, width=12.083),
            _shape("03", max_size=10.5, top=7.073, left=12.542, width=0.391),
        ]
        roles = infer_roles(shapes)
        self.assertEqual(roles[2], "footer")
        self.assertEqual(roles[3], "footer")

    def test_bottom_line_without_brand_echo_stays_body(self):
        # No upper-half shape shares a token with the bottom line: it is
        # content, not a brand band (guard against over-classification).
        # Kept under 15 chars so the conclusion-selection pass (>=15 chars
        # below the bottom zone) doesn't shadow the role under test.
        shapes = [
            _shape("两种人民币", max_size=30.0, top=1.146, left=0.625, width=12.083),
            _shape("资本账户再平衡观察", max_size=13.0, top=6.7,
                   left=0.625, width=12.083),
        ]
        roles = infer_roles(shapes)
        self.assertEqual(roles[1], "body")

    def test_band_sized_text_with_echo_but_large_font_stays_body(self):
        # 18pt bottom text is conclusion-sized, not footer furniture.
        shapes = [
            _shape("宏观观察", max_size=14.0, top=0.625, left=0.625, width=12.083),
            _shape("宏观观察下的再平衡", max_size=18.0, top=6.7,
                   left=0.625, width=12.083),
        ]
        roles = infer_roles(shapes)
        self.assertEqual(roles[1], "body")

    def test_band_with_real_content_below_stays_body(self):
        # A genuine body line below the band (and still above the 6.85
        # footer-zone rule) disqualifies the band as bottom-most.
        shapes = [
            _shape("宏观观察", max_size=14.0, top=0.625, left=0.625, width=12.083),
            _shape("宏观观察 · 短视频配套文稿", max_size=11.25, top=6.65,
                   left=0.625, width=12.083),
            _shape("汇率影响日常", max_size=13.0, top=6.75,
                   left=0.625, width=12.083),
        ]
        roles = infer_roles(shapes)
        self.assertEqual(roles[1], "body")

    def test_existing_footer_zone_rule_untouched(self):
        shapes = [_shape("两种人民币 · 宏观观察", max_size=10.5, top=7.073,
                         left=0.625, width=1.469)]
        roles = infer_roles(shapes)
        self.assertEqual(roles[0], "footer")


class TestRefineKeptInSync(unittest.TestCase):

    def test_refine_has_mirror_brand_band_rule(self):
        # infer_text_roles' docstring requires it stay in the same
        # role-classification standard as the gate's infer_roles.
        import inspect

        from wengao_ppt import refine
        source = inspect.getsource(refine.infer_text_roles)
        self.assertIn("_floor_brand_band", source)
        helper = inspect.getsource(refine._floor_brand_band)
        self.assertIn("同口径", helper)


class TestThemeAppliesFixesEndToEnd(unittest.TestCase):
    """apply_theme on a deck replicating the failing structure must keep
    the label box width and leave the brand band small and footer-classed."""

    def _build_deck(self, path):
        prs = Presentation()
        prs.slide_width = Inches(13.333)
        prs.slide_height = Inches(7.5)
        blank = prs.slide_layouts[6]

        s1 = prs.slides.add_slide(blank)
        for text, top, size in (
                ("宏观观察", 0.625, 12.0),
                ("两种人民币", 1.146, 54.0),
                ("汇率、资本管制与新质生产力", 2.635, 19.5),
                ("宏观观察 · 短视频配套文稿", 6.688, 11.25)):
            tb = s1.shapes.add_textbox(Inches(0.625), Inches(top),
                                       Inches(12.083), Inches(0.4))
            tb.text_frame.text = text
            tb.text_frame.paragraphs[0].runs[0].font.size = Pt(size)

        s3 = prs.slides.add_slide(blank)
        # 居民端 column card + the exact geometry that triggered the shrink.
        card = s3.shapes.add_textbox(Inches(0.625), Inches(1.927),
                                     Inches(4.896), Inches(4.188))
        card.fill.solid()
        card.text_frame.text = ""
        for text, left, top, w, size in (
                ("居民端", 1.312, 2.500, 0.760, 18.0),
                ("关键约束", 0.917, 2.969, 4.312, 11.25),
                ("资本管制，资本账户未完全开放", 0.917, 3.188, 4.312, 13.5),
                ("可用方向", 0.917, 3.594, 4.312, 11.25),
                ("国内低收益资产——存款、理财、房产", 0.917, 3.812, 4.312, 13.5)):
            tb = s3.shapes.add_textbox(Inches(left), Inches(top),
                                       Inches(w), Inches(0.25))
            tb.text_frame.text = text
            tb.text_frame.paragraphs[0].runs[0].font.size = Pt(size)
        tb = s3.shapes.add_textbox(Inches(0.625), Inches(7.073),
                                   Inches(1.469), Inches(0.177))
        tb.text_frame.text = "两种人民币 · 宏观观察"
        tb.text_frame.paragraphs[0].runs[0].font.size = Pt(10.5)
        prs.save(str(path))

    def test_label_box_not_shrunk_and_band_not_raised(self):
        import tempfile

        from wengao_ppt.theme import apply_theme

        with tempfile.TemporaryDirectory() as td:
            src = f"{td}/in.pptx"
            out = f"{td}/out.pptx"
            self._build_deck(src)
            apply_theme(src, out, "wengao-business")

            p = Presentation(out)
            boxes = {}
            for slide in p.slides:
                for sh in slide.shapes:
                    if getattr(sh, "has_text_frame", False):
                        t = sh.text_frame.text.strip()
                        if t:
                            boxes.setdefault(t, []).append(sh)
            self.assertIn("关键约束", boxes)
            w = boxes["关键约束"][0].width / 914400
            self.assertGreaterEqual(w, 4.0,
                                    "declared box must never shrink below "
                                    "its current width (grow-only contract)")
            self.assertIn("宏观观察 · 短视频配套文稿", boxes)
            strip = boxes["宏观观察 · 短视频配套文稿"][0]
            sizes = [r.font.size.pt for para in strip.text_frame.paragraphs
                     for r in para.runs if r.font.size]
            self.assertTrue(sizes and max(sizes) <= 11.5,
                            f"brand band must stay footer-sized, got {sizes}")

            # The gate's own classifier must agree on the result.
            import sys

            sys.path.insert(0, "scripts")
            import layout_gate as lg

            for slide in p.slides:
                shapes = lg.slide_shapes(slide)
                for i, s in enumerate(shapes):
                    if "宏观观察 · 短视频配套文稿" in s["text"]:
                        self.assertEqual(
                            lg.infer_roles(shapes)[i], "footer")


if __name__ == "__main__":
    unittest.main()
