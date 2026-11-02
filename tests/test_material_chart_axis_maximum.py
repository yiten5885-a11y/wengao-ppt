"""Regression test for a real pipeline finding on job-mt-60a2dcd8 /
job-57ce106e5450 (2026-11-02).

An earlier fix this session (commit 88ad603) added explicit value-axis
minimum/maximum to wengao_ppt/builders/chart.py's add_native_chart, to fix
HG02 CHART_AXIS_RANGE_UNRESOLVED findings. But that builder turned out NOT
to be the code path the real `from-material` CLI command actually uses —
`wengao_ppt.cli.cmd_from_material` loads samples/r5_material_to_ppt.py (or
wengao_ppt/material_flow.py, if present) directly via importlib, a
completely separate module with its own `add_chart` function. The fix
landed on inactive code.

Confirmed directly against the real failing candidate pptx: chart.value_axis
had minimum_scale=0.0 (samples/r5_material_to_ppt.py's add_chart already
always sets this) but maximum_scale=None — its axis-bounds block only ever
set maximum_scale for percentage-unit charts (fixed at 0-100%), never for
any other unit, so every non-percentage chart's maximum_scale stayed unset
and scripts/layout_gate.py's HG02 check could not resolve the axis range —
exactly the same CHART_AXIS_RANGE_UNRESOLVED finding recurring, unchanged.

samples/r5_material_to_ppt.py's add_chart now reuses
wengao_ppt.builders.chart._nice_axis_bounds (the same proven, tested
function) to set maximum_scale for the non-percentage case too. Verified
directly against the real fixture (tests/fixtures/r5-page-count-mismatch,
the same fixture BuilderPageManifestReconciliationTests already uses, whose
chart page plots real bound facts) before writing this test: the generated
chart's value_axis went from maximum_scale=None to maximum_scale=50.0,
correctly bounding its real [45.0, 8.0] series values.
"""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from pptx import Presentation

from samples import r5_material_to_ppt as material


class TestMaterialChartAxisMaximum(unittest.TestCase):

    def test_real_fixture_chart_gets_an_explicit_maximum_scale(self):
        from wengao_ppt import theme

        fixture_dir = Path(__file__).parent / "fixtures" / "r5-page-count-mismatch"
        fixture = json.loads((fixture_dir / "fixture_manifest.json").read_text(
            encoding="utf-8"))
        for source in fixture["source_files"]:
            payload = (fixture_dir / source["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(payload).hexdigest(), source["sha256"])
        plan = json.loads((fixture_dir / "content_plan.json").read_text(encoding="utf-8"))
        ledger = json.loads((fixture_dir / "fact_ledger.json").read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as temp_dir:
            out_path = Path(temp_dir) / "axis-max.generated.pptx"
            material.build_pptx(plan, ledger["facts"], out_path, theme)
            prs = Presentation(str(out_path))

        chart_axes = []
        for slide in prs.slides:
            for shape in slide.shapes:
                if getattr(shape, "has_chart", False):
                    chart = shape.chart
                    va = chart.value_axis
                    values = [v for ser in chart.plots[0].series for v in ser.values]
                    chart_axes.append((va.minimum_scale, va.maximum_scale, values))

        self.assertTrue(chart_axes, "fixture must produce at least one real native chart")
        for lo, hi, values in chart_axes:
            self.assertIsNotNone(lo)
            self.assertIsNotNone(hi,
                                 "maximum_scale must be set so HG02 can resolve the "
                                 "axis range (real regression: it was None)")
            self.assertLessEqual(lo, min(values))
            self.assertGreaterEqual(hi, max(values))


if __name__ == "__main__":
    unittest.main()
