"""W2-A　设计系统 tokens：网格 · 字号阶梯 · 主题色 · 按文字分的字体栈。

字号阶梯的唯一权威来源是 `contracts/layout_hard_gate_policy.json` 的
`font_roles_pt`（BLOCKING，不得放宽）。本模块把它的取值固化为默认档：

- title           [36, 40]   → 36
- body_preferred  [26, 28]   → 26（硬下限 24，不得靠缩字过门禁）
- conclusion      [28, 32]   → 28
- key_number      [44, 56]   → 48
- table           [20, 22]   → 20
- chart_auxiliary [18, 20]   → 18
- source_footer   [16, 18]   → 16
- cover_title     min 36, may exceed 40 → 44

注意：`page_catalog.FONT_PROFILES`（20/16/13pt）是旧容量口径，与硬门禁冲突；
本包**默认**使用硬门禁档，旧口径仅在 page_catalog 内部继续存在，不进入新版式。

画布边距取硬门禁 `geometry.canvas_text_margin_in` 的更严格值（只收紧不放松）：
left/right 0.55in（≥0.4）、top 0.42in（≥0.3）、bottom 0.32in（≥0.15）。

主题：默认一套与工作台视觉一致（墨蓝强调 + 冷灰中性；ui_reference 样稿未在
本机找到，色值为本模块显式登记的设计决定，写入报告）。对比度全部按
WCAG 相对亮度公式校验 ≥ 4.5（硬门禁 visual.normal_text_local_contrast_min）。

字体栈只列**本机实际可解析**、且可再分发或随系统提供的字体家族（无付费字体）；
栈序即回退链，末位总是 Arial Unicode MS（覆盖面最大的兜底，含 CJK/阿拉伯/天城文）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------- 画布与网格
EMU_PER_IN = 914400
#: 16:9 冻结画布（与 page_catalog.CANVAS_EMU 一致）
CANVAS_W_EMU = 12191695
CANVAS_H_EMU = 6858000

#: 硬门禁 canvas_text_margin_in 的下限（只作校验用）
HARD_GATE_MIN_MARGIN_IN = {"left": 0.4, "right": 0.4, "top": 0.3, "bottom": 0.15}
#: 设计边距（≥ 硬门禁下限）
MARGIN_IN = {"left": 0.55, "right": 0.55, "top": 0.42, "bottom": 0.32}

GRID_COLS = 12
GUTTER_IN = 0.22


def _in(v: float) -> int:
    return int(round(v * EMU_PER_IN))


MARGINS_EMU = {k: _in(v) for k, v in MARGIN_IN.items()}
GUTTER_EMU = _in(GUTTER_IN)
CONTENT_W_EMU = CANVAS_W_EMU - MARGINS_EMU["left"] - MARGINS_EMU["right"]
CONTENT_H_EMU = CANVAS_H_EMU - MARGINS_EMU["top"] - MARGINS_EMU["bottom"]
COL_W_EMU = (CONTENT_W_EMU - GUTTER_EMU * (GRID_COLS - 1)) // GRID_COLS


def col_span_width_emu(span: int) -> int:
    """span 列的总宽（含中间 gutter）。span 必须 1..12。"""
    span = int(span)
    if not 1 <= span <= GRID_COLS:
        raise ValueError(f"span 必须在 1..{GRID_COLS}，得到 {span}")
    return COL_W_EMU * span + GUTTER_EMU * (span - 1)


# ---------------------------------------------------------------- 字号阶梯
@dataclass(frozen=True)
class TypeRole:
    key: str
    pt: float
    bold: bool = False
    line_height: float = 1.35
    space_after_pt: float = 6.0
    color_key: str = "body"
    hard_range: tuple | None = None   # 硬门禁 font_roles_pt 的允许区间（仅登记）


TYPE_ROLES: dict[str, TypeRole] = {
    "cover_title":  TypeRole("cover_title", 44, bold=True, line_height=1.18,
                             color_key="ink", hard_range=("cover_title", 36, None)),
    "section_title": TypeRole("section_title", 36, bold=True, line_height=1.2,
                              color_key="ink", hard_range=("title", 36, 40)),
    "title":        TypeRole("title", 36, bold=True, line_height=1.2,
                             color_key="ink", hard_range=("title", 36, 40)),
    "lead":         TypeRole("lead", 28, line_height=1.35,
                             color_key="body", hard_range=("conclusion", 28, 32)),
    "conclusion":   TypeRole("conclusion", 28, line_height=1.35,
                             color_key="ink", hard_range=("conclusion", 28, 32)),
    "body":         TypeRole("body", 26, line_height=1.4,
                             color_key="body", hard_range=("body_preferred", 26, 28)),
    "key_number":   TypeRole("key_number", 48, bold=True, line_height=1.1,
                             color_key="accent", hard_range=("key_number", 44, 56)),
    "table":        TypeRole("table", 20, line_height=1.3,
                             color_key="body", hard_range=("table", 20, 22)),
    "chart_aux":    TypeRole("chart_aux", 18, line_height=1.3,
                             color_key="body", hard_range=("chart_auxiliary", 18, 20)),
    "ordinal":      TypeRole("ordinal", 18, bold=True, color_key="accent",
                             hard_range=("content_ordinal_allowed_ranges", 18, 22)),
    "caption":      TypeRole("caption", 18, color_key="neutral",
                             hard_range=("chart_auxiliary", 18, 20)),
    "footnote":     TypeRole("footnote", 16, color_key="neutral",
                             line_height=1.3, space_after_pt=0,
                             hard_range=("source_footer", 16, 18)),
}

#: 硬门禁 body_hard_min —— 任何正文字号不得低于该值
BODY_HARD_MIN_PT = 24.0

# ---------------------------------------------------------------- 主题色
def _lum(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    def _c(v):
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * _c(r) + 0.7152 * _c(g) + 0.0722 * _c(b)


def contrast_ratio(c1: str, c2: str) -> float:
    """WCAG 相对亮度对比度（1..21）。用于主题自检与 check_layout。"""
    l1, l2 = _lum(c1), _lum(c2)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05) if lo + 0.05 > 0 else 21.0


@dataclass(frozen=True)
class Theme:
    id: str
    name: str
    ink: str          # 标题/结论文字
    body: str         # 正文文字
    neutral: str      # 次要文字（脚注、说明）
    accent: str       # 强调色 1（≤2 个强调色）
    accent_2: str     # 强调色 2（图表第二系列等）
    paper: str        # 页面底色
    substrate: str    # 卡片/分区底色
    hairline: str     # 分隔线
    #: 语义色（中国惯例：涨红跌绿；仅图表语义使用，不算强调色）
    up: str = "#C0392B"
    down: str = "#1E8E5A"

    def roles(self) -> dict:
        return {"ink": self.ink, "body": self.body, "neutral": self.neutral,
                "accent": self.accent, "accent_2": self.accent_2,
                "paper": self.paper, "substrate": self.substrate,
                "hairline": self.hairline, "up": self.up, "down": self.down}


THEMES: dict[str, Theme] = {
    # 默认：与工作台一致的"墨蓝强调 + 冷灰中性"
    "ink": Theme(
        id="ink", name="墨蓝（默认）",
        ink="#0B1B33", body="#24303E", neutral="#5A6B7D",
        accent="#1D4ED8", accent_2="#0E7490",
        paper="#FFFFFF", substrate="#F2F5F9", hairline="#D8E0EA",
    ),
    "graphite": Theme(
        id="graphite", name="石墨青",
        ink="#1B1F24", body="#2B323A", neutral="#57616C",
        accent="#0F766E", accent_2="#7C5CD6",
        paper="#FFFFFF", substrate="#F4F4F2", hairline="#DCE0DD",
    ),
    "sand": Theme(
        id="sand", name="暖沙",
        ink="#2B2118", body="#3A2F24", neutral="#6B5D4E",
        accent="#96560E", accent_2="#1D4ED8",
        paper="#FFFFFF", substrate="#F8F4EC", hairline="#E4DACB",
    ),
}

DEFAULT_THEME_ID = "ink"

#: 正文/脚注对页面底色的最低对比度（硬门禁 visual.normal_text_local_contrast_min）
MIN_CONTRAST_NORMAL = 4.5
#: ≥32pt 加粗标题的最低对比度（硬门禁 visual.bold_title_32pt_or_larger…）
MIN_CONTRAST_BOLD_TITLE = 3.0


def theme_contrast_report(theme: Theme) -> list[dict]:
    """主题自检：每个文字角色对其常见底色的对比度。"""
    pairs = [
        ("ink", theme.ink, theme.paper), ("ink", theme.ink, theme.substrate),
        ("body", theme.body, theme.paper), ("body", theme.body, theme.substrate),
        ("neutral", theme.neutral, theme.paper),
        ("neutral", theme.neutral, theme.substrate),
        ("accent", theme.accent, theme.paper), ("accent_2", theme.accent_2, theme.paper),
    ]
    out = []
    for key, fg, bg in pairs:
        out.append({"role": key, "fg": fg, "bg": bg,
                    "ratio": round(contrast_ratio(fg, bg), 2)})
    return out


# ---------------------------------------------------------------- 字体栈
#: 按文字（script）分的字体栈。
#: **首选全部为 Noto 系（SIL OFL 1.1，可再分发）**——产品承诺"只用可再分发字体"，
#: 字体文件随包分发在 `<package_root>/fonts/`，渲染环境经 fontenv 加入搜索路径。
#: 系统字体只作兜底（用户机器上没装 Noto 时不至于变豆腐块），排在首选之后。
FONT_STACKS: dict[str, list[str]] = {
    "latin": ["Noto Sans", "Helvetica Neue", "Helvetica", "Arial", "Liberation Sans",
              "DejaVu Sans", "Arial Unicode MS"],
    "cjk_zh": ["Noto Sans SC", "Hiragino Sans GB", "Heiti SC", "Songti SC", "STHeiti",
               "Microsoft YaHei", "Arial Unicode MS"],
    # Noto Sans SC 是泛 CJK 字体，含日语假名；未另带 Noto Sans JP，不虚报家族名
    "cjk_ja": ["Noto Sans SC", "Hiragino Sans", "Hiragino Kaku Gothic ProN",
               "Arial Unicode MS"],
    "arabic": ["Noto Sans Arabic", "Noto Naskh Arabic", "Geeza Pro", "Al Bayan",
               "Arial Unicode MS"],
    "devanagari": ["Noto Sans Devanagari", "Devanagari MT", "Devanagari Sangam MN",
                   "ITF Devanagari", "Arial Unicode MS"],
    "cyrillic": ["Noto Sans", "Helvetica Neue", "Arial", "DejaVu Sans",
                 "Arial Unicode MS"],
}

#: 文字 → PPTX run 的 (ea, cs) 两个槽位应填的家族（latin 槽统一用栈首）
_SCRIPT_SLOTS: dict[str, tuple[str, str]] = {
    # script: (ea 槽位家族, cs 槽位家族)
    "cjk_zh": ("Noto Sans SC", "Noto Sans"),
    "cjk_ja": ("Noto Sans SC", "Noto Sans"),
    "arabic": (None, "Noto Sans Arabic"),
    "devanagari": (None, "Noto Sans Devanagari"),
}


def stack_for(script: str) -> list[str]:
    stack = FONT_STACKS.get(script)
    if not stack:
        raise KeyError(f"未登记的文字类型：{script}（已知：{sorted(FONT_STACKS)}）")
    return list(stack)


def apply_font_slots(font, script: str = "latin") -> None:
    """给 font 写 latin/ea/cs 三槽家族（python-pptx 的 `font.name` 只写 latin）。

    只写 latin 槽时，中文字符在部分渲染器会走泛型回退（实测落到 Carlito），
    整稿字体就不统一；表格/流程/时间线/图表这些**原生对象**尤其容易漏。
    三槽都写才能钉到栈首家族（Noto 系，OFL 可再分发）。
    """
    stack = stack_for(script)
    latin_family = stack[0]
    ea_family, cs_family = _SCRIPT_SLOTS.get(script, (None, None))
    try:
        font.name = latin_family
    except Exception:  # noqa: BLE001
        return
    rPr = getattr(font, "_rPr", None)
    if rPr is None:
        return
    from pptx.oxml.ns import qn  # noqa: PLC0415
    for tag, family in (("a:ea", ea_family), ("a:cs", cs_family)):
        if not family:
            continue
        el = rPr.find(qn(tag))
        if el is None:
            el = rPr.makeelement(qn(tag), {})
            rPr.append(el)
        el.set("typeface", family)


def script_for_text(text: str) -> str:
    """按字符构成判定主导文字。全拉丁（含西里尔按 cyrillic）优先。"""
    counts = {"cjk_zh": 0, "cjk_ja": 0, "arabic": 0, "devanagari": 0,
              "cyrillic": 0, "latin": 0}
    for ch in str(text or ""):
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF:
            counts["cjk_zh"] += 1
        elif 0x3040 <= o <= 0x30FF or 0x31F0 <= o <= 0x31FF:  # 平假名/片假名
            counts["cjk_ja"] += 1
        elif 0x0600 <= o <= 0x06FF or 0x0750 <= o <= 0x077F or 0xFE70 <= o <= 0xFEFF:
            counts["arabic"] += 1
        elif 0x0900 <= o <= 0x097F:
            counts["devanagari"] += 1
        elif 0x0400 <= o <= 0x04FF:
            counts["cyrillic"] += 1
        elif ch.isalpha():
            counts["latin"] += 1
    best = max(counts, key=lambda k: counts[k])
    return best if counts[best] > 0 else "latin"


def is_rtl(script: str) -> bool:
    return script == "arabic"


# ---------------------------------------------------------------- 装饰底线
#: 一套整稿的统一约束：不堆装饰——无阴影、无渐变、无艺术字、圆角半径固定
DECOR_RULES = {
    "shadow": False, "gradient": False, "wordart": False,
    "corner_radius_in": 0.08,
    "emphasis_colors_max": 2,
    "hairline_pt": 0.75,
}


def tokens_summary() -> dict:
    return {
        "canvas_emu": [CANVAS_W_EMU, CANVAS_H_EMU],
        "margins_in": dict(MARGIN_IN),
        "grid": {"cols": GRID_COLS, "gutter_in": GUTTER_IN,
                 "col_w_in": round(COL_W_EMU / EMU_PER_IN, 4)},
        "type_roles": {k: {"pt": v.pt, "bold": v.bold, "color": v.color_key,
                           "hard_range": v.hard_range}
                       for k, v in TYPE_ROLES.items()},
        "body_hard_min_pt": BODY_HARD_MIN_PT,
        "themes": {tid: {"name": t.name, "roles": t.roles(),
                         "contrast": theme_contrast_report(t)}
                   for tid, t in THEMES.items()},
        "font_stacks": dict(FONT_STACKS),
        "decor_rules": dict(DECOR_RULES),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(tokens_summary(), ensure_ascii=False, indent=2))
