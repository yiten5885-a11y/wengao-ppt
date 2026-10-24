"""取证 7：dump 问题 run 的完整 rPr XML。只读。"""
from pptx import Presentation

PPTX = ("/Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/"
        "wengao-visual-v13-material-fix/reports/gate-13p-20261021/v14-readable-13p-dedicated.pptx")
prs = Presentation(PPTX)

targets = {(7, "Text 12"), (2, "Text 21"), (1, "Text 10"), (12, "Text 1"), (11, "Text 29")}
for pi, slide in enumerate(prs.slides, 1):
    for sh in slide.shapes:
        if not getattr(sh, "has_text_frame", False):
            continue
        if (pi, sh.name) not in targets:
            continue
        for r_el in sh.text_frame._txBody.findall(".//{http://schemas.openxmlformats.org/drawingml/2006/main}r")[:2]:
            rpr = r_el.find("{http://schemas.openxmlformats.org/drawingml/2006/main}rPr")
            txt = r_el.find("{http://schemas.openxmlformats.org/drawingml/2006/main}t")
            from lxml import etree
            xml = etree.tostring(rpr, pretty_print=False).decode() if rpr is not None else "None"
            xml = xml.replace("{http://schemas.openxmlformats.org/drawingml/2006/main}", "a:")
            print(f"P{pi} {sh.name} run {txt.text[:8]!r}:")
            print("   ", xml[:700])
