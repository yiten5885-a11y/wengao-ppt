"""第三轮取证：问题形状的 bbox/fill/alpha/run 字色。只读，不改任何文件。"""
from pptx import Presentation

P = ("/Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/"
     "wengao-visual-v13-material-fix/reports/gate-13p-20261021/v14-readable-13p-dedicated.pptx")
prs = Presentation(P)
EMU = 914400
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


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


def runinfo(sh):
    out = []
    try:
        for p in sh.text_frame.paragraphs:
            for r in p.runs:
                col = "?"
                try:
                    ct = r.font.color.type
                    if ct is not None:
                        col = (str(r.font.color.rgb) if str(ct).startswith("MSO_THEME") is False and str(ct) != "SCHEME"
                               else f"theme:{r.font.color.theme_color}")
                except Exception:
                    col = "ERR"
                out.append(f"{r.text[:14]!r}@{r.font.size.pt if r.font.size else None}/{col}/{'B' if r.font.bold else '-'}")
    except Exception:
        pass
    return " | ".join(out[:10])


pages = {
    2: ["Text 4", "Text 5", "Text 8", "Text 12", "Text 16", "Text 17", "Text 21",
        "Shape 3", "Shape 19"],
    3: ["Shape 5", "Text 4"],
    7: ["Shape 18", "Text 17"],
    8: ["Shape 13", "Text 17"],
    10: ["Text 2"],
    11: ["Rectangle 34", "Text 16", "Text 19", "Text 22", "Text 25", "Text 29", "Shape 27"],
    12: ["Text 1"],
}
for pi, slide in enumerate(prs.slides, 1):
    if pi not in pages:
        continue
    print(f"===== P{pi} =====")
    for z, sh in enumerate(slide.shapes):
        if sh.name not in pages[pi]:
            continue
        L, T, W, H = sh.left / EMU, sh.top / EMU, sh.width / EMU, sh.height / EMU
        has_tx = getattr(sh, "has_text_frame", False)
        txt = sh.text_frame.text[:46].replace("\n", "\\n") if has_tx else ""
        print(f"z{z} {sh.name} ({L:.2f},{T:.2f},{W:.2f},{H:.2f}) fill={fillinfo(sh)}")
        if txt:
            print(f"     text={txt!r}")
        if has_tx:
            ri = runinfo(sh)
            if ri:
                print(f"     runs: {ri}")
