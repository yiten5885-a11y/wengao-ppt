#!/usr/bin/env python3
"""生成一份**中性原稿**，专门用来做"6 主题 6 风格"的对照底稿。

为什么需要它
------------
拿仓库里现成的导出件当对照源会得出错误结论：那些稿子**本身已经穿过某个主题**
（basic / business / visual-dark）。对 `wengao-adaptive`（保留原稿设计）来说，原稿是
什么色它就还什么色——实测把 basic 主题的稿子当源，adaptive 的 13 页渲染结果与
basic 逐页**字节相同**，于是"最接近的一对"永远是 adaptive 对某个浅色主题，
看起来像"主题没分开"，实际是源稿已被染色。

所以对照底稿必须是"没穿过任何主题"的中性稿：白底、深灰字、灰面板、灰表头。
它也不带任何品牌色，避免触发"业务色保护"。

产出：6 页，覆盖封面 / 要点 / 表格 / 数字卡 / 流程 / 结尾。
"""
from __future__ import annotations

import argparse
import json

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN
from pptx.util import Emu, Inches, Pt

INK = RGBColor(0x26, 0x26, 0x26)
MUTED = RGBColor(0x59, 0x59, 0x59)
PANEL = RGBColor(0xF2, 0xF2, 0xF2)
LINE = RGBColor(0xD9, 0xD9, 0xD9)
HEADER = RGBColor(0xE7, 0xE6, 0xE6)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)

LATIN = "Arial"
EA = "Songti"


def _text(slide, left, top, width, height, text, *, size=18, color=INK,
          bold=False, align=PP_ALIGN.LEFT):
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    tf = box.text_frame
    tf.word_wrap = True
    lines = text if isinstance(text, list) else [text]
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        r = p.add_run()
        r.text = line
        r.font.size = Pt(size)
        r.font.bold = bold
        r.font.color.rgb = color
        r.font.name = LATIN
    return box


def _panel(slide, left, top, width, height, fill=PANEL, shape=MSO_SHAPE.ROUNDED_RECTANGLE):
    sp = slide.shapes.add_shape(shape, Inches(left), Inches(top), Inches(width), Inches(height))
    sp.fill.solid()
    sp.fill.fore_color.rgb = fill
    sp.line.color.rgb = LINE
    sp.line.width = Pt(0.75)
    sp.shadow.inherit = False
    return sp


def build(path: str) -> None:
    prs = Presentation()
    prs.slide_width = Emu(int(13.333 * 914400))
    prs.slide_height = Emu(int(7.5 * 914400))
    blank = prs.slide_layouts[6]

    # 1 封面
    s = prs.slides.add_slide(blank)
    _text(s, 1.0, 2.6, 11.3, 1.2, "主题对照原稿", size=30, bold=True)
    _text(s, 1.0, 3.7, 11.3, 0.6, "同一份中性稿 × 六个主题：只换风格，不改内容", size=18, color=MUTED)
    _text(s, 1.0, 6.3, 11.3, 0.4, "中性底稿：白底 · 深灰字 · 灰面板，未套任何主题", size=11, color=MUTED)

    # 2 要点
    s = prs.slides.add_slide(blank)
    _text(s, 1.0, 0.7, 11.3, 0.8, "一、这页用来区分正文与强调", size=28, bold=True)
    _text(s, 1.0, 1.7, 11.3, 3.6, [
        "正文段落：字号、行距与颜色决定阅读节奏，是主题最容易暴露差异的地方。",
        "强调句：本行承载关键结论，主题的强调色会落在这里。",
        "来源与限定：小字说明用来检验小字号下的对比度余量。",
        "数字与单位：2026 年 1—9 月，同比 +18.4%，检验拉丁槽与等宽/比例字体的观感。",
    ], size=18)
    _panel(s, 1.0, 5.6, 11.3, 1.0, fill=PANEL, shape=MSO_SHAPE.RECTANGLE)
    _text(s, 1.25, 5.8, 10.8, 0.6, "结论：主题应当只改变表达方式，不改变事实与措辞。", size=16, color=MUTED)

    # 3 表格
    s = prs.slides.add_slide(blank)
    _text(s, 1.0, 0.7, 11.3, 0.8, "二、表格：表头、斑马纹与边框", size=28, bold=True)
    tbl_shape = s.shapes.add_table(5, 3, Inches(1.0), Inches(1.8), Inches(11.3), Inches(3.4))
    tbl = tbl_shape.table
    data = [["季度", "营业收入（亿元）", "同比"],
            ["2026 Q1", "3.20", "+12.5%"],
            ["2026 Q2", "3.75", "+15.1%"],
            ["2026 Q3", "4.10", "+18.4%"],
            ["2026 Q4（预测）", "4.60", "+19.2%"]]
    for r, row in enumerate(data):
        for c, val in enumerate(row):
            cell = tbl.cell(r, c)
            cell.text = ""
            p = cell.text_frame.paragraphs[0]
            run = p.add_run()
            run.text = val
            run.font.size = Pt(14 if r else 14)
            run.font.bold = r == 0
            run.font.name = LATIN
            run.font.color.rgb = INK
            cell.fill.solid()
            cell.fill.fore_color.rgb = HEADER if r == 0 else (WHITE if r % 2 else PANEL)
    _text(s, 1.0, 5.5, 11.3, 0.4, "数据为合成样例，仅用于版式对照。", size=11, color=MUTED)

    # 4 数字卡
    s = prs.slides.add_slide(blank)
    _text(s, 1.0, 0.7, 11.3, 0.8, "三、关键数字：强调色与字号层级", size=28, bold=True)
    cards = [("63.5%", "整体毛利率"), ("4.2 pt", "同比提升"), ("18 个", "覆盖城市"), ("2.7 亿", "活跃用户")]
    for i, (num, label) in enumerate(cards):
        left = 1.0 + (i % 2) * 5.75
        top = 1.9 + (i // 2) * 2.0
        _panel(s, left, top, 5.3, 1.6)
        _text(s, left + 0.35, top + 0.25, 4.6, 0.8, num, size=30, bold=True)
        _text(s, left + 0.35, top + 1.0, 4.6, 0.4, label, size=14, color=MUTED)

    # 5 流程
    s = prs.slides.add_slide(blank)
    _text(s, 1.0, 0.7, 11.3, 0.8, "四、流程：色块与序号", size=28, bold=True)
    steps = ["冻结事实", "结构规划", "版式合成", "门禁复核"]
    for i, step in enumerate(steps):
        left = 1.0 + i * 2.85
        _panel(s, left, 2.3, 2.5, 1.1)
        _text(s, left + 0.2, 2.45, 2.1, 0.4, f"0{i + 1}", size=14, color=MUTED)
        _text(s, left + 0.2, 2.8, 2.1, 0.4, step, size=16, bold=True)
    _text(s, 1.0, 4.2, 11.3, 1.2, [
        "每一步都有可复核的证据文件，缺证据即拒绝签发 READY。",
        "色块与序号是本页的强调元素，主题的强调色与面板色在这里最直观。",
    ], size=16, color=MUTED)

    # 6 结尾
    s = prs.slides.add_slide(blank)
    _text(s, 1.0, 3.0, 11.3, 1.0, "同一份稿，六种说法", size=30, bold=True, align=PP_ALIGN.LEFT)
    _text(s, 1.0, 4.0, 11.3, 0.6, "风格是表达方式，不是内容本身。", size=18, color=MUTED)

    prs.save(path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成主题对照用的中性原稿")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    build(a.out)
    print(json.dumps({"ok": True, "out": a.out}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
