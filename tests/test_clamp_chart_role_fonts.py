"""Regression test for a real pipeline failure found on job-mt-56ebac59 /
job-51d167cddbc1 (2026-11-02) — the first time this session's
material-generation fixes let a job reach the validate stage at all, and the
first real layout_gate finding hit there: HG07 ROLE_SIZE flagged a
COLUMN_CLUSTERED chart's own data-label text at 18pt against the chart_aux
role's 10-12pt max, on 3 of the deck's pages.

clamp_oversized_to_role_max (wengao_ppt/theme.py) already shrinks oversized
text_frame runs, but a chart's own labels live in separate chart-part XML a
text_frame walk never reaches, so charts were explicitly excluded. Worse:
the function was gated on refine_mod.infer_text_roles's per-shape role map,
which — confirmed directly against the real failing candidate pptx — never
assigns ANY role to a chart shape's own index at all (it only classifies
is_text shapes). scripts/layout_gate.py's independent, render-based role
inference is what actually assigns "chart_aux" to a chart's own rendered
label text, by geometric position, completely outside refine.py's
shape-centric classifier.

The fix: _clamp_chart_role_fonts targets a chart's data_labels/axis
tick-label/legend fonts directly against the chart_aux role's max,
called unconditionally for every chart shape rather than gated on a role
map entry that was never populated for charts. Verified directly against
the real failing candidate pptx (jobs/job-51d167cddbc1) before writing this
test: all 3 oversized charts (pages 6, 12, 14) clamped from 18pt to 12pt.
"""
import unittest

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches, Pt

from wengao_ppt import theme as theme_mod


def _chart_slide(data_label_pt=None, axis_pt=None):
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    data = CategoryChartData()
    data.categories = ["A", "B"]
    data.add_series("S1", (1, 2))
    gframe = slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(1), Inches(5), Inches(3), data)
    chart = gframe.chart
    if data_label_pt is not None:
        chart.plots[0].has_data_labels = True
        chart.plots[0].data_labels.font.size = Pt(data_label_pt)
    if axis_pt is not None:
        chart.category_axis.tick_labels.font.size = Pt(axis_pt)
        chart.value_axis.tick_labels.font.size = Pt(axis_pt)
    return prs, chart


class TestClampChartDataLabels(unittest.TestCase):

    def test_real_oversized_data_labels_shrink_to_chart_aux_max(self):
        prs, chart = _chart_slide(data_label_pt=18)
        changes = theme_mod.clamp_oversized_to_role_max(prs)
        data_label_changes = [c for c in changes if c.get("target") == "data_labels"]
        self.assertEqual(len(data_label_changes), 1)
        self.assertEqual(data_label_changes[0]["from"], 18.0)
        self.assertEqual(data_label_changes[0]["to"], 12.0)
        self.assertEqual(chart.plots[0].data_labels.font.size.pt, 12.0)

    def test_oversized_axis_tick_labels_also_shrink(self):
        prs, chart = _chart_slide(axis_pt=18)
        theme_mod.clamp_oversized_to_role_max(prs)
        self.assertEqual(chart.category_axis.tick_labels.font.size.pt, 12.0)
        self.assertEqual(chart.value_axis.tick_labels.font.size.pt, 12.0)

    def test_fonts_already_within_range_are_untouched(self):
        prs, chart = _chart_slide(data_label_pt=11, axis_pt=10)
        changes = theme_mod.clamp_oversized_to_role_max(prs)
        chart_changes = [c for c in changes if "target" in c]
        self.assertEqual(chart_changes, [])
        self.assertEqual(chart.plots[0].data_labels.font.size.pt, 11.0)

    def test_chart_with_no_explicit_fonts_is_left_alone(self):
        # Inherited/theme-default fonts (font.size is None) must never be
        # touched — the function can't know their effective rendered size.
        prs, _chart = _chart_slide()
        changes = theme_mod.clamp_oversized_to_role_max(prs)
        chart_changes = [c for c in changes if "target" in c]
        self.assertEqual(chart_changes, [])

    def test_chart_geometry_is_never_touched(self):
        prs, chart = _chart_slide(data_label_pt=18)
        slide = prs.slides[0]
        gframe = next(s for s in slide.shapes if s.has_chart)
        before = (gframe.left, gframe.top, gframe.width, gframe.height)
        theme_mod.clamp_oversized_to_role_max(prs)
        after = (gframe.left, gframe.top, gframe.width, gframe.height)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
