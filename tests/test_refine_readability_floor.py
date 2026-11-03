from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pptx import Presentation
from pptx.util import Emu, Inches, Pt

from wengao_ppt import refine
from wengao_ppt.refine import READABILITY_ROLE_PT

IN = 914400


def _deck(path: Path, boxes):
    """boxes: [(left, top, width, height, pt, text)] on one 13.33x7.5in slide."""
    prs = Presentation()
    prs.slide_width = Emu(int(13.333 * IN))
    prs.slide_height = Emu(int(7.5 * IN))
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    for left, top, width, height, pt, text in boxes:
        box = slide.shapes.add_textbox(Inches(left), Inches(top),
                                       Inches(width), Inches(height))
        run = box.text_frame.paragraphs[0].add_run()
        run.text = text
        run.font.size = Pt(pt)
    prs.save(str(path))
    return path


def _sizes_and_boxes(path):
    prs = Presentation(str(path))
    out = []
    for shape in prs.slides[0].shapes:
        if not getattr(shape, "has_text_frame", False):
            continue
        sizes = [r.font.size.pt for p in shape.text_frame.paragraphs
                 for r in p.runs if r.font.size]
        out.append({"text": shape.text_frame.text.strip(), "sizes": sizes,
                    "top": shape.top / IN, "height": shape.height / IN})
    return out


class ReadabilityFloorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wengao-floor-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _refine(self, boxes):
        src = _deck(self.root / "src.pptx", boxes)
        out = self.root / "out.pptx"
        stats = refine.refine_deck(src, out)
        return stats, _sizes_and_boxes(out)

    def test_small_body_text_is_raised_to_the_hard_minimum(self):
        lo, _hi = READABILITY_ROLE_PT["body"]
        _stats, shapes = self._refine([
            (0.7, 0.2, 11.9, 0.9, 25.5, "英语作文班级报告"),
            (0.7, 3.0, 11.9, 0.33, 12.0, "数据均为仿真，不对应真实学生。"),
        ])
        body = [s for s in shapes if "仿真" in s["text"]][0]
        self.assertTrue(body["sizes"], "body run lost its explicit size")
        self.assertGreaterEqual(min(body["sizes"]), lo)

    def test_the_box_grows_to_hold_the_bigger_text(self):
        _stats, shapes = self._refine([
            (0.7, 0.2, 11.9, 0.9, 25.5, "英语作文班级报告"),
            (0.7, 3.0, 11.9, 0.33, 12.0, "数据均为仿真，不对应真实学生。"),
        ])
        body = [s for s in shapes if "仿真" in s["text"]][0]
        # 24pt single line needs ~0.41in plus 0.1in of frame inset.
        self.assertGreater(body["height"], 0.33)

    def test_a_block_below_is_pushed_down_instead_of_being_overlapped(self):
        _stats, shapes = self._refine([
            (0.7, 0.2, 11.9, 0.9, 25.5, "英语作文班级报告"),
            (0.7, 3.0, 11.9, 0.33, 12.0, "上面这段会变大"),
            (0.7, 3.4, 11.9, 0.33, 12.0, "下面这段必须被推开"),
        ])
        upper = [s for s in shapes if "上面" in s["text"]][0]
        lower = [s for s in shapes if "下面" in s["text"]][0]
        self.assertGreaterEqual(lower["top"], upper["top"] + upper["height"] - 1e-6,
                                "raised text overlaps the block below it")

    def test_text_already_in_range_is_left_alone(self):
        _stats, shapes = self._refine([
            (0.7, 0.2, 11.9, 0.9, 30.0, "标题在区间内"),
            (0.7, 3.0, 11.9, 0.6, 18.0, "正文也在区间内，不应被改动。"),
        ])
        body = [s for s in shapes if "正文" in s["text"]][0]
        self.assertEqual(body["sizes"], [18.0])

    def test_refine_never_rewrites_the_text(self):
        boxes = [(0.7, 0.2, 11.9, 0.9, 25.5, "英语作文班级报告"),
                 (0.7, 3.0, 11.9, 0.33, 12.0, "数据均为仿真，不对应真实学生。")]
        _stats, shapes = self._refine(boxes)
        self.assertEqual({s["text"] for s in shapes},
                         {b[5] for b in boxes})

    def test_a_page_with_no_room_records_a_skip_instead_of_overflowing(self):
        # Fill the page so nothing can grow or be pushed anywhere.
        boxes = [(0.7, 0.2, 11.9, 0.5, 25.5, "标题")]
        top = 0.8
        while top < 7.1:
            boxes.append((0.7, round(top, 2), 11.9, 0.3, 12.0, f"第{len(boxes)}行密排文字"))
            top += 0.32
        stats, _shapes = self._refine(boxes)
        rules = [a["rule"] for p in stats["pages"] for a in p["actions"]]
        self.assertIn("readability_floor_skipped", rules)
        self.assertEqual(stats["warnings"], [])


if __name__ == "__main__":
    unittest.main()
