"""六个主题必须是六种风格：把"风格不同"写成可执行的判据。

背景
----
用户 2026-11-01：「6个主题都要分开」；2026-11-04：「6个主题6个风格，由你完成」。
此前注册表里 basic 与 business 只差 12° 色相、同一底色、同一 Songti/Arial 配对、
字号只差 1pt——实际输出是"同一个风格换个蓝"。卡片缩略图也是同一个模板只换颜色，
等于在界面上承认这些主题是同一套东西。

这里不写形容词，写判据：任取两个主题，必须至少在 3 条轴上真的不同——
  * 主色色相（环形距离 ≥ 25°）
  * 底色明度（相对亮度比 ≥ 1.5×）
  * 中文字体配对（不同）
  * 拉丁字体配对（不同）
  * 字号层级（至少一个角色不同）
以及它们都必须满足的硬约束：对比度下限、字号落在 ROLE_RANGE_PT 内、标题/正文比 ≥1.4。

为什么是 3 条而不是 1 条：只差色相就是"换个蓝"（正是这次要修的问题）；只差字体但配色
字号全同，读者仍会觉得是同一套。3 条是"换风格"而不是"换参数"的最低门槛，阈值可复核。
"""
from __future__ import annotations

import colorsys
import itertools
import unittest

from wengao_ppt import theme as theme_mod
from wengao_ppt.backdrop import contrast_ratio, hex_to_rgb
from wengao_ppt.type_scale import ROLE_RANGE_PT, TITLE_BODY_MIN_RATIO

#: 主题字号键 → 字号体系角色
SIZE_ROLE = {"title": "title", "subtitle": "lead", "heading": "lead",
             "body": "body", "note": "footer", "chart_text": "chart_aux"}

#: 文字可能落在这些底色上（幻灯片底、面板、表格行）
PANEL_KEYS = ("surface", "table_row_bg", "table_row_alt_bg")

#: 硬下限：HG09 的可读性门槛。note（页脚/来源）按 11pt 甚至 10pt 出现，
#: 细笔画在抗锯齿下的实际衰减已由 job-51d167cddbc1 实测（名义 5.48 → 实测 3.17），
#: 所以小字单独要 6.0，而不是贴着 4.5 走。
TEXT_FLOOR = 4.5
NOTE_FLOOR = 6.0

#: 图形对象（图表系列）的非文本下限
NON_TEXT_FLOOR = 3.0

#: 判定"色相不同"的环形阈值与"明暗不同"的亮度比阈值
HUE_MIN_DEG = 25.0
LUM_RATIO_MIN = 1.5
MIN_AXES = 3

STYLE_FIELDS = ("style_id", "style_name", "genre", "tagline", "tags", "preview")
PREVIEW_KINDS = {"official", "grid", "neon", "quiet", "cinema", "source"}


def _rgb(hexstr: str):
    return hex_to_rgb(hexstr)


def _hue(hexstr: str) -> float:
    r, g, b = [c / 255.0 for c in _rgb(hexstr)]
    return colorsys.rgb_to_hls(r, g, b)[0] * 360.0


def _hue_distance(a: str, b: str) -> float:
    d = abs(_hue(a) - _hue(b)) % 360.0
    return min(d, 360.0 - d)


def _lum(hexstr: str) -> float:
    """WCAG 相对亮度（0 黑 … 1 白）。

    与黑对比度定义为 (L+0.05)/0.05，所以 L = ratio*0.05 - 0.05。用它换算而不是
    再写一遍 sRGB 分段公式：门禁（HG09）就是按 contrast_ratio 判的，两处口径必须同源。
    """
    return contrast_ratio(_rgb(hexstr), (0, 0, 0)) * 0.05 - 0.05


def _lum_ratio(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _palette_themes() -> dict:
    return {k: v for k, v in theme_mod.THEMES.items() if not v.get("adaptive")}


def _fonts(t: dict) -> tuple:
    f = t["fonts"]
    return (f["heading_ea"], f["body_ea"]), (f["heading_latin"], f["body_latin"])


def _diff_axes(a: dict, b: dict) -> list[str]:
    """两个主题实际不同的"风格轴"清单（顺序固定，便于失败信息定位）。"""
    axes = []
    ca, cb = a["colors"], b["colors"]
    if _hue_distance(ca["primary"], cb["primary"]) >= HUE_MIN_DEG:
        axes.append(f"hue({_hue_distance(ca['primary'], cb['primary']):.0f}°)")
    if _lum_ratio(a["slide_bg"], b["slide_bg"]) >= LUM_RATIO_MIN:
        axes.append(f"bg-lightness({_lum_ratio(a['slide_bg'], b['slide_bg']):.1f}x)")
    ea_a, lat_a = _fonts(a)
    ea_b, lat_b = _fonts(b)
    if ea_a != ea_b:
        axes.append("ea-font")
    if lat_a != lat_b:
        axes.append("latin-font")
    if a["sizes"] != b["sizes"]:
        axes.append("type-scale")
    return axes


class RegistryShapeTests(unittest.TestCase):
    def test_exactly_six_themes(self):
        self.assertEqual(len(theme_mod.THEMES), 6,
                         f"注册表应为 6 个主题，实际 {sorted(theme_mod.THEMES)}")

    def test_every_theme_declares_a_full_style_block(self):
        for tid, t in theme_mod.THEMES.items():
            with self.subTest(theme=tid):
                st = t.get("style")
                self.assertIsInstance(st, dict, f"{tid} 缺 style 块")
                for f in STYLE_FIELDS:
                    self.assertIn(f, st, f"{tid}.style 缺字段 {f}")
                self.assertTrue(st["style_name"].strip())
                self.assertTrue(st["tagline"].strip())
                self.assertGreaterEqual(len(st["tags"]), 3, "风格至少 3 个关键词，便于界面上区分")
                self.assertIn(st["preview"], PREVIEW_KINDS,
                              f"{tid} 的缩略图类型 {st['preview']!r} 不在已实现的集合内")

    def test_style_ids_names_and_previews_are_unique(self):
        for field in ("style_id", "style_name", "preview"):
            values = [t["style"]["style_id" if field == "style_id" else field]
                      for t in theme_mod.THEMES.values()]
            self.assertEqual(len(values), len(set(values)),
                             f"{field} 有重复：{values}")

    def test_style_metadata_is_not_part_of_the_identity_digest(self):
        """补风格标签不能改变主题身份——历史任务里冻结的 digest 必须仍然对得上。"""
        for tid in theme_mod.THEMES:
            cfg = theme_mod.effective_config(tid)
            self.assertNotIn("style", cfg, f"{tid} 的 style 混进了身份摘要")

    def test_list_themes_exposes_the_style_block(self):
        for item in theme_mod.list_themes():
            self.assertIn("style", item)
            self.assertEqual(item["style"], theme_mod.THEMES[item["id"]]["style"])


class SixStylesAreDistinctTests(unittest.TestCase):
    def test_pairwise_themes_differ_on_at_least_three_axes(self):
        themes = _palette_themes()
        weak = []
        for a, b in itertools.combinations(sorted(themes), 2):
            axes = _diff_axes(themes[a], themes[b])
            if len(axes) < MIN_AXES:
                weak.append(f"{a} vs {b}: 只有 {len(axes)} 条轴不同 {axes}")
        self.assertEqual(weak, [],
                         "主题之间的差异不足（这就是\"同一个风格换个颜色\"）：\n" +
                         "\n".join(weak))

    def test_adaptive_theme_is_a_separate_strategy_not_a_palette(self):
        t = theme_mod.THEMES["wengao-adaptive"]
        self.assertTrue(t.get("adaptive"))
        for key in ("slide_bg", "colors", "fonts", "sizes"):
            self.assertIsNone(t.get(key), f"自适应主题不应带固定 {key}")

    def test_at_least_one_theme_is_dark_and_one_is_light(self):
        """6 种风格里既要能选出深色稿，也要有浅色稿；全深或全浅都说明风格没收开。"""
        lums = [_lum(t["slide_bg"]) for t in _palette_themes().values()]
        self.assertTrue(any(l < 0.05 for l in lums), "没有真正的深色主题")
        self.assertTrue(any(l > 0.7 for l in lums), "没有真正的浅色主题")


class ReadabilityTests(unittest.TestCase):
    def test_text_roles_clear_the_contrast_floor_on_every_panel(self):
        for tid, t in _palette_themes().items():
            c = t["colors"]
            bgs = [t["slide_bg"]] + [c[k] for k in PANEL_KEYS]
            roles = {"title": c["primary"], "subtitle": c["subtitle"],
                     "heading": c["heading"], "body": c["text"], "note": c["muted"]}
            for role, fg in roles.items():
                need = NOTE_FLOOR if role == "note" else TEXT_FLOOR
                for bg in bgs:
                    with self.subTest(theme=tid, role=role, bg=bg):
                        ratio = contrast_ratio(_rgb(fg), _rgb(bg))
                        self.assertGreaterEqual(
                            ratio, need,
                            f"{tid} {role}={fg} on {bg} 只有 {ratio:.2f}:1，低于 {need}:1")

    def test_table_header_and_chart_text_clear_the_floor(self):
        for tid, t in _palette_themes().items():
            c = t["colors"]
            with self.subTest(theme=tid, part="table_header"):
                self.assertGreaterEqual(
                    contrast_ratio(_rgb(c["table_header_text"]), _rgb(c["table_header_bg"])),
                    TEXT_FLOOR, f"{tid} 表头文字压不住表头底色")
            with self.subTest(theme=tid, part="chart_text"):
                self.assertGreaterEqual(
                    contrast_ratio(_rgb(c["chart_text"]), _rgb(c["chart_bg"])),
                    TEXT_FLOOR, f"{tid} 图表文字压不住图表底色")

    def test_chart_series_clear_the_non_text_floor(self):
        for tid, t in _palette_themes().items():
            c = t["colors"]
            for i, s in enumerate(c["chart_series"]):
                with self.subTest(theme=tid, series=i):
                    ratio = contrast_ratio(_rgb(s), _rgb(c["chart_bg"]))
                    self.assertGreaterEqual(ratio, NON_TEXT_FLOOR,
                                            f"{tid} 第 {i} 个系列色 {s} 对图表底色只有 {ratio:.2f}:1")

    def test_theme_sizes_stay_inside_the_role_ranges(self):
        for tid, t in _palette_themes().items():
            for key, role in SIZE_ROLE.items():
                lo, hi = ROLE_RANGE_PT[role]
                with self.subTest(theme=tid, size=key):
                    self.assertGreaterEqual(t["sizes"][key], lo,
                                            f"{tid} {key}={t['sizes'][key]} 低于 {role} 下限 {lo}")
                    self.assertLessEqual(t["sizes"][key], hi,
                                         f"{tid} {key}={t['sizes'][key]} 高于 {role} 上限 {hi}")

    def test_title_body_ratio_keeps_the_hierarchy(self):
        for tid, t in _palette_themes().items():
            ratio = t["sizes"]["title"] / t["sizes"]["body"]
            with self.subTest(theme=tid):
                self.assertGreaterEqual(ratio, TITLE_BODY_MIN_RATIO,
                                        f"{tid} 标题/正文只有 {ratio:.2f}，层级不够")


class TableBodyTextTests(unittest.TestCase):
    """表格数据行的文字必须压得住它**自己那行的底色**。

    真实缺陷（2026-11-04 六主题对照渲染发现）：`_style_table` 的数据行文字写死
    "000000"，理由是浅色格上要给抗锯齿留最大墨/底差。这个理由只在浅色主题成立——
    深色主题的行底色是 121C2E / 18253A，纯黑压上去只有约 1.2:1，整张表的数据行
    在渲染里几乎看不见。这条测试对每个主题真跑一次 apply_theme 再量对比度。
    """

    def _deck_with_table(self, path):
        from pptx import Presentation
        from pptx.util import Inches
        prs = Presentation()
        prs.slide_width = Inches(13.333)
        prs.slide_height = Inches(7.5)
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        table = slide.shapes.add_table(3, 2, Inches(1), Inches(1), Inches(6), Inches(2)).table
        for r in range(3):
            for c in range(2):
                table.cell(r, c).text = "数据"
        prs.save(path)

    def _rgb_of(self, cell):
        for para in cell.text_frame.paragraphs:
            for run in para.runs:
                if run.font.color and run.font.color.type is not None:
                    return str(run.font.color.rgb)
        return None

    def test_body_cells_clear_the_floor_on_their_own_fill(self):
        import tempfile
        from pathlib import Path

        from pptx import Presentation

        from wengao_ppt import theme as theme_mod

        tmp = Path(tempfile.mkdtemp(prefix="theme-table-"))
        src = tmp / "src.pptx"
        self._deck_with_table(src)
        try:
            for tid in theme_mod.THEMES:
                if theme_mod.THEMES[tid].get("adaptive"):
                    continue  # 不换肤，保持原稿，不属于本类问题
                with self.subTest(theme=tid):
                    dst = tmp / f"{tid}.pptx"
                    theme_mod.apply_theme(str(src), str(dst), tid)
                    prs = Presentation(str(dst))
                    table = next(sh.table for sh in prs.slides[0].shapes if sh.has_table)
                    for r in range(1, len(table.rows)):
                        for c in range(len(table.columns)):
                            cell = table.cell(r, c)
                            fg = self._rgb_of(cell)
                            self.assertIsNotNone(fg, f"{tid} r{r}c{c} 文字色未设置")
                            fill = str(cell.fill.fore_color.rgb)
                            ratio = contrast_ratio(hex_to_rgb(fg), hex_to_rgb(fill))
                            self.assertGreaterEqual(
                                ratio, TEXT_FLOOR,
                                f"{tid} 表格 r{r}c{c}: {fg} on {fill} 只有 {ratio:.2f}:1")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_dark_themes_get_light_table_text(self):
        """反向断言：深色主题必须选到浅色文字，别"修成"浅色也不需要改的样子。"""
        from wengao_ppt import theme as theme_mod
        for tid in ("wengao-dark", "wengao-visual-dark"):
            t = theme_mod.THEMES[tid]
            bg = t["colors"]["table_row_bg"]
            fg = theme_mod._table_body_fg(bg)
            self.assertEqual(fg, "FFFFFF", f"{tid} 深底 {bg} 上应选白字，实际 {fg}")
            for light in ("wengao-basic", "wengao-business", "wengao-minimal"):
                lbg = theme_mod.THEMES[light]["colors"]["table_row_bg"]
                self.assertEqual(theme_mod._table_body_fg(lbg), "000000",
                                 f"{light} 浅底 {lbg} 上应保持黑字")


if __name__ == "__main__":
    unittest.main()
