"""W2 验收项 6：recompose_slide / insert_slide 的"其余页对象不变"证据。

对同一份 3 页导入稿分别做页级重排与插页，输出**前后对象清单对比**
（每页逐形状：shape_id / 类型 / 名称 / 该页 XML sha256）到
`reports/rs-w2/recompose_insert_inventory.json`。

用法：
    .venv/bin/python scripts/rs_w2_recompose_insert_evidence.py [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pptx import Presentation  # noqa: E402

from wengao_ppt.layout import compose, templates, tokens  # noqa: E402


def build_imported_deck(path: Path) -> None:
    """模拟"导入的 PPT"：3 页，每页标题 + 若干要点文本框。"""
    prs = Presentation()
    prs.slide_width, prs.slide_height = tokens.CANVAS_W_EMU, tokens.CANVAS_H_EMU
    for i in range(3):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        tb = slide.shapes.add_textbox(tokens.MARGINS_EMU["left"],
                                      tokens.MARGINS_EMU["top"],
                                      tokens.CONTENT_W_EMU, 600000)
        tb.text_frame.text = f"第 {i + 1} 页标题"
        for k, line in enumerate([f"要点 {i + 1}-{j + 1}" for j in range(3)]):
            b = slide.shapes.add_textbox(tokens.MARGINS_EMU["left"],
                                         tokens.MARGINS_EMU["top"] + 700000 * (k + 1),
                                         tokens.CONTENT_W_EMU, 500000)
            b.text_frame.text = line
    prs.save(str(path))


def page_inventories(pptx: Path) -> list[dict]:
    prs = Presentation(str(pptx))
    out = []
    for pi, slide in enumerate(prs.slides, start=1):
        shapes = []
        for shp in slide.shapes:
            shapes.append({
                "shape_id": shp.shape_id,
                "type": str(shp.shape_type),
                "name": shp.name,
                "has_text": bool(shp.has_text_frame and shp.text_frame.text.strip()),
                "text_head": (shp.text_frame.text.strip()[:24]
                              if shp.has_text_frame else ""),
            })
        out.append({"page": pi, "shape_count": len(shapes), "shapes": shapes})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "reports" / "rs-w2"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    src = out / "recompose_src.pptx"
    build_imported_deck(src)
    inv_src = page_inventories(src)

    # ---- 1) recompose_slide：重排第 2 页 ---------------------------------
    rep_rec = compose.recompose_slide(src, 1, "a", "ink",
                                      str(out / "recompose_out.pptx"))
    inv_rec = page_inventories(out / "recompose_out.pptx")

    # ---- 2) insert_slide：在第 1 页后插入新页 ------------------------------
    rep_ins = compose.insert_slide(src, 0,
                                   {"role": "compare", "variant": "a",
                                    "title": "插入的对比页",
                                    "blocks": [{"kind": "bullets",
                                                "items": ["公司增速 18%"],
                                                "payload": {"side": "left"}},
                                               {"kind": "bullets",
                                                "items": ["行业均值 9%"],
                                                "payload": {"side": "right"}}]},
                                   "ink", str(out / "insert_out.pptx"))
    inv_ins = page_inventories(out / "insert_out.pptx")

    # ---- 3) 逐页对象清单对比 ---------------------------------------------
    def others_report(inv_before, inv_after, op):
        rows = []
        for b, a in zip(inv_before, inv_after):
            same = (b["shapes"] == a["shapes"])
            rows.append({"page": b["page"], "identical": same,
                         "shapes_before": b["shape_count"],
                         "shapes_after": a["shape_count"]})
        return rows

    report = {
        "schema": "rs.w2_recompose_insert_inventory.v1",
        "source_deck": {"pptx": str(src), "pages": len(inv_src),
                        "inventories": inv_src},
        "recompose_slide": {
            "page_index": rep_rec["page_index"],
            "others_unchanged": rep_rec["others_unchanged"],
            "warnings": rep_rec["warnings"],
            "page_xml_sha_before_after": rep_rec["page_xml_sha_before_after"],
            "target_page_before": rep_rec["inventory_before"],
            "target_page_after": rep_rec["inventory_after"],
            "others_inventory_rows": others_report(
                [p for i, p in enumerate(inv_src) if i != 1],
                [p for i, p in enumerate(inv_rec) if i != 1], "recompose"),
            "out_pptx": rep_rec["deck_path"],
        },
        "insert_slide": {
            "after_index": rep_ins["after_index"],
            "others_unchanged": rep_ins["others_unchanged"],
            "warnings": rep_ins["warnings"],
            "page_count_before": rep_ins["page_count_before"],
            "page_count_after": rep_ins["page_count_after"],
            "out_pptx": rep_ins["deck_path"],
            # 插入后既有页整体后移：原第 1 页 → 新第 1 页，新页 → 第 2 页，
            # 原第 2/3 页 → 第 3/4 页。逐页核对对象清单。
            "shifted_pages": [
                {"before_page": 1, "after_page": 1,
                 "identical": inv_src[0]["shapes"] == inv_ins[0]["shapes"]},
                {"before_page": 2, "after_page": 3,
                 "identical": inv_src[1]["shapes"] == inv_ins[2]["shapes"]},
                {"before_page": 3, "after_page": 4,
                 "identical": inv_src[2]["shapes"] == inv_ins[3]["shapes"]},
            ],
            "new_page_inventory": inv_ins[1],
        },
    }
    (out / "recompose_insert_inventory.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"recompose: others_unchanged={rep_rec['others_unchanged']} "
          f"target_shapes {len(rep_rec['inventory_before'])}→"
          f"{len(rep_rec['inventory_after'])}")
    print(f"insert: others_unchanged={rep_ins['others_unchanged']} "
          f"pages {rep_ins['page_count_before']}→{rep_ins['page_count_after']} "
          f"shifted={[r['identical'] for r in report['insert_slide']['shifted_pages']]}")
    ok = rep_rec["others_unchanged"] and rep_ins["others_unchanged"] \
        and all(r["identical"] for r in report["insert_slide"]["shifted_pages"])
    print("ALL_OK" if ok else "HAS_ISSUES")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
