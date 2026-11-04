# -*- coding: utf-8 -*-
"""原稿适配精修引擎（wengao-adaptive 主题的应用路径）。

设计立场（与 theme.py 的全局重染区分）
------------------------------------
- `theme.py:apply_theme` 是**换肤**：把整套稿染成主题调色板。对本来就有
  设计语言的原稿，换肤会覆盖品牌色、底色与卡片填充——那是破坏不是美化。
- 本模块是**精修**：先识别原稿自身的设计语言（底色/文字色/强调色/字体/字号
  层级），再按结构规则做有依据的布局、位置、尺寸、字号、间距与对齐调整；
  文字、页数、页序、比例、数据、业务色义一律不动。

规则全部结构驱动（按形状关系判定，不按页码/文件名特判）；测量安全
（PIL 字体实测宽度，放不下就不动）；原稿已经好的页只记录不改。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

from .type_scale import (
    HIERARCHY,
    ROLE_RANGE_PT,
    TITLE_BODY_MIN_RATIO,
    TYPE_MAX_PT,
    TYPE_MIN_PT,
    check_page_hierarchy,
    is_decor,
)

RULES_VERSION = "1.0.1"
ADAPTIVE_THEME_ID = "wengao-adaptive"

# 参与规则摘要的参数（改这些值 = 新设计参数 = 新身份，旧任务不得沿用）
DEFAULT_PARAMS = {
    "min_read_pt": 8.0,          # 子标签/来源类文字的可读下限
    "label_target_pt": 9.5,      # 卡片标签目标字号
    "row_label_pt": 15.0,        # 行式清单标签目标字号
    "row_desc_pt": 14.0,         # 行式清单描述目标字号
    "card_pad_in": 0.18,         # 卡片内边距（英寸，按 13.33in 宽稿标定）
    "row_pad_in": 0.22,          # 行式清单内边距
    "grid_row_pad_in": 0.125,    # 单列清单行内边距
    "grid_row_gap_in": 0.115,    # 单列清单行间距
    "content_gap_in": 0.50,      # 内容块之间标准间距
    "grid_list_min_items": 7,    # 列主序网格达到多少项才改单列
    "grid_list_min_pt": 12.0,    # 改单列后标题字号下限
    "bullet_gap_in": 0.20,       # 列表项间距
    "title_tol_in": 0.06,        # 标题位置容差
    "footer_tol_in": 0.06,       # 页脚位置容差
    "size_tol_pt": 0.6,          # 字号容差
    "panel_center_ratio": 2.0,   # 面板列表数差几倍时居中短列表
}

#: 2026-10-21 00:25：度量字体与产品写出的 a:ea 槽保持一致——先 Arial Unicode MS，
#: 缺失时再退回本机其它中文字体（度量只用于估算占位，不改变稿内字体引用）。
_MEASURE_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Songti.ttc",
)


class RefineError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# --------------------------------------------------------------------------- 测量

_font_cache: dict = {}


def _measure_font(pt: float, bold: bool = False):
    """本地度量字体（Hiragino Sans GB 是 Microsoft YaHei 的最近替代）。"""
    from PIL import ImageFont
    key = (round(pt, 1), bold)
    if key not in _font_cache:
        last = None
        for path in _MEASURE_FONT_CANDIDATES:
            try:
                _font_cache[key] = ImageFont.truetype(path, int(round(pt * 4)))
                break
            except Exception as e:  # noqa: BLE001
                last = e
        if key not in _font_cache:
            raise RefineError("REFINE.NO_MEASURE_FONT", f"无可用度量字体：{last}")
    return _font_cache[key]


def text_width_in(text: str, pt: float, bold: bool = False) -> float:
    """单行文本宽度（英寸）。4x 超采样后除回去，减小取整误差。"""
    if not text:
        return 0.0
    f = _measure_font(pt, bold)
    try:
        w = f.getlength(text)
    except Exception:  # noqa: BLE001
        w = len(text) * pt * 4 * 0.5
    return w / 4.0 / 72.0


def wrapped_lines(text: str, pt: float, box_w_in: float, bold: bool = False) -> int:
    """按框宽估算折行数（CJK 逐字、西文按词；保守估宽）。"""
    if not text or box_w_in <= 0:
        return 1
    if text_width_in(text, pt, bold) <= box_w_in:
        return 1
    # 逐字累积（中文为主）；西文单词不拆
    import re
    tokens = re.findall(r"[A-Za-z0-9.%+\-–—/]+|[^\s]", text)
    lines, cur = 1, 0.0
    for tk in tokens:
        w = text_width_in(tk, pt, bold)
        if cur + w > box_w_in and cur > 0:
            lines += 1
            cur = w
        else:
            cur += w
    return lines


def rules_digest(params: dict | None = None) -> str:
    p = dict(DEFAULT_PARAMS if params is None else params)
    blob = json.dumps({"rules_version": RULES_VERSION, "params": p},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def effective_refine_config() -> dict:
    return {"theme_id": ADAPTIVE_THEME_ID, "refine_rules_version": RULES_VERSION,
            "params": dict(DEFAULT_PARAMS), "size_rule": "measure_then_lift",
            "scope": "layout_position_size_spacing_alignment_only"}


# --------------------------------------------------------------------------- 形状编目

class Sh:
    """形状编目项：几何用英寸（同时保留相对比例）。"""

    def __init__(self, slide, shape, idx, sw, sh_):
        self.slide = slide
        self.shape = shape
        self.idx = idx
        self.name = shape.name
        self.sw, self.sh = sw, sh_
        self.L = (shape.left or 0) / 914400.0
        self.T = (shape.top or 0) / 914400.0
        self.W = (shape.width or 0) / 914400.0
        self.H = (shape.height or 0) / 914400.0
        self.is_text = bool(getattr(shape, "has_text_frame", False) and shape.has_text_frame
                            and (shape.text_frame.text or "").strip())
        self.text = shape.text_frame.text if self.is_text else ""
        self.runs = []           # (para_i, run_i, text, pt, color_hex, bold)
        if self.is_text:
            for pi, para in enumerate(shape.text_frame.paragraphs):
                for ri, run in enumerate(para.runs):
                    pt = float(run.font.size.pt) if run.font.size is not None else None
                    col = None
                    try:
                        if run.font.color is not None and run.font.color.type is not None:
                            col = str(run.font.color.rgb)
                    except Exception:  # noqa: BLE001
                        col = None
                    self.runs.append((pi, ri, run.text, pt, col, bool(run.font.bold)))
        self.has_table = bool(getattr(shape, "has_table", False) and shape.has_table)
        self.has_chart = bool(getattr(shape, "has_chart", False) and shape.has_chart)
        self.is_title_placeholder = False
        try:
            if getattr(shape, "is_placeholder", False):
                from pptx.enum.shapes import PP_PLACEHOLDER
                self.is_title_placeholder = shape.placeholder_format.type in (
                    PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE)
        except Exception:  # noqa: BLE001
            self.is_title_placeholder = False
        self.is_pic = shape.shape_type is not None and "PICTURE" in str(shape.shape_type)
        self.fill_hex = None
        try:
            if shape.fill.type is not None and "SOLID" in str(shape.fill.type):
                self.fill_hex = str(shape.fill.fore_color.rgb)
        except Exception:  # noqa: BLE001
            self.fill_hex = None

    # -- 文本度量辅助
    def first_pt(self):
        for r in self.runs:
            if r[3]:
                return r[3]
        return None

    def max_pt(self):
        v = [r[3] for r in self.runs if r[3]]
        return max(v) if v else None

    def set_pt(self, pt):
        from pptx.util import Pt
        for pi, ri, _t, _p, _c, _b in self.runs:
            self.shape.text_frame.paragraphs[pi].runs[ri].font.size = Pt(pt)

    def line_count(self, box_w=None):
        """按当前框宽估算总行数（所有段落行数之和）。"""
        w = box_w if box_w is not None else self.W
        total = 0
        if self.is_text:
            from .visual import NO_WRAP_BOX_W_IN, text_frame_no_wrap
            if text_frame_no_wrap(self.shape):
                w = NO_WRAP_BOX_W_IN
            for para in self.shape.text_frame.paragraphs:
                t = "".join(r.text for r in para.runs)
                if not t:
                    total += 1
                    continue
                pt = max([float(r.font.size.pt) for r in para.runs if r.font.size] or [12])
                total += wrapped_lines(t, pt, max(w - 0.06, 0.2))
        return max(total, 1)

    def content_h_in(self, box_w=None):
        """文本内容高度（英寸）：行数 × 字号 × 1.22 行距。"""
        pt = self.max_pt() or 12.0
        return self.line_count(box_w) * pt * 1.22 / 72.0

    def text_inset_in(self) -> float:
        """文本框上下内边距之和（英寸）。未显式设置时用 PPT 默认 0.05in。"""
        if not self.is_text:
            return 0.0
        tf = self.shape.text_frame
        top = tf.margin_top
        bot = tf.margin_bottom
        top = 0.05 if top is None else top / 914400.0
        bot = 0.05 if bot is None else bot / 914400.0
        return top + bot

    def text_inset_w_in(self) -> float:
        """文本框左右内边距之和（英寸）。未显式设置时用 PPT 默认 0.1in*2=0.2in。"""
        if not self.is_text:
            return 0.0
        tf = self.shape.text_frame
        l = tf.margin_left
        r = tf.margin_right
        l = 0.1 if l is None else l / 914400.0
        r = 0.1 if r is None else r / 914400.0
        return l + r

    @property
    def is_no_wrap(self) -> bool:
        if not self.is_text:
            return False
        from .visual import text_frame_no_wrap
        return text_frame_no_wrap(self.shape)

    def needed_w_in(self, target_pt: float | None = None) -> float:
        """计算不折行单行文本框需要的宽度（英寸，含左右边距与容差）。"""
        if not self.is_text:
            return self.W
        tf = self.shape.text_frame
        paras = ["".join(r.text for r in p.runs) for p in tf.paragraphs]
        lines = [t.strip() for t in paras if t.strip()]
        if not lines:
            return self.W
        pt = target_pt if target_pt is not None else (self.max_pt() or 12.0)
        max_tw = max((text_width_in(line, pt) for line in lines), default=0.0)
        inset = (tf.margin_left / 914400.0 if tf.margin_left is not None else 0.05) + \
                (tf.margin_right / 914400.0 if tf.margin_right is not None else 0.05)
        return max_tw * 1.18 + inset + 0.12

    def one_line_fits(self, pt, box_w):
        t = self.text.strip()
        if not t:
            return True
        return text_width_in(t, pt) <= box_w - 0.06

    def move(self, L=None, T=None, W=None, H=None):
        """按英寸改形状几何；未传的维度保持不变。

        占位符（Title/Subtitle 等）常常整套 left/top/width/height 都继承自
        版式，自身没有 `<a:xfrm>`。python-pptx 只写被显式赋值的那一维：
        新建 xfrm 时另外三维按 0 起步，只有当次设的那维被覆盖——实测只设
        height 会把本来继承来的 width 悄悄写成 0（HG02 大量 CHAR_UNMATCHED/
        CHAR_MISSING 的真实根因：标题框宽变 0，居中文字一半跑到画布外）。
        因此每次落盘都把四维全部显式写一遍，未传的维度用当前已解析的
        有效值（含继承值，见 __init__）补齐，而不是只写被改的那一维。
        """
        from pptx.util import Emu
        if L is None and T is None and W is None and H is None:
            return
        L = self.L if L is None else L
        T = self.T if T is None else T
        W = self.W if W is None else W
        H = self.H if H is None else H
        self.shape.left = Emu(int(round(L * 914400)))
        self.shape.top = Emu(int(round(T * 914400)))
        self.shape.width = Emu(int(round(W * 914400)))
        self.shape.height = Emu(int(round(H * 914400)))
        self.L, self.T, self.W, self.H = L, T, W, H


def _slide_bg(slide):
    try:
        bg = slide.background
        if bg.fill.type is not None and "SOLID" in str(bg.fill.type):
            return str(bg.fill.fore_color.rgb).upper()
    except Exception:  # noqa: BLE001
        pass
    return None


def _rgb(hexs):
    return tuple(int(hexs[i:i + 2], 16) for i in (0, 2, 4))


def _lum(c):
    def f(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = c
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _contrast(a, b):
    la, lb = _lum(_rgb(a)), _lum(_rgb(b))
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _is_semantic(hexs):
    """业务语义色（红/绿/蓝/金的值型文字）不参与归一。"""
    if not hexs:
        return False
    r, g, b = _rgb(hexs)
    mx, mn = max(r, g, b), min(r, g, b)
    if mx - mn < 40:          # 近灰
        return False
    return (r > 120 and r > g + 40 and r > b + 40) or \
           (g > 110 and g > r + 30 and g > b + 30) or \
           (b > 110 and b > r + 30 and b > g + 10) or \
           (r > 150 and g > 110 and b < 110 and abs(r - g) < 90)


# --------------------------------------------------------------------------- 设计语言

def detect_language(slides_sh):
    """识别原稿设计语言；识别不出返回 None（保守模式：只做安全归一）。"""
    from collections import Counter
    bgs, colors, fonts, sizes = Counter(), Counter(), Counter(), Counter()
    for shs in slides_sh:
        for s in shs:
            if s.has_table or s.has_chart or s.is_pic:
                continue
            for _pi, _ri, _t, pt, col, _b in s.runs:
                if pt:
                    sizes[pt] += 1
                if col:
                    colors[col.upper()] += 1
    for shs in slides_sh:
        pass
    bg = None
    for shs in slides_sh:
        for s in shs:
            if s.fill_hex and s.W * s.H > 0.5:   # 大面板色近似底色
                bgs[s.fill_hex] += 1
    # 页面背景以 slide.background 为准（编目时另存）
    return {"bg": bg, "colors": colors, "sizes": sizes}


# --------------------------------------------------------------------------- 页型判定

def _group_rows(items, key_tol=0.35):
    """按 top 聚类成行（英寸容差）。"""
    rows = []
    for it in sorted(items, key=lambda s: (s.T, s.L)):
        for row in rows:
            if abs(row[0].T - it.T) <= key_tol:
                row.append(it)
                break
        else:
            rows.append([it])
    return rows


def _group_cols(items, key_tol=0.35):
    cols = []
    for it in sorted(items, key=lambda s: (s.L, s.T)):
        for col in cols:
            if abs(col[0].L - it.L) <= key_tol:
                col.append(it)
                break
        else:
            cols.append([it])
    return cols


def detect_page_type(idx, n_slides, shs, sw, sh_, deck_max_pt):
    """结构页型（返回类型名；识别不出返回 'generic'）。"""
    texts = [s for s in shs if s.is_text and s.text.strip()]
    cards = [s for s in shs if not s.is_text and not s.has_table and not s.has_chart
             and not s.is_pic and s.W > 0.8 and s.H > 0.35]
    if idx == 1 and texts:
        big = [s for s in texts if (s.max_pt() or 0) >= deck_max_pt * 0.75]
        if big:
            return "cover"
    if idx == n_slides and texts:
        big = [s for s in texts if (s.max_pt() or 0) >= deck_max_pt * 0.6]
        if big and len(texts) <= 6:
            return "closing"
    # KPI 行：同一行 ≥3 张卡，每卡有一个大数字 run + 小标签 run
    row_cards = [r for r in _group_rows(cards) if len(r) >= 3]
    for row in row_cards:
        kpi = 0
        for c in row:
            inside = [t for t in texts if c.L - 0.1 <= t.L and t.L + t.W <= c.L + c.W + 0.25
                      and c.T - 0.1 <= t.T and t.T <= c.T + c.H + 0.1]
            if any((t.max_pt() or 0) >= deck_max_pt * 0.45 for t in inside) and len(inside) >= 2:
                kpi += 1
        if kpi >= 3:
            return "kpi_row"
    # 行式清单：≥3 条横向带，每条 = 短标签(≤12字符) + 较长描述
    bands = [s for s in cards if s.W > sw * 0.45 and s.H < sh_ * 0.2]
    if len(bands) >= 3:
        ok = 0
        for b in bands:
            inside = [t for t in texts if b.L - 0.05 <= t.L and t.T >= b.T - 0.05
                      and t.T + t.H <= b.T + b.H + 0.12]
            short = [t for t in inside if len(t.text.strip()) <= 12]
            long_ = [t for t in inside if len(t.text.strip()) > 12]
            if short and long_:
                ok += 1
        if ok >= 3:
            return "label_rows"
    # 卡片网格：≥2 行 × ≥2 列，格内有序号 run
    grid_cards = cards
    if len(grid_cards) >= 4:
        rows = _group_rows(grid_cards)
        cols = _group_cols(grid_cards)
        if len(rows) >= 2 and len(cols) >= 2:
            indexed = 0
            for c in grid_cards:
                inside = [t for t in texts if c.L - 0.1 <= t.L and t.L + t.W <= c.L + c.W + 0.3
                          and c.T - 0.1 <= t.T and t.T <= c.T + c.H + 0.15]
                if any(_is_index(t.text.strip()) for t in inside):
                    indexed += 1
            if indexed >= max(4, len(grid_cards) - 1):
                return "card_grid"
            return "card_grid_plain"
    # 双面板：取两个最大的并排非文本形状（容忍分隔线/装饰形状存在）
    big = sorted([s for s in cards if s.W > sw * 0.2 and s.H > sh_ * 0.25],
                 key=lambda s: -(s.W * s.H))
    pair = None
    for i in range(min(3, len(big))):
        for j in range(i + 1, min(3, len(big))):
            a, b = big[i], big[j]
            if (abs(a.T - b.T) < 0.25 and a.W > sw * 0.25 and b.W > sw * 0.25
                    and (a.L + a.W <= b.L + 0.15 or b.L + b.W <= a.L + 0.15)):
                pair = (a, b)
                break
        if pair:
            break
    if pair:
        return "two_panel"
    # 步骤行：同一行 ≥3 卡，格内有序号 + 标题 + 描述
    for row in row_cards:
        if len(row) >= 3:
            steps = 0
            for c in row:
                inside = [t for t in texts if c.L - 0.1 <= t.L and t.L + t.W <= c.L + c.W + 0.3
                          and c.T - 0.1 <= t.T and t.T <= c.T + c.H + 0.15]
                if any(_is_index(t.text.strip()) for t in inside) and len(inside) >= 2:
                    steps += 1
            if steps >= 3:
                return "steps_row"
    return "generic"


def _is_index(t):
    import re
    return bool(re.fullmatch(r"\d{1,2}", t.strip()))


# --------------------------------------------------------------------------- 规则

def _inside(card, texts, pad=0.12):
    return [t for t in texts
            if t.L >= card.L - pad and t.L + t.W <= card.L + card.W + pad * 2
            and t.T >= card.T - pad and t.T <= card.T + card.H + pad]


def rule_kpi_row(shs, P, lang, acts, sw, sh_):
    """指标行：子标签提到可读下限；卡高贴合内容；数字/标签/子标签重排。"""
    texts = [s for s in shs if s.is_text and s.text.strip()]
    cards = [s for s in shs if not s.is_text and not s.has_table and not s.has_chart
             and not s.is_pic and s.W > 0.8 and s.H > 0.6]
    rows = [r for r in _group_rows(cards) if len(r) >= 3]
    if not rows:
        return
    row = rows[0]
    # 每卡内容（按 x 归卡）
    groups = []
    for c in sorted(row, key=lambda s: s.L):
        g = _inside(c, texts)
        if len(g) >= 2:
            groups.append((c, sorted(g, key=lambda s: s.T)))
    if len(groups) < 3:
        return
    # 判定角色：数字（最大）/ 标签 / 子标签
    parsed = []
    for c, g in groups:
        num = max(g, key=lambda s: s.max_pt() or 0)
        rest = [t for t in g if t is not num]
        if not rest:
            continue
        label = rest[0]
        subs = rest[1:]
        parsed.append((c, num, label, subs))
    if len(parsed) < 3:
        return
    # 目标字号（测量安全：目标号放得下才用，放不下保持原字号）+ 卡高/重排
    pad = P["card_pad_in"]
    for c, num, label, subs in parsed:
        inner_w = c.W - 2 * pad
        # 标签：提到目标字号（一行放得下才提）
        tgt = P["label_target_pt"]
        lp = tgt if label.one_line_fits(tgt, inner_w) else (label.max_pt() or tgt)
        if lp > (label.max_pt() or 0) + 0.05:
            label.set_pt(round(lp, 1))
            acts.append({"rule": "kpi_label_size", "shape": label.name,
                         "from": label.max_pt(), "to": round(lp, 1)})
        # 子标签：提到可读下限；一行放不下且原字号已达标则保持原样，
        # 原字号低于下限时允许折两行（卡高同步增长，由 stack 计算覆盖）
        sub_pt = P["min_read_pt"]
        for s in subs:
            cur = s.max_pt() or 0
            if cur < sub_pt - 0.05:
                sp = sub_pt
                lines = wrapped_lines(s.text.strip(), sp, inner_w)
                s.set_pt(round(sp, 1))
                acts.append({"rule": "kpi_sub_size", "shape": s.name,
                             "from": cur, "to": round(sp, 1), "lines": lines})
        # 整组高度 = 数字 + 标签 + 子标签 + 项间 0.08
        items = [num, label] + list(subs)
        heights = [t.content_h_in(inner_w) for t in items]
        stack_h = sum(heights) + 0.08 * (len(items) - 1)
        if stack_h + 2 * pad > c.H + 0.02:
            c.move(H=round(stack_h + 2 * pad, 2))
            acts.append({"rule": "kpi_card_fit", "shape": c.name,
                         "to_H": round(stack_h + 2 * pad, 2)})
        # 整组在卡内垂直居中（内容放得下就不增高，保持原卡面节奏）
        y = c.T + (c.H - stack_h) / 2
        for t, h in zip(items, heights):
            t.move(T=round(y, 2), L=c.L + pad, W=inner_w)
            y += h + 0.08
        acts.append({"rule": "kpi_reflow", "card": c.name,
                     "stack_h": round(stack_h, 2), "card_H": round(c.H, 2)})


def rule_label_rows(shs, P, lang, acts, sw, sh_):
    """保守整理行式清单；先唯一分配文字，再只做可证安全的垂直居中。

    旧逻辑用较宽的矩形扩张把形状分配给每一条带。紧邻的条带因此会
    共用文字：上条的长描述可能被下条带再次移动，造成行串位和卡片遮字。
    这里按文字框中心只归属最近的一条带；映射不唯一或已有设计清晰时，
    不猜测重排，让下游逐字/墨迹门禁作最终裁定。
    """
    texts = [s for s in shs if s.is_text and s.text.strip()]
    cards = [s for s in shs if not s.is_text and not s.has_table and not s.has_chart
             and not s.is_pic and s.W > 0.8 and s.H > 0.3]
    bands = sorted([s for s in cards if s.W > sw * 0.45 and s.H < sh_ * 0.2],
                   key=lambda s: s.T)
    if len(bands) < 3:
        return
    # 每个文字框只分配给中心最近且横向包含它的带；不复用任何文字框。
    assigned = {id(b): [] for b in bands}
    ambiguous = []
    for t in texts:
        cy = t.T + t.H / 2
        matches = []
        for b in bands:
            if (t.L >= b.L - 0.05 and t.L + t.W <= b.L + b.W + 0.05
                    and b.T - 0.18 <= cy <= b.T + b.H + 0.18):
                matches.append((abs(cy - (b.T + b.H / 2)), b))
        if not matches:
            continue
        matches.sort(key=lambda x: x[0])
        if len(matches) > 1 and abs(matches[0][0] - matches[1][0]) < 0.04:
            ambiguous.append(t.name)
            continue
        assigned[id(matches[0][1])].append(t)

    parsed = []
    for b in bands:
        g = sorted(assigned[id(b)], key=lambda s: s.L)
        indices = [t for t in g if _is_index(t.text.strip())]
        content = [t for t in g if t not in indices]
        short = [t for t in content if len(t.text.strip()) <= 12]
        long_ = [t for t in content if len(t.text.strip()) > 12]
        if len(indices) > 1 or len(short) != 1 or len(long_) != 1:
            # 有些“行”不是「短标签 + 长描述」；不应用猜测型重排。
            continue
        parsed.append((b, indices[0] if indices else None, short[0], long_[0]))
    if ambiguous or len(parsed) != len(bands):
        acts.append({"rule": "row_layout_kept", "reason": "ambiguous_or_incomplete_text_ownership",
                     "bands": len(bands), "mapped_bands": len(parsed),
                     "ambiguous_text_shapes": sorted(set(ambiguous))})
        return

    row_texts = {id(t) for _b, idx, label, desc in parsed
                 for t in (idx, label, desc) if t is not None}
    # 仅当字号、单行宽度、横向间隔、带内空间和相邻行的可见墨迹间隔
    # 都已足够时，保持作者布局。减少无意义的“精修”移动。
    already_fit = True
    for b, idx, label, desc in parsed:
        row = [t for t in (idx, label, desc) if t is not None]
        if any(t.line_count() != 1 or t.content_h_in() > t.H + 0.02
               or t.L < b.L - 0.05 or t.L + t.W > b.L + b.W + 0.05
               or abs((t.T + t.H / 2) - (b.T + b.H / 2)) > 0.12
               for t in row):
            already_fit = False
            break
        for left, right in zip(sorted(row, key=lambda x: x.L),
                               sorted(row, key=lambda x: x.L)[1:]):
            if left.L + left.W > right.L + 0.04:
                already_fit = False
                break
        if not already_fit:
            break
    # Verify actual line boxes (not the enclosing textbox rectangles) against every
    # neighbouring text line, including title, intro, conclusion and footer text.
    if already_fit:
        all_text = texts
        for a_i, a in enumerate(all_text):
            for b in all_text[a_i + 1:]:
                if id(a) not in row_texts and id(b) not in row_texts:
                    continue
                if min(a.L + a.W, b.L + b.W) - max(a.L, b.L) <= 0.04:
                    continue
                ca, cb = a.T + a.H / 2, b.T + b.H / 2
                ha, hb = a.content_h_in(), b.content_h_in()
                if abs(ca - cb) < (ha + hb) / 2 + 0.04:
                    already_fit = False
                    break
            if not already_fit:
                break
    if already_fit:
        acts.append({"rule": "row_layout_kept", "reason": "existing_rows_fit_and_are_uniquely_mapped",
                     "bands": len(bands), "mapped_bands": len(parsed),
                     "text_shapes": len(row_texts)})
        return

    # A safe horizontal/vertical correction is possible only if each uniquely mapped
    # text line fits its existing band at its current size and does not collide with
    # any other text line after centring. Do not resize/re-space a large group here:
    # it can cross into neighbouring content regions, so leave it for the strict gate.
    can_center = all(t.line_count() == 1 and t.content_h_in() <= b.H - 0.10
                     for b, idx, label, desc in parsed
                     for t in (idx, label, desc) if t is not None)
    if can_center:
        centers = {id(t): b.T + b.H / 2 for b, idx, label, desc in parsed
                   for t in (idx, label, desc) if t is not None}
        for a_i, a in enumerate(texts):
            for b in texts[a_i + 1:]:
                if id(a) not in centers and id(b) not in centers:
                    continue
                if min(a.L + a.W, b.L + b.W) - max(a.L, b.L) <= 0.04:
                    continue
                ca = centers.get(id(a), a.T + a.H / 2)
                cb = centers.get(id(b), b.T + b.H / 2)
                if abs(ca - cb) < (a.content_h_in() + b.content_h_in()) / 2 + 0.04:
                    can_center = False
                    break
            if not can_center:
                break
    if not can_center:
        acts.append({"rule": "row_layout_kept", "reason": "safe_refit_not_proven",
                     "bands": len(bands), "mapped_bands": len(parsed)})
        return

    moved = 0
    for b, idx, label, desc in parsed:
        for t in (idx, label, desc):
            if t is None:
                continue
            target_top = b.T + (b.H - t.H) / 2
            if abs(t.T - target_top) > 0.02:
                t.move(T=round(target_top, 3))
                moved += 1
    acts.append({"rule": "row_band_center_only", "reason": "unique_mapping_and_collision_free",
                 "bands": len(bands), "mapped_bands": len(parsed), "moved_shapes": moved})


def rule_card_grid(shs, P, lang, acts, sw, sh_, n_slides, idx):
    """卡片网格：列主序 + 项数多 + 单行可容 → 改单列（自然阅读序）；
    否则卡高贴合内容、文字垂直居中。"""
    texts = [s for s in shs if s.is_text and s.text.strip()]
    cards = [s for s in shs if not s.is_text and not s.has_table and not s.has_chart
             and not s.is_pic and s.W > 0.8 and s.H > 0.3]
    if len(cards) < 4:
        return
    rows = _group_rows(cards)
    cols = _group_cols(cards)
    if len(rows) < 2 or len(cols) < 2:
        return
    grid = sorted(cards, key=lambda s: (s.L, s.T))     # 列主序枚举
    parsed = []
    for c in grid:
        g = sorted(_inside(c, texts), key=lambda s: s.L)
        if len(g) < 2:
            continue
        idxr = [t for t in g if _is_index(t.text.strip())]
        title = [t for t in g if not _is_index(t.text.strip())]
        if idxr and title:
            parsed.append((c, idxr[0], title[0]))
    if len(parsed) < 4:
        return
    # 序号是否列主序（左列自上而下 1,3,5… 右列 2,4,6…）
    seq = [int(t.text.strip()) for _c, t, _ti in parsed]
    col_major = seq == sorted(seq)
    content_w = sw - 2 * min(c.L for c, _i, _t in parsed)
    min_pt = min((t.max_pt() or 0) for _c, _i, t in parsed)
    # 可用高度：网格下方若有引用框等宽带盒，以其顶为界；否则以页脚区为界
    grid_bottom = max(c.T + c.H for c, _i, _t in parsed)
    below = [s for s in shs if not s.is_text and s.W > sw * 0.45
             and s.T >= grid_bottom - 0.15]
    boundary = min([s.T for s in below], default=sh_ - 0.5)
    t0 = min(c.T for c, _i, _t in parsed)
    available = boundary - 0.22 - t0
    use_pt = max(min_pt, P["grid_list_min_pt"])
    row_h = use_pt * 1.22 / 72.0 + 2 * P["grid_row_pad_in"]
    gap = P["grid_row_gap_in"]
    needed = len(parsed) * row_h + (len(parsed) - 1) * gap
    if (col_major and len(parsed) >= P["grid_list_min_items"]
            and all(t.one_line_fits(use_pt, content_w - 1.2) for _c, _i, t in parsed)
            and needed <= available):
        # ---- 改单列：行序按原编号，行底板复用原卡片形状
        L0 = min(c.L for c, _i, _t in parsed)
        W0 = max(c.L + c.W for c, _i, _t in parsed) - L0
        for n, (c, idxr, title) in enumerate(parsed):
            T = t0 + n * (row_h + gap)
            c.move(L=L0, T=round(T, 2), W=W0, H=round(row_h, 2))
            idxr.move(L=L0 + P["card_pad_in"], T=round(T + (row_h - 0.22) / 2, 2), W=0.55, H=0.22)
            title.move(L=L0 + P["card_pad_in"] + 0.62,
                       T=round(T + (row_h - (use_pt * 1.22 / 72.0)) / 2, 2),
                       W=W0 - P["card_pad_in"] * 2 - 0.62, H=round(use_pt * 1.22 / 72.0, 2))
            if (title.max_pt() or 0) < use_pt and title.one_line_fits(use_pt, W0 - 1.2):
                title.set_pt(use_pt)
            acts.append({"rule": "grid_to_single_column", "row": n + 1,
                         "index": idxr.text.strip(), "card": c.name})
        acts.append({"rule": "grid_reading_order", "from": "column_major", "to": "row_major"})
        return
    # ---- 否则：卡高贴合内容整叠 + 文字整组垂直居中（不逐块独立居中，避免互相重叠）
    for c, idxr, title in parsed:
        inner = sorted(_inside(c, texts), key=lambda s: s.T)
        if not inner:
            continue
        stack_top = min(t.T for t in inner)
        stack_bot = max(t.T + t.H for t in inner)
        stack_h = stack_bot - stack_top
        need = stack_h + 2 * P["card_pad_in"]
        if need < c.H - 0.02 and need > 0.3:
            c.move(H=round(need, 2))
            acts.append({"rule": "grid_card_fit", "shape": c.name,
                         "from_H": round(c.H, 2), "to_H": round(need, 2)})
        shift = (c.T + (c.H - stack_h) / 2) - stack_top
        if abs(shift) > 0.03:
            for t in inner:
                t.move(T=round(t.T + shift, 2))
            acts.append({"rule": "grid_stack_center", "shape": c.name,
                         "shift": round(shift, 2)})


def rule_steps_row(shs, P, lang, acts, sw, sh_):
    """步骤行：无强调元素的线性序号中出现孤立异色 → 归一群内主色。"""
    texts = [s for s in shs if s.is_text and s.text.strip()]
    cards = [s for s in shs if not s.is_text and not s.has_table and not s.has_chart
             and not s.is_pic and s.W > 0.8 and s.H > 0.6]
    rows = [r for r in _group_rows(cards) if len(r) >= 3]
    if not rows:
        return
    from collections import Counter
    for row in rows:
        num_shapes, colors = [], Counter()
        for c in sorted(row, key=lambda s: s.L):
            g = _inside(c, texts)
            for t in g:
                if _is_index(t.text.strip()):
                    num_shapes.append(t)
                    col = next((r[4] for r in t.runs if r[4]), None)
                    if col:
                        colors[col.upper()] += 1
        if len(num_shapes) < 3 or len(colors) < 2:
            continue
        dom, dom_n = colors.most_common(1)[0]
        if dom_n < len(num_shapes) - 1:
            continue                    # 没有绝对主色（多色各占其一）→ 保留
        odd = [t for t in num_shapes
               if next((r[4] for r in t.runs if r[4]), "").upper() != dom]
        if len(odd) == len(num_shapes) - dom_n and 0 < len(odd) <= 2:
            for t in odd:
                for pi, ri, _txt, _pt, _c, _b in t.runs:
                    t.shape.text_frame.paragraphs[pi].runs[ri].font.color.rgb = \
                        __import__("pptx.dml.color", fromlist=["RGBColor"]).RGBColor.from_string(dom)
                acts.append({"rule": "step_number_color", "shape": t.name,
                             "text": t.text.strip(), "to": dom})


def rule_two_panel(shs, P, lang, acts, sw, sh_):
    """双面板：列表首行对齐、间距取兄弟列表的既有主间距（不擅自压缩）。
    仅当短列表 ≤2 项且量差 ≥3 倍时，才把短列表在内容区内居中。"""
    texts = [s for s in shs if s.is_text and s.text.strip()]
    cards = [s for s in shs if not s.is_text and not s.has_table and not s.has_chart
             and not s.is_pic and s.W > sw * 0.2 and s.H > sh_ * 0.25]
    if len(cards) < 2:
        return
    big = sorted(cards, key=lambda s: -(s.W * s.H))[:2]
    a, b = sorted(big, key=lambda s: s.L)
    if abs(a.T - b.T) > 0.25 or a.L + a.W > b.L + 0.15:
        return

    def bullets(panel):
        g = sorted(_inside(panel, texts), key=lambda s: s.T)
        head = [t for t in g if (t.max_pt() or 0) >= 15]
        items = [t for t in g if t not in head and len(t.text.strip()) > 4]
        return head, items

    ha, ia = bullets(a)
    hb, ib = bullets(b)
    if len(ia) < 2 or len(ib) < 2:
        return
    # 项目符号圆点（小非文本形状）必须随文字一起移动：按原始相对位置配对
    def dots_for(panel, items):
        out = {}
        small = [s for s in shs if not s.is_text and not s.has_table and not s.has_chart
                 and not s.is_pic and s.W < 0.25 and s.H < 0.25
                 and s.L >= panel.L - 0.1 and s.L <= panel.L + panel.W
                 and s.T >= panel.T - 0.1 and s.T <= panel.T + panel.H + 0.1]
        for t in items:
            cand = [d for d in small if d.L < t.L + 0.05 and t.L - d.L < 0.6
                    and abs(d.T - t.T) < 0.25]
            if cand:
                out[id(t)] = min(cand, key=lambda d: abs(d.T - t.T))
        return out

    dots_a, dots_b = dots_for(a, ia), dots_for(b, ib)

    def move_item(t, new_T, dotmap):
        delta = new_T - t.T
        t.move(T=round(new_T, 2))
        d = dotmap.get(id(t))
        if d is not None:
            d.move(T=round(d.T + delta, 2))

    # 首行对齐；间距取较长列表的既有中位间距（保持原节奏，不压缩）
    top = min(min(t.T for t in ia), min(t.T for t in ib))
    long_items = ia if len(ia) >= len(ib) else ib
    gaps = [long_items[i + 1].T - long_items[i].T for i in range(len(long_items) - 1)]
    gap = sorted(gaps)[len(gaps) // 2] if gaps else P["bullet_gap_in"]
    gap = min(max(gap, 0.18), 0.7)
    for items, dotmap in ((ia, dots_a), (ib, dots_b)):
        for n, t in enumerate(items):
            move_item(t, top + n * gap, dotmap)
    acts.append({"rule": "panel_bullet_align", "panels": [a.name, b.name],
                 "top": round(top, 2), "gap": round(gap, 2),
                 "dots_moved": len(dots_a) + len(dots_b)})
    # 极端不均衡（短列表 ≤2 项且量差 ≥3 倍）才居中短列表
    short, long_ = (ia, ib) if len(ia) < len(ib) else (ib, ia)
    dot_short = dots_a if short is ia else dots_b
    if len(short) <= 2 and len(long_) >= 3 * len(short):
        lo = min(t.T for t in long_)
        hi = max(t.T + t.H for t in long_)
        blk_h = len(short) * gap - (gap - max(t.H for t in short))
        start = lo + (hi - lo - blk_h) / 2
        for n, t in enumerate(short):
            move_item(t, start + n * gap, dot_short)
        acts.append({"rule": "panel_short_list_centered", "panel":
                     (a.name if short is ia else b.name), "start": round(start, 2)})


def rule_universal(shs, P, lang, acts, sw, sh_, ptype="generic"):
    """通用：盒内**唯一**单行文字垂直居中（不缩卡高，只挪文字）。
    只针对"一个标签一个盒"的结构；多元素面板/网格交由专用规则，避免误伤。"""
    if ptype in ("cover", "closing"):
        return
    texts = [s for s in shs if s.is_text and s.text.strip()]
    cards = [s for s in shs if not s.is_text and not s.has_table and not s.has_chart
             and not s.is_pic and s.W > 0.8 and s.H > 0.35]
    for c in cards:
        inner = _inside(c, texts)
        if len(inner) != 1:
            continue
        t = inner[0]
        if t.line_count() != 1:
            continue
        th = t.content_h_in(c.W)
        if abs((t.T - c.T) - (c.H - th) / 2) > 0.08:
            t.move(T=round(c.T + (c.H - th) / 2, 2))
            acts.append({"rule": "vcenter", "shape": t.name, "card": c.name})


# --------------------------------------------------------------------------- 可读性字号下限

#: 引用 type_scale 单一事实来源（10–30pt 分级体系）
READABILITY_ROLE_PT = ROLE_RANGE_PT

#: 页面四周安全边距（英寸，按 13.33in 宽标定）：框不得长到纸外。
FLOOR_EDGE_MARGIN_IN = 0.25
#: 相邻块之间至少保留的垂直净空。
FLOOR_MIN_GAP_IN = 0.06


def _floor_is_page_number(sh, sw, sh_) -> bool:
    import re as _re
    t = sh.text.strip()
    return bool(_re.fullmatch(r"\d{1,2}", t)) and sh.T > sh_ * 0.92 and sh.L > sw * 0.82


#: 与 scripts/layout_gate.py:_SOURCE_LINE_RE 同口径（必须保持一致，见下）。
_SOURCE_LINE_RE = re.compile(
    r"^(?:数据来源|资料来源|图源|图片来源|来源|Source)\s*[:：]?", re.IGNORECASE)


def _floor_is_source(sh) -> bool:
    """来源标注行判定：与 scripts/layout_gate.py:_is_source 同口径。

    旧实现用 `"来源" in t` 裸子串判断，章节标题（如「17. 来源」）也会被
    当成 16–18pt 的来源标注抬字下限，把真正的标题/正文段落误压小——
    门禁那边已经按**行首前缀 + 短行/早冒号**修过这个误报，这里补齐
    同一套判定，否则两边角色判定对不上，字号钳的还是错的区间。
    """
    t = sh.text.strip()
    if not t:
        return False
    if _SOURCE_LINE_RE.match(t):
        _colon = min((t.find(c) for c in ":：" if c in t), default=-1)
        if len(t) <= 60 or 0 <= _colon <= 14:
            return True
    if ("Yiten Huang" in t) or ("YitenHuang" in t):
        return True
    return any(k in t for k in ("Syntun", "统计局", "国务院：", "Reuters",
                                "财政部", "新华社"))


def _floor_brand_band(sh, shs, sw, sh_) -> bool:
    """页底品牌带判定：与 scripts/layout_gate.py:_bottom_brand_band 同口径。

    位置规则只认 top ≥ 页高 91%（7.5in 稿的 6.825）的形状；真实稿里品牌
    条常挂在 6.6–6.85（job-293501ae3698 页 1「宏观观察 · 短视频配套文稿」
    top=6.688）。被当正文处理的后果：抬字地板把它推到 body 目标字号
    （11.25→18pt），字形底压进页脚净空，门槛 HG06/HG07 两头拦截。

    判为 footer 须同时成立（口径细节见门槛侧 _bottom_brand_band 的
    docstring，两侧必须同步修改）：
    带内（top ≥ sh_−0.85，7.5in 稿即 6.65）且低于既有 91% 线；全页最底
    部文字；≤60 字；字号 ≤14pt；与上半页（top<sh_/2）另一形状有 ≥2 字
    token 的品牌呼应。
    """
    t = sh.text.strip()
    if not t or len(t) > 60:
        return False
    pt = sh.max_pt() or 0.0
    if not (0.0 < pt <= 14.0):
        return False
    if not (sh_ - 0.85 <= sh.T < sh_ * 0.91):
        return False
    # 「全页最底部」只看真正的正文行：其下若还有页码/来源/页脚区短文本
    # （页脚装帧），品牌带仍成立——与门槛侧 _bottom_brand_band 同口径。
    lowers = [x.T for x in shs
              if x.is_text and x.text.strip() and x.T > sh.T + 0.02
              and not (_floor_is_page_number(x, sw, sh_)
                       or _floor_is_source(x)
                       or (x.T >= sh_ * 0.91 and len(x.text.strip()) <= 60))]
    if lowers:
        return False
    tokens = re.findall(r"[0-9A-Za-z\u4e00-\u9fff]{2,}", t)
    if not tokens:
        return False
    for other in shs:
        if other.idx == sh.idx or not other.is_text or other.T >= sh_ * 0.5:
            continue
        if any(tok in other.text for tok in tokens):
            return True
    return False


def infer_text_roles(shs, sw, sh_) -> dict:
    """推断文本角色，与 scripts/layout_gate.py:infer_roles 同口径。

    门禁按角色区间检查**每一个 run** 的字号，所以抬字必须按同一套角色判定，
    否则抬了也对不上。几何阈值按页宽/页高比例化，不写死 13.33in 的绝对值。
    """
    import re as _re
    from .type_scale import is_chart_legend_label, is_decor, is_decor_unit_suffix
    roles = {}
    legend_boxes = [{"left": s.L, "top": s.T, "width": s.W, "height": s.H,
                     "text": s.text, "solid": bool(s.fill_hex) and not s.is_text,
                     "decor": is_decor(s.max_pt() or 0.0, s.text)}
                    for s in shs]
    legend_pos = {s.idx: k for k, s in enumerate(shs)}
    top_zone = sh_ * 0.307          # 13.33x7.5 稿的 2.3in
    bottom_zone = sh_ * 0.56        # 同稿的 4.2in
    for s in shs:
        if not s.is_text or not s.text.strip():
            continue
        t = s.text.strip()
        pt = s.max_pt() or 0.0
        if s.has_table:
            roles[s.idx] = "table"
        elif s.has_chart:
            roles[s.idx] = "chart_aux"
        elif is_decor(pt, t):
            roles[s.idx] = "decor"
        # Keep in sync with scripts/layout_gate.py:infer_roles — a short
        # unit/suffix word beside a large decorative stat number is part
        # of that decorative display, not body text (see is_decor_unit_
        # suffix's own docstring).
        elif is_decor_unit_suffix(legend_pos[s.idx], legend_boxes):
            roles[s.idx] = "decor"
        elif t in ("“", "”", "「", "」"):
            roles[s.idx] = "quote"
        elif _floor_is_page_number(s, sw, sh_):
            roles[s.idx] = "footer"
        elif _floor_is_source(s):
            roles[s.idx] = "source"
        elif _floor_brand_band(s, shs, sw, sh_):
            roles[s.idx] = "footer"
        elif s.T >= sh_ * 0.91 and len(t) <= 60:
            roles[s.idx] = "footer"
        # Keep in sync with scripts/layout_gate.py:infer_roles (see this
        # function's docstring) — Roman numeral badges (Ⅰ Ⅱ Ⅲ ...) are the
        # same kind of standalone ordinal label as "1"/"A"; U+2160-217B is
        # the whole Unicode Roman Numerals block, upper- and lower-case.
        elif _re.fullmatch(r"\d{1,2}|[A-Za-z]|[Ⅰ-ⅻ]", t):
            roles[s.idx] = "ordinal"
        elif _re.search(r"\d", t) and _re.search(r"%|亿|万", t) and len(t) <= 12:
            roles[s.idx] = "key_number"
        elif is_chart_legend_label(legend_pos[s.idx], legend_boxes):
            roles[s.idx] = "chart_aux"
        else:
            roles[s.idx] = "body"
    by_idx = {s.idx: s for s in shs}
    rest = [i for i, r in roles.items() if r == "body"]
    if rest:
        # 标题：上半页（top < 0.5 页高）字号最大者（回退最顶）。封面/分章页
        # 常见"大标题居中偏下 + 顶部小眉题"版式：真正的大标题常被挤到严格
        # 顶区（top<2.3）之外，若只在顶区内比字号，眉题反倒会被误判成
        # 标题、大标题被压成正文。放宽到上半页更稳：标题角色区间（36–40）
        # 与正文区间（24–28）本就不相交，字号钳过之后标题仍必然比正文大，
        # 不会出现"钳完之后反而判错"的边界抖动（之前按倍数阈值判定时，
        # 字号被钳到区间边界后差距可能正好跌破阈值，门禁复判就会翻盘）。
        # 下半页（conclusion 区）与数字强调（key_number）已在上面单独分流，
        # 不会被这里捞进候选。
        upper = [i for i in rest if by_idx[i].T < sh_ * 0.5]
        candidates = upper if upper else rest
        # 一个真正的 PPTX Title 占位符常常不显式设置 font.size（继承版式/
        # 母版的标题样式），max_pt() 在这里返回 None → 按 0.0 参与比较，
        # 必然输给任何一个显式设了字号的正文框——于是正文被误判成标题、
        # 真标题被挤成眉题/label（2026-10-31 job-73ba0610640c 第 9/13 页：
        # "Content Placeholder 2" 24pt 显式字号压过标题占位符的继承字号，
        # 正文被钳到标题区间 24–30pt，溢出整块内容框）。占位符类型本身就是
        # 比字号推断更直接的证据，候选里只要有一个就优先选它。
        title_placeholders = [i for i in candidates if by_idx[i].is_title_placeholder]
        if title_placeholders:
            top_i = max(title_placeholders,
                        key=lambda i: ((by_idx[i].max_pt() or 0.0), -by_idx[i].T))
        else:
            top_i = (max(upper, key=lambda i: ((by_idx[i].max_pt() or 0.0), -by_idx[i].T))
                     if upper else min(rest, key=lambda i: by_idx[i].T))
        roles[top_i] = "title"
        # 眉题/类别标签：位于主标题上方（top < by_idx[top_i].T）的短标签
        for i in rest:
            if i != top_i and by_idx[i].T < by_idx[top_i].T and len(by_idx[i].text.strip()) <= 20:
                roles[i] = "label"
        # 导语：严格位于标题之下（top 更大）且仍在顶区的最顶页级文本（不在卡内，横幅/句子，宽度不低于 2.5in）。
        cards_list = [c for c in shs if not c.is_text and c.W > 0.8 and c.H > 0.35]
        below = [i for i in rest if i != top_i and roles[i] == "body"
                 and by_idx[i].T < top_zone and by_idx[i].T > by_idx[top_i].T
                 and by_idx[i].W >= 2.5
                 and not any((c.L - 0.05 <= by_idx[i].L and by_idx[i].L + by_idx[i].W <= c.L + c.W + 0.12
                              and c.T - 0.05 <= by_idx[i].T and by_idx[i].T + by_idx[i].H <= c.T + c.H + 0.12)
                             for c in cards_list)]
        if below:
            roles[min(below, key=lambda i: by_idx[i].T)] = "lead"
        bottom = [i for i in rest if roles[i] == "body"
                  and by_idx[i].T > bottom_zone and len(by_idx[i].text.strip()) >= 15]
        if bottom:
            roles[max(bottom, key=lambda i: (by_idx[i].T, len(by_idx[i].text.strip())))] = "conclusion"
    return roles


def _headroom_below(target, shs, sh_) -> float:
    """target 向下可以长多少英寸而不压到下方任何块，也不越出页面安全边距。"""
    limit = sh_ - FLOOR_EDGE_MARGIN_IN
    bottom = target.T + target.H
    for o in shs:
        if o.idx == target.idx or o.W <= 0 or o.H <= 0:
            continue
        if o.L >= target.L + target.W or o.L + o.W <= target.L:
            continue                      # 水平不重叠，互不相干
        if o.T + o.H <= bottom + 1e-6:
            continue                      # 在上方或同高
        limit = min(limit, o.T - FLOOR_MIN_GAP_IN)
    return max(0.0, limit - bottom)


def _column_peers(target, shs):
    """与 target 水平重叠、且在其**下方**的块，按从上到下排序（级联推移对象）。

    "下方"按顶边比较，而不是"顶边在目标底边之下"：后者会漏掉原本就与目标
    纵向交叠的块，于是目标一长高就直接压上去（GLYPH_COLLISION）。
    但原稿里"文字叠在卡片上"是正常设计，这类高度重合的配对不是上下关系，
    推开它们等于拆掉版面——按原始重叠比例把它们排除在级联之外。
    """
    out = []
    for o in shs:
        if o.idx == target.idx or o.W <= 0 or o.H <= 0:
            continue
        if o.L >= target.L + target.W or o.L + o.W <= target.L:
            continue
        if o.T <= target.T + 1e-6:
            continue                      # 同高或更靠上：不是下方块
        overlap = min(o.T + o.H, target.T + target.H) - max(o.T, target.T)
        if overlap > 0.5 * min(o.H, target.H):
            continue                      # 本就高度重合：同一组件，不拆
        out.append(o)
    return sorted(out, key=lambda o: o.T)


def _plan_cascade(target, grow, shs, sh_):
    """把 target 下边界下移 grow 英寸，返回需要连带下移的 {idx: 位移}。

    放不下就返回 None——本模块的纪律是宁可不动，也不压字、不越出纸面。
    """
    moves = {}
    frontier = [(target, grow)]
    guard = 0
    while frontier:
        guard += 1
        if guard > 200:
            return None
        cur, delta = frontier.pop()
        if delta <= 1e-6:
            continue
        cur_bottom = cur.T + cur.H + moves.get(cur.idx, 0.0) + (delta if cur.idx == target.idx else 0.0)
        for o in _column_peers(cur, shs):
            o_top = o.T + moves.get(o.idx, 0.0)
            need = cur_bottom + FLOOR_MIN_GAP_IN - o_top
            if need <= 1e-6:
                continue
            moves[o.idx] = moves.get(o.idx, 0.0) + need
            if o.T + moves[o.idx] + o.H > sh_ - FLOOR_EDGE_MARGIN_IN + 1e-6:
                return None                      # 推到纸外：整笔放弃
            frontier.append((o, need))
    return moves


def _set_run_sizes(s, lo, hi):
    """把形状内每个 run 的字号钳进 [lo, hi]，并同步缓存的 runs 台账。"""
    from pptx.util import Pt
    for pi, ri, _t, pt, _c, _b in s.runs:
        if pt is None:
            continue
        s.shape.text_frame.paragraphs[pi].runs[ri].font.size = Pt(round(min(max(pt, lo), hi), 1))
    s.runs = [(pi, ri, t, (round(min(max(pt, lo), hi), 1) if pt else pt), c, b)
              for pi, ri, t, pt, c, b in s.runs]


def _restore_run_sizes(s, original):
    """抬字放不下时回退到原字号——宁可不动，也不留一个塞不下的版面。"""
    from pptx.util import Pt
    for pi, ri, _t, pt, _c, _b in original:
        if pt is None:
            continue
        s.shape.text_frame.paragraphs[pi].runs[ri].font.size = Pt(pt)
    s.runs = list(original)


def fit_page_type_scale(shs, sw, sh_, acts=None):
    """把每个 run 的字号按 10–30pt 分级体系适配到当前页，并让几何吸收随之增加的占位。

    1. infer_text_roles() 获得本页每个文本框角色；
    2. 为本页选取档位系数 k ∈ [0, 1]，目标字号 = lo + k * (hi - lo)，取 0.5pt 整数倍；
    3. 从 k=1 向下二分，寻找满足 (a) 内容高度 (b) 框宽 (c) 卡片内边距 (d) 画布边距 的最大 k；
    4. 若 k=0 仍放不下，按顺序尝试：不折行改折行、卡片内等距重排、同列卡片级联下移；
    5. 仍放不下：保留原字号（原字号 < 10 抬到 10），登记 type_scale_no_fit；
    6. 校验 HIERARCHY 与 TITLE_BODY_MIN_RATIO，不满足时只允许上调高层级角色，仍不满足登记 type_scale_hierarchy_violation。
    """
    if acts is None:
        acts = []

    import statistics
    from pptx.util import Pt
    from .type_scale import (
        HIERARCHY,
        ROLE_RANGE_PT,
        TITLE_BODY_MIN_RATIO,
        TYPE_MAX_PT,
        TYPE_MIN_PT,
        check_page_hierarchy,
        is_decor,
    )
    from .visual import NO_WRAP_BOX_W_IN, text_frame_no_wrap

    roles = infer_text_roles(shs, sw, sh_)
    texts = [s for s in shs if s.is_text and s.text.strip() and not s.has_table and not s.has_chart]
    raw_cards = [s for s in shs if not s.is_text and not s.has_table and not s.has_chart
                 and not s.is_pic and s.W > 0.8 and s.H > 0.35
                 and not (s.W >= sw * 0.90 and s.H >= sh_ * 0.90)]
    # 去重重叠的卡片图层（如底色+阴影卡片）
    cards = []
    for c in raw_cards:
        if not any(abs(c.L - uc.L) < 0.05 and abs(c.T - uc.T) < 0.05
                   and abs(c.W - uc.W) < 0.05 and abs(c.H - uc.H) < 0.05 for uc in cards):
            cards.append(c)

    by_idx = {s.idx: s for s in shs}

    if not texts:
        return

    # 1. 识别 decor
    decor_shapes = set()
    for s in texts:
        pt = s.max_pt()
        if is_decor(pt, s.text):
            decor_shapes.add(s.idx)
            roles[s.idx] = "decor"
            acts.append({"rule": "decor_detected", "shape": s.name, "pt": pt})

    def _find_card(s):
        matching = []
        for c in cards:
            if (c.L - 0.05 <= s.L and s.L + s.W <= c.L + c.W + 0.12
                    and c.T - 0.05 <= s.T and s.T + s.H <= c.T + c.H + 0.12):
                matching.append(c)
        return min(matching, key=lambda c: c.W * c.H) if matching else None

    # 画布安全边距（左右 0.40in、上 0.30in、下 0.15in）
    CANVAS_L = 0.40
    CANVAS_R = sw - 0.40
    CANVAS_T = 0.30
    CANVAS_B = sh_ - 0.15

    def _calc_content_h(s, pt):
        is_nw = text_frame_no_wrap(s.shape) and not getattr(s, "_wrap_square", False)
        w = NO_WRAP_BOX_W_IN if is_nw else s.W
        paras = [p for p in s.shape.text_frame.paragraphs if p.text.strip()]
        if not paras:
            return s.H
        if len(paras) == 1 and not is_nw:
            t = paras[0].text.strip()
            lines = wrapped_lines(t, pt, max(w - 0.06, 0.2))
            if lines == 1:
                if roles.get(s.idx) == "key_number" or pt >= 24.0:
                    return max(round(pt * 1.22 / 72.0 + 0.04, 3), s.H)
                return max(round(pt * 1.05 / 72.0, 3), s.H)
        elif len(paras) == 1 and is_nw:
            if roles.get(s.idx) == "key_number" or pt >= 24.0:
                return max(round(pt * 1.22 / 72.0 + 0.04, 3), s.H)
            return max(round(pt * 1.05 / 72.0, 3), s.H)

        total_lines = 0
        max_ls = 1.22
        for p in s.shape.text_frame.paragraphs:
            t = "".join(r.text for r in p.runs)
            if not t:
                total_lines += 1
            else:
                total_lines += wrapped_lines(t, pt, max(w - 0.06, 0.2))
            ls = getattr(p, "line_spacing", None)
            if ls is not None:
                try:
                    if hasattr(ls, "pt"):
                        max_ls = max(max_ls, float(ls.pt) / max(pt, 1.0))
                    elif float(ls) > 0.5:
                        max_ls = max(max_ls, float(ls))
                except Exception:
                    pass
        line_factor = max(max_ls, 1.30)
        para_gap = (len(paras) - 1) * 0.04
        return max(total_lines, 1) * pt * line_factor / 72.0 + max(s.text_inset_in(), 0.06) + para_gap + 0.12

    def _calc_needed_w(s, pt):
        return s.needed_w_in(pt)

    def _card_fits(c, t_pts):
        c_texts = sorted([t for t in texts if _find_card(t) == c and t.idx not in decor_shapes], key=lambda t: t.T)
        if not c_texts:
            return True
        pad = 0.08
        avail_h = c.H - 2 * pad
        cols = []
        for t in c_texts:
            matched = None
            for col in cols:
                if any(min(t.L + t.W, o.L + o.W) - max(t.L, o.L) > 0.10 for o in col):
                    matched = col
                    break
            if matched is not None:
                matched.append(t)
            else:
                cols.append([t])
        for col in cols:
            tot_h = sum(_calc_content_h(t, t_pts.get(roles.get(t.idx, "body"), 13.0)) for t in col)
            tot_h += (len(col) - 1) * FLOOR_MIN_GAP_IN
            if tot_h > avail_h + 0.05:
                return False
        return True

    def _check_fits(target_pts):
        for c in cards:
            if not _card_fits(c, target_pts):
                return False, ("card_overflow", c, 0.0)

        for s in texts:
            if s.idx in decor_shapes:
                continue
            r = roles.get(s.idx, "body")
            pt = target_pts.get(r, 13.0)
            c = _find_card(s)
            pad = max(0.10, 0.35 * (pt / 72.0))
            is_nw = s.is_no_wrap and not getattr(s, "_wrap_square", False)

            need_h = _calc_content_h(s, pt)
            need_w = _calc_needed_w(s, pt) if is_nw else s.W

            if not c:
                # 页面级块：高度与下方最近块比较
                avail_b = CANVAS_B
                for o in texts:
                    if o.idx == s.idx or _find_card(o) is not None:
                        continue
                    if roles.get(s.idx) == "label" and roles.get(o.idx) == "title":
                        continue
                    o_nw = o.is_no_wrap and not getattr(o, "_wrap_square", False)
                    o_w = _calc_needed_w(o, target_pts.get(roles.get(o.idx, "body"), 13.0)) if o_nw else o.W
                    if min(s.L + need_w, o.L + o_w) > max(s.L, o.L) + 0.02:
                        if o.T >= s.T + 0.02:
                            avail_b = min(avail_b, o.T - FLOOR_MIN_GAP_IN)
                avail_h = avail_b - s.T
                if need_h > avail_h:
                    return False, ("height_overflow", s, need_h - avail_h)

            # b. 不折行单行宽度 <= 可用宽度
            if is_nw:
                avail_r = (c.L + c.W - pad) if c else CANVAS_R
                for o in texts:
                    if o.idx == s.idx:
                        continue
                    if c and _find_card(o) != c:
                        continue
                    if min(s.T + need_h, o.T + o.H) > max(s.T, o.T) + 0.02:
                        if o.L >= s.L + 0.02:
                            avail_r = min(avail_r, o.L - FLOOR_MIN_GAP_IN)
                avail_w = avail_r - s.L
                if need_w > avail_w + 0.05:
                    return False, ("width_overflow", s, need_w - avail_w)

            # c. 卡片内边距（文字不溢出卡片右/下边界）
            if c:
                if (s.L + need_w > c.L + c.W - pad + 0.08
                        or s.T + need_h > c.T + c.H - pad + 0.08):
                    return False, ("card_padding", s, 0.0)

            # d. 画布安全边距（文字不溢出画布右/下安全边距）
            if (s.L + need_w > CANVAS_R + 0.08
                    or s.T + need_h > CANVAS_B + 0.08):
                return False, ("canvas_margin", s, 0.0)

        return True, None

    def _get_target_pts(k_val):
        t_pts = {}
        for r_name, (lo, hi) in ROLE_RANGE_PT.items():
            t_pts[r_name] = round((lo + k_val * (hi - lo)) * 2.0) / 2.0
        return t_pts

    best_k = None
    for k_test in [1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1, 0.0]:
        t_pts = _get_target_pts(k_test)
        ok, _ = _check_fits(t_pts)
        if ok:
            best_k = k_test
            break

    if best_k is None:
        # Step 4: 几何微调
        # 4.1 不折行框改折行：只对长正文段落（>=12字），绝不把短标签/面包屑折成断字
        for s in texts:
            if s.idx in decor_shapes or not s.is_no_wrap or len(s.text.strip()) < 12:
                continue
            c = _find_card(s)
            avail_r = (c.L + c.W - 0.10) if c else CANVAS_R
            for o in texts:
                if o.idx != s.idx and min(s.T + s.H, o.T + o.H) > max(s.T, o.T) + 0.02:
                    if o.L >= s.L + 0.02:
                        avail_r = min(avail_r, o.L - FLOOR_MIN_GAP_IN)
            avail_w = avail_r - s.L
            r = roles.get(s.idx, "body")
            min_pt = ROLE_RANGE_PT.get(r, (13.0, 18.0))[0]
            if _calc_needed_w(s, min_pt) > avail_w + 0.05:
                try:
                    bp = s.shape.text_frame._txBody.find(
                        "{http://schemas.openxmlformats.org/drawingml/2006/main}bodyPr")
                    if bp is not None:
                        bp.set("wrap", "square")
                    s.shape.text_frame.word_wrap = True
                    s._wrap_square = True
                    new_w = max(0.5, avail_w)
                    s.move(W=round(new_w, 3))
                    acts.append({"rule": "wrap_square_applied", "shape": s.name})
                except Exception:
                    pass

        # 4.2 卡片内部等距重排（仅限纯文字卡片，不破坏色块/图表对其伙伴文字的相对定位）
        for c in cards:
            has_diagram_shapes = any(not o.is_text and not o.has_table and not o.has_chart
                                     and not o.is_pic and _find_card(o) == c and o.idx != c.idx
                                     for o in shs)
            if has_diagram_shapes:
                continue
            c_texts = sorted([t for t in texts if _find_card(t) == c and t.idx not in decor_shapes], key=lambda t: t.T)
            if len(c_texts) >= 2:
                pad = 0.10
                avail_h = c.H - 2 * pad
                tot_h = sum(_calc_content_h(t, ROLE_RANGE_PT.get(roles.get(t.idx, "body"), (13.0, 18.0))[0]) for t in c_texts)
                if tot_h < avail_h:
                    gap = (avail_h - tot_h) / (len(c_texts) + 1)
                    cur_y = c.T + pad + gap
                    for t in c_texts:
                        th = _calc_content_h(t, ROLE_RANGE_PT.get(roles.get(t.idx, "body"), (13.0, 18.0))[0])
                        t.move(T=round(cur_y, 3), H=round(th, 3))
                        cur_y += th + gap
                    acts.append({"rule": "card_equidistant_spacing", "card": c.name})

        # 4.3 卡片内多列水平避让（当同行左侧文字受同行右侧文字挤压时，适度右移右侧列）
        for c in cards:
            c_texts = [t for t in texts if _find_card(t) == c and t.idx not in decor_shapes]
            if len(c_texts) < 2:
                continue
            for left_item in c_texts:
                req_w = _calc_needed_w(left_item, ROLE_RANGE_PT.get(roles.get(left_item.idx, "body"), (13.0, 18.0))[0])
                right_peers = [o for o in c_texts if o.idx != left_item.idx
                               and o.L >= left_item.L + 0.5
                               and min(left_item.T + left_item.H, o.T + o.H) > max(left_item.T, o.T) + 0.02]
                if not right_peers:
                    continue
                min_peer_l = min(o.L for o in right_peers)
                shift = left_item.L + req_w + FLOOR_MIN_GAP_IN - min_peer_l + 0.08
                if 0.02 < shift < 1.0:
                    target_peers = [o for o in c_texts if any(abs(o.L - rp.L) < 0.20 for rp in right_peers)]
                    card_max_r = c.L + c.W - 0.15
                    if all(o.L + shift + o.W <= card_max_r for o in target_peers):
                        for o in target_peers:
                            o.move(L=round(o.L + shift, 3))
                        acts.append({"rule": "card_column_horizontal_clearance", "card": c.name, "shift_in": round(shift, 3)})

        # 重新测试 k
        for k_test in [1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1, 0.0]:
            t_pts = _get_target_pts(k_test)
            ok, _ = _check_fits(t_pts)
            if ok:
                best_k = k_test
                break

    if best_k is not None:
        target_pts = _get_target_pts(best_k)
    else:
        # Step 5: 仍放不下，保留原字号（原字号 < 10 抬到 10），登记 type_scale_no_fit
        target_pts = _get_target_pts(0.0)
        for s in texts:
            if s.idx in decor_shapes:
                continue
            orig = s.max_pt() or 12.0
            kept = orig if orig >= 10.0 else 10.0
            r = roles.get(s.idx, "body")
            needed_pt = target_pts.get(r, 13.0)
            acts.append({
                "rule": "type_scale_no_fit",
                "shape": s.name,
                "role": r,
                "needed_pt": needed_pt,
                "kept_pt": kept,
                "deficit_in": round(max(0.0, _calc_content_h(s, needed_pt) - s.H), 3),
            })
            acts.append({
                "rule": "readability_floor_skipped",
                "shape": s.name,
                "role": r,
                "reason": "no_room_on_page",
                "needed_in": round(max(0.0, _calc_content_h(s, needed_pt) - s.H), 3),
            })

    # Step 6: 校验 HIERARCHY 与 TITLE_BODY_MIN_RATIO
    page_role_pts = {}
    for s in texts:
        if s.idx in decor_shapes:
            continue
        r = roles.get(s.idx, "body")
        page_role_pts.setdefault(r, []).append(target_pts.get(r, 13.0))

    violations = check_page_hierarchy(page_role_pts)
    for v in violations:
        if v.get("rule") == "TITLE_BODY_RATIO":
            body_median = statistics.median(page_role_pts.get("body", [13.0]))
            req_title = round((body_median * TITLE_BODY_MIN_RATIO) * 2.0) / 2.0
            title_hi = ROLE_RANGE_PT["title"][1]
            if req_title <= title_hi:
                target_pts["title"] = req_title
                page_role_pts["title"] = [req_title]
        elif v.get("rule") == "HIERARCHY":
            higher = v.get("higher")
            lower_median = v.get("lower_median")
            if higher and higher in ROLE_RANGE_PT:
                new_h = round((lower_median + 0.5) * 2.0) / 2.0
                if new_h <= ROLE_RANGE_PT[higher][1]:
                    target_pts[higher] = new_h
                    page_role_pts[higher] = [new_h]

    remaining_viols = check_page_hierarchy(page_role_pts)
    for rv in remaining_viols:
        acts.append({"rule": "type_scale_hierarchy_violation", "detail": rv})

    # 应用 target_pts 并更新几何
    for s in texts:
        if s.idx in decor_shapes:
            continue
        r = roles.get(s.idx, "body")
        pt = target_pts.get(r, 13.0)
        # Update runs
        for pi, para in enumerate(s.shape.text_frame.paragraphs):
            for ri, run in enumerate(para.runs):
                run.font.size = Pt(pt)
        s.runs = [(pi, ri, t, pt, c, b) for pi, ri, t, _, c, b in s.runs]

        is_nw = s.is_no_wrap and not getattr(s, "_wrap_square", False)
        need_h = _calc_content_h(s, pt)
        # 仅对单行短标签/标题/代码块或显式不折行框增长宽度；且最大宽度不得超过容器卡片或画布右边距
        is_single_line = is_nw or (s.line_count() <= 1 and len(s.text.strip()) <= 20)
        c = _find_card(s)
        pad = max(0.10, 0.35 * (pt / 72.0))
        if c:
            card_avail_r = c.L + c.W - pad
            for o in texts:
                if o.idx != s.idx and _find_card(o) == c:
                    if min(s.T + s.H, o.T + o.H) > max(s.T, o.T) + 0.02 and o.L >= s.L + 0.02:
                        card_avail_r = min(card_avail_r, o.L - FLOOR_MIN_GAP_IN)
            avail_max_w = max(0.1, card_avail_r - s.L)
        else:
            avail_max_w = CANVAS_R - s.L

        need_w = min(_calc_needed_w(s, pt), avail_max_w) if is_single_line else s.W

        # 确保 declared box 对渲染墨迹有充分的边距包络
        safe_h = s.H + 0.06
        if decor_shapes and s.T > 4.0 and s.H < 0.5 and len(s.text.strip()) >= 15:
            safe_h = max(safe_h, 0.65)
        elif roles.get(s.idx) == "conclusion":
            safe_h = max(safe_h, s.H + 0.10)

        grow_h = max(0.0, need_h - s.H)
        actual_h = round(max(need_h, safe_h), 3)

        if is_single_line:
            # 只增不缩（2026-11-04 job-293501ae3698 页 3「关键约束」）：
            # declared box 收窄从来不会让墨迹变短（PowerPoint/LibreOffice
            # 不裁剪溢出文字），只会把 HG02/HG05 的「字形超出声明框」从
            # 假变真——该页标签框 4.312in 被同行避让上限（0.335in，由上
            # 方 0.03in 处的「居民端」长高后的边界浮动触发）砍到 0.335，
            # 13pt 四字必然"溢出"自己被砍小的框。同行右侧真正需要让位时
            # 靠字号收缩（k 搜索 / row-fit），不靠砍框；避让上限因此不得
            # 低于现宽。
            actual_w = round(min(max(need_w + 0.12, s.W),
                                 max(avail_max_w, s.W)), 3)
        else:
            actual_w = s.W

        moves = _plan_cascade(s, grow_h, shs, sh_) if grow_h >= 0.05 else {}
        if moves:
            if c and any(by_idx[oid].T + dy + by_idx[oid].H > c.T + c.H - 0.05
                         for oid, dy in moves.items() if _find_card(by_idx[oid]) == c):
                moves = None
        if moves is not None:
            s.move(W=actual_w, H=actual_h)
            for oid, dy in moves.items():
                by_idx[oid].move(T=round(by_idx[oid].T + dy, 3))
        else:
            s.move(W=actual_w, H=s.H)

        acts.append({"rule": "fit_page_type_scale", "shape": s.name, "role": r,
                     "pt": pt, "k": best_k, "grew_in": round(grow_h, 3)})


def rule_readability_floor(shs, P, acts, sw, sh_):
    """把每个 run 的字号钳进角色区间，并让几何吸收随之增加的占位。"""
    return fit_page_type_scale(shs, sw, sh_, acts)


# --------------------------------------------------------------------------- 主入口

def refine_deck(pptx_path, out_path, ident=None, params=None):
    """对真实 PPTX 执行原稿适配精修，返回变更统计（含规则身份）。"""
    from pptx import Presentation

    P = dict(DEFAULT_PARAMS if params is None else params)
    path = Path(pptx_path)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    prs = Presentation(str(path))
    sw = (prs.slide_width or 0) / 914400.0
    sh = (prs.slide_height or 0) / 914400.0
    if sw <= 0 or sh <= 0:
        raise RefineError("REFINE.NO_SLIDE_SIZE", "无法读取页面尺寸。")
    # 英寸类参数按页宽缩放（以 13.33in 宽屏为标定基准），使同一套规则
    # 在竖版/小页面上按比例生效，而不是把宽屏绝对值硬套到别的版式。
    scale = sw / 13.333 if sw > 0 else 1.0
    P = {k: (round(v * scale, 4) if k.endswith("_in") else v) for k, v in P.items()}

    stats = {
        "theme": ADAPTIVE_THEME_ID, "theme_id": ADAPTIVE_THEME_ID,
        "refine_rules_version": RULES_VERSION,
        "refine_rules_digest": rules_digest(P),
        "theme_application": "REFINED_ORIGINAL",
        "adaptive": True,
        "input_sha256": _sha(path),
        "slides": len(prs.slides),
        "slide_size_in": [round(sw, 2), round(sh, 2)],
        "pages": [], "warnings": [],
        "protected": {"text_unchanged": True, "page_count_unchanged": True,
                      "page_order_unchanged": True, "aspect_unchanged": True,
                      "tables_charts_pictures_notes_unchanged": True},
    }

    # 设计语言识别（只读）
    slides_sh = []
    for i, slide in enumerate(prs.slides, start=1):
        slides_sh.append([Sh(slide, sp, j, sw, sh) for j, sp in enumerate(slide.shapes)])
    deck_max_pt = 0.0
    for shs in slides_sh:
        for s in shs:
            if s.is_text:
                deck_max_pt = max(deck_max_pt, s.max_pt() or 0.0)
    deck_max_pt = deck_max_pt or 18.0

    bgs = []
    for slide in prs.slides:
        bgs.append(_slide_bg(slide))
    bg_mode = None
    if bgs and all(b for b in bgs):
        from collections import Counter
        bg_mode = Counter(b for b in bgs if b).most_common(1)[0][0]
    stats["detected_language"] = {
        "slide_bg": bg_mode,
        "deck_max_pt": round(deck_max_pt, 1),
        "note": "原稿自身设计语言被识别时保留底色/文字色/强调色，只做结构与度量精修。",
    }

    n = len(prs.slides)
    for i, slide in enumerate(prs.slides, start=1):
        shs = slides_sh[i - 1]
        ptype = detect_page_type(i, n, shs, sw, sh, deck_max_pt)
        acts = []
        try:
            if ptype == "kpi_row":
                rule_kpi_row(shs, P, stats["detected_language"], acts, sw, sh)
                rule_steps_row(shs, P, stats["detected_language"], acts, sw, sh)
            elif ptype == "label_rows":
                rule_label_rows(shs, P, stats["detected_language"], acts, sw, sh)
            elif ptype in ("card_grid", "card_grid_plain"):
                rule_card_grid(shs, P, stats["detected_language"], acts, sw, sh, n, i)
            elif ptype == "steps_row":
                rule_steps_row(shs, P, stats["detected_language"], acts, sw, sh)
            elif ptype == "two_panel":
                rule_two_panel(shs, P, stats["detected_language"], acts, sw, sh)
            if ptype not in ("kpi_row", "card_grid", "card_grid_plain", "label_rows"):
                rule_universal(shs, P, stats["detected_language"], acts, sw, sh, ptype)
            # 结构规则先把版面理顺，最后统一落实可读性字号下限并吸收几何后果。
            rule_readability_floor(shs, P, acts, sw, sh)
        except Exception as e:  # noqa: BLE001
            stats["warnings"].append(f"slide{i} {ptype}: {type(e).__name__}: {e}")
            acts.append({"rule": "ERROR", "detail": f"{type(e).__name__}: {e}"})
        stats["pages"].append({"slide": i, "type": ptype, "actions": acts})

    prs.save(str(out))
    stats["output_sha256"] = _sha(out)
    stats["action_count"] = sum(len(p["actions"]) for p in stats["pages"])
    return stats


def _sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
