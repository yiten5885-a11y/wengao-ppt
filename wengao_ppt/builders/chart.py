"""W2-D1　原生图表构建器（柱 / 条 / 折线）。

- 数据绑定：python-pptx `CategoryChartData` → chart part + 内嵌工作簿；
  回环校验用 `wengao_ppt.charts.extract_charts_pptx` 抽出 ChartModel，
  核对 series / categories / workbook 数值一致（`builders.roundtrip`）。
- 重点系列高亮：highlight_series 用主题强调色，其余系列用中性灰；
- 数据标签可开关；类别轴/值轴字号 = chart_auxiliary（18pt）。
- 涨跌语义色（中国惯例涨红跌绿）仅在调用方显式传 semantic="updown" 时使用。
"""
from __future__ import annotations

import math

from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_LEGEND_POSITION
from pptx.util import Emu, Pt

from ..layout import tokens

_AUX_PT = int(tokens.TYPE_ROLES["chart_aux"].pt)


def _nice_axis_bounds(values: list[float]) -> tuple[float, float]:
    """Pick an explicit (minimum, maximum) that comfortably bounds `values`.

    Real finding on job-mt-a1fde38e/job-99c401749423 (2026-11-02): with no
    explicit minimum/maximum, the chart XML never carries a <c:min>/<c:max>,
    so scripts/layout_gate.py's HG02 chart-axis check (which compares every
    rendered numeric token — axis ticks and data labels both — against the
    chart's declared axis range) can't resolve a range at all and reports
    CHART_AXIS_RANGE_UNRESOLVED (UNVERIFIED, blocks download/release) even
    though the chart itself rendered fine. Once minimum/maximum are set
    explicitly, the renderer draws its ticks strictly within that range by
    construction, so any bound that is >= the data's own range with some
    headroom is safe — this never needs to match PowerPoint's own
    auto-scale choice, only to contain it.
    """
    if not values:
        return 0.0, 1.0
    lo_data = min(0.0, min(values))
    hi_data = max(values)
    if hi_data <= 0.0:
        hi_data = 1.0
    magnitude = 10 ** math.floor(math.log10(hi_data))
    for mult in (1, 2, 2.5, 5, 10):
        candidate = magnitude * mult
        if candidate >= hi_data - 1e-6:
            return float(lo_data), float(candidate)
    return float(lo_data), float(magnitude * 10)


def _hex(theme: tokens.Theme, key: str) -> RGBColor:
    return RGBColor.from_string(theme.roles()[key].lstrip("#"))


def _set_chart_font(chart, theme: tokens.Theme):
    """图表整体字体统一到栈首家族（latin/ea/cs 三槽都写）。

    不设的话轴标签/图例会落到主题默认字体（本机是 Carlito），
    中文类别名只能靠泛型回退，整稿字体就不统一了。
    """
    try:
        chart.font.name = tokens.stack_for("latin")[0]
        chart.font.size = Pt(_AUX_PT)
        chart.font.color.rgb = _hex(theme, "body")
    except Exception:  # noqa: BLE001
        pass
    tokens.apply_font_slots(chart.font, "cjk_zh")


def _style_axes(chart, theme: tokens.Theme, series: dict):
    ca = chart.category_axis
    va = chart.value_axis
    for ax in (ca, va):
        try:
            ax.tick_labels.font.size = Pt(_AUX_PT)
            ax.tick_labels.font.color.rgb = _hex(theme, "neutral")
        except Exception:  # noqa: BLE001
            pass
    try:
        ca.format.line.color.rgb = _hex(theme, "hairline")
        va.has_major_gridlines = True
        va.major_gridlines.format.line.color.rgb = _hex(theme, "hairline")
        va.major_gridlines.format.line.width = Pt(0.5)
    except Exception:  # noqa: BLE001
        pass
    all_values = [float(v) for values in series.values() for v in values]
    lo, hi = _nice_axis_bounds(all_values)
    try:
        va.minimum_scale = lo
        va.maximum_scale = hi
    except Exception:  # noqa: BLE001
        pass


def _style_series(chart, theme: tokens.Theme, highlight: str | None,
                  names: list[str], semantic: str | None):
    for i, ser in enumerate(chart.plots[0].series):
        if semantic == "updown" and len(names) == 1:
            # 单系列涨跌图：正值涨红 / 负值跌绿由数据本身决定，底色用强调色
            color_key = "accent"
        elif highlight is not None and names[i] == highlight:
            color_key = "accent"
        else:
            color_key = "neutral" if i == 0 else "accent_2"
        fill = ser.format.fill
        fill.solid()
        fill.fore_color.rgb = _hex(theme, color_key)


def add_native_chart(slide, rect, chart_kind: str, categories: list[str],
                     series: dict, theme: tokens.Theme, *,
                     title: str = "", data_labels: bool = True,
                     highlight_series: str | None = None,
                     semantic: str | None = None, legend: bool = False):
    """在 slide 的 rect 上添加原生图表。返回 (graphic_frame, chart)。

    chart_kind: "column" | "bar" | "line"
    series: {系列名: [数值,...]}；调用方保证数值来自台账或 facts，构建器不改动。
    highlight_series: 要高亮的**系列名**。
    """
    kind_map = {"column": XL_CHART_TYPE.COLUMN_CLUSTERED,
                "bar": XL_CHART_TYPE.BAR_CLUSTERED,
                "line": XL_CHART_TYPE.LINE_MARKERS}
    if chart_kind not in kind_map:
        raise ValueError(f"不支持的图表类型：{chart_kind}（支持 {sorted(kind_map)}）")
    names = list(series.keys())
    if highlight_series is not None and highlight_series not in names:
        raise ValueError(f"highlight_series={highlight_series!r} 不在系列里：{names}")
    data = CategoryChartData()
    data.categories = [str(c) for c in categories]
    for name in names:
        data.add_series(str(name), tuple(series[name]))
    gf = slide.shapes.add_chart(kind_map[chart_kind], Emu(rect.x), Emu(rect.y),
                                Emu(rect.w), Emu(rect.h), data)
    gf.name = "w2:chart_aux"      # 轴/图例/数据标签/标题全部 18pt（chart_auxiliary 档）
    chart = gf.chart
    chart.has_title = bool(title)
    if title:
        chart.chart_title.text_frame.text = str(title)
        try:
            r = chart.chart_title.text_frame.paragraphs[0].runs[0]
            r.font.size = Pt(_AUX_PT)
            r.font.bold = True
        except Exception:  # noqa: BLE001
            pass
    chart.has_legend = legend
    if legend:
        chart.legend.position = XL_LEGEND_POSITION.BOTTOM
        chart.legend.include_in_layout = False
        try:
            chart.legend.font.size = Pt(_AUX_PT)
        except Exception:  # noqa: BLE001
            pass
    plot = chart.plots[0]
    if data_labels:
        try:
            plot.has_data_labels = True
            dl = plot.data_labels
            dl.font.size = Pt(_AUX_PT)
            dl.font.color.rgb = _hex(theme, "body")
            dl.position = XL_LABEL_POSITION.OUTSIDE_END
        except Exception:  # noqa: BLE001
            pass
    _style_axes(chart, theme, series)
    _style_series(chart, theme, highlight_series, names, semantic)
    _set_chart_font(chart, theme)
    return gf, chart
