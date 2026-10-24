#!/usr/bin/env python3
"""P0 诊断分析：PDF 文本对象 → 每段文字的实际渲染字体。

PDFium 文本对象对应内容流里的 Tf/Tj 单元；get_font().get_base_font_name()
给出 LibreOffice 实际用于该段文字的字体。对 slide 6（用户缺字页）逐段
输出 文字 → 字体，直接判定 CJK 字符走了哪个字体槽（latin/ea/cs/回退）。
"""
import sys
import pypdfium2 as pdfium


def analyze(pdf_path, page_idx=5):
    pdf = pdfium.PdfDocument(pdf_path)
    page = pdf[page_idx]
    tp = page.get_textpage()
    n = tp.count_rects()
    rows = []
    for i in range(n):
        obj = tp.get_textobj(i)
        try:
            font = obj.get_font()
            fname = font.get_base_name() if font else "?"
        except Exception as e:  # noqa: BLE001
            fname = f"ERR:{type(e).__name__}"
        try:
            text = obj.extract()
        except Exception:  # noqa: BLE001
            text = ""
        rows.append((fname, (text or "").replace("\n", " / ")))
    return rows


def main():
    base = sys.argv[1]
    variants = sys.argv[2:] or ["D0-exact", "D1-lang-zhCN", "D2-no-charset",
                                "D3-cs-hiragino", "D4-latin-hiragino", "D5-latin-songti"]
    for v in variants:
        path = f"{base}/{v}.pdf"
        try:
            rows = analyze(path)
        except Exception as e:  # noqa: BLE001
            print(f"== {v}: 读取失败 {e}")
            continue
        print(f"===== {v}（slide 6，{len(rows)} 个文本对象）=====")
        # 只看含中文的段 + 统计每种字体承载的字符数
        from collections import Counter
        font_chars = Counter()
        for fname, text in rows:
            cjk = [c for c in text if "\u4e00" <= c <= "\u9fff"]
            font_chars[fname] += len(cjk)
            if cjk:
                print(f"  [{fname}] {text[:52]!r}")
        print("  CJK 字符按字体分布:", dict(font_chars))
        print()


if __name__ == "__main__":
    main()
