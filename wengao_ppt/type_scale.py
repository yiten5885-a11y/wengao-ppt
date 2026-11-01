"""wengao_ppt/type_scale.py —— 10–30pt 分级字号体系（唯一事实来源）。

定义 10–30pt 体系下的角色区间、层级约束与工具函数。
主题（theme.py）、精修（refine.py）、门禁（layout_gate.py）与政策文件
全部统一从此处引用，不得各自定义。
"""
from __future__ import annotations

import statistics

TYPE_SCALE_VERSION = "WG-TYPE-20261027.1"
TYPE_MIN_PT, TYPE_MAX_PT = 10.0, 30.0

# 角色 -> (下限, 优选上限)。按页适配时在 [下限, 上限] 内取能放下的最大值。
ROLE_RANGE_PT: dict[str, tuple[float, float]] = {
    "title":      (24.0, 30.0),   # 页标题
    "key_number": (24.0, 30.0),   # 关键数字（原 44–56，封顶 30）
    "lead":       (16.0, 22.0),   # 导语/副标题/卡片大标题
    "conclusion": (16.0, 22.0),   # 关键结论
    "body":       (13.0, 18.0),   # 正文（密集页可到 13，宽松页 16–18）
    "table":      (11.0, 14.0),
    "label":      (11.0, 14.0),   # 卡片内小标签、眉题、等级标签
    "chart_aux":  (10.0, 12.0),
    "ordinal":    (12.0, 16.0),   # 序号徽标
    "footer":     (10.0, 11.0),   # 页脚、页码
    "source":     (10.0, 11.0),   # 来源标注
}

# 同一页内必须满足的层级（左 > 右，按该页实际使用字号的中位数比较）
HIERARCHY: list[tuple[str, str]] = [
    ("title", "lead"),
    ("lead", "body"),
    ("body", "label"),
    ("label", "footer"),
]

TITLE_BODY_MIN_RATIO = 1.4   # 页标题字号 / 正文字号 ≥ 1.4


def is_decor(pt: float | None, text: str = "") -> bool:
    """装饰性大字判定：原稿中 ≥60pt 的单个数字或 1–2 字符（如背景序号 82.5/96/225pt）。"""
    if pt is None or pt < 60.0:
        return False
    t = text.strip()
    return len(t) <= 3


#: Chart legend labels (2026-10-29 user decision): short text sitting beside a
#: column of small solid bars is a chart annotation (chart_aux, 10-12 pt), not
#: body text. Classified as body it was raised to the 13 pt body floor and grew
#: down into the next bar (job-3afdd5d88bd9 page 6: "L5 引领级" beside the L1-L5
#: level bars). A single accent bar next to a header label does not qualify:
#: the bars must form a column of at least two sharing a left edge.
LEGEND_MAX_CHARS = 16
LEGEND_BAR_MAX_H_IN = 0.35
LEGEND_BAR_MIN_H_IN = 0.08
LEGEND_BAR_MIN_COUNT = 2
LEGEND_BAR_LEFT_TOL_IN = 0.05
LEGEND_MAX_GAP_IN = 0.35
LEGEND_BAR_MIN_WIDTH_SPREAD_IN = 0.10


def is_chart_legend_label(i: int, boxes: list[dict]) -> bool:
    """Shared by refine.infer_text_roles and scripts/layout_gate.py:infer_roles.

    ``boxes`` are page shapes as dicts with inches: left, top, width, height,
    plus ``text`` (str) and ``solid`` (bool: real solid fill, no text).
    """
    s = boxes[i]
    t = (s.get("text") or "").strip()
    if not t or len(t) > LEGEND_MAX_CHARS or "\n" in t:
        return False
    # Hairlines (dividers, accent rules) are not chart bars.
    bars = [b for b in boxes if b.get("solid") and not (b.get("text") or "").strip()
            and LEGEND_BAR_MIN_H_IN <= b["height"] <= LEGEND_BAR_MAX_H_IN
            and b["width"] > 0]
    s_top, s_bot = s["top"], s["top"] + s["height"]
    s_left, s_right = s["left"], s["left"] + s["width"]
    for b in bars:
        if min(s_bot, b["top"] + b["height"]) <= max(s_top, b["top"]):
            continue  # not on the same horizontal band
        b_right = b["left"] + b["width"]
        overlaps = min(s_right, b_right) > max(s_left, b["left"])
        beside = 0 <= s_left - b_right <= LEGEND_MAX_GAP_IN
        if not (overlaps or beside):
            continue
        column = [c for c in bars if abs(c["left"] - b["left"]) <= LEGEND_BAR_LEFT_TOL_IN]
        widths = [c["width"] for c in column]
        # A bar chart encodes values in bar length; repeated equal widths are
        # tag chips or accent rules (e.g. "专业型"/"全栈型" chips), not a chart.
        if (len(column) >= LEGEND_BAR_MIN_COUNT
                and max(widths) - min(widths) >= LEGEND_BAR_MIN_WIDTH_SPREAD_IN):
            return True
    return False


#: A short CJK unit/suffix word rendered beside a large decorative stat
#: number (e.g. "30" + "倍" styled as "30倍" with the number huge and the
#: unit normal-sized, split into two separate shapes) is part of that
#: decorative display, not body text and not a page title — even though,
#: alone, its own font size is far under is_decor's 60pt floor. Classified
#: as body text it can win the page's "largest font in the upper half"
#: title selection outright (job-d239567879df page 4: "倍" at 24pt
#: out-scored the real, already-shrunk page title sized at 11pt).
UNIT_SUFFIX_MAX_CHARS = 3
UNIT_SUFFIX_MAX_GAP_IN = 0.15


def is_decor_unit_suffix(i: int, boxes: list[dict]) -> bool:
    """Shared by refine.infer_text_roles and scripts/layout_gate.py:infer_roles.

    ``boxes`` are page shapes as dicts with inches: left, top, width, height,
    plus ``text`` (str) and ``decor`` (bool: this shape is independently
    decor-sized, i.e. is_decor(pt, text) is already True for it).
    """
    s = boxes[i]
    t = (s.get("text") or "").strip()
    if not t or len(t) > UNIT_SUFFIX_MAX_CHARS or "\n" in t:
        return False
    s_top, s_bot = s["top"], s["top"] + s["height"]
    s_left = s["left"]
    for j, b in enumerate(boxes):
        if j == i or not b.get("decor"):
            continue
        b_top, b_bot = b["top"], b["top"] + b["height"]
        if min(s_bot, b_bot) <= max(s_top, b_top):
            continue  # no vertical overlap with the decor shape
        b_right = b["left"] + b["width"]
        if 0 <= s_left - b_right <= UNIT_SUFFIX_MAX_GAP_IN:
            return True
    return False


def check_page_hierarchy(role_pts: dict[str, list[float]]) -> list[dict]:
    """检查单页字号层级（按同角色中位数比较）与 title/body 比例。"""
    violations = []
    medians: dict[str, float] = {}
    for role, pts in role_pts.items():
        valid = [p for p in pts if p is not None]
        if valid:
            medians[role] = statistics.median(valid)

    # 1. 严格层级：高角色中位数 > 低角色中位数
    for high_role, low_role in HIERARCHY:
        if high_role in medians and low_role in medians:
            if medians[high_role] <= medians[low_role]:
                violations.append({
                    "rule": "HIERARCHY",
                    "higher": high_role, "higher_median": medians[high_role],
                    "lower": low_role, "lower_median": medians[low_role],
                })

    # 2. 标题 vs 正文比例 ≥ 1.4
    if "title" in medians and "body" in medians:
        ratio = medians["title"] / max(medians["body"], 0.1)
        if ratio < TITLE_BODY_MIN_RATIO - 1e-4:
            violations.append({
                "rule": "TITLE_BODY_RATIO",
                "ratio": round(ratio, 2),
                "required": TITLE_BODY_MIN_RATIO,
                "title_median": medians["title"],
                "body_median": medians["body"],
            })

    return violations
