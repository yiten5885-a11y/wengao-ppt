"""wengao_ppt.research.highlight

亮点规则计算引擎：
- 严格基于 Facts 纯规则计算，绝对不让模型凭空编造数字
- 计算差值、倍数、百分点、拐点、排名、连续领先期数
- 四种亮点类型：max_gap, inflection, rank_change, streak
- 差值与计算细节存储于 highlight.computed，供排版窗口直接引用
"""

from __future__ import annotations

import logging
from typing import Optional

from wengao_ppt.research.types import Fact, Highlight, Series

logger = logging.getLogger(__name__)


def compute_comparison_highlight(
    target_series: Series,
    benchmark_series: Series,
    facts_map: dict[str, Fact],
) -> Optional[Highlight]:
    """计算横向对比中的亮点（优先 max_gap 或连续领先）。"""
    if not target_series.points or not benchmark_series.points:
        return None

    # 按 x 轴对齐
    bench_dict = {p.x: p for p in benchmark_series.points}
    aligned_pairs = []
    for tp in target_series.points:
        if tp.x in bench_dict:
            bp = bench_dict[tp.x]
            diff = tp.value - bp.value
            pct_diff = (diff / bp.value * 100) if bp.value != 0 else 0.0
            aligned_pairs.append((tp, bp, diff, pct_diff))

    if not aligned_pairs:
        return None

    # 寻找最大差距 (max_gap)
    best_pair = max(aligned_pairs, key=lambda x: abs(x[2]))
    tp, bp, diff, pct_diff = best_pair

    tf = facts_map.get(tp.fact_ref)
    unit_str = tf.unit if tf else ""

    is_percentage_unit = unit_str in ("%", "个百分点")
    gap_desc = f"{abs(diff):.1f} 个百分点" if is_percentage_unit else f"{abs(diff):.1f} {unit_str}"
    direction = "高出" if diff > 0 else "低于"

    rule_name = "MAX_GAP_RULE_V1"
    computed_data = {
        "target_label": target_series.label,
        "target_value": tp.value,
        "benchmark_label": benchmark_series.label,
        "benchmark_value": bp.value,
        "diff": round(diff, 2),
        "abs_diff": round(abs(diff), 2),
        "pct_diff": round(pct_diff, 1),
        "unit": unit_str,
        "period": tp.x,
    }

    text = f"在 {tp.x}，{target_series.label}（{tp.value}{unit_str}）较 {benchmark_series.label}（{bp.value}{unit_str}）{direction} {gap_desc}"

    return Highlight(
        type="max_gap",
        text=text,
        fact_refs=[tp.fact_ref, bp.fact_ref],
        rule=rule_name,
        computed=computed_data,
    )


def compute_trend_highlight(
    series: Series,
    facts_map: dict[str, Fact],
) -> Optional[Highlight]:
    """计算纵向趋势中的亮点（拐点 inflection 或 连续增长 streak）。"""
    pts = series.points
    if len(pts) < 2:
        return None

    unit_str = ""
    f0 = facts_map.get(pts[0].fact_ref)
    if f0:
        unit_str = f0.unit

    # 1. 检查是否存在拐点（inflection）：后一段斜率反转或增速突变
    if len(pts) >= 3:
        for i in range(1, len(pts) - 1):
            prev_diff = pts[i].value - pts[i - 1].value
            next_diff = pts[i + 1].value - pts[i].value
            # 由降转升或由平转骤增
            if prev_diff <= 0 and next_diff > 0:
                reversal_amount = next_diff - prev_diff
                computed = {
                    "inflection_point": pts[i].x,
                    "prev_diff": round(prev_diff, 2),
                    "next_diff": round(next_diff, 2),
                    "reversal_amount": round(reversal_amount, 2),
                    "unit": unit_str,
                }
                text = f"在 {pts[i].x} 迎来拐点，由前期的停滞/下降转为强劲回升，{pts[i + 1].x} 增长 {next_diff:.1f}{unit_str}"
                return Highlight(
                    type="inflection",
                    text=text,
                    fact_refs=[pts[i - 1].fact_ref, pts[i].fact_ref, pts[i + 1].fact_ref],
                    rule="TREND_INFLECTION_V1",
                    computed=computed,
                )

    # 2. 检查连续增长/领先 streak
    increases = [pts[i].value - pts[i - 1].value for i in range(1, len(pts))]
    all_positive = all(d > 0 for d in increases)
    if all_positive:
        total_growth = pts[-1].value - pts[0].value
        computed = {
            "streak_length": len(pts),
            "start_period": pts[0].x,
            "end_period": pts[-1].x,
            "total_growth": round(total_growth, 2),
            "unit": unit_str,
        }
        text = f"实现连续 {len(pts)} 期持续增长，自 {pts[0].x} 至 {pts[-1].x} 累计提升 {total_growth:.1f}{unit_str}"
        return Highlight(
            type="streak",
            text=text,
            fact_refs=[pts[0].fact_ref, pts[-1].fact_ref],
            rule="TREND_STREAK_V1",
            computed=computed,
        )

    # 3. 兜底取最大区间增幅
    total_change = pts[-1].value - pts[0].value
    direction = "增长" if total_change >= 0 else "下降"
    computed = {
        "start_value": pts[0].value,
        "end_value": pts[-1].value,
        "total_change": round(total_change, 2),
        "unit": unit_str,
    }
    text = f"从 {pts[0].x} 的 {pts[0].value}{unit_str} 至 {pts[-1].x} 的 {pts[-1].value}{unit_str}，整体{direction} {abs(total_change):.1f}{unit_str}"
    return Highlight(
        type="max_gap",
        text=text,
        fact_refs=[pts[0].fact_ref, pts[-1].fact_ref],
        rule="TREND_OVERALL_SPAN_V1",
        computed=computed,
    )
