"""取证 4：'量' crop 像素直方图 + P1/P3/P4/P5/P13 关键形状 + PDF 内嵌字体。只读。"""
import sys
from pathlib import Path

ROOT = Path("/Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/wengao-visual-v13-material-fix")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import layout_gate as lg  # noqa: E402
import pypdfium2 as pdfium  # noqa: E402
from PIL import Image  # noqa: E402
import numpy as np  # noqa: E402
from pptx import Presentation  # noqa: E402

OUT = ROOT / "reports/gate-13p-20261021"
PDF = OUT / "render/v14-readable-13p-dedicated.pdf"
PPTX = OUT / "v14-readable-13p-dedicated.pptx"
EMU = 914400
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

doc = pdfium.PdfDocument(str(PDF))
img7 = Image.open(OUT / "render/full-P07.png").convert("RGB")
sx = img7.size[0] / 13.333
sy = img7.size[1] / 7.5
chars = lg.page_chars(doc[6])
c = next(c for c in chars if c["ch"] == "量" and 3.7 < c["y0"] < 3.9)
crop = img7.crop((int(c["x0"] * sx) - 2, int(c["y0"] * sy) - 2,
                  int(c["x1"] * sx) + 2, int(c["y1"] * sy) + 2))
arr = np.asarray(crop, dtype=np.uint8).reshape(-1, 3)
gray = arr.mean(axis=1)
hist, edges = np.histogram(gray, bins=16, range=(0, 256))
print("==== P7 '量' crop luminance histogram (16 bins) ====")
for h, e in zip(hist, edges):
    print(f"  {int(e):3d}-{int(e)+15:3d}: {'#' * int(h / max(hist) * 60)} {h}")

# P1/P3/P4/P5/P13 全形状（名字/框/fill/alpha）
prs = Presentation(str(PPTX))


def fillinfo(sh):
    try:
        el = sh._element.spPr
    except Exception:
        return "?"
    if el is None:
        return "?"
    sf = el.find(f"{A}solidFill")
    if sf is not None:
        cc = sf.find(f"{A}srgbClr")
        if cc is not None:
            al = cc.find(f"{A}alpha")
            return f"srgb#{cc.get('val')}" + (f"@{int(al.get('val'))/1000:.0f}%" if al is not None else "")
        sc = sf.find(f"{A}schemeClr")
        return f"scheme:{sc.get('val')}" if sc is not None else "srgb?"
    for t in ("gradFill", "blipFill", "pattFill", "grpFill", "noFill"):
        if el.find(f"{A}{t}") is not None:
            return t
    return "inherit"


for pi in (1, 3, 4, 5, 13):
    slide = prs.slides[pi - 1]
    print(f"==== P{pi} shapes (non-text & big only) ====")
    for z, sh in enumerate(slide.shapes):
        L, T, W, H = sh.left / EMU, sh.top / EMU, sh.width / EMU, sh.height / EMU
        has_tx = getattr(sh, "has_text_frame", False)
        t = sh.text_frame.text.strip() if has_tx else ""
        if (not has_tx or not t) and W * H < 0.5:
            continue  # 跳过小装饰
        txt = f" {t[:20]!r}" if t else ""
        print(f"  z{z} {sh.name} ({L:.2f},{T:.2f},{W:.2f},{H:.2f}) {fillinfo(sh)}{txt}")
