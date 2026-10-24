"""Tests for wengao_ppt.transitions module."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pptx import Presentation
from pptx.util import Inches, Pt

from wengao_ppt.transitions import (
    apply_transitions,
    check_transitions,
    SUPPORTED_FAMILIES,
)


class TransitionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.pptx_path = Path(self.tmp_dir) / "source.pptx"

        # Create a sample presentation with 3 slides
        prs = Presentation()
        # Slide 1: Cover
        s1 = prs.slides.add_slide(prs.slide_layouts[0])
        tb1 = s1.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(2))
        tb1.text_frame.text = "Slide 1: Cover Title"

        # Slide 2: Section
        s2 = prs.slides.add_slide(prs.slide_layouts[1])
        tb2 = s2.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(2))
        tb2.text_frame.text = "Slide 2: Section Header"

        # Slide 3: Content
        s3 = prs.slides.add_slide(prs.slide_layouts[1])
        tb3 = s3.shapes.add_textbox(Inches(1), Inches(1), Inches(8), Inches(2))
        tb3.text_frame.text = "Slide 3: Content Body"

        prs.save(str(self.pptx_path))

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_apply_default_fade_transitions(self):
        out_path = Path(self.tmp_dir) / "out_fade.pptx"
        roles = {1: "cover", 2: "section", 3: "content"}
        report = apply_transitions(
            self.pptx_path,
            out_path,
            policy={"enabled": True, "family": "fade", "duration_ms": 500},
            roles=roles,
        )

        self.assertEqual(report["transitions_applied"], 3)
        self.assertEqual(report["total_slides"], 3)
        self.assertTrue(report["enabled"])

        check = check_transitions(out_path, require_enabled=True, expected_family="fade")
        self.assertEqual(check["status"], "PASS")
        self.assertEqual(check["slides_with_transition"], 3)
        self.assertEqual(check["slides_missing_transition"], 0)
        self.assertEqual(len(check["errors"]), 0)

        # Inspect section slide variant
        prs_check = Presentation(str(out_path))
        s2_xml = prs_check.slides[1]._element.xml
        self.assertIn('thruBlk="1"', s2_xml)

    def test_apply_wipe_and_push_families(self):
        for family in ("wipe", "push"):
            with self.subTest(family=family):
                out_path = Path(self.tmp_dir) / f"out_{family}.pptx"
                report = apply_transitions(
                    self.pptx_path,
                    out_path,
                    policy={"enabled": True, "family": family, "duration_ms": 400},
                    roles={1: "cover", 2: "section", 3: "content"},
                )
                self.assertEqual(report["transitions_applied"], 3)
                check = check_transitions(out_path, require_enabled=True, expected_family=family)
                self.assertEqual(check["status"], "PASS")

    def test_idempotent_reapplication(self):
        out1 = Path(self.tmp_dir) / "out1.pptx"
        out2 = Path(self.tmp_dir) / "out2.pptx"

        apply_transitions(
            self.pptx_path,
            out1,
            policy={"enabled": True, "family": "fade", "duration_ms": 500},
        )
        apply_transitions(
            out1,
            out2,
            policy={"enabled": True, "family": "fade", "duration_ms": 500},
        )

        check = check_transitions(out2, require_enabled=True)
        self.assertEqual(check["status"], "PASS")

        prs = Presentation(str(out2))
        for slide in prs.slides:
            # Must only have exactly 1 transition element
            trans_tags = [c for c in slide._element if c.tag.endswith("}transition")]
            self.assertEqual(len(trans_tags), 1)

    def test_disable_transitions_removes_them(self):
        out_with = Path(self.tmp_dir) / "with_trans.pptx"
        out_without = Path(self.tmp_dir) / "without_trans.pptx"

        apply_transitions(self.pptx_path, out_with, policy={"enabled": True})
        check1 = check_transitions(out_with, require_enabled=True)
        self.assertEqual(check1["status"], "PASS")

        report_off = apply_transitions(out_with, out_without, policy={"enabled": False})
        self.assertEqual(report_off["transitions_removed"], 3)
        self.assertEqual(report_off["transitions_applied"], 0)

        check2 = check_transitions(out_without, require_enabled=True)
        self.assertEqual(check2["status"], "FAIL")
        self.assertEqual(check2["slides_missing_transition"], 3)

        check2_optional = check_transitions(out_without, require_enabled=False)
        self.assertEqual(check2_optional["status"], "PASS")

    def test_element_order_adherence(self):
        out_path = Path(self.tmp_dir) / "ordered.pptx"
        apply_transitions(self.pptx_path, out_path)

        prs = Presentation(str(out_path))
        for slide in prs.slides:
            tags = [child.tag.split("}")[-1] for child in slide._element]
            self.assertIn("transition", tags)
            trans_idx = tags.index("transition")
            # All preceding tags must be in ('cSld', 'clrMapOvr')
            for tag in tags[:trans_idx]:
                self.assertIn(tag, ("cSld", "clrMapOvr"))
            # All subsequent tags must be in ('timing', 'extLst')
            for tag in tags[trans_idx + 1:]:
                self.assertIn(tag, ("timing", "extLst"))

    def test_libreoffice_conversion(self):
        lo_bin = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
        if not lo_bin.is_file():
            self.skipTest("LibreOffice not installed on this machine")

        out_path = Path(self.tmp_dir) / "for_lo.pptx"
        apply_transitions(self.pptx_path, out_path, policy={"enabled": True, "family": "fade"})

        pdf_out_dir = Path(self.tmp_dir) / "pdf_out"
        pdf_out_dir.mkdir(parents=True, exist_ok=True)
        res = subprocess.run(
            [str(lo_bin), "--headless", "--convert-to", "pdf", str(out_path), "--outdir", str(pdf_out_dir)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, f"LibreOffice failed: {res.stderr}")
        expected_pdf = pdf_out_dir / "for_lo.pdf"
        self.assertTrue(expected_pdf.is_file(), "Expected converted PDF file")


if __name__ == "__main__":
    unittest.main()
