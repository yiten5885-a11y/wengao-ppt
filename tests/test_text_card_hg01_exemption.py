"""Unit tests for the HG01 content-conservation exemption for scene
text-card fallbacks (_text_card_sanctioned in scripts/layout_gate.py).

Context: _draw_text_card (wengao_ppt/semantic_scenes.py) adds a new,
text-bearing shape to a slide when no verifiable licensed photo could be
found for a scene (allow_text_card_fallback). The layout hard gate's HG01
check otherwise flags any text-bearing shape absent from the baseline as
TEXT_CONTENT_CHANGED, which blocked every deck containing even one
disclosed text-card fallback (job-43129b77c5b9, pages 6 and 8). The card
shape is named "WENGAO_TEXTCARD|<scene_key>" so HG01 can recognize and
sanction this specific, disclosed addition without weakening the general
content-conservation check for anything else.
"""
import unittest

from scripts.layout_gate import _TEXT_CARD_DISCLOSURE, _text_card_sanctioned


class _FakeShape:
    def __init__(self, name):
        self.name = name


class TestTextCardHG01Exemption(unittest.TestCase):

    def test_sanctioned_named_shape_with_disclosure_line(self):
        shape = _FakeShape("WENGAO_TEXTCARD|survivor_benefits_support")
        text = f"Scene Label\nsubject · action · setting\n{_TEXT_CARD_DISCLOSURE}"
        self.assertTrue(_text_card_sanctioned(shape, text))

    def test_rejects_shape_without_text_card_name(self):
        shape = _FakeShape("Rounded Rectangle 3")
        text = f"Scene Label\n{_TEXT_CARD_DISCLOSURE}"
        self.assertFalse(_text_card_sanctioned(shape, text))

    def test_rejects_text_card_named_shape_with_tampered_text(self):
        # The name alone must not be enough: an attacker (or a bug) renaming
        # an arbitrary shape to the sanctioned prefix must not bypass HG01
        # unless the shape's text is still the exact fixed disclosure line.
        shape = _FakeShape("WENGAO_TEXTCARD|survivor_benefits_support")
        text = "Totally different smuggled-in content"
        self.assertFalse(_text_card_sanctioned(shape, text))

    def test_rejects_empty_name(self):
        shape = _FakeShape("")
        text = _TEXT_CARD_DISCLOSURE
        self.assertFalse(_text_card_sanctioned(shape, text))


if __name__ == "__main__":
    unittest.main()
