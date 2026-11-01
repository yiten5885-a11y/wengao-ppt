"""Regression tests for two HG08 ORPHAN_CHAR_LINE false positives found
live on job-d239567879df, page 8.

1. A lone Roman numeral badge ("Ⅰ", "Ⅱ", "Ⅲ") fell through infer_roles'
   ordinal pattern (which only matched \\d{1,2} or a single A-Za-z letter)
   to the default "body" role, so the generic "orphan single-character
   line" rule treated an intentional single-character ordinal badge as if
   a sentence had awkwardly wrapped down to one stray character.
2. A large (69pt) decorative background digit "3" — role "decor" by
   definition (is_decor), always rendered as a lone oversized character —
   tripped the same rule because "decor" wasn't in its exemption list.

scripts/layout_gate.py:infer_roles and wengao_ppt/refine.py:
infer_text_roles must stay in the same role-classification standard (see
infer_text_roles' own docstring) or the gate and the font-size fitter
disagree about a shape's role; both are covered here.
"""
import unittest

from scripts.layout_gate import infer_roles


def _shape(text, max_size=None, font_size_pt=0.0, top=3.0):
    return {"text": text, "left": 1.0, "top": top, "right": 2.0, "bottom": top + 0.3,
           "max_size": max_size, "font_size_pt": font_size_pt}


class TestHG08OrdinalDecorExemptions(unittest.TestCase):

    def test_roman_numeral_badge_is_ordinal_role(self):
        shapes = [_shape("Ⅰ"), _shape("Ⅱ"), _shape("Ⅲ"), _shape("第二支柱")]
        roles = infer_roles(shapes)
        self.assertEqual(roles[0], "ordinal")
        self.assertEqual(roles[1], "ordinal")
        self.assertEqual(roles[2], "ordinal")

    def test_lowercase_roman_numeral_badge_is_ordinal_role(self):
        roles = infer_roles([_shape("ⅳ")])
        self.assertEqual(roles[0], "ordinal")

    def test_plain_digit_and_letter_ordinals_still_work(self):
        shapes = [_shape("1"), _shape("12"), _shape("A")]
        roles = infer_roles(shapes)
        self.assertEqual(roles[0], "ordinal")
        self.assertEqual(roles[1], "ordinal")
        self.assertEqual(roles[2], "ordinal")

    def test_large_decorative_digit_is_decor_role(self):
        roles = infer_roles([_shape("3", max_size=69.0)])
        self.assertEqual(roles[0], "decor")


class TestInferTextRolesKeptInSync(unittest.TestCase):

    def test_refine_ordinal_pattern_also_covers_roman_numerals(self):
        # infer_text_roles' own docstring requires it stay in the same
        # role-classification standard as infer_roles (the gate sizes runs
        # by the role it assigns, so a disagreement here means a role gets
        # fit to one range but gated against another). A real Sh wrapper
        # needs a live python-pptx shape to construct, so this checks the
        # pattern source directly rather than re-deriving a fake one.
        import inspect

        from wengao_ppt.refine import infer_text_roles
        source = inspect.getsource(infer_text_roles)
        self.assertIn(r"[Ⅰ-ⅻ]", source)


if __name__ == "__main__":
    unittest.main()
