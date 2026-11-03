import unittest

from pptx import Presentation
from pptx.util import Inches, Pt

from wengao_ppt.visual import fix_stacked_text_overlap

SLIDE_W, SLIDE_H = 13.333, 7.5


def _box(slide, text, left, top, width, height, pt, no_wrap):
    tb = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    tf = tb.text_frame
    tf.text = text
    tf.paragraphs[0].runs[0].font.size = Pt(pt)
    tf.word_wrap = not no_wrap   # False → <a:bodyPr wrap="none">
    return tb


def _slide():
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(SLIDE_W), Inches(SLIDE_H)
    return prs.slides.add_slide(prs.slide_layouts[6])


class StackedTextNoWrapTests(unittest.TestCase):
    def test_no_wrap_label_is_measured_as_one_line(self):
        # job-f51a976c5258 P3：1.3in 宽的单行小标题抬到 24pt 后，被按框宽折成
        # 多行估成 1.32in 高，把下方整列推出页面。
        slide = _slide()
        label = _box(slide, "组织 · 平台 · 计费", 1.78, 0.625, 1.312, 0.177, 24, no_wrap=True)
        below = _box(slide, "三家路径不同，KPI 是同一个", 0.625, 0.906, 12.08, 0.56, 24, no_wrap=True)
        fix_stacked_text_overlap(slide, slide_h_in=SLIDE_H, slide_w_in=SLIDE_W)
        self.assertLess(label.height / 914400, 0.8)
        self.assertLess(below.top / 914400, 1.5)

    def test_push_never_moves_text_off_the_canvas(self):
        slide = _slide()
        _box(slide, "很长的正文" * 30, 0.6, 5.0, 3.0, 0.4, 24, no_wrap=False)
        tail = _box(slide, "结论", 0.6, 5.5, 3.0, 0.4, 18, no_wrap=False)
        changed = fix_stacked_text_overlap(slide, slide_h_in=SLIDE_H, slide_w_in=SLIDE_W)
        self.assertLessEqual((tail.top + tail.height) / 914400, SLIDE_H)
        self.assertTrue(any(c.get("push_blocked_off_canvas") for c in changed))


if __name__ == "__main__":
    unittest.main()
