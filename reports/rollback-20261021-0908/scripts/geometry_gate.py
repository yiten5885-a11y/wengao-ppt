#!/usr/bin/env python3
"""几何级可见性验收（监督 2026-10-20 18:07/19:10 反馈的门禁化）。

两级测量：
A. **PPTX 级框体几何**（监督 19:10 要求的重点）：
   1. 全部形状框体在画布内（bottom/right 不超界）——refine 移动框体但
      文字可能仍可见（adaptive P2 左栏 T=4.04/底部 9.14in 的缺陷形态），
      只查 PDF 字形位置抓不到，必须量框体；
   2. 并排文本框两栏**同顶同高**（top 差 ≤0.05in）——目录/双栏正文对齐。
B. **渲染 PDF 级字形几何**：
   3. 全部文本在画布内（无出界/裁切）——PDF 抽字完整 ≠ 画面可见；
   4. 全页文本两两不重叠（遮挡门禁）；
   5. 目录页全部章节名在画布内可见；
   6. 内容条款（规范化空白后子串：三处原句 + 客户成功团队人手不足）；
   7. 页数 10–20。

PDF 文本坐标是底部原点（实测），统一翻转为 top-down：y_top = H - y1_raw。
"""
import sys, re, glob, os
import pypdfium2 as pdfium
from pptx import Presentation

CLAUSES = ["接口治理、续约预警三项改进措施绑定推进",
           "主要短板在跨区域数据同步延迟与老接口维护成本",
           "年内完成三大能力建设",
           "客户成功团队人手不足"]
CHAPTERS = ["一、年度经营概况", "二、产品能力进展", "三、客户与生态", "四、成本与效率",
            "五、质量与稳定性", "六、问题与改进", "七、下年度计划", "八、结论"]
CANVAS_W, CANVAS_H = 13.333, 7.5
ALIGN_TOL = 0.05      # 两栏同顶/同高容差（in）
FRAME_TOL = 0.02      # 框体边界容差（in）


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def page_texts(page):
    """[(x0, y_top, x1, y_bottom, text)]，top-down 英寸坐标（按文本块粗排）。"""
    H = page.get_size()[1]
    tp = page.get_textpage()
    out = []
    for i in range(tp.count_rects()):
        r = tp.get_rect(i)
        t = (tp.get_text_bounded(*r) or "").strip()
        if not t:
            continue
        out.append((r[0] / 72.0, (H - r[3]) / 72.0, r[2] / 72.0, (H - r[1]) / 72.0, t))
    return out


def check_frames(theme, pptx_path):
    """A 级：PPTX 框体边界 + 并排文本框两栏对齐。"""
    problems = []
    prs = Presentation(pptx_path)
    sw = (prs.slide_width or 0) / 914400.0
    sh_ = (prs.slide_height or 0) / 914400.0
    if abs(sw - CANVAS_W) > 0.05 or abs(sh_ - CANVAS_H) > 0.05:
        problems.append(f"画布尺寸异常: {sw:.2f}x{sh_:.2f}")
    for si, slide in enumerate(prs.slides, 1):
        boxes = []
        for sh in slide.shapes:
            L = (sh.left or 0) / 914400.0
            T = (sh.top or 0) / 914400.0
            W = (sh.width or 0) / 914400.0
            H = (sh.height or 0) / 914400.0
            if W <= 0 or H <= 0:
                continue
            # 1) 框体边界
            if T + H > sh_ + FRAME_TOL or L + W > sw + FRAME_TOL or T < -FRAME_TOL or L < -FRAME_TOL:
                problems.append(
                    f"P{si} 框体出界: {sh.name!r} "
                    f"L{L:.2f} T{T:.2f} R{L+W:.2f} B{T+H:.2f}")
            # 收集有文字的文本框（对齐检查对象）
            if (getattr(sh, "has_text_frame", False) and sh.has_text_frame
                    and (sh.text_frame.text or "").strip()):
                boxes.append((sh.name, L, T, W, H))
        # 2) 并排文本框两栏对齐：y 区间重叠 >50% 且 x 区间不重叠
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                na, la, ta, wa, ha = boxes[i]
                nb, lb, tb, wb, hb = boxes[j]
                oy = min(ta + ha, tb + hb) - max(ta, tb)
                if oy <= 0.5 * min(ha, hb):
                    continue
                if not (la + wa <= lb + 0.02 or lb + wb <= la + 0.02):
                    continue          # x 区间相交 → 不是并排两栏
                if abs(ta - tb) > ALIGN_TOL or abs(ha - hb) > ALIGN_TOL:
                    problems.append(
                        f"P{si} 两栏未对齐: {na!r}(T{ta:.2f}/H{ha:.2f}) × "
                        f"{nb!r}(T{tb:.2f}/H{hb:.2f})")
    return problems


def check_theme(theme, pdf_path, pptx_path=None):
    problems = []
    if pptx_path and os.path.isfile(pptx_path):
        problems += [f"[框体] {p}" for p in check_frames(theme, pptx_path)]
    pdf = pdfium.PdfDocument(pdf_path)
    n = len(pdf)
    if not (10 <= n <= 20):
        problems.append(f"页数 {n} 不在 10–20")
    all_text = []
    for pi in range(n):
        texts = page_texts(pdf[pi])
        all_text.append("\n".join(t for *_, t in texts))
        # 3) 画布内
        for x0, y0, x1, y1, t in texts:
            if y1 > CANVAS_H + 0.02 or x1 > CANVAS_W + 0.02 or y0 < -0.02 or x0 < -0.02:
                problems.append(f"P{pi+1} 文本出界: {t[:24]!r} y[{y0:.2f},{y1:.2f}] x[{x0:.2f},{x1:.2f}]")
        # 4) 文本两两不重叠（遮挡门禁；容差 0.05in 边长避免相邻行误报）。
        #    例外：图表旋转轴标签的相邻字形包络盒天然相交（同一串旋转文字
        #    的碎片，窄块且共享子串）——不是遮挡；真实遮挡（表格压说明等）
        #    双方是互不为子串的独立文本块。
        def _frag_pair(a, b) -> bool:
            if (a[2] - a[0]) >= 1.2 or (b[2] - b[0]) >= 1.2:
                return False
            ta, tb = _norm(a[4]), _norm(b[4])
            if not ta or not tb:
                return False
            for k in range(min(len(ta), len(tb)), 1, -1):
                if any(ta[i:i + k] in tb for i in range(len(ta) - k + 1)):
                    return True
            return False
        for i in range(len(texts)):
            for j in range(i + 1, len(texts)):
                a, b = texts[i], texts[j]
                ix = min(a[2], b[2]) - max(a[0], b[0])
                iy = min(a[3], b[3]) - max(a[1], b[1])
                if ix > 0.05 and iy > 0.05 and not _frag_pair(a, b):
                    problems.append(
                        f"P{pi+1} 文本重叠: {a[4][:18]!r} × {b[4][:18]!r} "
                        f"(交叠 {ix:.2f}×{iy:.2f}in)")
        # 5) 目录页章节名可见
        if pi == 1:
            joined = _norm("".join(t for *_, t in texts))
            for ch in CHAPTERS:
                if _norm(ch) not in joined:
                    problems.append(f"目录缺章节: {ch}")
    full = _norm("\n".join(all_text))
    for c in CLAUSES:
        if _norm(c) not in full:
            problems.append(f"内容条款缺失: {c}")
    return {"theme": theme, "pages": n, "problems": problems}


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else "themes"
    results = []
    for d in sorted(glob.glob(os.path.join(base, "wengao-*"))):
        if ".bak" in os.path.basename(d):
            continue          # 导出备份目录（历史渲染），不是本轮验收对象
        pdfp = os.path.join(d, "render.pdf")
        pptxp = os.path.join(d, "annual_review_synthetic.generated.wengao.pptx")
        if os.path.isfile(pdfp):
            results.append(check_theme(os.path.basename(d), pdfp, pptxp))
    for r in results:
        status = "PASS" if not r["problems"] else "FAIL"
        print(f"[{status}] {r['theme']} ({r['pages']} 页)")
        for p in r["problems"][:10]:
            print(f"    - {p}")
    bad = [r for r in results if r["problems"]]
    print(f"\n合计 {len(results)} 主题，{len(bad)} 个未通过")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
