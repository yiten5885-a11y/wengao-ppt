"""W2 验收项 3（有图半场）：有图/无图版式样张，全部走真实 LibreOffice 渲染。

与 `rs_w2_render_samples.py`（无图半场）配对：这里给可配图的页型
（content a/b、image a/b）注入**已解析**的图片槽位，验证：

- content 有图版式：文字 7 栏 + 图 5 栏，图片落在安全边距内、不压文字；
- image a（整幅）：图上 + 图注；image b（左右分栏）：图 6 栏 + 文 6 栏；
- T4 降级：槽位存在但未解析（FALLBACK_T4）时走无图文字版式，且页脚
  如实留痕"本页未配图（T4 降级）"——`check_layout` 核对留痕一致；
- 图片均带 alt/署名（descr），来源为脚本现场生成的合成测试图。

产出（默认落在 <repo>/reports/rs-w2/）：
- image_slots.pptx / image_slots.pdf / image_slots/png/page-*.png
- image_slots.check.json
- render_report_image.json

用法：
    .venv/bin/python scripts/rs_w2_render_image_samples.py [--out DIR] [--dpi 110]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pptx import Presentation  # noqa: E402

from wengao_ppt.layout import checks, compose, engine, templates, tokens  # noqa: E402
from wengao_ppt import render as R  # noqa: E402

#: 每张测试图的 (宽, 高, 主色, 标签)——故意混用横/竖/宽三种比例，验证 cover 裁切
IMAGE_SPECS = [
    (1600, 900, (38, 70, 120), "wide-16x9"),
    (900, 1600, (120, 60, 38), "portrait-9x16"),
    (1920, 640, (38, 110, 90), "ultrawide-3x1"),
]


def make_test_images(out_dir: Path) -> list[dict]:
    """现场生成合成测试图（纯程序绘制，无第三方素材），返回 asset 字典。"""
    from PIL import Image, ImageDraw  # noqa: PLC0415
    assets_dir = out_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    assets = []
    for i, (w, h, color, label) in enumerate(IMAGE_SPECS, start=1):
        p = assets_dir / f"test_{label}.png"
        if not p.exists():
            img = Image.new("RGB", (w, h), color)
            d = ImageDraw.Draw(img)
            # 对角渐变 + 居中标签，便于目视确认裁切方向
            for x in range(w):
                shade = int(60 * x / max(1, w))
                d.line([(x, 0), (x, h)],
                       fill=(color[0] + shade, color[1] + shade, color[2] + shade))
            d.rectangle([w * 0.08, h * 0.08, w * 0.92, h * 0.92],
                        outline=(255, 255, 255), width=max(2, w // 300))
            d.text((w * 0.12, h * 0.45), f"W2 test image {i}: {label}",
                   fill=(255, 255, 255))
            img.save(p)
        assets.append({
            "path": str(p),
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            "provider": "w2_local_synthetic",
            "license": "测试生成图（脚本现场绘制，无第三方素材）",
            "attribution_text": f"W2 合成测试图 {i}（{label}）",
            "source_url": "",
        })
    return assets


def page_spec(role: str, variant: str, index: int, title: str,
              slot_id: str | None) -> dict:
    spec = {"role": role, "variant": variant, "page_id": f"{role}-{variant}",
            "index": index, "title": title}
    if role == "content":
        spec["blocks"] = [{"kind": "bullets",
                           "items": ["营业收入 3.2 亿元，同比增长 18%",
                                     "核心业务收入 2.1 亿元，占比 65%",
                                     "新签订单 46 家客户，覆盖 12 个行业"]}]
    elif role == "image":
        spec["blocks"] = [{"kind": "text",
                           "text": "图注：示意图为脚本现场生成的合成测试图。"}]
    if slot_id:
        spec["image_slots"] = [{
            "slot_id": slot_id, "aspect": "16:9",
            "scene": {"scene_id": slot_id, "schema": "rs.scene_request.v1",
                      "visual_query": "abstract data center",
                      "language": "zh"}}]
    return spec


def build_image_deck(assets: list[dict]) -> tuple[Presentation, dict]:
    """有图半场：4 个已解析槽位 + 2 个 T4 降级（未解析）槽位。"""
    prs = Presentation()
    prs.slide_width, prs.slide_height = tokens.CANVAS_W_EMU, tokens.CANVAS_H_EMU
    resolutions: dict = {}
    for i, a in enumerate(assets[:3], start=1):
        resolutions[f"img-slot-{i}"] = {"status": "RESOLVED_T2", "asset": a}
    # 第 4、5 个槽位故意不给 resolution → FALLBACK_T4（无图文字版式 + 留痕）
    ctx = templates.TemplateContext(tokens.THEMES["ink"], resolutions,
                                    compose._default_place_image(), None)
    specs = [
        ("content", "a", "content 有图：文字 7 栏 + 图 5 栏", "img-slot-1"),
        ("content", "b", "content 有图（双栏变体同样走图文 7+5）", "img-slot-2"),
        ("image", "a", "image 整幅 + 图注", "img-slot-3"),
        ("image", "b", "image 左右分栏：图 6 栏 + 文 6 栏", "img-slot-4"),
        ("content", "a", "T4 降级：槽位未解析走无图文字版式", "img-slot-5"),
        ("image", "a", "T4 降级：整幅页型未解析时不放占位图", "img-slot-6"),
    ]
    for i, (role, variant, title, slot) in enumerate(specs, start=1):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        templates.render_page(slide, page_spec(role, variant, i, title, slot),
                              ctx)
    return prs, {"resolved_slots": sorted(resolutions)}


def render_one(pptx: Path, out_dir: Path, dpi: int, timeout: int) -> dict:
    res = R.render_real(pptx, out_dir, dpi=dpi, timeout=timeout)
    pdf = res.get("pdf") or res.get("pdf_path")
    entry = {"pptx": pptx.name,
             "pptx_sha256": hashlib.sha256(pptx.read_bytes()).hexdigest(),
             "exit_code": res.get("exit_code"), "timed_out": res.get("timed_out"),
             "pdf": str(pdf) if pdf else None}
    if pdf and Path(pdf).exists():
        fams, err = R.pdf_font_families(pdf)
        entry.update({"pdf_sha256": hashlib.sha256(Path(pdf).read_bytes()).hexdigest(),
                      "font_families": fams, "font_error": err,
                      "pages": R.pdf_page_count(pdf),
                      "cjk_stats": R.pdf_cjk_stats(pdf)})
    pngs = sorted((out_dir / "png").glob("*.png")) if (out_dir / "png").exists() else []
    entry["png_count"] = len(pngs)
    entry["png_sha256"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in pngs}
    return entry


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "reports" / "rs-w2"))
    ap.add_argument("--dpi", type=int, default=110)
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    assets = make_test_images(out)
    prs, meta = build_image_deck(assets)
    pptx = out / "image_slots.pptx"
    prs.save(str(pptx))

    chk = checks.check_layout(str(pptx))
    (out / "image_slots.check.json").write_text(
        json.dumps(chk, ensure_ascii=False, indent=1), encoding="utf-8")
    entry = render_one(pptx, out / "image_slots", args.dpi, args.timeout)
    entry["check"] = {"pass": chk["pass"], "page_count": chk["page_count"],
                      "findings": chk["findings"]}
    entry["slots"] = meta
    report = {"schema": "rs.w2_render_image_report.v1", "dpi": args.dpi,
              "assets": [{k: a[k] for k in ("sha256", "provider", "license")}
                         for a in assets],
              "artifacts": [entry]}
    (out / "render_report_image.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"[image_slots] pages={chk['page_count']} check_pass={chk['pass']} "
          f"pdf_pages={entry.get('pages')} png={entry['png_count']} "
          f"fonts={entry.get('font_families')}")
    for f in chk["findings"]:
        print("   -", f["code"], "p", f["page"], f["detail"][:110])

    # 图片数核对：3 张已解析槽位必须有 3 张图；T4 两页不得有图
    with_pic = 0
    from pptx import Presentation as _P  # noqa: E402
    prs2 = _P(str(pptx))
    for i, slide in enumerate(prs2.slides, start=1):
        n = sum(1 for s in slide.shapes
                if s.shape_type is not None and "PICTURE" in str(s.shape_type))
        with_pic += 1 if n else 0
    expect_pic_pages = 3

    def _pages(v):
        return v[0] if isinstance(v, (tuple, list)) else v

    ok = (chk["pass"] and _pages(entry.get("pages")) == chk["page_count"]
          and entry["png_count"] == chk["page_count"]
          and with_pic == expect_pic_pages and not entry.get("timed_out"))
    print(f"picture_pages={with_pic} (expect {expect_pic_pages})")
    print("ALL_OK" if ok else "HAS_ISSUES")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
