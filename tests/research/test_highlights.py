"""tests/research/test_highlights.py

验证亮点规则计算引擎纯代码数学逻辑。
"""

import pytest
from wengao_ppt.research.highlight import compute_comparison_highlight, compute_trend_highlight
from wengao_ppt.research.types import Fact, Series, SeriesPoint


def test_comparison_highlight_max_gap():
    f1 = Fact(
        fact_id="f1", origin="WEB_RESEARCH", subject="公司A", metric="毛利率",
        value=28.5, unit="%", period="2024", quote_span="公司A毛利率28.5%",
    )
    f2 = Fact(
        fact_id="f2", origin="WEB_RESEARCH", subject="行业均值", metric="毛利率",
        value=19.1, unit="%", period="2024", quote_span="行业均值毛利率19.1%",
    )
    facts_map = {"f1": f1, "f2": f2}

    s_target = Series(label="公司A", origin="WEB_RESEARCH", points=[SeriesPoint(x="2024", value=28.5, fact_ref="f1")])
    s_bench = Series(label="行业均值", origin="WEB_RESEARCH", points=[SeriesPoint(x="2024", value=19.1, fact_ref="f2")])

    hl = compute_comparison_highlight(s_target, s_bench, facts_map)
    assert hl is not None
    assert hl.type == "max_gap"
    assert hl.computed["diff"] == 9.4
    assert hl.computed["target_value"] == 28.5
    assert hl.computed["benchmark_value"] == 19.1
    assert "高出 9.4 个百分点" in hl.text


def test_trend_highlight_streak():
    facts = [
        Fact(fact_id=f"f{i}", origin="WEB_RESEARCH", subject="业务X", metric="销量", value=v, unit="万台", period=f"2024Q{i}")
        for i, v in enumerate([10.0, 15.0, 22.0, 30.0], 1)
    ]
    f_map = {f.fact_id: f for f in facts}
    pts = [SeriesPoint(x=f.period, value=f.value, fact_ref=f.fact_id) for f in facts]
    series = Series(label="业务X", origin="WEB_RESEARCH", points=pts)

    hl = compute_trend_highlight(series, f_map)
    assert hl is not None
    assert hl.type == "streak"
    assert hl.computed["streak_length"] == 4
    assert hl.computed["total_growth"] == 20.0
    assert "连续 4 期持续增长" in hl.text


def test_trend_highlight_inflection():
    # 下降后剧烈反弹
    facts = [
        Fact(fact_id="f1", origin="WEB_RESEARCH", subject="指标Y", metric="营收", value=100.0, unit="亿元", period="Q1"),
        Fact(fact_id="f2", origin="WEB_RESEARCH", subject="指标Y", metric="营收", value=80.0, unit="亿元", period="Q2"),
        Fact(fact_id="f3", origin="WEB_RESEARCH", subject="指标Y", metric="营收", value=120.0, unit="亿元", period="Q3"),
    ]
    f_map = {f.fact_id: f for f in facts}
    pts = [SeriesPoint(x=f.period, value=f.value, fact_ref=f.fact_id) for f in facts]
    series = Series(label="指标Y", origin="WEB_RESEARCH", points=pts)

    hl = compute_trend_highlight(series, f_map)
    assert hl is not None
    assert hl.type == "inflection"
    assert hl.computed["inflection_point"] == "Q2"
