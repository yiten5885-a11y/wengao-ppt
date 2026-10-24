"""第三轮取证 3：从 PDF 渲染采样问题位置的像素 + P2 全形状清单。只读。"""
import pypdfium2 as pdfium
from pptx import Presentation

BASE = ("/Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/"
        "wengao-visual-v13-material-fix/reports/gate-13p-20261021")
PDF = f"{BASE}/render/v14-readable-13p-dedicated.pptx.pdf"
PDF = f"{BASE}/render/v14-readable-13p-dedicated.pdf"
PPTX = f"{BASE}/v14-readable-13p-dedicated.pptx"
EMU = 914400
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

# ---------- 像素采样：问题字符周边 30x30 区域的中位色 ----------
doc = pdfium.PdfDocument(PDF)
DPI = 144
samples = {
    2: [("(7.35,6.80) Text21 '。'", 7.35, 6.80), ("(3.0,5.0) 卡内空白", 3.0, 5.0),
        ("(1.0,1.0) 页面角", 1.0, 1.0), ("(6.5,6.2) 带内空白", 6.5, 6.2)],
    7: [("(6.3,3.9) Text12 '量' 附近", 6.3, 3.9), ("(1.0,4.2) 行间空白", 1.0, 4.2)],
    10: [("(10.1,2.5) Text2 '”'", 10.10, 2.50), ("(11.5,2.0) 图板内", 11.5, 2.0)],
    11: [("(0.65,6.35) Text29 '亮' 附近", 0.65, 6.35), ("(3.0,6.5) 带内", 3.0, 6.5)],
    12: [("(0.9,0.5) 标题 '转' 附近", 0.90, 0.50), ("(6.0,0.5) 标题右空白", 6.0, 0.5)],
}
import numpy as np
for pageno, spots in samples.items():
    page = doc[pageno - 1]
    img = page.render(scale=DPI / 72).to_pil()
    sx = img.size[0] / 13.333
    sy = img.size[1] / 7.5
    print(f"==== P{pageno} pixel samples (median of 41x41 patch) ====")
    for label, x, y in spots:
        px, py = int(x * sx), int(y * sy)
        arr = np.asarray(img.crop((max(px - 20, 0), max(py - 20, 0),
                                   px + 21, py + 21)), dtype=np.float32)
        med = tuple(int(v) for v in np.median(arr.reshape(-1, 3), axis=0))
        print(f"  {label}: med={med}")

# ---------- P2 全形状清单 ----------
prs = Presentation(PPTX)


def fillinfo(sh):
    try:
        el = sh._element.spPr
    except Exception:
        return "?"
    if el is None:
        return "?"
    sf = el.find(f"{A}solidFill")
    if sf is not None:
        c = sf.find(f"{A}srgbClr")
        if c is not None:
            al = c.find(f"{A}alpha")
            return f"srgb#{c.get('val')}" + (f"@{int(al.get('val'))/1000:.0f}%" if al is not None else "")
        sc = sf.find(f"{A}schemeClr")
        if sc is not None:
            return f"scheme:{sc.get('val')}"
    for t in ("gradFill", "blipFill", "pattFill", "grpFill", "noFill"):
        if el.find(f"{A}{t}") is not None:
            return t
    return "inherit"


slide = prs.slides[1]
print("==== P2 all shapes ====")
for z, sh in enumerate(slide.shapes):
    L, T, W, H = sh.left / EMU, sh.top / EMU, sh.width / EMU, sh.height / EMU
    has_tx = getattr(sh, "has_text_frame", False)
    txt = ""
    if has_tx:
        t = sh.text_frame.text.strip().replace("\n", "|")
        txt = f" {t[:24]!r}" if t else " <empty-txBody>"
    print(f"  z{z} {sh.name} ({L:.2f},{T:.2f},{W:.2f},{H:.2f}) {fillinfo(sh)}{txt}")
