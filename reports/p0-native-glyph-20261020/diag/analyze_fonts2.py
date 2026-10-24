#!/usr/bin/env python3
"""P0 诊断分析 v2：PDF 文本对象 → 每段文字的实际渲染字体（修正文本提取）。

- 每对象文本用 textpage.get_text_bounded(*obj.get_bounds())（extract()
  在本版本 pypdfium2 上对所有对象返回同一串，不可用）；
- 字体用 obj.get_font().get_base_name()；
- 对全部 13 页统计：每段文字 → 字体；CJK 字符按字体分布；
- D5 探针逻辑：latin=Songti SC / ea=Hiragino Sans GB。CJK 若渲染为
  Songti → LibreOffice 用 latin 槽选 CJK；若为 Hiragino → 用 ea 槽。
"""
import sys
from collections import Counter
import pypdfium2 as pdfium


def page_rows(page):
    tp = page.get_textpage()
    rows = []
    for i in range(tp.count_rects()):
        obj = tp.get_textobj(i)
        try:
            font = obj.get_font()
            fname = font.get_base_name() if font else "?"
        except Exception as e:  # noqa: BLE001
            fname = f"ERR:{type(e).__name__}"
        try:
            b = obj.get_bounds()
            text = tp.get_text_bounded(b[0], b[1], b[2], b[3]) or ""
        except Exception:  # noqa: BLE001
            text = ""
        rows.append((fname, text.replace("\n", " / ")))
    return rows


def is_cjk(ch):
    return "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f" or "\uff00" <= ch <= "\uffef"


def main():
    base = sys.argv[1]
    variants = sys.argv[2:] or ["D0-exact", "D1-lang-zhCN", "D2-no-charset",
                                "D3-cs-hiragino", "D4-latin-hiragino", "D5-latin-songti"]
    for v in variants:
        path = f"{base}/{v}.pdf"
        pdf = pdfium.PdfDocument(path)
        print(f"===== {v}（{len(pdf)} 页）=====")
        total = Counter()
        for pi in range(len(pdf)):
            rows = page_rows(pdf[pi])
            cjk_by_font = Counter()
            for fname, text in rows:
                n = sum(1 for c in text if is_cjk(c))
                if n:
                    cjk_by_font[fname] += n
                    total[fname] += n
            if cjk_by_font:
                dist = ", ".join(f"{k}×{n}" for k, n in cjk_by_font.most_common())
                print(f"  P{pi+1}: CJK 字体分布 → {dist}")
        print(f"  合计: {dict(total)}")
        print()


if __name__ == "__main__":
    main()
