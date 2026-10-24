#!/usr/bin/env python3
"""冻结小批样稿：基于不可变 exact_v12 生成 P2/P6/P8 可读重排 + 东亚字体修正。

只写候选目录内的 reports/readability-20261021/，不覆盖 D0–D5/S6 等既有产物，
不改 8821/8823，不发布。基线 exact_v12.pptx 只读（生成前后校验 SHA 不变）。

输出：
- v13-readable-P268-13p.pptx  13 页（页数/页序/文字/备注/转场/配图/粒子守恒，
                              仅 P2/P6/P8 重排，全稿东亚字体槽改 Arial Unicode MS）
- readable_p268_report.json   身份、字号分布、几何与守恒校验结果
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pptx import Presentation  # noqa: E402
from wengao_ppt import readability as rb  # noqa: E402

BASELINE = ROOT / "reports/p0-native-glyph-20261020/evidence/exact_v12.pptx"
OUT_DIR = ROOT / "reports/readability-20261021"
OUT_PPTX = OUT_DIR / "v13-readable-P268-13p.pptx"
OUT_PPTX_ALL = OUT_DIR / "v13-readable-13p-glyphfit.pptx"
OUT_JSON = OUT_DIR / "readable_p268_report.json"
OUT_JSON_ALL = OUT_DIR / "readable_13p_report.json"
BASELINE_SHA = "e43dad9e36c07a78b5a4d8490448427e8802886730e4091f266e6a451d14ea9b"

IN = 914400.0
TOL_IN = 0.02          # 画布边界容差
OVERLAP_MIN_IN2 = 0.01  # 文字框互相遮挡的最小面积


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def slide_texts(pptx: Path) -> dict:
    """{page: [a:t 文本...]}（含备注文本，单独返回）"""
    z = zipfile.ZipFile(pptx)
    names = sorted(
        [n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)],
        key=lambda n: int(re.search(r"(\d+)", n.split("/")[-1]).group(1)))
    out = {}
    for i, n in enumerate(names, 1):
        x = z.read(n).decode("utf8", errors="replace")
        out[i] = re.findall(r"<a:t>(.*?)</a:t>", x, re.S)
    return out


def notes_texts(pptx: Path) -> dict:
    z = zipfile.ZipFile(pptx)
    out = {}
    for n in z.namelist():
        m = re.fullmatch(r"ppt/notesSlides/notesSlide(\d+)\.xml", n)
        if not m:
            continue
        x = z.read(n).decode("utf8", errors="replace")
        out[int(m.group(1))] = re.findall(r"<a:t>(.*?)</a:t>", x, re.S)
    return out


def zip_names(pptx: Path):
    z = zipfile.ZipFile(pptx)
    return z.namelist()


def size_distribution(prs, page: int) -> list:
    slide = prs.slides[page - 1]
    rows = []
    for sh in slide.shapes:
        if not getattr(sh, "has_text_frame", False):
            continue
        txt = sh.text_frame.text.strip()
        pts = sorted({round(r.font.size.pt, 2) for p in sh.text_frame.paragraphs
                      for r in p.runs if r.font.size})
        if not pts:
            continue
        rows.append({
            "shape": sh.name, "pt": pts,
            "left_in": round(sh.left / IN, 3), "top_in": round(sh.top / IN, 3),
            "w_in": round(sh.width / IN, 3), "h_in": round(sh.height / IN, 3),
            "text": txt[:40],
        })
    return rows


def geometry_check(prs) -> dict:
    sw, sh_ = prs.slide_width / IN, prs.slide_height / IN
    problems = []
    boxes = []
    for idx, slide in enumerate(prs.slides, 1):
        for s in slide.shapes:
            if s.left is None or s.top is None:
                continue
            l, t = s.left / IN, s.top / IN
            w, h = s.width / IN, s.height / IN
            if l < -TOL_IN or t < -TOL_IN or l + w > sw + TOL_IN or t + h > sh_ + TOL_IN:
                problems.append(
                    f"P{idx} {s.name} 越界: ({l:.2f},{t:.2f})-({l + w:.2f},{t + h:.2f})")
            txt = s.text_frame.text.strip() if getattr(s, "has_text_frame", False) else ""
            if txt:
                boxes.append((idx, s.name, l, t, w, h))
    overlaps = []
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i], boxes[j]
            if a[0] != b[0]:
                continue
            ox = min(a[2] + a[4], b[2] + b[4]) - max(a[2], b[2])
            oy = min(a[3] + a[5], b[3] + b[5]) - max(a[3], b[3])
            if ox > 0.02 and oy > 0.02 and ox * oy > OVERLAP_MIN_IN2:
                overlaps.append(f"P{a[0]} {a[1]} × {b[1]} 交叠 {ox:.2f}x{oy:.2f}in")
    return {"canvas_in": [round(sw, 3), round(sh_, 3)],
            "out_of_canvas": problems, "text_overlaps": overlaps}


def main() -> int:
    before = sha256(BASELINE)
    if before != BASELINE_SHA:
        print(f"[FATAL] 基线 SHA 不符: {before}")
        return 2
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(BASELINE, OUT_PPTX)

    prs = Presentation(str(OUT_PPTX))
    base_texts = slide_texts(BASELINE)
    base_notes = notes_texts(BASELINE)
    base_names = zip_names(BASELINE)

    info = rb.apply_readable_batch(prs, pages=(2, 6, 8))
    prs.save(str(OUT_PPTX))

    # 13 页全覆盖：同一字号与容纳规则（专用版式页 + 通用页引擎）
    import shutil as _sh
    _sh.copyfile(BASELINE, OUT_PPTX_ALL)
    prs_all = Presentation(str(OUT_PPTX_ALL))
    info_all = rb.apply_readable_deck(prs_all, pages=(2, 6, 8), all_pages=True)
    prs_all.save(str(OUT_PPTX_ALL))

    after_base = sha256(BASELINE)
    out_sha = sha256(OUT_PPTX)

    prs2 = Presentation(str(OUT_PPTX))
    new_texts = slide_texts(OUT_PPTX)
    new_notes = notes_texts(OUT_PPTX)

    text_diff = [p for p in base_texts if base_texts[p] != new_texts.get(p)]
    notes_diff = [p for p in base_notes if base_notes[p] != new_notes.get(p)]
    missing_notes = [p for p in base_notes if p not in new_notes]
    added_notes = [p for p in new_notes if p not in base_notes]

    report = {
        "generated_at": "2026-10-21 00:25 需求",
        "layout_version": rb.LAYOUT_VERSION,
        "baseline": {"path": str(BASELINE), "sha256": before,
                     "sha256_after": after_base, "unchanged": before == after_base},
        "output": {"path": str(OUT_PPTX), "sha256": out_sha,
                   "slides": len(prs2.slides)},
        "ea_font": rb.NATIVE_EA_FONT,
        "ea_font_changes": info["ea_font_changes"],
        "size_policy": rb.SIZE_POLICY,
        "acts": info["acts"],
        "zip": {"baseline_members": len(base_names),
                "output_members": len(zip_names(OUT_PPTX)),
                "missing_vs_baseline": [n for n in base_names
                                        if n not in zip_names(OUT_PPTX)][:20]},
        "conservation": {
            "pages": len(new_texts),
            "text_changed_pages": text_diff,
            "notes_changed_pages": notes_diff,
            "notes_missing": missing_notes,
            "notes_added": added_notes,
        },
        "size_distribution": {str(p): size_distribution(prs2, p) for p in (2, 6, 8)},
        "geometry": geometry_check(prs2),
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    prs3 = Presentation(str(OUT_PPTX_ALL))
    all_texts = slide_texts(OUT_PPTX_ALL)
    all_notes = notes_texts(OUT_PPTX_ALL)
    report_all = {
        "generated_at": report["generated_at"],
        "layout_version": rb.LAYOUT_VERSION,
        "baseline": report["baseline"],
        "output": {"path": str(OUT_PPTX_ALL), "sha256": sha256(OUT_PPTX_ALL),
                   "slides": len(prs3.slides)},
        "ea_font": rb.NATIVE_EA_FONT, "ea_font_changes": info_all["ea_font_changes"],
        "size_policy": rb.SIZE_POLICY,
        "pages_relayout": info_all["pages_relayout"],
        "acts": info_all["acts"],
        "zip": {"baseline_members": len(base_names),
                "output_members": len(zip_names(OUT_PPTX_ALL)),
                "missing_vs_baseline": [n for n in base_names
                                        if n not in zip_names(OUT_PPTX_ALL)][:20]},
        "conservation": {
            "pages": len(all_texts),
            "text_changed_pages": [p for p in base_texts if base_texts[p] != all_texts.get(p)],
            "notes_changed_pages": [p for p in base_notes if base_notes[p] != all_notes.get(p)],
            "notes_missing": [p for p in base_notes if p not in all_notes],
        },
        "size_distribution": {str(p): size_distribution(prs3, p) for p in range(1, 14)},
        "geometry": geometry_check(prs3),
    }
    OUT_JSON_ALL.write_text(json.dumps(report_all, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    print()
    print("13p output:", OUT_PPTX_ALL)
    print("13p sha256:", report_all["output"]["sha256"],
          "| pages relayout:", report_all["pages_relayout"])
    print("13p text changed:", report_all["conservation"]["text_changed_pages"],
          "| notes:", report_all["conservation"]["notes_changed_pages"])
    print("13p out_of_canvas:", report_all["geometry"]["out_of_canvas"] or "none")

    print("baseline sha (before/after):", before[:16], after_base[:16],
          "unchanged:", before == after_base)
    print("output:", OUT_PPTX)
    print("output sha256:", out_sha)
    print("slides:", len(prs2.slides), "| ea font changes:", info["ea_font_changes"])
    print("text changed pages:", text_diff, "| notes changed:", notes_diff,
          "| notes missing:", missing_notes)
    g = report["geometry"]
    print("out_of_canvas:", g["out_of_canvas"] or "none")
    print("text_overlaps:", g["text_overlaps"] or "none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
