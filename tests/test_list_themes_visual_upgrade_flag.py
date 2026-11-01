"""Regression test for a product issue: the workbench theme picker showed
all 6 registered themes as equal peer color-swatch cards, including
wengao-visual-dark — which doesn't just change colors, it also runs the
visual_upgrade generation branch (keyword imagery + transition/particle
animation). list_themes() previously never exposed that distinction to
the frontend at all, so there was no data to separate it on (user,
2026-11-01: "这里的6个主题不能是同一个入口").
"""
import unittest

from wengao_ppt import theme as theme_mod


class TestListThemesVisualUpgradeFlag(unittest.TestCase):

    def test_visual_upgrade_flag_present_on_every_theme(self):
        themes = theme_mod.list_themes()
        self.assertTrue(themes)
        for t in themes:
            self.assertIn("visual_upgrade", t)
            self.assertIsInstance(t["visual_upgrade"], bool)

    def test_visual_upgrade_flag_matches_the_registry(self):
        themes = {t["id"]: t for t in theme_mod.list_themes()}
        for tid, t in theme_mod.THEMES.items():
            self.assertEqual(themes[tid]["visual_upgrade"], bool(t.get("visual_upgrade")),
                             f"{tid} visual_upgrade flag disagrees with the registry")

    def test_at_least_one_theme_is_flagged_visual_upgrade(self):
        # Not a tautology about the current registry: this is the condition
        # the workbench UI's split-section rendering depends on existing at
        # all. If this ever goes to zero, the UI falls back to showing only
        # the plain-theme grid, silently losing the separation.
        themes = theme_mod.list_themes()
        self.assertTrue(any(t["visual_upgrade"] for t in themes))


if __name__ == "__main__":
    unittest.main()
