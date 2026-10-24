"""取证 5：rPr effectLst 阴影统计 + 遮挡未验证项触发源。只读。"""
from pptx import Presentation
from collections import Counter

PPTX = ("/Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/"
        "wengao-visual-v13-material-fix/reports/gate-13p-20261021/v14-readable-13p-dedicated.pptx")
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
prs = Presentation(PPTX)

cnt = Counter()
samples = []
for pi, slide in enumerate(prs.slides, 1):
    for sh in slide.shapes:
        if not getattr(sh, "has_text_frame", False):
            continue
        tx = sh.text_frame._txBody
        for tag in ("effectLst", "effectDag"):
            found = tx.findall(f".//{A}{tag}")
            if found:
                cnt[tag] += len(found)
                if len(samples) < 6:
                    eff = found[0]
                    kids = [k.tag.split('}')[-1] for k in eff]
                    attrs = {k.tag.split('}')[-1]: dict(k.attrib) for k in eff}
                    samples.append(f"P{pi} {sh.name}: {kids} {attrs}")
print("text-body effect counts:", dict(cnt))
for s in samples:
    print(" ", s)

# spPr 级 effectLst（形状阴影）
cnt2 = Counter()
for pi, slide in enumerate(prs.slides, 1):
    for sh in slide.shapes:
        try:
            sp = sh._element.spPr
        except Exception:
            continue
        if sp is not None and sp.findall(f"{A}effectLst"):
            cnt2[pi] += 1
print("shape spPr effectLst by page:", dict(cnt2))
