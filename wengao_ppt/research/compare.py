"""wengao_ppt.research.compare

横向对比、纵向趋势与洞察视图组装：
- 组装 Views (kind: comparison | trend | insight)
- 识别用户数据与网络数据冲突并标记（并列呈现，标待确认，不覆盖用户值）
- 生成页脚来源 Footnote
- 对无法构建的视图记录 Omission
"""

from __future__ import annotations

import logging
from typing import Optional

from wengao_ppt.research.ground import check_comparability
from wengao_ppt.research.highlight import compute_comparison_highlight, compute_trend_highlight
from wengao_ppt.research.types import (
    Conflict,
    Fact,
    Footnote,
    Highlight,
    OmissionRecord,
    Series,
    SeriesPoint,
    View,
)

logger = logging.getLogger(__name__)


def detect_conflicts(user_facts: list[Fact], web_facts: list[Fact]) -> list[Conflict]:
    """检测用户材料事实与外部网络事实的数值冲突。

    若同一主体、同一时期、同一度量但数值差异超过 3%，判定为冲突。
    """
    conflicts: list[Conflict] = []
    c_idx = 1
    for uf in user_facts:
        for wf in web_facts:
            # 主体相似且周期重叠
            same_subj = uf.subject in wf.subject or wf.subject in uf.subject
            same_period = uf.period == wf.period or ("2024" in uf.period and "2024" in wf.period)
            same_unit = uf.unit == wf.unit
            if same_subj and same_period and same_unit:
                # 检查数值差异是否明显
                if abs(uf.value - wf.value) > 0.03 * max(abs(uf.value), abs(wf.value), 1.0):
                    cid = f"c{c_idx:02d}"
                    c_idx += 1
                    conflicts.append(Conflict(conflict_id=cid, user_fact=uf.fact_id, web_fact=wf.fact_id))
    return conflicts


def build_footnote(fact_refs: list[str], facts_map: dict[str, Fact]) -> Footnote:
    """基于事实引用自动生成严谨的来源脚注。"""
    publishers = []
    for ref in fact_refs:
        f = facts_map.get(ref)
        if f and f.publisher:
            pub_text = f"{f.publisher} ({f.period})"
            if pub_text not in publishers:
                publishers.append(pub_text)
    text = "数据来源: " + "; ".join(publishers) if publishers else "数据来源: 权威公开报告与材料核对"
    return Footnote(text=text, fact_refs=fact_refs)


def assemble_views(
    all_facts: list[Fact],
    job_options: dict,
) -> tuple[list[View], list[OmissionRecord], list[Conflict]]:
    """将经过核对的事实集合组装为合规的 Views。"""
    facts_map = {f.fact_id: f for f in all_facts}
    user_facts = [f for f in all_facts if f.origin == "USER_MATERIAL"]
    web_facts = [f for f in all_facts if f.origin == "WEB_RESEARCH"]

    conflicts = detect_conflicts(user_facts, web_facts)
    views: list[View] = []
    omissions: list[OmissionRecord] = []
    view_counter = 1

    # ==========================================
    # 0. 优先处理冲突视图 (Conflict Parallel Presentation View)
    # 规则：若存在用户事实与外部事实冲突，必须并列呈现，标待确认，两端都在 series 中
    # ==========================================
    handled_conflict_facts = set()
    for conf in conflicts:
        uf = facts_map.get(conf.user_fact)
        wf = facts_map.get(conf.web_fact)
        if uf and wf:
            handled_conflict_facts.add(uf.fact_id)
            handled_conflict_facts.add(wf.fact_id)

            s_user = Series(
                label=f"{uf.subject} (材料自述)",
                origin=uf.origin,
                points=[SeriesPoint(x=uf.period, value=uf.value, fact_ref=uf.fact_id)],
            )
            s_web = Series(
                label=f"{wf.subject} ({wf.publisher or '外部统计'}-待确认)",
                origin=wf.origin,
                points=[SeriesPoint(x=wf.period, value=wf.value, fact_ref=wf.fact_id)],
            )

            diff = round(uf.value - wf.value, 2)
            abs_diff = round(abs(diff), 2)
            unit_str = uf.unit
            gap_desc = f"{abs_diff} 个百分点" if unit_str in ("%", "个百分点") else f"{abs_diff} {unit_str}"

            hl = Highlight(
                type="max_gap",
                text=f"材料自述（{uf.value}{unit_str}）与外部统计（{wf.value}{unit_str}）存在 {gap_desc} 差异，并列呈现标待确认",
                fact_refs=[uf.fact_id, wf.fact_id],
                rule="CONFLICT_COMPARISON_V1",
                computed={
                    "user_value": uf.value,
                    "web_value": wf.value,
                    "diff": diff,
                    "abs_diff": abs_diff,
                    "unit": unit_str,
                    "period": uf.period,
                },
            )
            fn = Footnote(
                text=f"数据来源: 用户材料自述 ({uf.period}); {wf.publisher or '外部报告'} ({wf.period}) [数据存在差异，并列呈现供裁决]",
                fact_refs=[uf.fact_id, wf.fact_id],
            )
            vid = f"v{view_counter:02d}"
            view_counter += 1
            views.append(
                View(
                    view_id=vid,
                    kind="comparison",
                    title=f"{uf.subject} {uf.metric}（材料自述 vs 外部统计）",
                    metric=uf.metric,
                    unit=uf.unit,
                    period=uf.period,
                    series=[s_user, s_web],
                    highlight=hl,
                    conflicts=[conf],
                    footnote=fn,
                )
            )

    # ==========================================
    # 1. 尝试构建横向对比视图 (Comparison View)
    # ==========================================
    # 寻找同一度量但属于不同主体（如公司 vs 行业均值/竞品）的事实
    comparison_grouped: dict[str, list[Fact]] = {}
    for f in all_facts:
        if f.fact_id in handled_conflict_facts:
            continue
        key = f"{f.metric}_{f.unit}"
        comparison_grouped.setdefault(key, []).append(f)

    comp_view_created = False
    for metric_unit, group in comparison_grouped.items():
        if len(group) >= 2:
            # 区分不同主体
            subj_dict: dict[str, Fact] = {}
            for f in group:
                if f.subject not in subj_dict:
                    subj_dict[f.subject] = f

            if len(subj_dict) >= 2:
                subjects = list(subj_dict.keys())
                f_a = subj_dict[subjects[0]]
                f_b = subj_dict[subjects[1]]

                # 可比性检查
                is_comp, omission = check_comparability(f_a, f_b)
                if not is_comp and omission:
                    omissions.append(omission)
                    continue

                common_period = f_a.period if f_a.period == f_b.period else f"{f_a.period}/{f_b.period}"

                s1 = Series(label=f_a.subject, origin=f_a.origin, points=[SeriesPoint(x=common_period, value=f_a.value, fact_ref=f_a.fact_id)])
                s2 = Series(label=f_b.subject, origin=f_b.origin, points=[SeriesPoint(x=common_period, value=f_b.value, fact_ref=f_b.fact_id)])

                highlight = compute_comparison_highlight(s1, s2, facts_map)
                ref_list = [f_a.fact_id, f_b.fact_id]
                footnote = build_footnote(ref_list, facts_map)

                vid = f"v{view_counter:02d}"
                view_counter += 1
                views.append(
                    View(
                        view_id=vid,
                        kind="comparison",
                        title=f"{f_a.subject} 与 {f_b.subject} {f_a.metric} 对比",
                        metric=f_a.metric,
                        unit=f_a.unit,
                        period=common_period,
                        series=[s1, s2],
                        highlight=highlight,
                        conflicts=[c for c in conflicts if c.user_fact in ref_list or c.web_fact in ref_list],
                        footnote=footnote,
                    )
                )
                comp_view_created = True
                break

    if not comp_view_created and not any(v.kind == "comparison" for v in views):
        omissions.append(OmissionRecord(want="横向对比视图：行业竞品/均值基准", code="RESEARCH.NO_RELIABLE_SOURCE"))

    # ==========================================
    # 2. 尝试构建纵向趋势视图 (Trend View)
    # ==========================================
    # 寻找同一主体、同一度量但跨不同时期的事实序列
    trend_grouped: dict[str, list[Fact]] = {}
    for f in all_facts:
        key = f"{f.subject}_{f.metric}"
        trend_grouped.setdefault(key, []).append(f)

    trend_view_created = False
    for subj_metric, group in trend_grouped.items():
        if len(group) >= 2:
            # 按时间排序
            sorted_facts = sorted(group, key=lambda x: str(x.period))
            pts = [SeriesPoint(x=f.period, value=f.value, fact_ref=f.fact_id) for f in sorted_facts]
            first_f = sorted_facts[0]
            series = Series(label=first_f.subject, origin=first_f.origin, points=pts)

            highlight = compute_trend_highlight(series, facts_map)
            ref_list = [f.fact_id for f in sorted_facts]
            footnote = build_footnote(ref_list, facts_map)

            vid = f"v{view_counter:02d}"
            view_counter += 1
            views.append(
                View(
                    view_id=vid,
                    kind="trend",
                    title=f"{first_f.subject} {first_f.metric} 历史趋势",
                    metric=first_f.metric,
                    unit=first_f.unit,
                    period=f"{sorted_facts[0].period} - {sorted_facts[-1].period}",
                    series=[series],
                    highlight=highlight,
                    conflicts=[c for c in conflicts if c.user_fact in ref_list or c.web_fact in ref_list],
                    footnote=footnote,
                )
            )
            trend_view_created = True
            break

    if not trend_view_created:
        omissions.append(OmissionRecord(want="纵向趋势视图：多期历史数据", code="RESEARCH.NO_RELIABLE_SOURCE"))

    # ==========================================
    # 3. 构建核心洞察视图 (Insight View)
    # ==========================================
    # 若有突出的外部权威事实或用户核心事实，生成单项大数洞察
    insight_candidate = web_facts[0] if web_facts else (user_facts[0] if user_facts else None)
    if insight_candidate:
        s = Series(
            label=insight_candidate.subject,
            origin=insight_candidate.origin,
            points=[SeriesPoint(x=insight_candidate.period, value=insight_candidate.value, fact_ref=insight_candidate.fact_id)],
        )
        highlight = Highlight(
            type="max_gap",
            text=f"{insight_candidate.subject} 核心指标 {insight_candidate.metric} 达到 {insight_candidate.value}{insight_candidate.unit}",
            fact_refs=[insight_candidate.fact_id],
            rule="INSIGHT_KEY_METRIC_V1",
            computed={"value": insight_candidate.value, "unit": insight_candidate.unit},
        )
        vid = f"v{view_counter:02d}"
        views.append(
            View(
                view_id=vid,
                kind="insight",
                title=f"{insight_candidate.subject} {insight_candidate.metric} 关键洞察",
                metric=insight_candidate.metric,
                unit=insight_candidate.unit,
                period=insight_candidate.period,
                series=[s],
                highlight=highlight,
                conflicts=[],
                footnote=build_footnote([insight_candidate.fact_id], facts_map),
            )
        )

    return views, omissions, conflicts
