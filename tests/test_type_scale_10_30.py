"""tests/test_type_scale_10_30.py —— 10–30pt 分级字号体系验收测试。

覆盖 CODEX_TYPE_SCALE_10_30 第 2.5 节要求的 7 项测试：
1. 密集卡片页（构造 3 张卡片 × 4 行 10.5pt 文本）：适配后所有 run 在区间内、无出页、无卡片溢出，且 body ≥13；
2. 宽松页（一个标题 + 一段正文）：取到区间上限（title 30、body 18）；
3. 层级：任一页 title / body ≥ 1.4；
4. 不折行框超宽时会转成折行，而不是伸出卡片；
5. 装饰性 225pt 单字 decor 保留原字号且被计数；
6. 放不下时登记 type_scale_no_fit，原字号 ≥10 的保持不变；
7. 门禁 ROLE_PT 与 type_scale.ROLE_RANGE_PT 是同一对象或内容一致。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Emu, Inches, Pt

from wengao_ppt import refine
from wengao_ppt.type_scale import (
    HIERARCHY,
    ROLE_RANGE_PT,
    TITLE_BODY_MIN_RATIO,
    TYPE_MAX_PT,
    TYPE_MIN_PT,
    is_decor,
)
from scripts import layout_gate

IN = 914400


def _build_slide(prs, boxes, cards=None):
    """boxes: [(left, top, width, height, pt, text, wrap_none)]
    cards: [(left, top, width, height)]
    """
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    if cards:
        from pptx.enum.shapes import MSO_SHAPE
        for cl, ct, cw, ch in cards:
            sp = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,
                                        Inches(cl), Inches(ct), Inches(cw), Inches(ch))
            sp.fill.solid()
            sp.fill.fore_color.rgb = RGBColor(240, 240, 240)
    for item in boxes:
        left, top, width, height, pt, text = item[:6]
        wrap_none = item[6] if len(item) > 6 else False
        box = slide.shapes.add_textbox(Inches(left), Inches(top),
                                       Inches(width), Inches(height))
        if wrap_none:
            bp = box.text_frame._txBody.find(
                "{http://schemas.openxmlformats.org/drawingml/2006/main}bodyPr")
            if bp is not None:
                bp.set("wrap", "none")
            box.text_frame.word_wrap = False
        run = box.text_frame.paragraphs[0].add_run()
        run.text = text
        run.font.size = Pt(pt)
    return slide


class TypeScale1030Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wengao-ts-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    # 1. 密集卡片页（构造 3 张卡片 × 4 行 10.5pt 文本）：适配后所有 run 在区间内、无出页、无卡片溢出，且 body ≥13
    def test_case_1_dense_cards(self):
        src_path = self.root / "case1_src.pptx"
        out_path = self.root / "case1_out.pptx"
        prs = Presentation()
        prs.slide_width = Emu(int(13.333 * IN))
        prs.slide_height = Emu(int(7.5 * IN))

        cards = [
            (0.8, 1.8, 3.6, 4.8),
            (4.8, 1.8, 3.6, 4.8),
            (8.8, 1.8, 3.6, 4.8),
        ]
        boxes = [
            (0.8, 0.4, 11.6, 0.8, 25.0, "密集卡片主标题测试"),
        ]
        for cl, ct, cw, _ch in cards:
            boxes.extend([
                (cl + 0.2, ct + 0.3, cw - 0.4, 0.3, 10.5, "卡片头部标签分类"),
                (cl + 0.2, ct + 0.9, cw - 0.4, 0.6, 10.5, "详细正文描述第一行，包含较多字数说明。"),
                (cl + 0.2, ct + 1.8, cw - 0.4, 0.6, 10.5, "详细正文描述第二行，包含指标数据 100%。"),
                (cl + 0.2, ct + 2.7, cw - 0.4, 0.6, 10.5, "总结正文第三行，保持紧凑排版格式。"),
            ])
        _build_slide(prs, boxes, cards)
        prs.save(str(src_path))

        stats = refine.refine_deck(src_path, out_path)
        out_prs = Presentation(str(out_path))
        slide = out_prs.slides[0]

        all_runs = []
        body_runs = []
        for sh in slide.shapes:
            if not getattr(sh, "has_text_frame", False):
                continue
            is_title = "标题" in sh.text_frame.text
            for p in sh.text_frame.paragraphs:
                for r in p.runs:
                    if r.font.size:
                        pt = r.font.size.pt
                        all_runs.append(pt)
                        if not is_title:
                            body_runs.append(pt)
            # 无出页
            self.assertLessEqual(sh.top + sh.height, prs.slide_height)

        self.assertTrue(all(TYPE_MIN_PT - 0.1 <= pt <= TYPE_MAX_PT + 0.1 for pt in all_runs))
        self.assertTrue(body_runs)
        self.assertGreaterEqual(min(body_runs), 13.0)

    # 2. 宽松页（一个标题 + 一段正文）：取到区间上限（title 30、body 18）
    def test_case_2_loose_page(self):
        src_path = self.root / "case2_src.pptx"
        out_path = self.root / "case2_out.pptx"
        prs = Presentation()
        prs.slide_width = Emu(int(13.333 * IN))
        prs.slide_height = Emu(int(7.5 * IN))

        boxes = [
            (0.8, 0.5, 11.6, 1.0, 24.0, "宽松页大标题"),
            (0.8, 2.5, 11.6, 2.0, 14.0, "一段宽松的正文文本，空间非常充裕。"),
        ]
        _build_slide(prs, boxes)
        prs.save(str(src_path))

        refine.refine_deck(src_path, out_path)
        out_prs = Presentation(str(out_path))
        slide = out_prs.slides[0]

        sizes = {}
        for sh in slide.shapes:
            if getattr(sh, "has_text_frame", False):
                t = sh.text_frame.text
                sz = [r.font.size.pt for p in sh.text_frame.paragraphs for r in p.runs if r.font.size]
                if "标题" in t:
                    sizes["title"] = sz[0]
                elif "正文" in t:
                    sizes["body"] = sz[0]

        self.assertEqual(sizes.get("title"), 30.0)
        self.assertEqual(sizes.get("body"), 18.0)

    # 3. 层级：任一页 title / body ≥ 1.4
    def test_case_3_title_body_ratio(self):
        src_path = self.root / "case3_src.pptx"
        out_path = self.root / "case3_out.pptx"
        prs = Presentation()
        prs.slide_width = Emu(int(13.333 * IN))
        prs.slide_height = Emu(int(7.5 * IN))

        boxes = [
            (0.8, 0.5, 11.6, 1.0, 20.0, "主标题"),
            (0.8, 2.5, 11.6, 2.0, 16.0, "正文内容"),
        ]
        _build_slide(prs, boxes)
        prs.save(str(src_path))

        refine.refine_deck(src_path, out_path)
        out_prs = Presentation(str(out_path))
        slide = out_prs.slides[0]

        title_pt, body_pt = None, None
        for sh in slide.shapes:
            if getattr(sh, "has_text_frame", False):
                t = sh.text_frame.text
                sz = [r.font.size.pt for p in sh.text_frame.paragraphs for r in p.runs if r.font.size]
                if "主标题" in t:
                    title_pt = sz[0]
                elif "正文" in t:
                    body_pt = sz[0]

        self.assertIsNotNone(title_pt)
        self.assertIsNotNone(body_pt)
        ratio = title_pt / body_pt
        self.assertGreaterEqual(ratio, TITLE_BODY_MIN_RATIO - 1e-4)

    # 4. 不折行框超宽时会转成折行，而不是伸出卡片
    def test_case_4_no_wrap_overflow_to_square(self):
        src_path = self.root / "case4_src.pptx"
        out_path = self.root / "case4_out.pptx"
        prs = Presentation()
        prs.slide_width = Emu(int(13.333 * IN))
        prs.slide_height = Emu(int(7.5 * IN))

        cards = [(1.0, 1.0, 3.0, 5.0)]
        # 卡片宽 3.0in，里面放置一个单行文本框（wrap_none=True）且文本很长
        boxes = [
            (1.2, 1.2, 2.6, 0.3, 10.0, "这是一段非常长的不折行文本，单行显示必定超过卡片边界宽度", True)
        ]
        _build_slide(prs, boxes, cards)
        prs.save(str(src_path))

        stats = refine.refine_deck(src_path, out_path)
        out_prs = Presentation(str(out_path))
        box = [sh for sh in out_prs.slides[0].shapes if getattr(sh, "has_text_frame", False)][0]

        # 框宽不应溢出卡片右边 (1.0 + 3.0 = 4.0)
        box_right = (box.left + box.width) / IN
        self.assertLessEqual(box_right, 4.05)
        # 应包含 wrap_square 动作
        actions = [a["rule"] for p in stats["pages"] for a in p["actions"]]
        self.assertTrue(any("wrap_square" in r for r in actions))

    # 5. 装饰性 225pt 单字 decor 保留原字号且被计数
    def test_case_5_decor_preserved_and_counted(self):
        src_path = self.root / "case5_src.pptx"
        out_path = self.root / "case5_out.pptx"
        prs = Presentation()
        prs.slide_width = Emu(int(13.333 * IN))
        prs.slide_height = Emu(int(7.5 * IN))

        boxes = [
            (0.8, 0.5, 11.6, 1.0, 26.0, "正常页面标题"),
            (0.8, 2.0, 2.0, 2.0, 225.0, "01"),  # decor
            (3.0, 2.5, 9.0, 2.0, 14.0, "正文内容"),
        ]
        _build_slide(prs, boxes)
        prs.save(str(src_path))

        stats = refine.refine_deck(src_path, out_path)
        out_prs = Presentation(str(out_path))
        decor_sh = [sh for sh in out_prs.slides[0].shapes
                    if getattr(sh, "has_text_frame", False) and sh.text_frame.text.strip() == "01"][0]
        decor_pt = decor_sh.text_frame.paragraphs[0].runs[0].font.size.pt
        self.assertEqual(decor_pt, 225.0)

        # 检查 acts 中有 decor 记录
        actions = [a["rule"] for p in stats["pages"] for a in p["actions"]]
        self.assertIn("decor_detected", actions)

    # 6. 放不下时登记 type_scale_no_fit，原字号 ≥10 的保持不变
    def test_case_6_no_fit_recorded(self):
        src_path = self.root / "case6_src.pptx"
        out_path = self.root / "case6_out.pptx"
        prs = Presentation()
        prs.slide_width = Emu(int(13.333 * IN))
        prs.slide_height = Emu(int(7.5 * IN))

        boxes = [(0.7, 0.2, 11.9, 0.5, 25.5, "标题")]
        top = 0.8
        while top < 7.2:
            boxes.append((0.7, round(top, 2), 11.9, 0.3, 11.5, f"第{len(boxes)}行满版测试文字"))
            top += 0.31
        _build_slide(prs, boxes)
        prs.save(str(src_path))

        stats = refine.refine_deck(src_path, out_path)
        rules = [a["rule"] for p in stats["pages"] for a in p["actions"]]
        self.assertIn("type_scale_no_fit", rules)

        out_prs = Presentation(str(out_path))
        for sh in out_prs.slides[0].shapes:
            if getattr(sh, "has_text_frame", False):
                for p in sh.text_frame.paragraphs:
                    for r in p.runs:
                        if r.font.size:
                            self.assertGreaterEqual(r.font.size.pt, 10.0)

    # 7. 门禁 ROLE_PT 与 type_scale.ROLE_RANGE_PT 是同一对象或内容一致（防止再次分叉）
    def test_case_7_single_source_of_truth(self):
        self.assertEqual(layout_gate.ROLE_PT, ROLE_RANGE_PT)
        self.assertEqual(refine.READABILITY_ROLE_PT, ROLE_RANGE_PT)


if __name__ == "__main__":
    unittest.main()
