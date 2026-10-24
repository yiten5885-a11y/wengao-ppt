#!/usr/bin/env python3
"""取证：P2 数字字形 y 聚类 + P3/P12 低对比字符的承载面形状栈（只读）。"""
import sys

from pptx import Presentation
import pypdfium2 as pdfium

A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
ROOT = "."

PPTX = "reports/gate-13p-20261021/v14-readable-13p-dedicated.pptx"
PDF = "reports/gate-13p-20261021/render/v14-readable-13p-dedicated.pdf"

prs = Presentation(PPTX)
doc = pdfium.PdfDocument(PDF)

print("== P2 Text 4/8/12/16 的字符框（y 中心聚类诊断） ==")
page = doc[1]
H_IN = 7.5
tp = page.get_textpage()
n = tp.count_chars()
rows = []
for i in range(n):
    ch = tp.get_text_range(i, 1)
    l, b, r, t = tp.get_charbox(i)
    # pdfium: 底左原点 → top-down 英寸
    cy_in = (H_IN - (t + b) / 2 / 72.0)
    rows.append((ch, round(l / 72.0, 3), round(cy_in, 3)))
# Text 4/8/12/16 = 44pt 数字格（P2）。打印所有数字/符号字符
for ch, x, cy in rows:
    if ch.strip() and (ch.isdigit() or ch in "+.%"):
        print(f"  ch={ch!r} x={x} cy={cy}")

print()
print("== P3 全形状栈（左/上/宽/高/填充/文字） ==")
def dump_slide(idx, targets):
    sl = prs.slides[idx]
    for sh in sl.shapes:
        el = getattr(sh, "_element", None)
        name = sh.name
        try:
            box = (round(sh.left / 914400, 2), round(sh.top / 914400, 2),
                   round(sh.width / 914400, 2), round(sh.height / 914400, 2))
        except Exception:  # noqa: BLE001
            box = "?"
        fill = "-"
        if el is not None:
            spPr = el.find(f"{P}spPr")
            if spPr is not None:
                for ch2 in spPr:
                    tag = ch2.tag.split("}")[-1]
                    if tag in ("solidFill", "gradFill", "blipFill", "noFill"):
                        srgb = ch2.find(f"{A}srgbClr")
                        al = ch2.find(f"{A}srgbClr/{A}alpha")
                        fill = (tag + ":" + (srgb.get("val") if srgb is not None else "?")
                                + (f"@{int(al.get('val'))/1000:.0f}%" if al is not None else ""))
                        break
        txt = sh.text_frame.text.strip()[:12] if getattr(sh, "has_text_frame", False) else ""
        mark = " <<<" if name in targets else ""
        print(f"  {name}: {box} {fill} {txt!r}{mark}")

dump_slide(2, {"Text 2", "Text 26", "Rectangle 30", "Shape 23"})
print()
print("== P12 全形状栈 ==")
dump_slide(11, {"Text 22", "Rectangle 30", "Shape 23", "Picture 21"})
