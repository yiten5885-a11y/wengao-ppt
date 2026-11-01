"""Regression test for a false-positive found live on job-d239567879df.

_candidate_blockers (wengao_ppt/semantic_scenes.py) only skipped a
near-full-slide backdrop shape via _is_background_shape when
placement_mode == "replace_visual_image". A themed deck's full-slide
solid-fill background shape — drawn first, so behind every other shape,
including any later-inserted scene photo — structurally can never obscure
anything, regardless of how the photo was placed. Restricting the skip to
replace_visual_image left "empty_area" placements with no such exemption,
so a scene photo placed next to normal body text was wrongly flagged
SCENE.PICTURE_OBSCURED_OR_BLOCKED / filled_shape_or_card_overlap for
merely geometrically overlapping the slide's own backdrop.

Reproduced live: job-d239567879df, scene pension_equity_concept (page 15),
placement_mode "empty_area", blocked by shape 134 — a 13.333x7.5in
full-slide solid-fill rectangle.
"""
import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Inches

from wengao_ppt.semantic_scenes import _candidate_blockers


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (800, 600), (60, 90, 120)).save(buf, format="PNG")
    return buf.getvalue()


class TestCandidateBlockersBackgroundShape(unittest.TestCase):

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _build_slide(self, *, photo_rect):
        prs = Presentation()
        prs.slide_width = Inches(13.333)
        prs.slide_height = Inches(7.5)
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        # A full-slide solid-fill backdrop, drawn first (so behind
        # everything else) — the same shape shape that triggered the real
        # false positive.
        backdrop = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE, Inches(0), Inches(0), Inches(13.333), Inches(7.5))
        backdrop.fill.solid()
        backdrop.line.fill.background()
        # Body text, unrelated to the photo's rect.
        box = slide.shapes.add_textbox(Inches(0.6), Inches(0.6), Inches(5), Inches(1))
        box.text_frame.text = "unrelated body text"
        photo_path = self.root / "photo.png"
        photo_path.write_bytes(_png_bytes())
        picture = slide.shapes.add_picture(
            str(photo_path), Inches(photo_rect[0]), Inches(photo_rect[1]),
            Inches(photo_rect[2]), Inches(photo_rect[3]))
        # Named the way apply_plan marks the scene's own embedded photo, so
        # _candidate_blockers correctly recognizes it as the scene picture
        # itself rather than a second, obstructing picture.
        picture._element.nvPicPr.cNvPr.set("name", "WENGAO_SCENE|scene_under_test|deadbeef")
        candidate = self.root / "candidate.pptx"
        prs.save(candidate)
        return candidate

    def test_fullslide_backdrop_does_not_block_empty_area_placement(self):
        rect = [7.4, 0.9834, 5.5333, 5.5333]
        candidate = self._build_slide(photo_rect=rect)
        spec = {"page": 1, "rect": rect, "placement_mode": "empty_area",
               "scene_key": "scene_under_test"}
        overlap, blockers = _candidate_blockers(candidate, spec)
        self.assertEqual(blockers, [])

    def test_fullslide_backdrop_does_not_block_replace_visual_image_placement(self):
        rect = [7.4, 0.9834, 5.5333, 5.5333]
        candidate = self._build_slide(photo_rect=rect)
        spec = {"page": 1, "rect": rect, "placement_mode": "replace_visual_image",
               "target_rect": rect, "scene_key": "scene_under_test"}
        overlap, blockers = _candidate_blockers(candidate, spec)
        self.assertEqual(blockers, [])

    def test_genuine_small_filled_card_still_blocks(self):
        rect = [0.5, 0.5, 4.0, 3.0]
        candidate = self._build_slide(photo_rect=[7.4, 0.9834, 5.5333, 5.5333])
        prs = Presentation(str(candidate))
        slide = prs.slides[0]
        card = slide.shapes.add_shape(
            MSO_SHAPE.ROUNDED_RECTANGLE, Inches(0.5), Inches(0.5), Inches(4.0), Inches(3.0))
        card.fill.solid()
        card.line.fill.background()
        prs.save(candidate)
        spec = {"page": 1, "rect": rect, "placement_mode": "empty_area",
               "scene_key": "scene_under_test"}
        overlap, blockers = _candidate_blockers(candidate, spec)
        self.assertIn("filled_shape_or_card_overlap", blockers)


if __name__ == "__main__":
    unittest.main()
