"""Regression test for a false-positive found live on job-d239567879df.

strip_text_pptx (scripts/layout_gate.py) makes the "no-text" pixel-diff
baseline used for HG02 visibility checks. It only recognized chart parts at
the standard ppt/charts/chartN.xml path; a real user-uploaded PPTX (likely
authored outside this pipeline's own chart-generation, which always writes
to ppt/charts/) had its chart at ppt/slides/charts/chart1.xml instead — a
legitimate, real-world OOXML packaging variant. Because the chart-stripping
branch's regex never matched, that chart's axis labels were never stripped
in the "no-text" render, so the with-text/without-text diff was zero
everywhere inside the chart and every one of its 60 rendered characters was
flagged CHAR_INVISIBLE, even though the chart renders its axis labels
perfectly legibly (confirmed against the real rendered page).
"""
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches

from scripts.layout_gate import strip_text_pptx


def _relocate_chart_under_slides(src: Path, dst: Path) -> None:
    """Repackage src's ppt/charts/chart1.xml under ppt/slides/charts/,
    matching the real-world packaging job-d239567879df's source file used.
    Only the two chart part paths need moving for strip_text_pptx's own
    filename-pattern matching; the slide's relationship target is also
    updated so the fixture stays an internally consistent, openable pptx.
    """
    with zipfile.ZipFile(str(src), "r") as zin, \
            zipfile.ZipFile(str(dst), "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            name = info.filename
            if name == "ppt/charts/chart1.xml":
                name = "ppt/slides/charts/chart1.xml"
            elif name == "ppt/charts/_rels/chart1.xml.rels":
                name = "ppt/slides/charts/_rels/chart1.xml.rels"
            elif name == "ppt/slides/_rels/slide1.xml.rels":
                data = data.replace(b"../charts/chart1.xml", b"charts/chart1.xml")
            zout.writestr(name, data)


class TestStripTextPptxChartPaths(unittest.TestCase):

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        data = CategoryChartData()
        data.categories = ["2009", "2014"]
        data.add_series("s", (55, 70))
        slide.shapes.add_chart(
            XL_CHART_TYPE.LINE, Inches(1), Inches(1), Inches(4), Inches(3), data)
        self.standard_path = self.root / "standard.pptx"
        prs.save(self.standard_path)

    def _tick_lbl_pos_values(self, pptx_path: Path, chart_member: str) -> list[str]:
        with zipfile.ZipFile(str(pptx_path)) as z:
            xml = z.read(chart_member).decode("utf-8")
        import re
        return re.findall(r'tickLblPos val="([^"]*)"', xml)

    def test_standard_charts_path_still_stripped(self):
        out = self.root / "standard-notext.pptx"
        strip_text_pptx(self.standard_path, out)
        values = self._tick_lbl_pos_values(out, "ppt/charts/chart1.xml")
        self.assertTrue(values)
        self.assertTrue(all(v == "none" for v in values))

    def test_slides_nested_charts_path_also_stripped(self):
        nested = self.root / "nested.pptx"
        _relocate_chart_under_slides(self.standard_path, nested)
        # Sanity: this is a real occurrence of the bug, not a hypothetical —
        # before the fix, the chart's tickLblPos stayed "nextTo" here.
        before = self._tick_lbl_pos_values(nested, "ppt/slides/charts/chart1.xml")
        self.assertTrue(before)
        out = self.root / "nested-notext.pptx"
        strip_text_pptx(nested, out)
        values = self._tick_lbl_pos_values(out, "ppt/slides/charts/chart1.xml")
        self.assertTrue(values)
        self.assertTrue(all(v == "none" for v in values))


if __name__ == "__main__":
    unittest.main()
