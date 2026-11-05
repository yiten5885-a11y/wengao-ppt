"""主题 2.0 的设计契约：装帧、表格、图表三类语言都要可执行地验收。

背景（2026-11-05）：用户指出"6 个主题风格还不行，审美/品味/专业性要跨越式提高"。
1.x 的六个主题在同一份中性稿上只有配色与页边 3-4 根线的差别，表格清一色
"实心表头 + 四边细框 + 明显斑马纹"。2.0 把主题升级为装帧语言 + 现代横线表 +
成套调色板。本文件只锁住**新增的设计维度**，原有的可读性/身份/避让契约仍由
test_theme_six_styles.py 与 test_theme_furniture.py 守住。
"""
from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from pptx import Presentation
from pptx.util import Inches

from wengao_ppt import theme as T

APPLIED = [tid for tid, t in T.THEMES.items() if not t.get("adaptive")]


def _deck_with_table(path: Path) -> Path:
    """一页一个 3 行 3 列表格（真实 PPTX 表格，非示意图）。"""
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1.0), Inches(2.6), Inches(8), Inches(1))
    box.text_frame.text = "主题对照"
    tbl_shape = slide.shapes.add_table(3, 3, Inches(1.0), Inches(4.0),
                                       Inches(8.0), Inches(1.6))
    for r, row in enumerate(tbl_shape.table.rows):
        for c, cell in enumerate(row.cells):
            cell.text = f"{r}-{c}"
    prs.save(str(path))
    return path


def _cell_edges(cell) -> dict[str, str]:
    """返回四条边的线型：'none'（noFill）/ 'solid:<HEX>' / 'absent'。"""
    from pptx.oxml.ns import qn
    tcPr = cell._tc.get_or_add_tcPr()
    out = {}
    for side, tag in (("L", "a:lnL"), ("R", "a:lnR"), ("T", "a:lnT"), ("B", "a:lnB")):
        ln = tcPr.find(qn(tag))
        if ln is None:
            out[side] = "absent"
        elif ln.find(qn("a:noFill")) is not None:
            out[side] = "none"
        else:
            clr = ln.find(".//" + qn("a:srgbClr"))
            out[side] = "solid:" + (clr.get("val") if clr is not None else "?")
    return out


class DesignLanguageRegistryTests(unittest.TestCase):
    def test_every_applied_theme_declares_distinct_style_tokens(self):
        tables = [T.THEMES[t].get("table_style") for t in APPLIED]
        charts = [T.THEMES[t].get("chart_style") for t in APPLIED]
        self.assertTrue(all(tables), f"有主题缺 table_style: {tables}")
        self.assertTrue(all(charts), f"有主题缺 chart_style: {charts}")
        self.assertEqual(len(set(tables)), len(APPLIED), "表格语言不是每套不同")
        self.assertEqual(len(set(charts)), len(APPLIED), "图表语言不是每套不同")

    def test_style_tokens_enter_identity(self):
        """两个令牌都改输出，必须进 config_digest，否则历史任务会对不上。"""
        for tid in APPLIED:
            with self.subTest(theme=tid):
                cfg = T.effective_config(tid)
                self.assertIn("table_style", cfg)
                self.assertIn("chart_style", cfg)
                self.assertIn("furniture", cfg)

    def test_changing_table_style_changes_digest(self):
        tid = "wengao-basic"
        before = T.config_digest(tid)
        original = T.THEMES[tid]["table_style"]
        try:
            T.THEMES[tid]["table_style"] = "cinema"
            self.assertNotEqual(T.config_digest(tid), before)
        finally:
            T.THEMES[tid]["table_style"] = original
        self.assertEqual(T.config_digest(tid), before)

    def test_registry_version_bumped(self):
        self.assertEqual(T.REGISTRY_VERSION, "2.0.0")


class ModernTableTests(unittest.TestCase):
    """表格：竖线关闭、表头只留一条强调线、数据行只留底边细线。"""

    def test_vertical_rules_are_off_and_header_has_one_accent_rule(self):
        for tid in APPLIED:
            with self.subTest(theme=tid):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp = Path(tmp)
                    src = _deck_with_table(tmp / "src.pptx")
                    out = tmp / "themed.pptx"
                    T.apply_theme(src, out, theme_name=tid)
                    prs = Presentation(str(out))
                    tbl = next(s.table for s in prs.slides[0].shapes if s.has_table)
                    accent = T.THEMES[tid]["colors"][
                        "accent" if T.THEMES[tid]["table_style"] in ("editorial", "ruled")
                        else "primary"].upper()
                    header = _cell_edges(tbl.rows[0].cells[0])
                    self.assertEqual(header["L"], "none", f"{tid} 表头还留着左竖线")
                    self.assertEqual(header["R"], "none", f"{tid} 表头还留着右竖线")
                    accent_sides = [s for s in ("T", "B") if header[s].startswith("solid")]
                    self.assertEqual(len(accent_sides), 1,
                                     f"{tid} 表头强调线不是恰好一条: {header}")
                    self.assertEqual(header[accent_sides[0]], "solid:" + accent,
                                     f"{tid} 强调线颜色没用主题令牌: {header}")
                    body = _cell_edges(tbl.rows[1].cells[0])
                    self.assertEqual(body["L"], "none")
                    self.assertEqual(body["R"], "none")
                    self.assertEqual(body["T"], "none")
                    self.assertTrue(body["B"].startswith("solid"),
                                    f"{tid} 数据行没有行分隔线: {body}")

    def test_banding_follows_the_declared_style(self):
        """公文/书卷是零斑马的编辑部表，商务/仪表/影院保留极淡行节奏。"""
        for tid in APPLIED:
            with self.subTest(theme=tid):
                banded = T._table_style_rules(T.THEMES[tid]["table_style"],
                                              T.THEMES[tid]["colors"])["banded"]
                expected = T.THEMES[tid]["table_style"] not in ("editorial", "quiet")
                self.assertEqual(banded, expected)


class FurnitureFinishTests(unittest.TestCase):
    """2.0 装帧：装饰矩形必须零效果，否则 LibreOffice 会给每根刻度套灰投影。"""

    def test_decorations_carry_no_style_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            prs = Presentation()
            prs.slide_width = Inches(13.333)
            prs.slide_height = Inches(7.5)
            slide = prs.slides.add_slide(prs.slide_layouts[6])
            box = slide.shapes.add_textbox(Inches(1.0), Inches(2.6), Inches(8), Inches(1))
            box.text_frame.text = "第 1 页标题"
            src = tmp / "src.pptx"
            prs.save(str(src))
            for tid in APPLIED:
                with self.subTest(theme=tid):
                    out = tmp / f"{tid}.pptx"
                    T.apply_theme(src, out, theme_name=tid)
                    with zipfile.ZipFile(out) as z:
                        xml = z.read("ppt/slides/slide1.xml").decode("utf-8")
                    self.assertIn("wg-furniture:", xml)
                    self.assertNotIn("<p:style>", xml,
                                     f"{tid} 装饰形状仍带 p:style/effectRef，会渲染出默认阴影")
                    self.assertNotIn("outerShdw", xml)


if __name__ == "__main__":
    unittest.main()
