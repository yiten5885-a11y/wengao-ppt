"""Regression tests for two bugs found live on job-0634b837b776 (page 7):

1. _excerpt_matches (wengao_ppt/semantic_scenes.py) required an exact
   whitespace-normalized substring match. _source_pages joins separately
   positioned on-slide text lines with "\\n", which _norm then collapses to
   a literal space. Chinese text has no real inter-word spaces, so a model
   reading the rendered slide legitimately quotes text that spans such a
   line break without that artificial space — and was rejected as
   "not grounded" even though it is a faithful, contiguous quote.

2. _PAGE_EVIDENCE_NOT_GROUNDED_RE (used by _repair_page_coverage_evidence to
   parse the page number back out of a SCENE.PAGE_EVIDENCE_NOT_GROUNDED
   error so it can attempt a repair) never matched the no_scene branch's
   actual message, which has a literal space on each side of "no_scene"
   ("...页的 no_scene 证据...") that the regex's alternation didn't allow
   for. The repair silently no-op'd (returned False) every time, so this
   "repairable" error was never actually repaired in production.
"""
import re
import unittest

from wengao_ppt.semantic_scenes import (_PAGE_EVIDENCE_NOT_GROUNDED_RE,
                                        _excerpt_matches)

# The exact page text captured live from job-0634b837b776's page 7: three
# separately positioned on-slide text lines joined with "\n" by
# _source_pages, forming one visually continuous sentence plus a second,
# unrelated sentence and a footer line.
_PAGE_7_TEXT = (
    "汇率的数字，\n终由生产力书写。\n"
    "新质生产力，才是人民币真正的“锚”。\n"
    "宏观观察 · 短视频配套文稿"
)


class TestCJKExcerptGrounding(unittest.TestCase):

    def test_quote_spanning_a_joined_line_break_is_grounded(self):
        # The model's real, legitimate quote from this job: continuous text
        # that happens to span the "\n" _source_pages inserted between two
        # separately positioned on-slide shapes.
        self.assertTrue(_excerpt_matches(_PAGE_7_TEXT, "汇率的数字，终由生产力书写。"))

    def test_quote_within_a_single_line_still_grounds(self):
        self.assertTrue(_excerpt_matches(_PAGE_7_TEXT, "新质生产力，才是人民币真正的“锚”。"))

    def test_unrelated_text_is_not_grounded(self):
        self.assertFalse(_excerpt_matches(_PAGE_7_TEXT, "completely unrelated text"))

    def test_latin_word_boundary_space_still_required(self):
        # The CJK collapse must never bridge two Latin words across a real
        # word-boundary space — only CJK-adjacent whitespace collapses.
        source = "alpha\nbeta"
        self.assertFalse(_excerpt_matches(source, "alphabeta"))
        self.assertTrue(_excerpt_matches(source, "alpha beta"))


class TestPageEvidenceNotGroundedRegex(unittest.TestCase):

    def test_matches_no_scene_branch_message_with_spaces(self):
        detail = "真实模型第 7 页的 no_scene 证据不在该页原文中。"
        match = _PAGE_EVIDENCE_NOT_GROUNDED_RE.search(detail)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "7")

    def test_matches_scene_ledger_branch_message_without_spaces(self):
        detail = "真实模型第 12 页的场景账本证据不在该页原文中。"
        match = _PAGE_EVIDENCE_NOT_GROUNDED_RE.search(detail)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "12")


if __name__ == "__main__":
    unittest.main()
