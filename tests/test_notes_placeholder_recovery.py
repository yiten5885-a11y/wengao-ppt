"""Regression test for a crash found live on job-d239567879df.

apply_plan (wengao_ppt/semantic_scenes.py) writes each scene photo's
licensed-use attribution into the slide's notes text frame — required by
the SCENE.ATTRIBUTION_NOTES_MISSING validator, not optional metadata. It
used to access `slide.notes_slide.notes_text_frame.text` directly, which
crashed with `AttributeError: 'NoneType' object has no attribute 'text'`
on job-d239567879df's uploaded source file: python-pptx's own docs say
notes_text_frame is None "if the notes master does not have a body
placeholder, or if the notes placeholder has been deleted from the notes
slide" — confirmed live, that file's notes master has zero placeholders at
all, so even python-pptx's own `clone_master_placeholders` recovery is a
no-op. _ensure_notes_text_frame handles both real-world cases: the common
one (master has a body placeholder, this slide's own copy was deleted) via
clone_master_placeholders, and the degenerate one (master has none at all)
via a placeholder borrowed from python-pptx's own bundled default
template.
"""
import unittest

from pptx import Presentation

from wengao_ppt.semantic_scenes import _ensure_notes_text_frame


class TestEnsureNotesTextFrame(unittest.TestCase):

    def test_existing_notes_text_frame_returned_unchanged(self):
        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        notes_slide = slide.notes_slide
        self.assertIsNotNone(notes_slide.notes_text_frame)
        frame = _ensure_notes_text_frame(notes_slide, prs)
        frame.text = "unchanged path still writes"
        self.assertEqual(notes_slide.notes_text_frame.text, "unchanged path still writes")

    def test_recovers_via_master_when_this_slides_placeholder_was_deleted(self):
        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        notes_slide = slide.notes_slide
        placeholder = notes_slide.notes_placeholder
        placeholder._element.getparent().remove(placeholder._element)
        self.assertIsNone(notes_slide.notes_text_frame)

        frame = _ensure_notes_text_frame(notes_slide, prs)

        self.assertIsNotNone(frame)
        frame.text = "recovered via master clone"
        self.assertEqual(notes_slide.notes_text_frame.text, "recovered via master clone")

    def test_recovers_via_donor_template_when_master_has_no_placeholders(self):
        prs = Presentation()
        # Strip every placeholder from the notes master itself — the real
        # condition found live on job-d239567879df, where
        # clone_master_placeholders has nothing to clone.
        master_tree = prs.notes_master.shapes._spTree
        for placeholder in list(prs.notes_master.placeholders):
            master_tree.remove(placeholder.element)
        self.assertEqual(list(prs.notes_master.placeholders), [])

        slide = prs.slides.add_slide(prs.slide_layouts[6])
        notes_slide = slide.notes_slide
        self.assertIsNone(notes_slide.notes_text_frame)

        frame = _ensure_notes_text_frame(notes_slide, prs)

        self.assertIsNotNone(frame)
        frame.text = "recovered via donor template"
        self.assertEqual(notes_slide.notes_text_frame.text, "recovered via donor template")


if __name__ == "__main__":
    unittest.main()
