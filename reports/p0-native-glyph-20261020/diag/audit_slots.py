#!/usr/bin/env python3
"""P0 诊断：exact v12 的 OOXML 字体槽全量审计。

对每个 slide/layout/master/theme 的每个 run（含表格/备注），记录：
  latin / ea / cs 的 typeface+charset、lang、是否含 CJK 文本。
重点找出：**含 CJK 但没有 a:ea 的 run**（LibreOffice 对这类 run 用
latin 槽选 CJK → Avenir Next 无汉字 → 原生缺字方框）。
"""
import re
import zipfile
from collections import Counter
from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / "reports/p0-native-glyph-20261020/evidence/exact_v12.pptx"

CJK_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")
RUN_RE = re.compile(rb"<a:r>(.*?)</a:r>", re.S)
TEXT_RE = re.compile(rb"<a:t>(.*?)</a:t>", re.S)
LATIN_RE = re.compile(rb'<a:latin typeface="([^"]*)"(?:\s+charset="(-?\d+)")?[^>]*/>')
EA_RE = re.compile(rb'<a:ea typeface="([^"]*)"(?:\s+charset="(-?\d+)")?[^>]*/>')
CS_RE = re.compile(rb'<a:cs typeface="([^"]*)"(?:\s+charset="(-?\d+)")?[^>]*/>')
LANG_RE = re.compile(rb'lang="([^"]*)"')


def unesc(b: bytes) -> str:
    return (b.decode("utf-8")
            .replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&apos;", "'"))


def audit_part(name: str, xml: bytes):
    rows = []
    for m in RUN_RE.finditer(xml):
        run = m.group(1)
        tm = TEXT_RE.search(run)
        if not tm:
            continue
        text = unesc(tm.group(1))
        if not text.strip():
            continue
        latin = LATIN_RE.search(run)
        ea = EA_RE.search(run)
        cs = CS_RE.search(run)
        lang = LANG_RE.search(run)
        rows.append({
            "part": name,
            "text": text,
            "cjk": bool(CJK_RE.search(text)),
            "latin": (latin.group(1).decode(), latin.group(2).decode() if latin.group(2) else None) if latin else None,
            "ea": (ea.group(1).decode(), ea.group(2).decode() if ea.group(2) else None) if ea else None,
            "cs": (cs.group(1).decode(), cs.group(2).decode() if cs.group(2) else None) if cs else None,
            "lang": lang.group(1).decode() if lang else None,
        })
    return rows


def main():
    z = zipfile.ZipFile(SRC)
    parts = [n for n in z.namelist()
             if re.fullmatch(r"ppt/(slides/slide|slideLayouts/slideLayout|slideMasters/slideMaster|notesSlides/notesSlide)\d+\.xml", n)]
    parts += [n for n in z.namelist() if "theme" in n and n.endswith(".xml")]
    all_rows = []
    for n in sorted(parts):
        all_rows += audit_part(n, z.read(n))

    cjk_rows = [r for r in all_rows if r["cjk"]]
    no_ea = [r for r in cjk_rows if not r["ea"]]
    print(f"总 run 数: {len(all_rows)}；含 CJK 的 run: {len(cjk_rows)}")
    print(f"含 CJK 但**没有 a:ea** 的 run: {len(no_ea)}")
    print()
    print("== CJK run 的字体槽组合分布 ==")
    combo = Counter()
    for r in cjk_rows:
        lat = r["latin"][0] if r["latin"] else "-"
        ea = r["ea"][0] if r["ea"] else "-"
        cs = r["cs"][0] if r["cs"] else "-"
        combo[(lat, ea, cs)] += 1
    for (lat, ea, cs), n in combo.most_common():
        print(f"  ×{n:4d}  latin={lat!r} ea={ea!r} cs={cs!r}")
    print()
    print("== 含 CJK 但无 ea 的 run 明细（前 25 条）==")
    for r in no_ea[:25]:
        print(f"  [{r['part']}] latin={r['latin']} cs={r['cs']} lang={r['lang']} text={r['text'][:40]!r}")
    print()
    print("== CJK run 的 lang 分布 ==")
    for k, v in Counter(r["lang"] for r in cjk_rows).most_common():
        print(f"  {k}: {v}")
    print()
    print("== ea charset 值分布 ==")
    for k, v in Counter(r["ea"][1] if r["ea"] else None for r in cjk_rows).most_common():
        print(f"  charset={k}: {v}")
    print()
    print("== cs 非空时的字体分布 ==")
    for k, v in Counter(r["cs"][0] if r["cs"] else None for r in cjk_rows).most_common():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
