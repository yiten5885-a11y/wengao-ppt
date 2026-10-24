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
from pathlib import Path

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
        """按当前框宽估算总行数（取最长段）。"""
        w = box_w if box_w is not None else self.W
        n = 1
        if self.is_text:
            for para in self.shape.text_frame.paragraphs:
                t = "".join(r.text for r in para.runs)
                if not t:
                    continue
                pt = max([float(r.font.size.pt) for r in para.runs if r.font.size] or [12])
                n = max(n, wrapped_lines(t, pt, max(w - 0.06, 0.2)))
        return n

    def content_h_in(self, box_w=None):
        """文本内容高度（英寸）：行数 × 字号 × 1.22 行距。"""
        pt = self.max_pt() or 12.0
        return self.line_count(box_w) * pt * 1.22 / 72.0

    def one_line_fits(self, pt, box_w):
        t = self.text.strip()
        if not t:
            return True
        return text_width_in(t, pt) <= box_w - 0.06

    def move(self, L=None, T=None, W=None, H=None):
        from pptx.util import Emu
        if L is not None:
            self.shape.left = Emu(int(round(L * 914400)))
        if T is not None:
            self.shape.top = Emu(int(round(T * 914400)))
        if W is not None:
            self.shape.width = Emu(int(round(W * 914400)))
        if H is not None:
            self.shape.height = Emu(int(round(H * 914400)))
        if L is not None:
            self.L = L
        if T is not None:
            self.T = T
        if W is not None:
            self.W = W
        if H is not None:
            self.H = H


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
