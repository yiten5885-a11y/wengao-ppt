"""第三轮取证 2：P2 PDF 字符框 + P7/P8/P10/P12 全形状清单 + Text21 br 检查。只读。"""
import pypdfium2 as pdfium
from pptx import Presentation

BASE = ("/Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/"
        "wengao-visual-v13-material-fix/reports/gate-13p-20261021")
PPTX = f"{BASE}/v14-readable-13p-dedicated.pptx"
PDF = f"{BASE}/render/v14-readable-13p-dedicated.pdf"
EMU = 914400
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
IN = 72.0

# ---------- 1) PDF 字符框：P2 全部字符（判 TOKEN_SPLIT 性质） ----------
doc = pdfium.PdfDocument(PDF)
page = doc[1]  # P2
H = page.get_size()[1] / IN
tp = page.get_textpage()
print("==== P2 PDF charboxes (y in inches from top) ====")
rows = {}
for i in range(tp.count_chars()):
    ch = tp.get_text_range(i, 1)
    if not ch.strip():
        continue
    b = tp.get_charbox(i)  # (l, b, r, t) PDF 坐标（原点左下）
    y0 = H - b[3] / IN
    y1 = H - b[1] / IN
    x0 = b[0] / IN
    yc = (y0 + y1) / 2
    rows.setdefault(round(yc, 2), []).append((round(x0, 2), ch))
for yc in sorted(rows):
    if 1.7 <= yc <= 7.1:
        cs = "".join(c for _, c in sorted(rows[yc]))
        print(f"  y={yc}: {cs[:64]}")

# ---------- 2) P7/P8/P10/P12 全形状 ----------
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


for pi in (7, 8, 10, 12):
    slide = prs.slides[pi - 1]
    print(f"==== P{pi} all shapes ====")
    for z, sh in enumerate(slide.shapes):
        L, T, W, Hh = sh.left / EMU, sh.top / EMU, sh.width / EMU, sh.height / EMU
        has_tx = getattr(sh, "has_text_frame", False)
        txt = ""
        if has_tx:
            t = sh.text_frame.text.strip().replace("\n", "|")
            txt = f" {t[:26]!r}" if t else " <empty-txBody>"
        print(f"  z{z} {sh.name} ({L:.2f},{T:.2f},{W:.2f},{Hh:.2f}) {fillinfo(sh)}{txt}")

# ---------- 3) P2 Text 21 br 检查 ----------
sl2 = prs.slides[1]
for sh in sl2.shapes:
    if sh.name == "Text 21":
        tx = sh.text_frame._txBody
        brs = tx.findall(f".//{A}br")
        print(f"==== P2 Text 21: br count = {len(brs)}")
        for p_i, p in enumerate(tx.findall(f"{A}p")):
            parts = []
            for child in p:
                tag = child.tag.split('}')[-1]
                if tag == "r":
                    ts = child.find(f"{A}t")
                    parts.append(ts.text if ts is not None and ts.text else "")
                elif tag == "br":
                    parts.append("<BR>")
            print(f"  para{p_i}: {''.join(parts)!r}")
