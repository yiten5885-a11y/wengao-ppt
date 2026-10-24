"""W2-C　页型模板：12 页型 × ≥2 个结构不同的变体（含有图 / 无图两种版式）。

页型与变体（variant "a" / "b"）：
- cover    居中标题 / 左下基线+竖排强调条
- section  编号+细线 / 中带
- content  单栏要点 / 双栏要点（可带图：文字 7 栏 + 图 5 栏）
- compare  双卡对照 / 原生条形图
- trend    折线整幅 / 柱图+洞察栏
- insight  大数居左 / 大数居中
- image    整幅带说明 / 左图右文；无图时走 T4 文字版式（明确标"本页未配图"，
           不用占位图或文字卡冒充场景图）
- process  横向 chevron / 纵向步骤
- timeline 横向轴线 / 纵向轴线
- table    整幅表格 / 表格+备注栏
- quote    居中引语 / 强调条引语卡
- closing  居中结语 / 行动清单

约束：
- 字号、颜色、字体全部来自 tokens（硬门禁档），模板**不改字号**；
- 文本块布局经 engine.flow_blocks；放不下如实报 overflow；
- 图片通过注入的 place_image 放置（位置由模板给矩形，W1 裁切插入）；
- FALLBACK_T4 / 缺图页走 no_image 版式重排，不留空洞。
"""
from __future__ import annotations

from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Emu, Pt

from . import engine, measure, tokens
from ..builders import chart as chart_builder
from ..builders import process as process_builder
from ..builders import table as table_builder
from ..builders import timeline as timeline_builder

PT = Pt

#: 能放图的页型（其余页型忽略 image_slots）
IMAGE_CAPABLE_ROLES = {"content", "image", "compare", "insight"}


# ---------------------------------------------------------------- 基础设施
def _rgb(theme: tokens.Theme, key: str) -> RGBColor:
    return RGBColor.from_string(theme.roles()[key].lstrip("#"))


def set_run_font(run, role_key: str, theme: tokens.Theme, text: str, *,
                 bold: bool | None = None, color_key: str | None = None):
    """按角色与文字设置 run 字体（latin/ea/cs 三槽位都写，避免回退错族）。"""
    role = tokens.TYPE_ROLES[role_key]
    script = tokens.script_for_text(text)
    f = run.font
    f.size = PT(int(role.pt))
    f.bold = bool(role.bold) if bold is None else bold
    f.color.rgb = _rgb(theme, color_key or role.color_key)
    tokens.apply_font_slots(f, script)


def _tag(shp, role_key: str):
    """把角色写进形状名（`w2:<role>`），供 check_layout 按角色套用
    contracts/layout_hard_gate_policy.json 的字号区间。"""
    try:
        shp.name = f"w2:{role_key}"
    except Exception:  # noqa: BLE001
        pass
    return shp


def _textbox(slide, rect, *, anchor_top=True, role_key: str | None = None):
    tb = slide.shapes.add_textbox(Emu(rect.x), Emu(rect.y), Emu(rect.w), Emu(rect.h))
    if role_key:
        _tag(tb, role_key)
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    if not anchor_top:
        from pptx.enum.text import MSO_ANCHOR
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    return tb, tf


def _para(tf, first: bool):
    return tf.paragraphs[0] if first else tf.add_paragraph()


def _apply_rtl(p, text: str, align) -> None:
    """RTL 文字（阿拉伯语等）：段落基线方向 rtl=1 + 左对齐改右对齐。

    不设 rtl 时整行按 LTR 基线排——阿拉伯字符形状虽对，但行首在左、
    行尾在右，阅读顺序与对齐方向都不对（W2 验收项 2）。
    调用方显式要 CENTER/RIGHT 时尊重调用方，只把默认 LEFT 翻成 RIGHT。
    """
    if not tokens.is_rtl(tokens.script_for_text(text)):
        p.alignment = align
        return
    p.alignment = PP_ALIGN.RIGHT if align == PP_ALIGN.LEFT else align
    pPr = p._p.get_or_add_pPr()
    pPr.set("rtl", "1")


def _fill_text(tf, text: str, role_key: str, theme: tokens.Theme, *,
               align: PP_ALIGN = PP_ALIGN.LEFT, bold: bool | None = None,
               color_key: str | None = None, first_used: bool = False):
    p = _para(tf, not first_used)
    _apply_rtl(p, text, align)
    p.line_spacing = tokens.TYPE_ROLES[role_key].line_height
    r = p.add_run()
    r.text = str(text or "")
    set_run_font(r, role_key, theme, r.text, bold=bold, color_key=color_key)
    return p


def _rect_shape(slide, rect, theme: tokens.Theme, fill_key: str,
                line_key: str | None = None):
    from pptx.enum.shapes import MSO_SHAPE
    shp = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Emu(rect.x), Emu(rect.y),
                                 Emu(rect.w), Emu(rect.h))
    shp.fill.solid()
    shp.fill.fore_color.rgb = _rgb(theme, fill_key)
    if line_key:
        shp.line.color.rgb = _rgb(theme, line_key)
        shp.line.width = PT(tokens.DECOR_RULES["hairline_pt"])
    else:
        shp.line.fill.background()
    shp.shadow.inherit = False
    return shp


def _hairline(slide, rect, theme: tokens.Theme):
    from pptx.enum.shapes import MSO_SHAPE
    shp = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Emu(rect.x), Emu(rect.y),
                                 Emu(rect.w), Emu(rect.h))
    shp.fill.solid()
    shp.fill.fore_color.rgb = _rgb(theme, "hairline")
    shp.line.fill.background()
    shp.shadow.inherit = False
    return shp


def _image_rect_and_asset(page: dict, ctx: "TemplateContext",
                          content: engine.Rect) -> tuple[engine.Rect | None, dict | None, str]:
    """解析本页图片槽位 → (矩形, asset, status)。无槽位/未解析返回 (None, None, …)。"""
    slots = page.get("image_slots") or []
    if not slots:
        return None, None, "NO_SLOT"
    slot = slots[0]
    scene = slot.get("scene") or {}
    scene_id = scene.get("scene_id") or slot.get("slot_id")
    res = (ctx.resolutions or {}).get(scene_id) or {}
    status = res.get("status") or "FALLBACK_T4"
    if str(status).startswith("RESOLVED") and res.get("asset"):
        return content, res["asset"], status
    return None, None, status


def _image_note(page: dict, status: str) -> str:
    """图片槽位状态 → 页脚留痕。

    只有**真的没上图**才写"本页未配图"：槽位未解析/降级（FALLBACK_T4 等）
    如实留痕；已解析（RESOLVED_*）或无槽位（NO_SLOT）都不写——写反了会让
    有图页自称"未配图"，属于留痕造假。
    """
    base = (page.get("footnote") or {}).get("text") or ""
    if status == "NO_SLOT" or str(status).startswith("RESOLVED"):
        return base
    note = "本页未配图"
    if status == "FALLBACK_T4":
        note += "（T4 降级）"
    return f"{base}　{note}".strip() if base else note


# ---------------------------------------------------------------- 公共件
def _title_band(slide, page: dict, ctx: "TemplateContext",
                regions: engine.PageRegions, align: PP_ALIGN = PP_ALIGN.LEFT,
                color_key: str = "ink") -> list:
    shapes = []
    tb, tf = _textbox(slide, regions.title, role_key="title")
    _fill_text(tf, page.get("title") or "", "title", ctx.theme, align=align,
               color_key=color_key)
    shapes.append(tb)
    return shapes


def _footnote_band(slide, page: dict, ctx: "TemplateContext",
                   regions: engine.PageRegions, extra: str = "") -> list:
    if regions.footnote is None:
        return []
    text = (page.get("footnote") or {}).get("text") or ""
    if extra:
        text = f"{text}　{extra}".strip()
    if not text:
        return []
    tb, tf = _textbox(slide, regions.footnote, role_key="footnote")
    _fill_text(tf, text, "footnote", ctx.theme)
    return [tb]


def _bullets_text(slide, rect, items: list[str], ctx: "TemplateContext",
                  role_key: str = "body", bullet_char: str = "·") -> tuple[object, dict]:
    tb, tf = _textbox(slide, rect, role_key=role_key)
    est = measure.measure_blocks(items, rect.w,
                                 tokens.TYPE_ROLES[role_key].pt,
                                 tokens.TYPE_ROLES[role_key].line_height,
                                 tokens.TYPE_ROLES[role_key].space_after_pt)
    for i, it in enumerate(items):
        p = _para(tf, i == 0)
        _apply_rtl(p, it, PP_ALIGN.LEFT)
        p.line_spacing = tokens.TYPE_ROLES[role_key].line_height
        p.space_after = PT(int(tokens.TYPE_ROLES[role_key].space_after_pt))
        rb = p.add_run(); rb.text = f"{bullet_char} "
        set_run_font(rb, role_key, ctx.theme, rb.text,
                     color_key="accent" if role_key == "body" else None)
        r = p.add_run(); r.text = str(it)
        set_run_font(r, role_key, ctx.theme, r.text)
    return tb, est


def _render_blocks(slide, blocks: list[dict], rect: engine.Rect,
                   g: engine.Grid, ctx: "TemplateContext", *,
                   allow_two_col: bool = True, col_limit: int | None = None) -> dict:
    """把 blocks 经引擎布进 rect，逐块落到幻灯片。返回布局报告。"""
    eb = [engine.Block(kind=b.get("kind", "text"),
                       text=b.get("text") or b.get("content") or "",
                       payload=b, role=b.get("role", "body"),
                       fact_refs=b.get("fact_refs") or [])
          for b in blocks]
    res = engine.flow_blocks(eb, rect, g, allow_two_col=allow_two_col)
    for p in res.get("placements", []):
        _draw_block(slide, p, ctx)
    if col_limit is not None:
        for p in res.get("placements", []):
            if p.col_start >= col_limit:
                p.overflow = True
    return {"mode": res.get("mode"), "overflow": res.get("overflow") or [],
            "placements": [p.as_dict() for p in res.get("placements", [])]}


def _draw_block(slide, p: engine.Placed, ctx: "TemplateContext"):
    b = p.block
    role_key = b.role if b.role in tokens.TYPE_ROLES else "body"
    if b.kind == "bullets":
        items = b.payload.get("items") or [b.text]
        _bullets_text(slide, p.rect, items, ctx, role_key)
    elif b.kind == "bignumber":
        _big_number(slide, p.rect, b.payload, ctx)
    elif b.kind == "chart":
        _chart_block(slide, p.rect, b.payload, ctx)
    elif b.kind == "table":
        _table_block(slide, p.rect, b.payload, ctx)
    elif b.kind == "process":
        process_builder.add_process(slide, p.rect, b.payload.get("steps") or [],
                                    ctx.theme,
                                    direction=b.payload.get("direction", "h"))
    elif b.kind == "timeline":
        timeline_builder.add_timeline(slide, p.rect,
                                      b.payload.get("milestones") or [],
                                      ctx.theme,
                                      direction=b.payload.get("direction", "h"))
    elif b.kind == "quote":
        tb, tf = _textbox(slide, p.rect, role_key="lead")
        _fill_text(tf, b.text, "lead", ctx.theme, color_key="ink")
    else:
        tb, tf = _textbox(slide, p.rect, role_key=role_key)
        _fill_text(tf, b.text, role_key, ctx.theme)


def _big_number(slide, rect, payload: dict, ctx: "TemplateContext"):
    """大数卡：数值 / 标签 / 注释各自独立文本框。

    分开而不是挤进一个文本框——硬门禁 HG07 要求"有效字号满足角色"，
    混排会让检查器无法判定每个 run 的角色，也容易让注释把大数挤出框。
    """
    value = str(payload.get("value") or "")
    label = str(payload.get("label") or "")
    note = str(payload.get("note") or "")
    IN = tokens.EMU_PER_IN
    kn = tokens.TYPE_ROLES["key_number"]
    cap = tokens.TYPE_ROLES["caption"]
    y = rect.y
    v_h = int(kn.pt * kn.line_height * 12700)
    tb, tf = _textbox(slide, engine.Rect(rect.x, y, rect.w, v_h), role_key="key_number")
    _fill_text(tf, value, "key_number", ctx.theme)
    y += v_h + int(0.06 * IN)
    if label:
        l_h = measure.block_height_emu(label, rect.w, tokens.TYPE_ROLES["body"].pt,
                                       tokens.TYPE_ROLES["body"].line_height)
        tb2, tf2 = _textbox(slide, engine.Rect(rect.x, y, rect.w, l_h), role_key="body")
        _fill_text(tf2, label, "body", ctx.theme, color_key="neutral")
        y += l_h + int(0.05 * IN)
    if note:
        n_h = measure.block_height_emu(note, rect.w, cap.pt, cap.line_height)
        tb3, tf3 = _textbox(slide, engine.Rect(rect.x, y, rect.w, n_h), role_key="caption")
        _fill_text(tf3, note, "caption", ctx.theme, color_key="neutral")


def _chart_block(slide, rect, payload: dict, ctx: "TemplateContext"):
    kind = payload.get("chart_kind") or "column"
    chart_builder.add_native_chart(
        slide, rect, kind, payload.get("categories") or [],
        payload.get("series") or {}, ctx.theme,
        title=payload.get("title") or "",
        data_labels=bool(payload.get("data_labels", True)),
        highlight_series=payload.get("highlight_series"),
        semantic=payload.get("semantic"), legend=bool(payload.get("legend", False)))


def _table_block(slide, rect, payload: dict, ctx: "TemplateContext"):
    table_builder.add_native_table(slide, rect, payload.get("headers") or [],
                                   payload.get("rows") or [], ctx.theme,
                                   col_ratios=payload.get("col_ratios"))


# ---------------------------------------------------------------- 页型模板
def _cover_centered(slide, page, ctx, variant):
    regions = engine.page_regions(has_lead=True, has_footnote=False,
                                  title_role="cover_title")
    g = regions.grid
    shapes = []
    title = page.get("title") or ""
    kn = tokens.TYPE_ROLES["cover_title"]
    # 标题框高按实测行数给（长标题可两行），绝不用固定一档高度
    t_h = max(regions.title.h,
              measure.block_height_emu(title, g.rect(1, 10, 0, 0).w,
                                       kn.pt, kn.line_height))
    mid = engine.Rect(g.rect(1, 10, 0, 0).x, regions.title.y + int(1.2 * tokens.EMU_PER_IN),
                      g.rect(1, 10, 0, 0).w, t_h)
    tb, tf = _textbox(slide, mid, anchor_top=False, role_key="cover_title")
    _fill_text(tf, page.get("title") or "", "cover_title", ctx.theme,
               align=PP_ALIGN.CENTER)
    shapes.append(tb)
    rule = engine.Rect(mid.x + mid.w // 2 - int(0.4 * tokens.EMU_PER_IN),
                       mid.y + mid.h + int(0.25 * tokens.EMU_PER_IN),
                       int(0.8 * tokens.EMU_PER_IN), int(0.045 * tokens.EMU_PER_IN))
    shapes.append(_rect_shape(slide, rule, ctx.theme, "accent"))
    blocks = page.get("blocks") or []
    texts = [b.get("text") or b.get("content") or "" for b in blocks
             if b.get("kind") in (None, "text")]
    if texts:
        sub = engine.Rect(mid.x, rule.y + int(0.35 * tokens.EMU_PER_IN), mid.w,
                          int(1.4 * tokens.EMU_PER_IN))
        tb2, tf2 = _textbox(slide, sub, role_key="lead")
        for t in texts:
            _fill_text(tf2, t, "lead", ctx.theme, align=PP_ALIGN.CENTER,
                       color_key="neutral")
        shapes.append(tb2)
    return {"mode": "cover_centered", "overflow": [], "placements": []}


def _cover_left_rule(slide, page, ctx, variant):
    g = engine.Grid()
    shapes = []
    y = g.top + int(2.6 * tokens.EMU_PER_IN)
    bar = engine.Rect(g.left, y, int(0.09 * tokens.EMU_PER_IN),
                      int(1.7 * tokens.EMU_PER_IN))
    shapes.append(_rect_shape(slide, bar, ctx.theme, "accent"))
    title = page.get("title") or ""
    kn = tokens.TYPE_ROLES["cover_title"]
    t_h = max(int(2.0 * tokens.EMU_PER_IN),
              measure.block_height_emu(title, g.rect(1, 10, 0, 0).w,
                                       kn.pt, kn.line_height))
    t_rect = engine.Rect(g.col_x(1), y - int(0.15 * tokens.EMU_PER_IN),
                         g.rect(1, 10, 0, 0).w, t_h)
    tb, tf = _textbox(slide, t_rect, anchor_top=False, role_key="cover_title")
    _fill_text(tf, page.get("title") or "", "cover_title", ctx.theme)
    shapes.append(tb)
    for b in (page.get("blocks") or []):
        if b.get("kind") in (None, "text") and (b.get("text") or b.get("content")):
            sub = engine.Rect(g.col_x(1), t_rect.y + t_rect.h + int(0.2 * tokens.EMU_PER_IN),
                              t_rect.w, int(0.9 * tokens.EMU_PER_IN))
            tb2, tf2 = _textbox(slide, sub, role_key="lead")
            _fill_text(tf2, b.get("text") or b.get("content"), "lead", ctx.theme,
                       color_key="neutral")
            shapes.append(tb2)
            break
    return {"mode": "cover_left_rule", "overflow": [], "placements": []}


def _section_number_rule(slide, page, ctx, variant):
    regions = engine.page_regions(has_footnote=False)
    g = regions.grid
    shapes = []
    num = str(page.get("index", ""))
    # 节页大编号是展示型数字，归 key_number 档（44–56pt），不是行内序号档。
    # 它的框高必须按自己 44pt 的字算，不能蹭标题带（36pt）的高度。
    kn = tokens.TYPE_ROLES["cover_title"]
    num_h = max(regions.title.h, int(kn.pt * kn.line_height * 12700))
    # 节页大编号是展示型数字，归 key_number 档（44–56pt），不是行内序号档
    nb, ntf = _textbox(slide, engine.Rect(g.rect(0, 2, 0, 0).x, regions.title.y,
                                         g.rect(0, 2, 0, 0).w, num_h),
                       role_key="key_number")
    _fill_text(ntf, num.zfill(2), "cover_title", ctx.theme, color_key="accent")
    shapes.append(nb)
    tb, tf = _textbox(slide, g.rect(2, 10, regions.title.y, num_h),
                    role_key="section_title")
    _fill_text(tf, page.get("title") or "", "section_title", ctx.theme)
    shapes.append(tb)
    rule = engine.Rect(g.left, regions.title.y + regions.title.h + int(0.18 * tokens.EMU_PER_IN),
                       g.content_w, int(0.03 * tokens.EMU_PER_IN))
    shapes.append(_hairline(slide, rule, ctx.theme))
    blocks = [b for b in (page.get("blocks") or []) if (b.get("text") or b.get("content"))]
    if blocks:
        c = engine.Rect(g.col_x(2), rule.y + int(0.4 * tokens.EMU_PER_IN),
                        g.rect(2, 10, 0, 0).w, int(2.2 * tokens.EMU_PER_IN))
        rep = _render_blocks(slide, blocks, c, g, ctx, allow_two_col=False)
    else:
        rep = {"mode": "section", "overflow": [], "placements": []}
    rep["mode"] = "section_number_rule"
    return rep


def _section_band(slide, page, ctx, variant):
    g = engine.Grid()
    shapes = []
    band_y = g.top + int(2.2 * tokens.EMU_PER_IN)
    band = engine.Rect(g.left, band_y, g.content_w, int(1.5 * tokens.EMU_PER_IN))
    shapes.append(_rect_shape(slide, band, ctx.theme, "substrate"))
    tb, tf = _textbox(slide, band.inset(int(0.4 * tokens.EMU_PER_IN), 0),
                    anchor_top=False, role_key="section_title")
    _fill_text(tf, page.get("title") or "", "section_title", ctx.theme)
    shapes.append(tb)
    blocks = [b for b in (page.get("blocks") or []) if (b.get("text") or b.get("content"))]
    if blocks:
        c = engine.Rect(g.left, band.y + band.h + int(0.4 * tokens.EMU_PER_IN),
                        g.content_w, int(1.8 * tokens.EMU_PER_IN))
        rep = _render_blocks(slide, blocks, c, g, ctx, allow_two_col=False)
    else:
        rep = {"mode": "section", "overflow": [], "placements": []}
    rep["mode"] = "section_band"
    return rep


def _content_bullets(slide, page, ctx, variant, two_col: bool):
    regions = engine.page_regions(has_footnote=True)
    g = regions.grid
    status = None
    img_asset = None
    img_rect = None
    if page.get("image_slots"):
        img_rect, img_asset, status = _image_rect_and_asset(page, ctx, regions.content)
    blocks = page.get("blocks") or []
    if img_asset is not None and variant in ("a", "b"):
        # 文字 7 栏 + 图 5 栏
        text_rect = g.rect(0, 7, regions.content.y, regions.content.h)
        img_r = g.rect(7, 5, regions.content.y, regions.content.h)
        ctx.place_image(slide, img_r, img_asset, fit="cover",
                        alt_text=_alt_for(page, ctx))
        rep = _render_blocks(slide, blocks, text_rect, g, ctx, allow_two_col=False)
        rep["mode"] = "content_with_image"
        rep["image_status"] = status
    else:
        rep = _render_blocks(slide, blocks, regions.content, g, ctx,
                             allow_two_col=two_col)
        rep["mode"] = "content_two_col" if two_col else "content_single"
        if page.get("image_slots"):
            rep["image_status"] = status
    extra = _image_note(page, rep.get("image_status")) if page.get("image_slots") else ""
    _footnote_band(slide, page, ctx, regions, extra)
    return rep


def _alt_for(page, ctx) -> str:
    slots = page.get("image_slots") or []
    if not slots:
        return ""
    scene = slots[0].get("scene") or {}
    res = (ctx.resolutions or {}).get(scene.get("scene_id") or slots[0].get("slot_id")) or {}
    asset = res.get("asset") or {}
    return str(asset.get("attribution_text") or asset.get("alt") or page.get("title") or "")


def _compare_cards(slide, page, ctx, variant):
    regions = engine.page_regions(has_footnote=True)
    g = regions.grid
    blocks = page.get("blocks") or []
    groups = {}
    for b in blocks:
        side = str((b.get("payload") or b).get("side") or "left")
        groups.setdefault(side, []).append(b)
    left = groups.get("left") or blocks[:len(blocks) // 2]
    right = groups.get("right") or blocks[len(blocks) // 2:]
    col_w_rect_a = g.rect(0, 6, regions.content.y, regions.content.h)
    col_w_rect_b = g.rect(6, 6, regions.content.y, regions.content.h)
    card_a = _rect_shape(slide, col_w_rect_a.inset(0, 0), ctx.theme, "substrate")
    card_b = _rect_shape(slide, col_w_rect_b.inset(0, 0), ctx.theme, "paper",
                         line_key="hairline")
    pad = int(0.22 * tokens.EMU_PER_IN)
    rep_l = _render_blocks(slide, left, col_w_rect_a.inset(pad, pad), g, ctx,
                           allow_two_col=False)
    rep_r = _render_blocks(slide, right, col_w_rect_b.inset(pad, pad), g, ctx,
                           allow_two_col=False)
    overflow = rep_l["overflow"] + rep_r["overflow"]
    _footnote_band(slide, page, ctx, regions)
    return {"mode": "compare_cards", "overflow": overflow,
            "placements": rep_l["placements"] + rep_r["placements"]}


def _compare_chart(slide, page, ctx, variant):
    regions = engine.page_regions(has_footnote=True)
    g = regions.grid
    blocks = page.get("blocks") or []
    chart_blocks = [b for b in blocks if b.get("kind") == "chart"]
    note_blocks = [b for b in blocks if b.get("kind") != "chart"]
    overflow = []
    placements = []
    if chart_blocks:
        payload = chart_blocks[0]
        payload.setdefault("chart_kind", "bar")
        rect = g.rect(0, 8, regions.content.y,
                      min(regions.content.h, int(4.2 * tokens.EMU_PER_IN)))
        _chart_block(slide, rect, payload, ctx)
        placements.append({"kind": "chart", "rect": rect.as_dict()})
        if note_blocks:
            nrect = g.rect(8, 4, regions.content.y, regions.content.h)
            rep = _render_blocks(slide, note_blocks, nrect, g, ctx,
                                 allow_two_col=False)
            overflow += rep["overflow"]
            placements += rep["placements"]
    else:
        rep = _render_blocks(slide, blocks, regions.content, g, ctx)
        overflow += rep["overflow"]
        placements += rep["placements"]
    _footnote_band(slide, page, ctx, regions)
    return {"mode": "compare_chart", "overflow": overflow, "placements": placements}


def _trend_line(slide, page, ctx, variant):
    regions = engine.page_regions(has_footnote=True)
    g = regions.grid
    blocks = page.get("blocks") or []
    chart_blocks = [b for b in blocks if b.get("kind") == "chart"]
    if chart_blocks:
        payload = chart_blocks[0]
        payload.setdefault("chart_kind", "line")
        payload.setdefault("legend", True)
        rect = g.rect(0, 12, regions.content.y,
                      min(regions.content.h, int(4.6 * tokens.EMU_PER_IN)))
        _chart_block(slide, rect, payload, ctx)
        rest = [b for b in blocks if b.get("kind") != "chart"]
        rep = {"mode": "trend_line", "overflow": [], "placements": [{"kind": "chart"}]}
        if rest:
            r2 = _render_blocks(slide, rest, g.rect(0, 12, rect.y + rect.h + int(0.1 * tokens.EMU_PER_IN),
                                                    max(0, regions.content.h - rect.h)),
                                g, ctx, allow_two_col=False)
            rep["overflow"] = r2["overflow"]
            rep["placements"] += r2["placements"]
    else:
        rep = _render_blocks(slide, blocks, regions.content, g, ctx)
        rep["mode"] = "trend_line"
    _footnote_band(slide, page, ctx, regions)
    return rep


def _trend_column_note(slide, page, ctx, variant):
    regions = engine.page_regions(has_footnote=True)
    g = regions.grid
    blocks = page.get("blocks") or []
    chart_blocks = [b for b in blocks if b.get("kind") == "chart"]
    rest = [b for b in blocks if b.get("kind") != "chart"]
    overflow, placements = [], []
    if chart_blocks:
        payload = chart_blocks[0]
        payload.setdefault("chart_kind", "column")
        rect = g.rect(0, 8, regions.content.y,
                      min(regions.content.h, int(4.2 * tokens.EMU_PER_IN)))
        _chart_block(slide, rect, payload, ctx)
        placements.append({"kind": "chart"})
        if rest:
            nrect = g.rect(8, 4, regions.content.y, regions.content.h)
            r2 = _render_blocks(slide, rest, nrect, g, ctx, allow_two_col=False)
            overflow += r2["overflow"]; placements += r2["placements"]
    else:
        r2 = _render_blocks(slide, blocks, regions.content, g, ctx)
        overflow += r2["overflow"]; placements += r2["placements"]
    _footnote_band(slide, page, ctx, regions)
    return {"mode": "trend_column_note", "overflow": overflow, "placements": placements}


def _insight_left(slide, page, ctx, variant):
    regions = engine.page_regions(has_footnote=True)
    g = regions.grid
    blocks = page.get("blocks") or []
    big = next((b for b in blocks if b.get("kind") == "bignumber"), None)
    rest = [b for b in blocks if b is not big]
    overflow, placements = [], []
    if big is not None:
        big_rect = g.rect(0, 5, regions.content.y, min(regions.content.h, int(2.6 * tokens.EMU_PER_IN)))
        _big_number(slide, big_rect, big, ctx)
        placements.append({"kind": "bignumber"})
        if rest:
            trect = g.rect(5, 7, regions.content.y, regions.content.h)
            r2 = _render_blocks(slide, rest, trect, g, ctx, allow_two_col=False)
            overflow += r2["overflow"]; placements += r2["placements"]
    else:
        r2 = _render_blocks(slide, blocks, regions.content, g, ctx)
        overflow += r2["overflow"]; placements += r2["placements"]
    _footnote_band(slide, page, ctx, regions)
    return {"mode": "insight_left", "overflow": overflow, "placements": placements}


def _insight_center(slide, page, ctx, variant):
    regions = engine.page_regions(has_footnote=True)
    g = regions.grid
    blocks = page.get("blocks") or []
    big = next((b for b in blocks if b.get("kind") == "bignumber"), None)
    rest = [b for b in blocks if b is not big]
    overflow, placements = [], []
    mid_rect = g.rect(1, 10, regions.content.y + int(0.3 * tokens.EMU_PER_IN),
                      int(2.2 * tokens.EMU_PER_IN))
    if big is not None:
        _big_number(slide, mid_rect, big, ctx)
        placements.append({"kind": "bignumber"})
    if rest:
        trect = g.rect(1, 10, mid_rect.y + mid_rect.h + int(0.2 * tokens.EMU_PER_IN),
                       max(0, regions.content.h - mid_rect.h - int(0.5 * tokens.EMU_PER_IN)))
        r2 = _render_blocks(slide, rest, trect, g, ctx, allow_two_col=False)
        overflow += r2["overflow"]; placements += r2["placements"]
    _footnote_band(slide, page, ctx, regions)
    return {"mode": "insight_center", "overflow": overflow, "placements": placements}


def _image_full(slide, page, ctx, variant):
    regions = engine.page_regions(has_footnote=True)
    g = regions.grid
    img_rect, asset, status = _image_rect_and_asset(page, ctx, regions.content)
    if asset is not None:
        cap_h = int(1.1 * tokens.EMU_PER_IN)
        img_r = engine.Rect(regions.content.x, regions.content.y, regions.content.w,
                            max(0, regions.content.h - cap_h))
        ctx.place_image(slide, img_r, asset, fit="cover", alt_text=_alt_for(page, ctx))
        cap_r = g.rect(0, 12, img_r.y + img_r.h + int(0.12 * tokens.EMU_PER_IN), cap_h)
        tb, tf = _textbox(slide, cap_r, role_key="caption")
        caps = [b.get("text") or "" for b in (page.get("blocks") or [])
                if b.get("kind") in ("text", None)]
        _fill_text(tf, "　".join(c for c in caps if c), "caption", ctx.theme,
                   color_key="neutral")
        rep = {"mode": "image_full", "overflow": [], "placements": [], "image_status": status}
    else:
        rep = _t4_text(slide, page, ctx, regions, g)
        rep["image_status"] = status
    _footnote_band(slide, page, ctx, regions, _image_note(page, rep.get("image_status", "NO_SLOT")))
    return rep


def _image_split(slide, page, ctx, variant):
    regions = engine.page_regions(has_footnote=True)
    g = regions.grid
    img_rect, asset, status = _image_rect_and_asset(page, ctx, regions.content)
    if asset is not None:
        img_r = g.rect(0, 6, regions.content.y, regions.content.h)
        ctx.place_image(slide, img_r, asset, fit="cover", alt_text=_alt_for(page, ctx))
        t_r = g.rect(6, 6, regions.content.y, regions.content.h)
        rep = _render_blocks(slide, page.get("blocks") or [], t_r, g, ctx,
                             allow_two_col=False)
        rep["mode"] = "image_split"
        rep["image_status"] = status
        rep["placements"] = []
    else:
        rep = _t4_text(slide, page, ctx, regions, g)
        rep["image_status"] = status
    _footnote_band(slide, page, ctx, regions, _image_note(page, rep.get("image_status", "NO_SLOT")))
    return rep


def _t4_text(slide, page, ctx, regions, g) -> dict:
    """T4 无图版式：文字重排占满内容区，不留空洞、不放占位图。"""
    rep = _render_blocks(slide, page.get("blocks") or [], regions.content, g, ctx,
                         allow_two_col=True)
    rep["mode"] = "t4_no_image"
    return rep


def _process_h(slide, page, ctx, variant):
    regions = engine.page_regions(has_footnote=True)
    g = regions.grid
    steps, rest = [], []
    for b in (page.get("blocks") or []):
        if b.get("kind") == "process":
            steps = b.get("steps") or b.get("payload", {}).get("steps") or []
        else:
            rest.append(b)
    overflow, placements = [], []
    if steps:
        rect = g.rect(0, 12, regions.content.y + int(0.2 * tokens.EMU_PER_IN),
                      min(regions.content.h, int(2.2 * tokens.EMU_PER_IN)))
        process_builder.add_process(slide, rect, steps, ctx.theme, direction="h")
        placements.append({"kind": "process"})
        body_r = g.rect(0, 12, rect.y + rect.h + int(0.3 * tokens.EMU_PER_IN),
                        max(0, regions.content.h - rect.h - int(0.5 * tokens.EMU_PER_IN)))
        if rest:
            r2 = _render_blocks(slide, rest, body_r, g, ctx, allow_two_col=False)
            overflow += r2["overflow"]; placements += r2["placements"]
    else:
        r2 = _render_blocks(slide, rest, regions.content, g, ctx)
        overflow += r2["overflow"]; placements += r2["placements"]
    _footnote_band(slide, page, ctx, regions)
    return {"mode": "process_h", "overflow": overflow, "placements": placements}


def _process_v(slide, page, ctx, variant):
    regions = engine.page_regions(has_footnote=True)
    g = regions.grid
    steps, rest = [], []
    for b in (page.get("blocks") or []):
        if b.get("kind") == "process":
            steps = b.get("steps") or b.get("payload", {}).get("steps") or []
        else:
            rest.append(b)
    overflow, placements = [], []
    if steps:
        rect = g.rect(1, 10, regions.content.y, regions.content.h)
        process_builder.add_process(slide, rect, steps, ctx.theme, direction="v")
        placements.append({"kind": "process"})
        body_r = g.rect(0, 12, regions.content.y, int(0.0 * tokens.EMU_PER_IN))
    if rest:
        r2 = _render_blocks(slide, rest, g.rect(0, 12, regions.content.y,
                                                int(0.0 if steps else regions.content.h)),
                            g, ctx, allow_two_col=False)
        overflow += r2["overflow"]; placements += r2["placements"]
    _footnote_band(slide, page, ctx, regions)
    return {"mode": "process_v", "overflow": overflow, "placements": placements}


def _timeline_h(slide, page, ctx, variant):
    regions = engine.page_regions(has_footnote=True)
    g = regions.grid
    ms, rest = [], []
    for b in (page.get("blocks") or []):
        if b.get("kind") == "timeline":
            ms = b.get("milestones") or b.get("payload", {}).get("milestones") or []
        else:
            rest.append(b)
    overflow, placements = [], []
    if ms:
        rect = g.rect(0, 12, regions.content.y + int(0.6 * tokens.EMU_PER_IN),
                      min(regions.content.h, int(3.0 * tokens.EMU_PER_IN)))
        timeline_builder.add_timeline(slide, rect, ms, ctx.theme, direction="h")
        placements.append({"kind": "timeline"})
        if rest:
            body_r = g.rect(0, 12, rect.y + rect.h + int(0.3 * tokens.EMU_PER_IN),
                            max(0, regions.content.h - rect.h - int(0.4 * tokens.EMU_PER_IN)))
            r2 = _render_blocks(slide, rest, body_r, g, ctx, allow_two_col=False)
            overflow += r2["overflow"]; placements += r2["placements"]
    else:
        r2 = _render_blocks(slide, rest, regions.content, g, ctx)
        overflow += r2["overflow"]; placements += r2["placements"]
    _footnote_band(slide, page, ctx, regions)
    return {"mode": "timeline_h", "overflow": overflow, "placements": placements}


def _timeline_v(slide, page, ctx, variant):
    regions = engine.page_regions(has_footnote=True)
    g = regions.grid
    ms, rest = [], []
    for b in (page.get("blocks") or []):
        if b.get("kind") == "timeline":
            ms = b.get("milestones") or b.get("payload", {}).get("milestones") or []
        else:
            rest.append(b)
    overflow, placements = [], []
    if ms:
        rect = g.rect(1, 10, regions.content.y, regions.content.h)
        timeline_builder.add_timeline(slide, rect, ms, ctx.theme, direction="v")
        placements.append({"kind": "timeline"})
    if rest:
        w = int(0.0 if ms else regions.content.h)
        r2 = _render_blocks(slide, rest, g.rect(0, 12, regions.content.y, w),
                            g, ctx, allow_two_col=False)
        overflow += r2["overflow"]; placements += r2["placements"]
    _footnote_band(slide, page, ctx, regions)
    return {"mode": "timeline_v", "overflow": overflow, "placements": placements}


def _table_full(slide, page, ctx, variant):
    regions = engine.page_regions(has_footnote=True)
    g = regions.grid
    blocks = page.get("blocks") or []
    rep = _render_blocks(slide, blocks, regions.content, g, ctx, allow_two_col=False)
    rep["mode"] = "table_full"
    _footnote_band(slide, page, ctx, regions)
    return rep


def _table_note(slide, page, ctx, variant):
    regions = engine.page_regions(has_footnote=True)
    g = regions.grid
    blocks = page.get("blocks") or []
    tblocks = [b for b in blocks if b.get("kind") == "table"]
    rest = [b for b in blocks if b.get("kind") != "table"]
    overflow, placements = [], []
    if tblocks:
        rect = g.rect(0, 8, regions.content.y, regions.content.h)
        payload = tblocks[0]
        _table_block(slide, rect, payload, ctx)
        placements.append({"kind": "table"})
        if rest:
            nrect = g.rect(8, 4, regions.content.y, regions.content.h)
            r2 = _render_blocks(slide, rest, nrect, g, ctx, allow_two_col=False)
            overflow += r2["overflow"]; placements += r2["placements"]
    else:
        r2 = _render_blocks(slide, blocks, regions.content, g, ctx)
        overflow += r2["overflow"]; placements += r2["placements"]
    _footnote_band(slide, page, ctx, regions)
    return {"mode": "table_note", "overflow": overflow, "placements": placements}


def _quote_centered(slide, page, ctx, variant):
    regions = engine.page_regions(has_footnote=False)
    g = regions.grid
    blocks = page.get("blocks") or []
    q = next((b for b in blocks if b.get("kind") == "quote"), None)
    text = (q.get("text") if q else None) or (blocks[0].get("text") if blocks else "") or ""
    rect = g.rect(1, 10, regions.content.y + int(0.8 * tokens.EMU_PER_IN),
                  int(2.6 * tokens.EMU_PER_IN))
    tb, tf = _textbox(slide, rect, anchor_top=False, role_key="lead")
    _fill_text(tf, f"“{text}”", "lead", ctx.theme, align=PP_ALIGN.CENTER,
               color_key="ink")
    src = (page.get("footnote") or {}).get("text") or ""
    if src:
        sr = g.rect(1, 10, rect.y + rect.h + int(0.2 * tokens.EMU_PER_IN),
                    int(0.5 * tokens.EMU_PER_IN))
        tb2, tf2 = _textbox(slide, sr, role_key="footnote")
        _fill_text(tf2, f"—— {src}", "footnote", ctx.theme, align=PP_ALIGN.CENTER)
    return {"mode": "quote_centered", "overflow": [], "placements": []}


def _quote_card(slide, page, ctx, variant):
    regions = engine.page_regions(has_footnote=False)
    g = regions.grid
    blocks = page.get("blocks") or []
    q = next((b for b in blocks if b.get("kind") == "quote"), None)
    text = (q.get("text") if q else None) or (blocks[0].get("text") if blocks else "") or ""
    card = g.rect(1, 10, regions.content.y + int(0.6 * tokens.EMU_PER_IN),
                  int(3.0 * tokens.EMU_PER_IN))
    _rect_shape(slide, card, ctx.theme, "substrate")
    bar = engine.Rect(card.x, card.y, int(0.09 * tokens.EMU_PER_IN), card.h)
    _rect_shape(slide, bar, ctx.theme, "accent")
    inner = card.inset(int(0.35 * tokens.EMU_PER_IN), int(0.3 * tokens.EMU_PER_IN))
    tb, tf = _textbox(slide, inner, anchor_top=False, role_key="lead+footnote")
    _fill_text(tf, f"“{text}”", "lead", ctx.theme)
    src = (page.get("footnote") or {}).get("text") or ""
    if src:
        _fill_text(tf, f"—— {src}", "footnote", ctx.theme, color_key="neutral")
    return {"mode": "quote_card", "overflow": [], "placements": []}


def _closing_statement(slide, page, ctx, variant):
    regions = engine.page_regions(has_lead=True, has_footnote=False)
    g = regions.grid
    blocks = page.get("blocks") or []
    text = next((b.get("text") for b in blocks if b.get("kind") in ("text", None)
                 and b.get("text")), "") or page.get("title") or ""
    rect = g.rect(1, 10, regions.content.y + int(0.6 * tokens.EMU_PER_IN),
                  int(2.8 * tokens.EMU_PER_IN))
    tb, tf = _textbox(slide, rect, anchor_top=False, role_key="conclusion")
    _fill_text(tf, text, "conclusion", ctx.theme, align=PP_ALIGN.CENTER)
    rule = engine.Rect(g.left + g.content_w // 2 - int(0.4 * tokens.EMU_PER_IN),
                       rect.y + rect.h + int(0.3 * tokens.EMU_PER_IN),
                       int(0.8 * tokens.EMU_PER_IN), int(0.045 * tokens.EMU_PER_IN))
    _rect_shape(slide, rule, ctx.theme, "accent")
    return {"mode": "closing_statement", "overflow": [], "placements": []}


def _closing_actions(slide, page, ctx, variant):
    regions = engine.page_regions(has_footnote=True)
    g = regions.grid
    blocks = page.get("blocks") or []
    items = []
    for b in blocks:
        if b.get("kind") == "bullets":
            items += b.get("items") or []
        elif b.get("text"):
            items.append(b["text"])
    overflow, placements = [], []
    if items:
        ord_rect = g.rect(0, 12, regions.content.y, regions.content.h)
        tb, tf = _textbox(slide, ord_rect, role_key="body+ordinal")
        for i, it in enumerate(items):
            p = _para(tf, i == 0)
            p.line_spacing = 1.4
            p.space_after = PT(8)
            r1 = p.add_run(); r1.text = f"{i + 1:02d}　"
            set_run_font(r1, "ordinal", ctx.theme, r1.text)
            r2 = p.add_run(); r2.text = str(it)
            set_run_font(r2, "body", ctx.theme, r2.text)
        placements.append({"kind": "actions"})
    else:
        r = _render_blocks(slide, blocks, regions.content, g, ctx)
        overflow += r["overflow"]; placements += r["placements"]
    _footnote_band(slide, page, ctx, regions)
    return {"mode": "closing_actions", "overflow": overflow, "placements": placements}


#: 页型 → 变体注册表。variant "a" 为主推，"b" 为结构不同的第二变体。
VARIANTS: dict[str, dict[str, object]] = {
    "cover":    {"a": _cover_centered, "b": _cover_left_rule},
    "section":  {"a": _section_number_rule, "b": _section_band},
    "content":  {"a": lambda s, p, c, v: _content_bullets(s, p, c, v, two_col=False),
                 "b": lambda s, p, c, v: _content_bullets(s, p, c, v, two_col=True)},
    "compare":  {"a": _compare_cards, "b": _compare_chart},
    "trend":    {"a": _trend_line, "b": _trend_column_note},
    "insight":  {"a": _insight_left, "b": _insight_center},
    "image":    {"a": _image_full, "b": _image_split},
    "process":  {"a": _process_h, "b": _process_v},
    "timeline": {"a": _timeline_h, "b": _timeline_v},
    "table":    {"a": _table_full, "b": _table_note},
    "quote":    {"a": _quote_centered, "b": _quote_card},
    "closing":  {"a": _closing_statement, "b": _closing_actions},
}

ROLE_ALIAS = {
    "cover": "cover", "section": "section", "content": "content",
    "compare": "compare", "trend": "trend", "insight": "insight",
    "image": "image", "process": "process", "timeline": "timeline",
    "table": "table", "quote": "quote", "closing": "closing",
}


class TemplateContext:
    """模板上下文：主题、图片解析结果与注入的 place_image。"""

    def __init__(self, theme: tokens.Theme, resolutions: dict | None = None,
                 place_image=None, emit=None):
        self.theme = theme
        self.resolutions = resolutions or {}
        self._place_image = place_image
        self.emit = emit

    def place_image(self, slide, rect, asset: dict, fit: str = "cover",
                    alt_text: str = ""):
        if self._place_image is None:
            raise RuntimeError("place_image 未注入（W1 实现或测试桩）")
        return self._place_image(slide, rect, asset, fit=fit, alt_text=alt_text)


#: 需要标准标题带的页型。cover/section 有自己的标题处理，不在此列（否则会出现两个标题）。
TITLE_BAND_ROLES = {"content", "compare", "trend", "insight", "image",
                    "process", "timeline", "table", "quote", "closing"}


def render_page(slide, page: dict, ctx: TemplateContext) -> dict:
    """渲染一页。page 为 C5 PageSpec dict。返回页级布局报告。"""
    role = ROLE_ALIAS.get(str(page.get("role") or "content"))
    if role is None:
        raise ValueError(f"未登记的页型 role：{page.get('role')!r}（已知 {sorted(VARIANTS)}）")
    variant = str(page.get("variant") or "a")
    fns = VARIANTS[role]
    if variant not in fns:
        raise ValueError(f"页型 {role} 没有 variant={variant!r}（可用 {sorted(fns)}）")
    # 标题带统一在这里画：各模板的正文区本来就始于 regions.content.y（标题带之下），
    # 所以先画标题不会与任何模板的排版冲突；漏画标题 = HG01 内容不完整。
    if role in TITLE_BAND_ROLES and str(page.get("title") or "").strip():
        regions = engine.page_regions(has_footnote=True)
        _title_band(slide, page, ctx, regions)
    report = fns[variant](slide, page, ctx, variant)
    report.update({"page_id": page.get("page_id"), "index": page.get("index"),
                   "role": role, "variant": variant})
    return report


def render_deck(prs, plan: dict, ctx: TemplateContext) -> list[dict]:
    """把 DeckPlan 的每一页渲染进一个新 Presentation（按页序）。"""
    from pptx.util import Emu as _Emu
    reports = []
    blank = prs.slide_layouts[6]
    for page in plan.get("pages", []):
        slide = prs.slides.add_slide(blank)
        reports.append(render_page(slide, page, ctx))
    return reports
