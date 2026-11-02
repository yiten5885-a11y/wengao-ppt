"""Regression test for a real pipeline finding on job-mt-a1fde38e /
job-99c401749423 (2026-11-02) — the run immediately after fixing the two
layout_gate issues (chart data-label font, wengao-business contrast margin)
that had blocked the previous run's validate stage. Validation this time
produced blocking=0 but verdict NEEDS_REVIEW with a new finding:
LAYOUT.HARD_GATE_UNVERIFIED, unverified_count=3, all HG02
CHART_AXIS_RANGE_UNRESOLVED on the deck's 3 native charts.

Root cause: wengao_ppt/builders/chart.py's add_native_chart never set an
explicit minimum_scale/maximum_scale on the value axis, so the chart XML
carries no <c:min>/<c:max> at all. scripts/layout_gate.py's HG02 chart-axis
check compares every rendered numeric token (axis ticks and data labels
both) against the chart's declared axis range — with no range declared, it
can't resolve one and reports UNVERIFIED rather than PASS or FAIL. This
blocks download/formal release ("缺证据/检查异常不能当通过；下载与正式放行
保持阻断") even though the chart itself renders correctly.

_nice_axis_bounds picks an explicit range that always contains the series
data by construction (the renderer draws ticks strictly within whatever
range is declared), so it needs no exact match to PowerPoint's own
auto-scale — only containment. Verified against the real failing chart
data before writing this test (ticks observed 0/10/20/.../50 for a chart
whose values top out near 50) that this produces the same natural range
rather than unnecessarily inflating it.
"""
import unittest

from pptx import Presentation

from wengao_ppt.builders.chart import add_native_chart, _nice_axis_bounds
from wengao_ppt.layout import tokens, engine


def _blank_slide():
    prs = Presentation()
    prs.slide_width, prs.slide_height = tokens.CANVAS_W_EMU, tokens.CANVAS_H_EMU
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    return prs, slide


class TestNiceAxisBounds(unittest.TestCase):

    def test_matches_the_real_failing_chart_shape(self):
        # The real failing chart's rendered ticks were 0,10,20,30,40,50 for
        # data topping out in the mid-to-high 40s.
        self.assertEqual(_nice_axis_bounds([45.0, 48.0, 38.0, 46.5]), (0.0, 50.0))

    def test_always_contains_the_data_including_negative_floor(self):
        for values in ([45, 48, 38, 52], [2400, 1800, 900], [5.5, 3.2, 1.8],
                       [1, 1, 1], [0.3, 0.1]):
            with self.subTest(values=values):
                lo, hi = _nice_axis_bounds(values)
                self.assertLessEqual(lo, min(values))
                self.assertGreaterEqual(hi, max(values))

    def test_empty_values_returns_a_safe_default(self):
        self.assertEqual(_nice_axis_bounds([]), (0.0, 1.0))

    def test_all_zero_or_negative_values_still_returns_a_positive_range(self):
        lo, hi = _nice_axis_bounds([0.0, 0.0])
        self.assertGreater(hi, lo)


class TestAddNativeChartSetsExplicitAxisBounds(unittest.TestCase):

    def test_value_axis_gets_explicit_minimum_and_maximum(self):
        prs, slide = _blank_slide()
        rect = engine.Rect(914400, 914400, 6000000, 3000000)
        _gf, chart = add_native_chart(
            slide, rect, "column", ["A", "B", "C"], {"S1": [45, 48, 38]},
            tokens.THEMES["ink"])
        va = chart.value_axis
        self.assertIsNotNone(va.minimum_scale)
        self.assertIsNotNone(va.maximum_scale)
        self.assertLessEqual(va.minimum_scale, 38)
        self.assertGreaterEqual(va.maximum_scale, 48)

    def test_bounds_contain_every_series_across_multiple_series(self):
        prs, slide = _blank_slide()
        rect = engine.Rect(914400, 914400, 6000000, 3000000)
        _gf, chart = add_native_chart(
            slide, rect, "column", ["A", "B"],
            {"S1": [10, 20], "S2": [5, 95]}, tokens.THEMES["ink"])
        va = chart.value_axis
        self.assertLessEqual(va.minimum_scale, 5)
        self.assertGreaterEqual(va.maximum_scale, 95)


if __name__ == "__main__":
    unittest.main()
