#!/usr/bin/env python3
"""真实字形级门禁：越界 + 真实字形碰撞 + 文本溢出（监督 01:20 要求的反向用例）。

为什么要有它
------------
对象框不相交、字形不越画布，**不等于真实字形不相撞**。失败件
SHA 5a6bcb2d 的 P2 就是反例：卡片说明在 26pt 下真实折成两行，字形纵向
2.8284–3.7159in，既压住上方 +0.9%（2.4545–2.9269）也压住下方 Syntun 来源
（3.5783–3.8475），而三个"对象框"彼此并不相交。本门禁测的是**渲染出来的字形**。

判据（三条，任一命中即失败）
----------------------------
1. FRAME_OVERFLOW：文本形状的真实字形框超出自身文本框（容差 0.03in）；
2. GLYPH_COLLISION：两个不同文本形状的真实字形框互相侵入（x/y 同时 >0.03in）；
3. CANVAS_OUT：字形超出画布（容差 0.05in）。

做法：LibreOffice 真实渲染 → pypdfium2 取**字符级**字形框 → 按基线聚成行 →
把行归属到文本形状（先看文字包含关系，再退回框重叠）→ 逐形状求并集再比对。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pptx import Presentation  # noqa: E402

IN = 72.0
FRAME_TOL_IN = 0.03     # 字形超出文本框的容差
COLLIDE_TOL_IN = 0.03   # 两个字形框互相侵入的最小量
CANVAS_TOL_IN = 0.05
LINE_CLUSTER_IN = 0.11  # 同一行的字符中心纵向聚类半径

SOFFICE = "/Applications/LibreOffice.app/Contents/MacOS/soffice"


def sha256(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def render_pdf(pptx: Path, outdir: Path) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    fc = ROOT / ".fontconfig/fonts.conf"
    env = dict(os.environ)
    if fc.exists():
        env["FONTCONFIG_FILE"] = str(fc)
    subprocess.run([SOFFICE, "--headless", "--norestore", "--convert-to", "pdf",
                    "--outdir", str(outdir), str(pptx)],
                   env=env, check=True, capture_output=True, timeout=300)
    out = outdir / (Path(pptx).stem + ".pdf")
    if not out.exists():
        raise SystemExit(f"[glyph_gate] 渲染失败: {out}")
    return out


_PUNCT = "，。、；：！？“”‘’（）()《》〈〉·—…～%+-–"


def _norm(s: str) -> str:
    """匹配用归一化：去空白与常见标点（折行会把标点甩到单独一行/OCR 顺序不定）。"""
    s = "".join((s or "").split())
    return "".join(ch for ch in s if ch not in _PUNCT)


_X_GAP_IN = 0.30   # 同一纵向簇内，横向断续超过该值即视为不同文本块


def _split_by_x(grp):
    grp = sorted(grp, key=lambda c: c[0])
    runs, cur = [], [grp[0]]
    for prev, c in zip(grp, grp[1:]):
        if c[0] - prev[2] > _X_GAP_IN:
            runs.append(cur)
            cur = [c]
        else:
            cur.append(c)
    runs.append(cur)
    return runs


def page_lines(page) -> list:
    """字符级字形框 → 行（英寸，top-down）。返回 [(x0,y0,x1,y1,text)]。"""
    H = page.get_size()[1] / IN
    tp = page.get_textpage()
    n = tp.count_chars()
    chars = []
    for i in range(n):
        try:
            box = tp.get_charbox(i)
            ch = tp.get_text_range(i, 1)
        except Exception:  # noqa: BLE001
            continue
        if not ch or not ch.strip():
            continue
        x0, y0r, x1, y1r = box
        ytop, ybot = H - y1r / IN, H - y0r / IN
        chars.append((x0 / IN, ytop, x1 / IN, ybot, ch))
    # 按纵向中心聚类成行
    chars.sort(key=lambda c: ((c[1] + c[3]) / 2, c[0]))
    lines, cur = [], []
    for c in chars:
        if not cur:
            cur = [c]
            continue
        cy = (c[1] + c[3]) / 2
        ref = sum((k[1] + k[3]) / 2 for k in cur) / len(cur)
        if abs(cy - ref) <= LINE_CLUSTER_IN:
            cur.append(c)
        else:
            lines.append(cur)
            cur = [c]
    if cur:
        lines.append(cur)
    out = []
    for grp in lines:
        for run in _split_by_x(grp):
            out.append((min(c[0] for c in run), min(c[1] for c in run),
                        max(c[2] for c in run), max(c[3] for c in run),
                        "".join(c[4] for c in run)))
    return out


def assign(lines, shapes):
    """行 → 文本形状。返回 {shape_idx: bbox}，以及未归属行。"""
    buckets, unmatched = {}, []
    for ln in lines:
        lx0, ly0, lx1, ly1, lt = ln
        nt = _norm(lt)
        if not nt:
            continue          # 纯标点/装饰引号行：不参与归属，避免误判溢出
        best, best_score = None, None
        for si, (name, sx0, sy0, sx1, sy1, st) in shapes.items():
            contains = nt and nt in _norm(st)
            # 横向重叠度
            ox = min(lx1, sx1) - max(lx0, sx0)
            if ox <= 0:
                continue
            cy = (ly0 + ly1) / 2
            dy = 0.0 if sy0 <= cy <= sy1 else min(abs(cy - sy0), abs(cy - sy1))
            score = (0 if contains else 1000.0) + dy + max(0.0, -ox) * 0.01
            if best_score is None or score < best_score:
                best, best_score = si, score
        if best is None or (best_score or 0) >= 1000.0:
            unmatched.append({"text": lt[:40], "box": [round(v, 3) for v in ln[:4]]})
            continue
        b = buckets.get(best)
        if b is None:
            buckets[best] = [lx0, ly0, lx1, ly1]
        else:
            b[0] = min(b[0], lx0); b[1] = min(b[1], ly0)
            b[2] = max(b[2], lx1); b[3] = max(b[3], ly1)
    return buckets, unmatched


def check(pptx: Path, pdf: Path, pages=None) -> dict:
    import pypdfium2 as pdfium
    prs = Presentation(str(pptx))
    doc = pdfium.PdfDocument(str(pdf))
    W_IN, H_IN = prs.slide_width / 914400.0, prs.slide_height / 914400.0
    findings, per_page = [], {}
    for idx, slide in enumerate(prs.slides, 1):
        if pages and idx not in pages:
            continue
        shapes = {}
        for si, sh in enumerate(slide.shapes):
            if not getattr(sh, "has_text_frame", False):
                continue
            txt = sh.text_frame.text
            if not txt.strip():
                continue
            shapes[si] = (sh.name, sh.left / 914400.0, sh.top / 914400.0,
                          (sh.left + sh.width) / 914400.0,
                          (sh.top + sh.height) / 914400.0, txt)
        lines = page_lines(doc[idx - 1])
        buckets, unmatched = assign(lines, shapes)
        page_items = []
        # 1) 越界 + 2) 画布
        for si, gb in buckets.items():
            name, sx0, sy0, sx1, sy1, _t = shapes[si]
            sides = []
            if gb[0] < sx0 - FRAME_TOL_IN: sides.append("left")
            if gb[1] < sy0 - FRAME_TOL_IN: sides.append("top")
            if gb[2] > sx1 + FRAME_TOL_IN: sides.append("right")
            if gb[3] > sy1 + FRAME_TOL_IN: sides.append("bottom")
            if sides:
                findings.append({"page": idx, "type": "FRAME_OVERFLOW", "shape": name,
                                 "sides": sides,
                                 "declared": [round(v, 3) for v in (sx0, sy0, sx1, sy1)],
                                 "glyph": [round(v, 3) for v in gb]})
                page_items.append(f"OVERFLOW {name} {sides} glyph={[round(v,2) for v in gb]}")
            if (gb[0] < -CANVAS_TOL_IN or gb[1] < -CANVAS_TOL_IN
                    or gb[2] > W_IN + CANVAS_TOL_IN or gb[3] > H_IN + CANVAS_TOL_IN):
                findings.append({"page": idx, "type": "CANVAS_OUT", "shape": name,
                                 "glyph": [round(v, 3) for v in gb]})
        # 3) 真实字形碰撞
        keys = list(buckets)
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                a, b = buckets[keys[i]], buckets[keys[j]]
                dx = min(a[2], b[2]) - max(a[0], b[0])
                dy = min(a[3], b[3]) - max(a[1], b[1])
                if dx > COLLIDE_TOL_IN and dy > COLLIDE_TOL_IN:
                    na, nb = shapes[keys[i]][0], shapes[keys[j]][0]
                    findings.append({"page": idx, "type": "GLYPH_COLLISION",
                                     "shapes": [na, nb],
                                     "overlap_in": [round(dx, 3), round(dy, 3)],
                                     "glyph_a": [round(v, 3) for v in a],
                                     "glyph_b": [round(v, 3) for v in b]})
                    page_items.append(f"COLLIDE {na}×{nb} {dx:.2f}x{dy:.2f}in")
        per_page[idx] = {"lines": len(lines), "shapes_with_glyphs": len(buckets),
                         "unmatched_lines": len(unmatched), "issues": page_items}
    return {"pptx": str(pptx), "pptx_sha256": sha256(pptx), "pdf": str(pdf),
            "pdf_sha256": sha256(pdf), "pages_checked": sorted(per_page),
            "canvas_in": [round(W_IN, 3), round(H_IN, 3)],
            "tolerances": {"frame_in": FRAME_TOL_IN, "collision_in": COLLIDE_TOL_IN,
                           "canvas_in": CANVAS_TOL_IN},
            "findings": findings, "per_page": per_page,
            "verdict": "PASS" if not findings else "FAIL",
            "counts": {"overflow": sum(1 for f in findings if f["type"] == "FRAME_OVERFLOW"),
                       "collision": sum(1 for f in findings if f["type"] == "GLYPH_COLLISION"),
                       "canvas_out": sum(1 for f in findings if f["type"] == "CANVAS_OUT")}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pptx")
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--pages", default=None, help="逗号分隔页号，默认全部")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()
    pptx = Path(args.pptx).resolve()
    outdir = Path(args.outdir) if args.outdir else pptx.parent / "render"
    if outdir.exists():
        for f in outdir.glob("*.pdf"):
            f.unlink()
    pdf = render_pdf(pptx, outdir)
    pages = [int(x) for x in args.pages.split(",")] if args.pages else None
    rep = check(pptx, pdf, pages)
    out = Path(args.json) if args.json else pptx.parent / "glyph_gate_result.json"
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"pptx {rep['pptx_sha256'][:16]}  pdf {rep['pdf_sha256'][:16]}")
    print("verdict:", rep["verdict"], "| counts:", rep["counts"])
    for f in rep["findings"][:40]:
        if f["type"] == "GLYPH_COLLISION":
            print(f"  P{f['page']} COLLISION {f['shapes']} {f['overlap_in']}")
        else:
            print(f"  P{f['page']} {f['type']} {f.get('shape')} {f.get('sides') or ''} "
                  f"glyph={f.get('glyph')}")
    print("report:", out)
    return 0 if rep["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
