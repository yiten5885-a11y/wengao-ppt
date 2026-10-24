#!/usr/bin/env python3
"""只读取证：基线 v12 的深色+金色设计事实（页背景/主题色/代表 run 字色）。"""
import re
import sys

from pptx import Presentation

A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"

BASE = sys.argv[1] if len(sys.argv) > 1 else \
    "reports/p0-native-glyph-20261020/evidence/exact_v12.pptx"
prs = Presentation(BASE)

print("== 基线每页 p:bg ==")
for i, slide in enumerate(prs.slides, 1):
    bg = slide._element.find(f"{P}cSld/{P}bg")
    desc = "none(inherit master)"
    if bg is not None:
        bgPr = bg.find(f"{P}bgPr")
        if bgPr is not None:
            sf = bgPr.find(f"{A}solidFill")
            if sf is not None:
                c = sf.find(f"{A}srgbClr")
                if c is not None:
                    desc = "srgb " + (c.get("val") or "")
                sc = sf.find(f"{A}schemeClr")
                if sc is not None:
                    desc = "scheme " + (sc.get("val") or "")
            elif bgPr.find(f"{A}blipFill") is not None:
                desc = "blip(picture)"
            elif bgPr.find(f"{A}gradFill") is not None:
                desc = "grad"
        else:
            desc = "bgRef"
    print(f"P{i}: {desc}")

print()
print("== 主题色 clrScheme ==")
try:
    m = prs.slide_masters[0]
    theme_part = m.part.part_related_by(
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme")
    xml = theme_part.blob.decode("utf-8", "ignore")
    mm = re.search(r"<a:clrScheme.*?</a:clrScheme>", xml, re.S)
    if mm:
        for nam, v1, v2 in re.findall(
                r'<a:(dk1|lt1|dk2|lt2|accent1|accent2|accent3|accent4|accent5|'
                r'accent6|hlink|folHlink)>.*?(?:val="([0-9A-Fa-f]{6})"|'
                r'lastClr="([0-9A-Fa-f]{6})")', mm.group(0)):
            print(" ", nam, v1 or v2)
except Exception as e:  # noqa: BLE001
    print("  theme read failed:", e)

print()
print("== 代表页 run 字色/字号（P9 Text13/Text17、P1 标题） ==")
def dump_runs(slide_idx, names):
    sl = prs.slides[slide_idx]
    for sh in sl.shapes:
        if getattr(sh, "has_text_frame", False) and sh.name in names:
            for pa in sh.text_frame.paragraphs:
                for r in pa.runs:
                    try:
                        rgb = str(r.font.color.rgb)
                    except Exception:  # noqa: BLE001
                        rgb = "(inherit)"
                    sz = r.font.size.pt if r.font.size else "(inherit)"
                    print(f"  P{slide_idx+1} {sh.name} {r.text[:14]!r} sz={sz} rgb={rgb}")

dump_runs(8, {"Text 13", "Text 17"})
dump_runs(0, {"Text 1", "Text 2", "Text 3"})

print()
print("== P9 全形状填充（文本承载面用色） ==")
sl = prs.slides[8]
for sh in sl.shapes:
    el = getattr(sh, "_element", None)
    if el is None:
        continue
    spPr = el.find(f"{P}spPr")
    if spPr is None:
        print(f"  {sh.name}: ({sh.shape_type}) no spPr")
        continue
    parts = []
    for ch in spPr:
        tag = ch.tag.split("}")[-1]
        if tag in ("solidFill", "gradFill", "blipFill", "noFill", "pattFill"):
            srgb = ch.find(f"{A}srgbClr")
            alpha = ch.find(f"{A}srgbClr/{A}alpha")
            a = alpha.get("val") if alpha is not None else None
            parts.append(f"{tag}:{srgb.get('val') if srgb is not None else '?'}"
                         + (f"@{int(a)/1000:.0f}%" if a else ""))
    txt = sh.text_frame.text.strip()[:10] if getattr(sh, "has_text_frame", False) else ""
    print(f"  {sh.name}: {' '.join(parts) or '-'} {txt!r}")
