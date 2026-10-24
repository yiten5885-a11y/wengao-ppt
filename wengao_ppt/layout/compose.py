"""W2-F　成稿与导入 PPT 的页级接口。

- `compose_deck(plan, resolutions, theme, out_path, place_image, emit)`（契约 C6）
  `place_image` 作为参数注入（默认尝试 W1 实现；W2 测试期可注入桩）；
  `FALLBACK_T4` / 缺图自动走该页型的"无图版式"，不留空洞。
- `recompose_slide(pptx_in, page_index, variant, theme, out_path)`：
  导入 PPT 的页级重排；**其余页对象不得变化**（逐页 XML sha256 前后对比写进报告）。
- `insert_slide(pptx_in, after_index, page_spec, theme, out_path)`：
  在导入 PPT 中插入新页（如检索得到的对比页）；其余页同样不得变化。

错误码登记在 `contracts/errors/layout.json`（LAYOUT.*）。
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from pptx import Presentation

from . import templates, tokens
from .tokens import CANVAS_H_EMU, CANVAS_W_EMU


def _emit(emit, stage, status, summary, **kw):
    if emit is None:
        return
    try:
        emit({"stage": stage, "status": status, "summary": summary, **kw})
    except Exception:  # noqa: BLE001
        pass


def _w1_place_image():
    """W1 的真实实现（`wengao_ppt.media.place.place_image`）。

    接口适配：W2 模板给的是 `engine.Rect`（EMU），W1 收英寸序列，
    这里做单位换算后再转发；cover 裁切/署名/备注都走 W1 原实现。
    """
    try:
        from ..media.place import place_image as w1_place_image  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None

    def _adapted(slide, rect, asset: dict, fit: str = "cover",
                 alt_text: str = ""):
        inches = [rect.x / tokens.EMU_PER_IN, rect.y / tokens.EMU_PER_IN,
                  rect.w / tokens.EMU_PER_IN, rect.h / tokens.EMU_PER_IN]
        return w1_place_image(slide, inches, asset, fit=fit, alt_text=alt_text)
    return _adapted


def _default_place_image():
    """优先用 W1 的实现；找不到时用 W2 的测试桩（cover 裁切 + descr 署名）。

    桩只用于 W2 自身测试与无 W1 环境的开发；真实交付必须走 W1 注入。
    """
    try:
        from ..scene_images import place_image as w1_place_image  # noqa: PLC0415
        return w1_place_image
    except Exception:  # noqa: BLE001
        pass
    adapted = _w1_place_image()
    if adapted is not None:
        return adapted
    return _stub_place_image


def _stub_place_image(slide, rect, asset: dict, fit: str = "cover",
                      alt_text: str = ""):
    """W2 测试桩：按 cover 语义等比裁切后插入 picture，写 descr。"""
    import tempfile  # noqa: PLC0415

    from PIL import Image
    path = asset.get("path") if isinstance(asset, dict) else str(asset)
    img = Image.open(path)
    iw, ih = img.size
    target_ratio = rect.w / rect.h
    src_ratio = iw / ih
    if src_ratio > target_ratio:   # 太宽 → 裁左右
        new_w = int(ih * target_ratio)
        box = ((iw - new_w) // 2, 0, (iw + new_w) // 2, ih)
    else:                          # 太高 → 裁上下
        new_h = int(iw / target_ratio)
        box = (0, (ih - new_h) // 2, iw, (ih + new_h) // 2)
    cropped = img.crop(box)
    fd, tmp = tempfile.mkstemp(suffix=".png", prefix="w2_img_")
    try:
        cropped.save(tmp)
        pic = slide.shapes.add_picture(tmp, rect.x, rect.y, rect.w, rect.h)
    finally:
        import os  # noqa: PLC0415
        os.close(fd)
        Path(tmp).unlink(missing_ok=True)
    if alt_text:
        pic._element._nvXxPr.cNvPr.set("descr", str(alt_text))
    return pic.shape_id


def _resolve_theme(theme) -> tokens.Theme:
    if isinstance(theme, tokens.Theme):
        return theme
    tid = str(theme or tokens.DEFAULT_THEME_ID)
    if tid not in tokens.THEMES:
        raise ValueError(f"未登记的主题：{tid}（可用 {sorted(tokens.THEMES)}）")
    return tokens.THEMES[tid]


def _slide_xml_sha(slide) -> str:
    return hashlib.sha256(slide._element.xml.encode("utf-8")).hexdigest()


def _page_inventory(slide) -> list[dict]:
    inv = []
    for shp in slide.shapes:
        inv.append({"shape_id": shp.shape_id, "type": str(shp.shape_type),
                    "name": shp.name})
    return inv


def compose_deck(plan: dict, resolutions: list | None, theme, out_path,
                 place_image=None, emit=None) -> dict:
    """按 DeckPlan 成稿。返回 ComposeReport（契约 C6）。

    resolutions: list[SceneResolution]（契约 C6 直连）或兼容易用的 dict。
    """
    th = _resolve_theme(theme)
    place_fn = place_image or _default_place_image()
    prs = Presentation()
    prs.slide_width, prs.slide_height = CANVAS_W_EMU, CANVAS_H_EMU
    if isinstance(resolutions, list):
        res_map = {r["scene_id"]: r for r in resolutions if isinstance(r, dict) and "scene_id" in r}
    elif isinstance(resolutions, dict):
        res_map = dict(resolutions)
    else:
        res_map = {}
    ctx = templates.TemplateContext(th, res_map, place_fn, emit)
    page_reports = templates.render_deck(prs, plan, ctx)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out))
    overflow = [{"page_id": r.get("page_id"), **ov} for r in page_reports
                for ov in (r.get("overflow") or [])]
    report = {
        "schema": "rs.compose_report.v1",
        "deck_path": str(out),
        "deck_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        "pages": page_reports,
        "page_count": len(page_reports),
        "theme": th.id,
        "overflow": overflow,
        "warnings": [],
    }
    job_dir = plan.get("job_dir") or plan.get("meta", {}).get("job_dir")
    if job_dir:
        ev = Path(job_dir) / "evidence" / "layout"
        ev.mkdir(parents=True, exist_ok=True)
        (ev / "layout_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    _emit(emit, "layout", "done", f"版式完成：{len(page_reports)} 页", pages=len(page_reports))
    return report


def _rebuild_slide_from_inventory(slide, page_spec: dict, th: tokens.Theme,
                                  place_image=None, resolutions=None) -> dict:
    """把导入页的现有文本抽取成 blocks 后按模板重排（原地替换该页内容）。"""
    texts = []
    for shp in slide.shapes:
        if shp.has_text_frame:
            t = shp.text_frame.text.strip()
            if t:
                texts.append(t)
    title = texts[0] if texts else (page_spec.get("title") or "")
    blocks = page_spec.get("blocks")
    if blocks is None:
        blocks = [{"kind": "bullets", "items": texts[1:]}] if len(texts) > 1 else \
                 ([{"kind": "text", "text": t} for t in texts[1:]])
    spec = dict(page_spec)
    spec["title"] = title
    spec["blocks"] = blocks
    # 清空该页所有形状后按模板重画
    sp_tree = slide.shapes._spTree
    for shp in list(slide.shapes):
        sp_tree.remove(shp._element)
    ctx = templates.TemplateContext(th, resolutions or {}, place_image, None)
    return templates.render_page(slide, spec, ctx)


def recompose_slide(pptx_in, page_index: int, variant: str, theme, out_path,
                    page_spec: dict | None = None, resolutions=None,
                    place_image=None, emit=None) -> dict:
    """页级重排：只改第 page_index 页，其余页对象级不变（XML sha256 证据）。"""
    th = _resolve_theme(theme)
    place_fn = place_image or _default_place_image()
    prs = Presentation(str(pptx_in))
    if not 0 <= page_index < len(prs.slides._sldIdLst):
        raise IndexError(f"page_index 越界：{page_index}（共 {len(prs.slides._sldIdLst)} 页）")
    before = [_slide_xml_sha(s) for s in prs.slides]
    inv_before = _page_inventory(prs.slides[page_index])
    spec = dict(page_spec or {})
    spec.setdefault("role", "content")
    spec.setdefault("variant", variant)
    spec.setdefault("page_id", f"recompose-{page_index + 1}")
    spec.setdefault("index", page_index + 1)
    slide = prs.slides[page_index]
    page_report = _rebuild_slide_from_inventory(slide, spec, th, place_fn, resolutions)
    after = [_slide_xml_sha(s) for s in prs.slides]
    others_unchanged = all(after[i] == before[i] for i in range(len(after)) if i != page_index)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out))
    report = {
        "schema": "rs.compose_report.v1",
        "op": "recompose_slide", "page_index": page_index,
        "deck_path": str(out),
        "deck_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        "page_report": page_report,
        "inventory_before": inv_before,
        "inventory_after": _page_inventory(prs.slides[page_index]),
        "others_unchanged": others_unchanged,
        "page_xml_sha_before_after": {str(i): [before[i], after[i]]
                                      for i in range(len(after))},
        "warnings": ([] if others_unchanged else
                     ["RECOMPOSE.OTHERS_CHANGED：非目标页 XML 发生变化，视为缺陷"]),
    }
    _emit(emit, "layout", "done" if others_unchanged else "warn",
          f"页级重排：第 {page_index + 1} 页", **{})
    return report


def insert_slide(pptx_in, after_index: int, page_spec: dict, theme, out_path,
                 resolutions=None, place_image=None, emit=None) -> dict:
    """在 after_index 页之后插入新页；其余页对象级不变。"""
    th = _resolve_theme(theme)
    place_fn = place_image or _default_place_image()
    prs = Presentation(str(pptx_in))
    n = len(prs.slides._sldIdLst)
    if not -1 <= after_index < n:
        raise IndexError(f"after_index 越界：{after_index}（共 {n} 页）")
    before = [_slide_xml_sha(s) for s in prs.slides]
    blank = prs.slide_layouts[6]
    slide = prs.slides.add_slide(blank)
    spec = dict(page_spec)
    spec.setdefault("page_id", f"inserted-{n + 1}")
    spec.setdefault("index", n + 1)
    ctx = templates.TemplateContext(th, resolutions or {}, place_fn, None)
    page_report = templates.render_page(slide, spec, ctx)
    # 新页指纹必须在内容画完之后取——空白页的 XML 与成稿页不同
    new_sha = _slide_xml_sha(slide)
    # 移动到 after_index+1 的位置（0 基：after_index=-1 → 末尾）
    sldIdLst = prs.slides._sldIdLst
    ids = list(sldIdLst)
    new_id = ids[-1]
    sldIdLst.remove(new_id)
    insert_pos = after_index + 1
    if insert_pos >= len(ids) - 1:
        sldIdLst.append(new_id)
    else:
        sldIdLst.insert(insert_pos, new_id)
    after = [_slide_xml_sha(s) for s in prs.slides]
    # 插入会让后续页在 sldIdLst 里后移，逐位对比必然误报；按多重集合比才对：
    # 既有页的 XML 必须原样都在，且只多出新建的那一页。
    others_unchanged = sorted(after) == sorted(before + [new_sha])
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out))
    report = {
        "schema": "rs.compose_report.v1",
        "op": "insert_slide", "after_index": after_index,
        "deck_path": str(out),
        "deck_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        "page_report": page_report,
        "others_unchanged": others_unchanged,
        "page_count_before": len(before), "page_count_after": len(after),
        "warnings": ([] if others_unchanged else
                     ["RECOMPOSE.OTHERS_CHANGED：既有页 XML 发生变化，视为缺陷"]),
    }
    _emit(emit, "layout", "done" if others_unchanged else "warn",
          f"插入新页：第 {after_index + 2} 页", **{})
    return report
