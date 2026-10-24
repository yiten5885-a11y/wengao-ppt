"""复刻门禁 HG09：同 PNG 同 charbox 同算法，验证报告值能否复现。只读。"""
import sys
from pathlib import Path

ROOT = Path("/Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/wengao-visual-v13-material-fix")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import layout_gate as lg  # noqa: E402
import pypdfium2 as pdfium  # noqa: E402
from PIL import Image  # noqa: E402
import numpy as np  # noqa: E402

OUT = ROOT / "reports/gate-13p-20261021"
PDF = OUT / "render/v14-readable-13p-dedicated.pdf"
PPTX = OUT / "v14-readable-13p-dedicated.pptx"
W_IN, H_IN = 13.333, 7.5

pf = lg.render_pngs(PDF, OUT / "render", "full", width_px=1920)
print("png_full[2] =", pf[2][0], "size=", pf[2][2])

prs_p2_chars = lg.page_chars(pdfium.PdfDocument(str(PDF))[1])
doc = pdfium.PdfDocument(str(PDF))
from pptx import Presentation  # noqa: E402

prs = Presentation(str(PPTX))


def contrast(c1, c2):
    def lum(c):
        def f(v):
            v = v / 255.0
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        r, g, b = (f(x) for x in c)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    l1, l2 = lum(c1), lum(c2)
    if l1 < l2:
        l1, l2 = l2, l1
    return (l1 + 0.05) / (l2 + 0.05)


targets = [
    (2, "Text 21", "。"), (2, "Text 21", "，"),
    (7, "Text 12", "量"), (7, "Text 16", "，"),
    (11, "Text 29", "亮"), (11, "Text 23", "国"),
    (1, "Text 10", "观"),
]
for pageno, shname, ch in targets:
    slide = prs.slides[pageno - 1]
    shapes = lg.slide_shapes(slide)
    chars = lg.page_chars(doc[pageno - 1])
    mapping, unmatched = lg.assign_chars(chars, shapes)
    si = next(i for i, s in enumerate(shapes) if s["name"] == shname)
    lst = [chars[ci] for ci, s in mapping.items() if s == si]
    full_img = Image.open(pf[pageno][0]).convert("RGB")
    sx = full_img.size[0] / W_IN
    sy = full_img.size[1] / H_IN
    hits = [c for c in lst if c["ch"] == ch]
    if not hits:
        print(f"P{pageno} {shname} {ch!r}: no mapped char")
        continue
    c = hits[0]
    px0 = max(int(c["x0"] * sx) - 2, 0)
    py0 = max(int(c["y0"] * sy) - 2, 0)
    px1 = min(int(c["x1"] * sx) + 2, full_img.size[0])
    py1 = min(int(c["y1"] * sy) + 2, full_img.size[1])
    arr = np.asarray(full_img.crop((px0, py0, px1, py1)), dtype=np.float32)
    flat = arr.reshape(-1, 3)
    med = np.median(flat, axis=0)
    dist = np.abs(flat - med).max(axis=1)
    ink = flat[dist > 60]
    if len(ink) < 4:
        print(f"P{pageno} {shname} {ch!r}: ink<4 (skip), med={tuple(int(v) for v in med)}")
        continue
    dorder = np.argsort(-np.abs(ink - med).max(axis=1))
    core = ink[dorder[:max(4, len(dorder) // 3)]]
    txt = tuple(int(v) for v in np.median(core, axis=0))
    bg = tuple(int(v) for v in med)
    print(f"P{pageno} {shname} {ch!r} box=({c['x0']:.2f},{c['y0']:.2f}) "
          f"crop={px1-px0}x{py1-py0} text={txt} bg={bg} cr={contrast(txt, bg):.2f}")
