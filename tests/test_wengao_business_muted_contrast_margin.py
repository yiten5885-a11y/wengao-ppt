"""Regression test for a real pipeline finding on job-mt-56ebac59 /
job-51d167cddbc1 (2026-11-02): wengao-business's muted/subtitle color
(5B6B7A, nominal 5.48:1 on white) was chosen with what the theme's own
comment called a rendering-anti-aliasing safety margin over the 4.5:1 HG09
floor — but a real generated deck's layout_gate re-check measured actual
rendered contrast as low as 3.17:1 on narrow full-width CJK punctuation
("（"/"）") in an 11pt "来源与限定" caption using that color, 30 HG09
findings across 2 pages. The nominal 5.48:1 margin was not nearly enough for
thin strokes at that size.

muted/subtitle moved to 3A4855 (nominal 9.38:1), extrapolated from the real
measured degradation ratio (5.48 -> 3.17, ~58% retained) rather than a
guess: at that retention rate 9.38:1 nominal should still clear ~5.4:1
rendered, a real margin above the 4.5 floor. This test pins the nominal
math any future palette edit must not quietly regress below; the real proof
remains a fresh layout_gate re-check on an actual generated deck, not this
test alone.
"""
import unittest

from wengao_ppt import theme as theme_mod
from wengao_ppt.backdrop import contrast_ratio, hex_to_rgb

WHITE = (255, 255, 255)


class TestWengaoBusinessMutedContrastMargin(unittest.TestCase):

    def test_muted_color_moved_off_the_real_failing_value(self):
        colors = theme_mod.THEMES["wengao-business"]["colors"]
        self.assertNotEqual(colors["muted"], "5B6B7A")
        self.assertNotEqual(colors["subtitle"], "5B6B7A")

    def test_muted_and_subtitle_clear_a_real_safety_margin(self):
        colors = theme_mod.THEMES["wengao-business"]["colors"]
        for key in ("muted", "subtitle"):
            ratio = contrast_ratio(hex_to_rgb(colors[key]), WHITE)
            # 9.0, not 4.5: the bare HG09 floor is exactly what the previous
            # 5.48:1 value already nominally cleared and still failed real
            # rendering on thin glyphs, so the regression bar here is the
            # much larger, evidence-derived margin, not the floor itself.
            self.assertGreaterEqual(ratio, 9.0,
                                    f"{key}={colors[key]} dropped below the margin "
                                    "established after the real HG09 failure")

    def test_muted_and_subtitle_stay_in_sync(self):
        colors = theme_mod.THEMES["wengao-business"]["colors"]
        self.assertEqual(colors["muted"], colors["subtitle"])

    def test_version_bumped_for_the_palette_change(self):
        # 1.2.0：主色由 2F5B7C 移到钢青 175A63、中文字体 Songti→STHeiti、
        # 拉丁字体 Arial→Helvetica Neue、正文 16→17pt（用户"6个主题6个风格"）。
        # 1.3.0：再加 furniture=grid（左竖轨 + 左上/右下短带 + 右缘刻度）——
        # 构件真的改变了每页的输出字节，所以同样必须动版本号。
        # 2.0.0（2026-11-05）：主题整体升级为装帧语言——配色换成深松绿/黄铜的
        # 成套调色板、家具从 4 条扩到 15 条（瑞士网格）、表格/图表引入
        # table_style/chart_style 令牌。全部进 config_digest。
        # 配色或构件一改就必须动版本号，否则同一 theme_id 的两个不同外观会共享
        # 同一个版本标识，历史任务里冻结的身份就说不清自己对的是哪一版。
        self.assertEqual(theme_mod.THEMES["wengao-business"]["version"], "2.0.0")


if __name__ == "__main__":
    unittest.main()
