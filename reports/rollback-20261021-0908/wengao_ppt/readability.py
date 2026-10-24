# -*- coding: utf-8 -*-
"""可读性与重排引擎（2026-10-21 00:25 用户纠正：排版能力要提高、字体还是太小）。

背景与依据（只写已发生的事实，不写根因定论）
--------------------------------------------
- 用户原话："排版能力要大大提高，字体还是太小，看起来不协调"（三张真实截图
  P6/P2/P8 由监督目视，本模块不读图）。
- 监督实测 exact_v12/e43dad9e：P2 显式字号 9.5/10/10.5/11/12/15/17/20，
  P6 多为 16、标题 20，P8 多为 12/13/14、标题 20 —— 13 页处理链没落实字号要求。
- 原生缺字：监督独立对照 S6（**仅**把 a:ea typeface 由 Hiragino Sans GB 改成
  Arial Unicode MS，其余字节/ZIP 成员顺序/文字/备注/图片/转场/粒子全部保持）
  真实原生逐页放映 13 页未见中文缺字方框；S7（改 PingFang SC）虽无方框但出现
  毛笔/楷体感回退，不采用。本模块因此把东亚槽定为 Arial Unicode MS。

本模块做什么
------------
1. `set_native_ea_font`：只改东亚字体槽（a:ea）的 typeface，拉丁/复杂文种/
   lang/charset/pitchFamily 一律不动；
2. `SIZE_POLICY`：按内容角色给出字号（页标题 36–40、导语与主正文 26–28、
   常规正文下限 24、小标题与关键结论 28–32、关键数字 44–56、表格 20–22、
   图表与辅助 18–20、页脚与来源 16–18）；
3. `layout_p2/p6/p8`：P2 四组数据改 2×2 宽卡并建立数字/说明层级；
   P6 两侧条目统一顶线行距与 26–28 磅正文、中间"同一个人"留呼吸空间、底部结论放大；
   P8 五项影响改统一基线有序行、减少分散小框。

不做什么（硬约束）
------------------
- 不改文字、不删事实、不裁切、不把正文搬进备注；
- 不用 normAutofit 缩字塞内容（统一 noAutofit + 显式行距）；
- 不改页数/页序、不改转场、不删配图与粒子（只按内容需要移位/缩放）；
- 不在系统安装或分发字体文件；平台适用范围仅本机 macOS 实测（Arial Unicode.ttf
  存在于 /System/Library/Fonts），其他平台需各自实测后再声明。
"""
from __future__ import annotations

from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE
from pptx.util import Pt

IN = 914400.0
CANVAS_W_IN = 13.3333
CANVAS_H_IN = 7.5

#: 左右页边距与内容宽度（按 13.333in 宽稿标定）
MARGIN_IN = 0.72
CONTENT_W_IN = CANVAS_W_IN - 2 * MARGIN_IN      # 11.893in
RIGHT_IN = CANVAS_W_IN - MARGIN_IN              # 12.613in

#: 东亚字体槽（原生放映已验证的选择，见模块文档）
NATIVE_EA_FONT = "Arial Unicode MS"

#: 新可读基线（单位：磅）。角色 → 目标值；区间是允许范围，目标值取区间内确定值。
SIZE_POLICY = {
    "page_title": 36,      # 36–40
    "lead": 26,            # 26–28（导语/副标题，下限 24）
    "body": 26,            # 26–28，常规正文不得低于 24
    "body_min": 24,
    "section_head": 28,    # 28–32（小标题/关键结论）
    "conclusion": 30,      # 28–32
    "key_number": 44,      # 44–56
    "table": 21,           # 20–22
    "chart_label": 19,     # 18–20
    "aux": 18,             # 18–20（来源/序号等辅助说明）
    "footer": 16,          # 16–18
    "index": 20,           # 18–20（有序序号）
}

LAYOUT_VERSION = "readability-2026-10-21.0120-glyphfit-p268"


class ReadabilityError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# --------------------------------------------------------------------------- 度量
#: 度量安全系数：LibreOffice 实际排版比 PIL 度量宽约 4–6%（P2 说明行实测），
#: 估算折行时按 pt*1.06 量，避免"估算 1 行、实渲 2 行"压到相邻角色。
MEASURE_SAFETY = 1.06

#: 文本框高度经验式（来自 LibreOffice + Arial Unicode MS 实测字形框，非拍脑袋）：
#:   框高 = 顶部偏移 + (行数-1)×行距 + 单行字形纵向范围 + 底部内边距
#: 以下四个常数由本候选真实渲染（LibreOffice + Arial Unicode MS）实测标定，
#: 不是估算：spcPct=132% 是"字体自然行高"的百分比，Arial Unicode 自然行高约
#: 1.2em，故实际行距 ≈1.58em（实测 26pt 行间 0.57in、30pt 行间 0.655in）。
ASC_OFFSET_EM = 0.55    # 框顶 → 首行字形顶（实测 0.48–0.55em）
LINE_PITCH_EM = 1.58    # 实测行距（= 1.32 spcPct × 1.2em 自然行高）
GLYPH_EM_CJK = 1.10     # 单行字形纵向范围（实测 CJK 0.96–1.09em，取上限）
GLYPH_EM_DIGIT = 0.80   # 纯数字/符号（实测 44pt 数字 0.77em）
BOTTOM_PAD_IN = 0.06    # 字形底 → 框底


def measure_lines(text: str, pt: float, box_w_in: float, bold: bool = False) -> int:
    """按框宽估算折行数（复用 refine 的 4x 超采样度量，带 1.06 安全系数）。"""
    from .refine import wrapped_lines
    hard = (text or "").count("\n") + 1
    try:
        return max(hard, wrapped_lines(text, pt * MEASURE_SAFETY, box_w_in, bold))
    except Exception:  # noqa: BLE001  度量不可用时不阻塞排版，退化为按字宽估算
        if not text:
            return 1
        w_in = len(text) * pt * MEASURE_SAFETY / 72.0
        return max(1, int(w_in / max(box_w_in, 0.01)) + (1 if w_in % box_w_in else 0))


def box_h(pt: float, lines: int = 1, digit_only: bool = False) -> float:
    """容纳 n 行 pt 磅文字所需的**真实**框高（英寸）。"""
    em = GLYPH_EM_DIGIT if digit_only else GLYPH_EM_CJK
    return (ASC_OFFSET_EM + (lines - 1) * LINE_PITCH_EM + em) * pt / 72.0 + BOTTOM_PAD_IN


def _digit_only(text: str) -> bool:
    import re
    return re.fullmatch(r"[0-9+\-.%/,\s]*", text or "") is not None


def line_h_in(pt: float, factor: float = 1.32) -> float:
    return pt * factor / 72.0


# --------------------------------------------------------------------------- 字体槽
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _qn(tag: str) -> str:
    return "{%s}%s" % (_A, tag)


def _ensure_ea(rpr_el, font: str) -> bool:
    """在 rPr/defRPr 上把 a:ea 的 typeface 设为 font（缺失则按 schema 顺序插入）。"""
    ea = rpr_el.find(_qn("ea"))
    if ea is None:
        ea = rpr_el.makeelement(_qn("ea"), {})
        latin = rpr_el.find(_qn("latin"))
        cs = rpr_el.find(_qn("cs"))
        if latin is not None:
            latin.addnext(ea)
        elif cs is not None:
            cs.addprevious(ea)
        else:
            rpr_el.append(ea)
    if ea.get("typeface") == font:
        return False
    ea.set("typeface", font)
    return True


def set_native_ea_font(prs, font: str = NATIVE_EA_FONT) -> int:
    """只改东亚字体槽：slides + layouts + masters + notes。

    返回被修改的 a:ea 元素数量。拉丁槽/复杂文种/lang/charset/pitchFamily 不动。
    """
    n = 0
    parts = []
    for slide in prs.slides:
        parts.append(slide._element)
        try:
            if slide.has_notes_slide:
                parts.append(slide.notes_slide._element)
        except Exception:  # noqa: BLE001
            pass
    for layout in prs.slide_layouts:
        parts.append(layout._element)
        try:
            parts.append(layout.slide_master._element)
        except Exception:  # noqa: BLE001
            pass
    for master in prs.slide_masters:
        parts.append(master._element)
    seen = set()
    for root in parts:
        if id(root) in seen:
            continue
        seen.add(id(root))
        for tag in ("rPr", "defRPr", "endParaRPr"):
            for el in root.iter(_qn(tag)):
                if _ensure_ea(el, font):
                    n += 1
        # 主题字体方案里的 ea（majorFont/minorFont）
        for el in root.iter():
            if el.tag.endswith("}ea") and el.get("typeface") and el.get("typeface") != font:
                el.set("typeface", font)
                n += 1
    return n


# --------------------------------------------------------------------------- 形状
def _by_name(slide, name: str):
    for sh in slide.shapes:
        if sh.name == name:
            return sh
    return None


def set_box(shape, x, y, w, h):
    shape.left, shape.top = int(round(x * IN)), int(round(y * IN))
    shape.width, shape.height = int(round(w * IN)), int(round(h * IN))


def set_pt(shape, pt: float, line_factor: float = 1.32, wrap: bool = True):
    """把一个形状里**全部**字号槽（rPr/defRPr/endParaRPr）设为 pt，并关掉缩字。"""
    if not getattr(shape, "has_text_frame", False):
        return
    tf = shape.text_frame
    tf.word_wrap = wrap
    tf.auto_size = MSO_AUTO_SIZE.NONE          # 禁止 normAutofit 缩字塞内容
    # 顶锚定：原稿不少框是居中锚定，字形会从框中心向上下同时溢出，
    # 于是"对象框不相交"而真实字形相撞（5a6bcb2d P2 的失败形态之一）。
    tf.vertical_anchor = MSO_ANCHOR.TOP
    tf.margin_left = tf.margin_right = 0
    tf.margin_top = tf.margin_bottom = 0
    for para in tf.paragraphs:
        para.line_spacing = line_factor
        para.font.size = Pt(pt)
        for run in para.runs:
            run.font.size = Pt(pt)
    # XML 兜底：把列表样式层级的 defRPr / endParaRPr 也统一，避免原生端回退小字
    for tag in ("rPr", "defRPr", "endParaRPr"):
        for el in shape._element.iter(_qn(tag)):
            el.set("sz", str(int(round(pt * 100))))


def _fit(slide, name, x, y, w, pt, lines=None, digit_only=None):
    """按实际度量安置文本形状：折行数 → 真实框高（含顶部偏移与底部内边距）。

    这是修掉 5a6bcb2d P2 失败件的关键：不再把所有角色塞进固定高度框，
    而是按字体度量算高度，保证真实字形框**被文本框容纳**且彼此不相撞。
    """
    sh = _by_name(slide, name)
    if sh is None:
        return 0.0
    txt = sh.text_frame.text
    if digit_only is None:
        digit_only = _digit_only(txt)
    n = lines if lines else measure_lines(txt, pt, w)
    h = box_h(pt, n, digit_only)
    set_box(sh, x, y, w, h)
    set_pt(sh, pt)
    return h


def _place(slide, name, x, y, w, h, pt, line_factor=1.32, wrap=True):
    """pt=None 表示纯装饰形状（无文字），只重排不改字号。"""
    sh = _by_name(slide, name)
    if sh is None:
        return None
    set_box(sh, x, y, w, h)
    if pt is not None:
        set_pt(sh, pt, line_factor, wrap)
    return sh


# --------------------------------------------------------------------------- 页布局
def layout_p2(slide, acts):
    """P2：四组数据 2×2 宽卡；数字/说明/来源按真实度量各自给足高度（顶锚定+内边距）。

    修 5a6bcb2d 的已知失败：说明 26pt 在窄列里真实折行，原固定 0.49in 框装不下，
    字形既压住 +0.9% 也压住 Syntun 来源（对象框却不相交）。现在说明独立成列、
    按实测折行数给高度；来源与数字各自独立高度；结论与页脚之间留真实字形净空。
    """
    SP = SIZE_POLICY
    _fit(slide, "Text 1", MARGIN_IN, 0.22, CONTENT_W_IN, SP["page_title"], lines=1)
    _fit(slide, "Text 2", MARGIN_IN, 1.13, CONTENT_W_IN, SP["lead"], lines=1)

    gap = 0.30
    cw = (CONTENT_W_IN - gap) / 2
    col_x = [MARGIN_IN, MARGIN_IN + cw + gap]
    row_y = [1.90, 3.81]
    card_h = 1.81
    cards = ["Shape 3", "Shape 7", "Shape 11", "Shape 15"]
    nums = ["Text 4", "Text 8", "Text 12", "Text 16"]
    labels = ["Text 5", "Text 9", "Text 13", "Text 17"]
    srcs = ["Text 6", "Text 10", "Text 14", "Text 18"]
    for i, (cx, cy) in enumerate([(col_x[i % 2], row_y[i // 2]) for i in range(4)]):
        _place(slide, cards[i], cx, cy, cw, card_h, None)
        # 数字列 44pt（纯数字符号，独立高度）｜说明列 26pt 按实测 2 行给高度
        _fit(slide, nums[i], cx + 0.20, cy + 0.06, 2.35, SP["key_number"], lines=1)
        _fit(slide, labels[i], cx + 2.65, cy + 0.06, 2.95, SP["body"], lines=2)
        _fit(slide, srcs[i], cx + 0.20, cy + 1.35, cw - 0.40, SP["footer"], lines=1)

    # 底部结论：引号上抬（避免与结论首行合成同一文本行），结论与页脚留 ≈0.14in 净空
    _place(slide, "Shape 19", MARGIN_IN, 5.68, 9.60, 1.48, None)
    _place(slide, "Text 20", 0.85, 5.48, 0.35, 0.42, 32)
    _fit(slide, "Text 21", 1.25, 5.74, 8.87, SP["conclusion"], lines=2)
    for nm in ("Picture 26", "Rectangle 27"):
        _place(slide, nm, 10.61, 5.68, 2.00, 1.21, None)

    _fit(slide, "Text 22", MARGIN_IN, 7.03, 6.10, SP["footer"], lines=1)
    _fit(slide, "Text 24", 6.95, 7.03, 4.60, SP["footer"], lines=1)
    _fit(slide, "Text 23", 11.80, 7.03, 0.81, SP["footer"], lines=1)
    if acts is not None:
        acts.append({"rule": "layout_p2", "page": 2, "layout_version": LAYOUT_VERSION})


def layout_p6(slide, acts):
    """P6：两侧条目同顶线/行距 + 26pt 正文；竖线在标签下方起；结论整幅单行。

    修 5a6bcb2d：① 结论只剩“需求。”的失衡断行 → 改整幅单行（30pt、框宽
    11.36in，实测容纳得下 23 字）；② 中央竖线穿过“同一个人” → 标签移到中央列
    顶部，竖线自标签下方起，不盖字；配图落中央列底部服务“同一个人”语义。
    """
    SP = SIZE_POLICY
    _fit(slide, "Text 1", MARGIN_IN, 0.22, CONTENT_W_IN, SP["page_title"], lines=1)
    _fit(slide, "Text 2", MARGIN_IN, 1.13, CONTENT_W_IN, SP["lead"], lines=1)

    panel_y, panel_h = 1.83, 4.31
    panels = {"left": (MARGIN_IN, 5.05), "right": (7.563, 5.05)}
    groups = {
        "left": {"rect": "Shape 3", "head": "Text 4",
                 "dots": ["Shape 5", "Shape 7", "Shape 9", "Shape 11"],
                 "items": ["Text 6", "Text 8", "Text 10", "Text 12"],
                 "lines": [1, 1, 1, 1]},
        "right": {"rect": "Shape 13", "head": "Text 14",
                  "dots": ["Shape 15", "Shape 17", "Shape 19", "Shape 21"],
                  "items": ["Text 16", "Text 18", "Text 20", "Text 22"],
                  "lines": [1, 1, 2, 1]},
    }
    for side, (px, pw) in panels.items():
        g = groups[side]
        _place(slide, g["rect"], px, panel_y, pw, panel_h, None)
        _fit(slide, g["head"], px + 0.22, 1.91, pw - 0.42, SP["section_head"], lines=1)
        y, item_w = 2.66, pw - 0.65
        for dot, item, n in zip(g["dots"], g["items"], g["lines"]):
            h = box_h(SP["body"], n)
            _place(slide, dot, px + 0.28, y + 0.16, 0.08, 0.08, None)
            _fit(slide, item, px + 0.55, y, item_w, SP["body"], lines=n)
            y += h + 0.06

    _fit(slide, "Text 24", 5.78, 1.91, 1.77, SP["section_head"], lines=1)
    _place(slide, "Shape 23", 6.667, 2.66, 0.0, 1.55, None)
    for nm in ("Picture 31", "Rectangle 32"):
        _place(slide, nm, 5.79, 4.28, 1.75, 1.06, None)

    _place(slide, "Shape 25", MARGIN_IN, 6.20, CONTENT_W_IN, 0.95, None)
    _place(slide, "Text 26", 0.85, 5.94, 0.35, 0.42, 32)
    _fit(slide, "Text 27", 1.25, 6.26, 11.36, SP["conclusion"], lines=1)

    _fit(slide, "Text 28", MARGIN_IN, 7.03, 6.10, SP["footer"], lines=1)
    _fit(slide, "Text 29", 11.80, 7.03, 0.81, SP["footer"], lines=1)
    if acts is not None:
        acts.append({"rule": "layout_p6", "page": 6, "layout_version": LAYOUT_VERSION})


def layout_p8(slide, acts):
    """P8：五项影响改统一基线有序行；行框按 26pt 真实字形高度给内边距。

    修 5a6bcb2d：行框 0.52in 装不下 26pt 字形（真实需要 0.656in），字形压在框
    底；结论同样缺底部内边距。现在行框 0.656in、结论框按 2 行度量；同行内
    序号/标题/说明按**字形顶**对齐（不是框顶对齐）。导语取 24pt（正文下限）：
    该页要同时容纳 5 行正文 + 30pt 两行结论 + 页脚，26pt 导语会挤掉行间距。
    """
    SP = SIZE_POLICY
    _fit(slide, "Text 1", MARGIN_IN, 0.20, CONTENT_W_IN, SP["page_title"], lines=1)
    _fit(slide, "Text 2", MARGIN_IN, 1.11, CONTENT_W_IN, SP["body_min"], lines=2)

    bands = ["Shape 3", "Shape 8", "Shape 13", "Shape 18", "Shape 23"]
    bars = ["Shape 4", "Shape 9", "Shape 14", "Shape 19", "Shape 24"]
    idxs = ["Text 5", "Text 10", "Text 15", "Text 20", "Text 25"]
    titles = ["Text 6", "Text 11", "Text 16", "Text 21", "Text 26"]
    descs = ["Text 7", "Text 12", "Text 17", "Text 22", "Text 27"]
    row_h, row_gap, y0 = box_h(SP["body"], 1), 0.05, 2.24
    base_off = ASC_OFFSET_EM * SP["body"] / 72.0
    desc_w = CONTENT_W_IN - 3.75 + MARGIN_IN - 0.13
    for i in range(5):
        ry = y0 + i * (row_h + row_gap)
        _place(slide, bands[i], MARGIN_IN, ry, CONTENT_W_IN, row_h, None)
        _place(slide, bars[i], MARGIN_IN, ry, 0.07, row_h, None)
        for nm, pt, x, w in ((idxs[i], SP["index"], 0.90, 0.55),
                             (titles[i], SP["body"], 1.52, 2.10),
                             (descs[i], SP["body"], 3.75, desc_w)):
            dy = base_off - ASC_OFFSET_EM * pt / 72.0   # 字形顶对齐，而非框顶对齐
            _fit(slide, nm, x, ry + dy, w, pt, lines=1)

    _place(slide, "Shape 28", MARGIN_IN, 5.74, 9.60, 1.36, None)
    _place(slide, "Text 29", 0.85, 5.52, 0.35, 0.42, 32)
    _fit(slide, "Text 30", 1.25, 5.78, 8.87, SP["conclusion"], lines=2)
    for nm in ("Picture 34", "Rectangle 35"):
        _place(slide, nm, 10.61, 5.74, 2.00, 1.21, None)

    _fit(slide, "Text 31", MARGIN_IN, 7.06, 6.10, SP["footer"], lines=1)
    _fit(slide, "Text 32", 11.80, 7.06, 0.81, SP["footer"], lines=1)
    if acts is not None:
        acts.append({"rule": "layout_p8", "page": 8, "layout_version": LAYOUT_VERSION})


PAGE_LAYOUTS = {2: layout_p2, 6: layout_p6, 8: layout_p8}


def apply_readable_deck(prs, pages=(2, 6, 8), all_pages=False):
    """全稿处理：指定页走专用版式，其余页走同一套通用规则（all_pages=True）。

    返回的 acts 记录每页走了哪条规则，便于监督核对"不是一次性脚本样稿"。
    """
    acts = []
    ea = set_native_ea_font(prs)
    for i, slide in enumerate(prs.slides, 1):
        if i in PAGE_LAYOUTS:
            PAGE_LAYOUTS[i](slide, acts)
        elif all_pages:
            layout_generic(slide, i, acts)
    return {"ea_font_changes": ea, "acts": acts, "layout_version": LAYOUT_VERSION,
            "pages_relayout": sorted({a["page"] for a in acts})}


def apply_readable_batch(prs, pages=(2, 6, 8)):
    """兼容旧调用：只重排指定页（其余页仅改东亚字体槽）。"""
    return apply_readable_deck(prs, pages=pages, all_pages=False)


# --------------------------------------------------------------------------- 通用页
_FOOTER_HINTS = ("Yiten Huang", "来源", "资料来源")


def _is_footer_text(t: str) -> bool:
    import re as _re
    t = (t or "").strip()
    if any(h in t for h in _FOOTER_HINTS):
        return True
    return bool(_re.fullmatch(r"\d{1,2}", t))


def _is_mark_text(t: str) -> bool:
    return (t or "").strip() in ("“", "”", "「")


def _columns_of(shapes, tol_in=1.5):
    """按 x 中心聚类成列（保留原横向结构的语义）。"""
    cols = []
    for sh in sorted(shapes, key=lambda s: s.left):
        c = (sh.left + sh.width / 2) / IN
        for col in cols:
            if abs(c - col["c"]) <= tol_in:
                col["items"].append(sh)
                col["c"] = sum((x.left + x.width / 2) / IN for x in col["items"]) / len(col["items"])
                break
        else:
            cols.append({"c": c, "items": [sh]})
    for col in cols:
        col["items"].sort(key=lambda s: s.top)
    return cols


def layout_generic(slide, page, acts):
    """其余页的同一套规则：**只改字号与纵向容纳**，不改文字。

    与 P2/P6/P8 同一口径：
    - 角色：最上为页标题(36)、其次导语(26/24)、底部**长句**(≥20字)为结论(30)、
      含 Yiten Huang/来源/纯页码 为页脚(16)、其余为正文；
    - 每个形状按框宽实测折行数（含显式换行）→ 真实框高（顶锚定 + 底部内边距）；
    - 横向按原结构分列并**等宽归一化**，列内纵向重排；依次尝试
      (边距0.72/26pt/行距0.10) → (0.72/24pt/0.08) → (0.60/24pt/0.06) →
      (0.55/24pt/0.05)，取第一个容纳得下的组合，正文不低于 24pt 下限；
    - 页脚多行也钳在画布内；结论与页脚留真实字形净空；配图移到右下槽。
    """
    SP = SIZE_POLICY
    texts = [sh for sh in slide.shapes
             if getattr(sh, "has_text_frame", False) and sh.text_frame.text.strip()]
    if not texts:
        return
    ordered = sorted(texts, key=lambda s: (s.top, s.left))
    footers = [s for s in texts if _is_footer_text(s.text_frame.text)]
    marks = [s for s in texts if _is_mark_text(s.text_frame.text)]
    rest = [s for s in ordered if s not in footers and s not in marks]
    if not rest:
        return
    title = rest[0]
    lead = rest[1] if len(rest) > 1 else None
    tail = rest[2:]
    bottom = [s for s in tail if s.top / IN > 4.5 and len(s.text_frame.text.strip()) >= 20]
    conclusion = max(bottom, key=lambda s: len(s.text_frame.text)) if bottom else None
    content = [s for s in tail if s is not conclusion]

    # 标题 / 导语
    _fit(slide, title.name, MARGIN_IN, 0.22, CONTENT_W_IN, SP["page_title"], lines=1)
    y = title.top / IN + title.height / IN + 0.10
    if lead is not None:
        n = measure_lines(lead.text_frame.text, SP["lead"], CONTENT_W_IN)
        pt = SP["lead"] if n <= 2 else SP["body_min"]
        _fit(slide, lead.name, MARGIN_IN, y, CONTENT_W_IN, pt, lines=min(n, 3))
        y = lead.top / IN + lead.height / IN + 0.14

    # 页脚：多行也钳在画布内
    footer_y = 7.03
    for s in footers:
        w = min(max(s.width / IN, 1.2), CONTENT_W_IN)
        n = min(measure_lines(s.text_frame.text, SP["footer"], w), 3)
        h = box_h(SP["footer"], n)
        fy = min(footer_y, CANVAS_H_IN - 0.06 - h)
        _fit(slide, s.name, min(max(s.left / IN, MARGIN_IN), CANVAS_W_IN - MARGIN_IN - w),
             fy, w, SP["footer"], lines=n)

    # 结论（页脚之上留净空）
    if conclusion is not None:
        cw = 9.60
        n = min(measure_lines(conclusion.text_frame.text, SP["conclusion"], cw), 3)
        h = box_h(SP["conclusion"], n)
        concl_y = min(footer_y, CANVAS_H_IN - 0.06 - h) - 0.14 - (h - BOTTOM_PAD_IN)
        _fit(slide, conclusion.name, MARGIN_IN + 0.53, concl_y, cw, SP["conclusion"], lines=n)
        for m in marks:
            _place(slide, m.name, MARGIN_IN + 0.13, concl_y - 0.26, 0.35, 0.42, 32)
        content_bottom = concl_y - 0.14
    else:
        content_bottom = footer_y - 0.16

    # 正文：分列 → 等宽归一化 → 列内重排，取第一个容纳得下的组合
    if content:
        cols = _columns_of(content)
        avail = max(content_bottom - y, 0.5)
        plan = None
        for margin, pt, gap in ((0.72, SP["body"], 0.10), (0.72, SP["body_min"], 0.08),
                                (0.60, SP["body_min"], 0.06), (0.55, SP["body_min"], 0.05)):
            cw_tot = CANVAS_W_IN - 2 * margin
            col_gap = 0.20
            col_w = (cw_tot - col_gap * (len(cols) - 1)) / len(cols)
            heights = []
            for col in cols:
                hsum = 0.0
                for s in col["items"]:
                    w = 0.55 if len(s.text_frame.text.strip()) <= 2 else col_w
                    n = measure_lines(s.text_frame.text, pt, w)
                    hsum += box_h(pt, n) + gap
                heights.append(hsum - gap)
            if max(heights) <= avail:
                plan = (margin, pt, gap, col_w, col_gap)
                break
        if plan is None:
            plan = (0.55, SP["body_min"], 0.05,
                    (CANVAS_W_IN - 1.10 - 0.20 * (len(cols) - 1)) / len(cols), 0.20)
        margin, pt, gap, col_w, col_gap = plan
        for j, col in enumerate(cols):
            cx = margin + j * (col_w + col_gap)
            cy = y
            for s in col["items"]:
                short = len(s.text_frame.text.strip()) <= 2
                w = 0.55 if short else col_w
                n = measure_lines(s.text_frame.text, pt, w)
                _fit(slide, s.name, cx, cy, w, pt, lines=n)
                cy += box_h(pt, n) + gap

    for sh in slide.shapes:
        if sh.__class__.__name__ == "Picture" and sh.width / IN < 8.0:
            set_box(sh, 10.61, 5.55, 2.00, 1.21)
    if acts is not None:
        acts.append({"rule": "layout_generic", "page": page,
                     "layout_version": LAYOUT_VERSION})
