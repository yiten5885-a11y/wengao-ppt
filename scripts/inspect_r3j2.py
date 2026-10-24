"""取证 10：找文字描边 a:ln 的继承来源。只读。"""
import re
import zipfile
from pptx import Presentation
from lxml import etree

PPTX = ("/Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/"
        "wengao-visual-v13-material-fix/reports/gate-13p-20261021/v14-readable-13p-dedicated.pptx")
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
prs = Presentation(PPTX)


def show(el, label):
    if el is None:
        return
    xml = etree.tostring(el).decode().replace(f"{{{A}}}", "a:")
    if "<a:ln" in xml:
        print(f"  {label}: HAS a:ln → {xml[:400]}")


for pi, slide in enumerate(prs.slides, 1):
    for sh in slide.shapes:
        if not getattr(sh, "has_text_frame", False):
            continue
        if (pi, sh.name) not in {(7, "Text 12"), (1, "Text 10"), (2, "Text 21"), (13, "Text 3")}:
            continue
        tx = sh.text_frame._txBody
        print(f"P{pi} {sh.name}:")
        show(tx.find(f"{A}lstStyle"), "txBody/lstStyle")
        for p in tx.findall(f"{A}p"):
            ppr = p.find(f"{A}pPr")
            if ppr is not None:
                show(ppr.find(f"{A}defRPr"), "pPr/defRPr")

z = zipfile.ZipFile(PPTX)
for n in z.namelist():
    if "theme" in n:
        xml = z.read(n).decode("utf-8", errors="replace")
        cnt = xml.count("<a:ln")
        print(f"{n}: <a:ln> x{cnt}")
        for m in re.finditer(r"<a:ln[^>]*>.{0,200}", xml[:200000]):
            print("   ", m.group(0)[:220])
