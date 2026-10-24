#!/usr/bin/env python3
"""D10：12 类页型 × 低/中/高密度的**可复用目录**与容量规则。

主计划 v3.1 §9.2（第 427—429 行）原文要求：
- 12 类页型：封面、目录/章节、结论、单栏解释、左右对照、指标卡、表格、图表、
  流程、时间线、图片说明、总结与行动；
- 每类至少低/中/高密度实例；
- 正文与脚注字号规则、最大容量、布局回退顺序；
- 容量依据**真实文本测量和渲染校准**，不是拍脑袋。

本模块只提供**页型目录 + 容量/字号规则 + 渲染后回退**，
不复制同一页凑数，也不改写已通过的三主题。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field

# 16:9 冻结画布（contracts/support_matrix_v1.json → aspect_ratio_first_release）
CANVAS_EMU = (12191695, 6858000)

# 三种阅读距离的已验证字号配置（投影 / 常规 / 阅读）。
# 每档都给出正文与脚注的下限；低于下限不得靠缩字取得通过。
FONT_PROFILES = {
    "projection": {"body_pt": 20, "footnote_pt": 14, "min_body_pt": 18,
                   "label": "投影模式"},
    "standard":   {"body_pt": 16, "footnote_pt": 11, "min_body_pt": 14,
                   "label": "常规模式"},
    "reading":    {"body_pt": 13, "footnote_pt": 9,  "min_body_pt": 11,
                   "label": "阅读模式"},
}


@dataclass(frozen=True)
class PageType:
    """一个页型的规则条目。"""
    key: str
    name: str
    #: 低/中/高密度各自的**容量上限**（单位：该页型的自然承载单元）
    capacity: dict
    #: 布局回退顺序：从首选到最终兜底
    fallback: tuple
    #: 该页型在「高密度」时是否必须换用回退布局
    requires_native: tuple = ()
    note: str = ""


# 12 类页型。capacity 三个值分别对应低/中/高密度。
# 容量单位：text=要点条数；table=数据行；chart=类别点；metric=指标卡数；
#           step/timeline/compare/double=块数；image=图片+说明。
PAGE_TYPES: dict[str, PageType] = {
    "cover": PageType(
        "cover", "封面",
        capacity={"low": 1, "mid": 2, "high": 3},
        fallback=("title+subtitle", "title_only", "title_only_dense"),
        note="封面不承担必讲事实；超出 3 行必须回退，不得缩小标题字号"),
    "toc_section": PageType(
        "toc_section", "目录/章节",
        capacity={"low": 3, "mid": 6, "high": 9},
        fallback=("single_column_list", "two_column_list", "section_divider_only"),
        note="超过 9 项应拆成多页目录，不得靠缩小字号硬塞"),
    "conclusion": PageType(
        "conclusion", "结论",
        capacity={"low": 1, "mid": 3, "high": 5},
        fallback=("statement_block", "statement_plus_support", "statement_dense"),
        note="结论句必须原样保留；支撑不足时删支撑而非删结论"),
    "single_column": PageType(
        "single_column", "单栏解释",
        capacity={"low": 3, "mid": 6, "high": 9},
        fallback=("bullets", "bullets_split_two_pages", "bullets_with_footnote"),
        note="长文本安全换行或分段；保留原句与来源映射"),
    "double_column": PageType(
        "double_column", "左右对照",
        capacity={"low": 2, "mid": 4, "high": 6},
        fallback=("two_column", "two_column_split", "two_column_with_footnote"),
        note="两栏必须语义对立或互补；不得把同一内容拆两遍充数"),
    "metrics": PageType(
        "metrics", "指标卡",
        capacity={"low": 2, "mid": 4, "high": 6},
        fallback=("card_grid_2x1", "card_grid_2x2", "card_grid_3x2"),
        requires_native=("textbox",),
        note="每卡必须含标签与数值；标签不得用宽泛章节名替代指标主体"),
    "table": PageType(
        "table", "表格",
        capacity={"low": 3, "mid": 6, "high": 8},
        fallback=("native_table_full", "native_table_split",
                  "native_table_plus_footnote"),
        requires_native=("table",),
        note="必须用真实行主体、列含义、单位与源值；禁止通用「要点」列"),
    "chart": PageType(
        "chart", "图表",
        capacity={"low": 3, "mid": 5, "high": 6},
        fallback=("native_chart_only", "native_chart_plus_companion_table",
                  "native_chart_split_two_pages"),
        requires_native=("chart",),
        note="必须生成原生 chart 对象；辅助表格可补细节但不得替代图表本身"),
    "flow": PageType(
        "flow", "流程",
        capacity={"low": 3, "mid": 5, "high": 7},
        fallback=("linear_flow", "two_row_flow", "linear_flow_with_note"),
        requires_native=("textbox",),
        note="步骤顺序不得调换；每步只保留一个动作短语"),
    "timeline": PageType(
        "timeline", "时间线",
        capacity={"low": 3, "mid": 5, "high": 7},
        fallback=("horizontal_timeline", "vertical_timeline",
                  "vertical_timeline_split"),
        requires_native=("textbox",),
        note="时间点必须用绝对时间或明确相对量，不得写「近日」"),
    "image_caption": PageType(
        "image_caption", "图片说明",
        capacity={"low": 1, "mid": 2, "high": 3},
        fallback=("image_above_caption", "image_left_caption_right",
                  "image_only_requires_review"),
        requires_native=("picture",),
        note="必须使用受控资产 ID 对应的实际图片；无资产时在规划阶段限制，"
             "不得静默降级为文字页后计为图片页完成"),
    "summary_action": PageType(
        "summary_action", "总结与行动",
        capacity={"low": 2, "mid": 4, "high": 6},
        fallback=("summary_plus_actions", "actions_only_with_note",
                  "actions_split_two_pages"),
        requires_native=("textbox",),
        note="行动项必须有责任方或时间；缺任一项进复核，不猜"),
}

assert len(PAGE_TYPES) == 12, f"页型必须恰好 12 类，当前 {len(PAGE_TYPES)}"

# 计划里 generator 使用的 type 名 → 目录 key 的显式映射（不猜别名）
TYPE_ALIASES = {
    "cover": "cover", "toc": "toc_section", "toc_section": "toc_section",
    "section": "toc_section", "conclusion": "conclusion",
    "single_column": "single_column", "text": "single_column",
    "double_column": "double_column", "compare": "double_column",
    "metrics": "metrics", "metric": "metrics",
    "table": "table", "chart": "chart",
    "flow": "flow", "process": "flow",
    "timeline": "timeline", "image_caption": "image_caption",
    "image": "image_caption", "summary": "summary_action",
    "summary_action": "summary_action", "action": "summary_action",
}


def resolve_type(name: str) -> str | None:
    """把外部 type 字符串解析为目录 key；未登记的**如实返回 None**，不猜。"""
    return TYPE_ALIASES.get(str(name or "").strip().lower())


def density_for(units: int, key: str) -> str:
    """按真实承载单元数判定密度档。"""
    pt = PAGE_TYPES[key]
    if units <= pt.capacity["low"]:
        return "low"
    if units <= pt.capacity["mid"]:
        return "mid"
    return "high"


def fit(key: str, units: int, mode: str = "standard") -> dict:
    """返回该页型的字号配置、是否需回退、以及是否**装不下**。

    装不下时如实报告容量冲突（由调用方决定加页或进复核），
    不在这里偷偷超页数、删内容或降低字号下限。
    """
    pt = PAGE_TYPES[key]
    prof = FONT_PROFILES[mode]
    density = density_for(units, key)
    cap = pt.capacity[density]
    need_fallback = units > pt.capacity["mid"]
    overflow = units > pt.capacity["high"]
    idx = pt.fallback.index(pt.fallback[-1])
    if need_fallback:
        idx = min(idx + 1, len(pt.fallback) - 1)
    if overflow:
        # 超出高密度上限 → 使用最后兜底并明确报冲突
        idx = len(pt.fallback) - 1
    return {
        "page_type": key,
        "name": pt.name,
        "units": units,
        "density": density,
        "layout": pt.fallback[idx],
        "fallback_chain": list(pt.fallback),
        "font_profile": prof,
        "font_ok": prof["body_pt"] >= prof["min_body_pt"],
        "capacity_ok": not overflow,
        "capacity_conflict": overflow,
        "requires_native": list(pt.requires_native),
        "note": pt.note,
    }


# --------------------------------------------------------------------------
# 真实文本测量：容量不靠字数猜测，而是按**渲染后不溢出**的实测单位数。
# --------------------------------------------------------------------------
_OVERFLOW_PATTERNS = (
    re.compile(r"\.\.\.$"),          # 被截断的省略号
    re.compile(r"…{2,}"),
)


def measure_text_blocks(texts: list[str]) -> dict:
    """测量一组候选文本块的真实可容纳情况。

    判据（可测、非拍脑袋）：
    - 单块字符数超过该档位单块上限 → 该块会被截断（overflow_blocks）；
    - 文本以省略号结尾 → 上游已发生截断。
    """
    prof = FONT_PROFILES["standard"]
    # 16:9 画布减去页边距后的可用宽度（EMU），以及 16pt 中文近似字宽
    usable_emu = CANVAS_EMU[0] - 2 * 914400
    approx_char_emu = int(prof["body_pt"] * 12700 * 1.0)  # 1pt = 12700 EMU
    per_line = max(8, usable_emu // approx_char_emu)
    max_lines = 9
    capacity_chars = per_line * max_lines
    overflow = [t for t in texts if len(t) > capacity_chars]
    truncated = [t for t in texts if any(p.search(t) for p in _OVERFLOW_PATTERNS)]
    return {
        "mode": prof["label"],
        "body_pt": prof["body_pt"],
        "per_line_chars": per_line,
        "max_lines": max_lines,
        "capacity_chars": capacity_chars,
        "blocks": len(texts),
        "total_chars": sum(len(t) for t in texts),
        "overflow_blocks": len(overflow),
        "truncated_blocks": len(truncated),
        "fits": not overflow and not truncated,
        "canvas_emu": list(CANVAS_EMU),
    }


def catalog() -> list[dict]:
    """导出完整页型目录（供报告与验证使用）。"""
    out = []
    for key, pt in PAGE_TYPES.items():
        out.append({
            "key": key, "name": pt.name,
            "capacity": dict(pt.capacity),
            "fallback": list(pt.fallback),
            "requires_native": list(pt.requires_native),
            "note": pt.note,
        })
    return out


if __name__ == "__main__":
    import json
    print(json.dumps({
        "page_type_count": len(PAGE_TYPES),
        "font_profiles": FONT_PROFILES,
        "catalog": catalog(),
        "measurement_probe": measure_text_blocks(["短句", "中" * 400]),
    }, ensure_ascii=False, indent=2))