"""主题家具判据：六个主题必须是六种**页面构成**，且不得挡内容、不得踩门禁。

背景（2026-11-04）：用户两次要求"6 个主题 6 个风格"。第一轮只改了配色/字体，
把同一份中性稿渲染出来，六张首页仍是"一行标题 + 大片空白"——换色不是换风格。
本轮给每个换肤主题加了 `furniture`（页面构件），这个文件把"确有构成差异"和
"构件安全"写成可执行判据。

三类断言：
1. **注册表**：每个换肤主题都声明构件；自适应主题**不声明**（它的风格就是不换肤）；
   声明合法（粗细、区间、颜色令牌）；构件进 `config_digest`。
2. **构成互异**：任取两套主题，构件签名不同，且差异不止"少了一条"——
   边分布 / 条数 / 粗细三项里至少两项不同。
3. **安全**：所有构件细长（HG09 EMPTY_CONTAINER 的豁免条件）、只落在内容之外、
   压到形状树最底层、可复现；页面被内容铺满时**放弃**而不是硬画。
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pptx import Presentation          # noqa: E402
from pptx.util import Emu, Inches      # noqa: E402

from wengao_ppt import furniture as F  # noqa: E402
from wengao_ppt import theme as T      # noqa: E402

IN = 914400.0
APPLIED = [tid for tid, t in T.THEMES.items() if not t.get("adaptive")]


def _deck(path: Path, pages: int = 2, full_bleed: bool = False) -> Path:
    """极简可复现的测试稿：每页一个标题框（可选：铺满整页的白底矩形）。"""
    from pptx.enum.shapes import MSO_SHAPE
    prs = Presentation()
    prs.slide_width = Emu(int(13.333 * IN))
    prs.slide_height = Emu(int(7.5 * IN))
    blank = prs.slide_layouts[6]
    for i in range(pages):
        s = prs.slides.add_slide(blank)
        if full_bleed:
            bg = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Emu(0), Emu(0),
                                    prs.slide_width, prs.slide_height)
            bg.name = "full-bleed-bg"
        tb = s.shapes.add_textbox(Inches(1.0), Inches(2.6), Inches(11.3), Inches(1.2))
        tb.name = f"title-{i + 1}"
        tb.text_frame.text = f"第 {i + 1} 页标题"
    prs.save(str(path))
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _signature(tid: str) -> dict:
    ident = T.resolve_theme(tid)
    spec = F.spec_of(ident)
    parts = ident["params"]["furniture"]["parts"]
    return {
        "id": spec["id"],
        "count": len(parts),
        "edges": tuple(sorted(p["edge"] for p in parts)),
        "thickness": tuple(sorted(round(float(p["thickness_in"]), 3) for p in parts)),
        # 跨度也是构成的一部分：business 的顶带只占左侧 34%、dark 的顶带通栏，
        # 两者条数与边分布可以相同，版式语言却完全不同。少了这条轴，
        # 判据会把"L 形角标"和"闭合边框"误判成同一套。
        "spans": tuple(sorted(
            (p["edge"], round(float(p.get("span", (0.0, 1.0))[0]), 2),
             round(float(p.get("span", (0.0, 1.0))[1]), 2)) for p in parts)),
        "boxes": tuple(sorted(
            (q.edge, round(q.x0, 2), round(q.y0, 2), round(q.x1, 2), round(q.y1, 2))
            for q in F.solve(spec, 13.333, 7.5, ident["params"]["colors"]))),
    }


class FurnitureRegistryTests(unittest.TestCase):
    """注册表层面：谁有构件、声明是否合法、是否进身份摘要。"""

    def test_every_applied_theme_declares_furniture(self):
        self.assertEqual(len(APPLIED), 5, APPLIED)
        for tid in APPLIED:
            with self.subTest(theme=tid):
                spec = F.spec_of(T.resolve_theme(tid))
                self.assertTrue(spec["id"], f"{tid} 没有 furniture.id")
                self.assertGreaterEqual(len(spec["parts"]), 1,
                                        f"{tid} 声明了换肤但一条构件都没有")

    def test_adaptive_declares_none(self):
        """自适应主题的风格就是"不换肤"，给它加构件与它的定义矛盾。"""
        spec = F.spec_of(T.resolve_theme("wengao-adaptive"))
        self.assertEqual(spec, {"id": None, "parts": []})
        row = next(t for t in T.list_themes() if t["id"] == "wengao-adaptive")
        self.assertIsNone(row["furniture"]["id"])
        self.assertEqual(row["furniture"]["parts"], 0)

    def test_every_spec_is_valid(self):
        for tid in APPLIED:
            ident = T.resolve_theme(tid)
            with self.subTest(theme=tid):
                self.assertEqual(
                    F.validate(F.spec_of(ident), ident["params"]["colors"]), [])

    def test_list_themes_exposes_furniture_for_the_ui(self):
        for row in T.list_themes():
            with self.subTest(theme=row["id"]):
                self.assertIn("furniture", row)
                self.assertIn("id", row["furniture"])
                self.assertIn("parts", row["furniture"])
                declared = len((T.THEMES[row["id"]].get("furniture") or {}).get("parts") or [])
                self.assertEqual(row["furniture"]["parts"], declared)

    def test_furniture_enters_config_digest(self):
        """构件改输出，所以必须改身份；style 仍然不进（纯展示说法）。"""
        for tid in APPLIED:
            with self.subTest(theme=tid):
                cfg = T.effective_config(tid)
                self.assertIn("furniture", cfg)
                self.assertNotIn("style", cfg)

    def test_changing_one_part_changes_the_digest(self):
        tid = "wengao-basic"
        before = T.config_digest(tid)
        original = T.THEMES[tid]["furniture"]
        try:
            mutated = {"id": original["id"], "parts": [dict(p) for p in original["parts"]]}
            mutated["parts"][0]["thickness_in"] = 0.07   # 0.085 -> 0.07
            T.THEMES[tid]["furniture"] = mutated
            self.assertNotEqual(T.config_digest(tid), before,
                                "改了构件粗细却没改身份摘要——历史任务会与新输出对不上")
        finally:
            T.THEMES[tid]["furniture"] = original
        self.assertEqual(T.config_digest(tid), before, "还原后摘要必须回到原值")

    def test_invalid_declarations_fail_closed(self):
        colors = T.resolve_theme("wengao-basic")["params"]["colors"]

        def bad(part: dict) -> dict:
            return {"id": "bad", "parts": [part]}

        cases = {
            "thickness >= HG09 阈值": ({"edge": "top", "thickness_in": 0.2,
                                    "color": "primary"},
                                   "HG09"),
            "边名非法": ({"edge": "middle", "thickness_in": 0.05,
                      "color": "primary"}, "edge"),
            "区间倒挂": ({"edge": "top", "thickness_in": 0.05, "span": [0.8, 0.2],
                      "color": "primary"}, "span"),
            "颜色不是调色板令牌": ({"edge": "top", "thickness_in": 0.05,
                            "color": "#123456"}, "调色板"),
        }
        for label, (part, needle) in cases.items():
            with self.subTest(case=label):
                problems = F.validate(bad(part), colors)
                self.assertTrue(problems, f"{label} 应当被判不合法")
                self.assertTrue(any(needle in p for p in problems),
                                f"{label} 的问题描述没提到 {needle}: {problems}")

    def test_out_of_canvas_part_raises(self):
        colors = T.resolve_theme("wengao-basic")["params"]["colors"]
        # offset 7.48 + 粗细 0.05 = 7.53in > 画布 7.50in
        spec = {"id": "edge-case",
                "parts": [{"edge": "top", "offset_in": 7.48, "thickness_in": 0.05,
                           "color": "primary"}]}
        self.assertEqual(F.validate(spec, colors), [])   # 声明本身合法
        with self.assertRaises(F.FurnitureError):        # 但解算后越界
            F.solve(spec, 13.333, 7.5, colors)


class SixCompositionsTests(unittest.TestCase):
    """六套构成必须真的互不相同——不是"少一条线"式的差别。"""

    def test_pairwise_signatures_differ(self):
        sigs = {tid: _signature(tid) for tid in APPLIED}
        seen: dict[tuple, str] = {}
        for tid, sig in sigs.items():
            key = sig["boxes"]
            self.assertNotIn(key, seen, f"{tid} 与 {seen.get(key)} 的构件几何完全相同")
            seen[key] = tid

    def test_pairs_differ_on_at_least_two_axes(self):
        """条数 / 边分布 / 粗细集合 / 跨度 —— 四项里至少两项不同。

        只比"条数"太弱：3 条全在顶部和 3 条围一圈是两种完全不同的版式语言。
        只比"边分布"也不够：business 与 dark 都用齐四条边，但前者是"L 形角标 +
        短带"，后者是"通栏上下 + 半高左右"的闭合框——差别在跨度上。
        """
        sigs = {tid: _signature(tid) for tid in APPLIED}
        tids = sorted(sigs)
        for i, a in enumerate(tids):
            for b in tids[i + 1:]:
                with self.subTest(pair=f"{a}|{b}"):
                    axes = sum((
                        sigs[a]["count"] != sigs[b]["count"],
                        sigs[a]["edges"] != sigs[b]["edges"],
                        sigs[a]["thickness"] != sigs[b]["thickness"],
                        sigs[a]["spans"] != sigs[b]["spans"],
                    ))
                    self.assertGreaterEqual(axes, 2, f"{a} vs {b} 只差 {axes} 条轴")

    def test_at_least_one_theme_has_no_horizontal_band_at_top(self):
        """防止六套退化成"顶部一条带的不同粗细"。"""
        def top_full(tid):
            return any(p["edge"] == "top" and float(p.get("offset_in", 0)) < 0.01
                       and p.get("span", (0, 1))[0] <= 0.001
                       and p.get("span", (0, 1))[1] >= 0.999
                       for p in T.THEMES[tid]["furniture"]["parts"])
        self.assertLess(sum(top_full(t) for t in APPLIED), len(APPLIED),
                        "所有主题都在顶部画通栏带，构成没有拉开")

    def test_vertical_and_bottom_parts_are_used_somewhere(self):
        edges = {p["edge"] for tid in APPLIED
                 for p in T.THEMES[tid]["furniture"]["parts"]}
        self.assertEqual(edges, {"top", "bottom", "left", "right"},
                         f"四条边没有全部用到，实际 {sorted(edges)}")


class FurniturePlacementTests(unittest.TestCase):
    """应用层面：细长、不挡内容、压底层、可复现、该放弃时放弃。"""

    def test_parts_are_hg09_exempt_and_on_every_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = _deck(tmp / "src.pptx", pages=3)
            out = tmp / "themed.pptx"
            stats = T.apply_theme(src, out, theme_name="wengao-basic")
            fu = stats["furniture"]
            self.assertEqual(fu["pages"], 3)
            self.assertEqual(fu["pages_with_furniture"], 3)
            self.assertEqual(fu["skipped_count"], 0)
            self.assertEqual(fu["added"], 3 * fu["parts"])

            prs = Presentation(str(out))
            for pageno, slide in enumerate(prs.slides, start=1):
                marks = [s for s in slide.shapes if s.name.startswith("wg-furniture:")]
                with self.subTest(page=pageno):
                    self.assertEqual(len(marks), fu["parts"])
                    for shp in marks:
                        w = shp.width / IN
                        h = shp.height / IN
                        # HG09 EMPTY_CONTAINER 的豁免条件，逐字对齐门禁实现
                        exempt = (w < 0.15 or h < 0.15 or w * h < 0.5)
                        self.assertTrue(
                            exempt,
                            f"{shp.name} 是 {w:.2f}x{h:.2f}in 的空实底形状，"
                            "会被 layout_gate HG09 判成 EMPTY_CONTAINER")

    def test_furniture_sits_below_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = _deck(tmp / "src.pptx", pages=1)
            out = tmp / "themed.pptx"
            stats = T.apply_theme(src, out, theme_name="wengao-basic")
            prs = Presentation(str(out))
            slide = prs.slides[0]
            names = [s.name for s in slide.shapes]
            n = stats["furniture"]["parts"]
            for i in range(n):
                self.assertTrue(names[i].startswith("wg-furniture:"),
                                f"第 {i} 个形状是 {names[i]}，构件没有压到最底层")
            self.assertEqual(names[n], "title-1", f"内容不在构件之上: {names}")

    def test_never_covers_text(self):
        """构件与页面上每个既有形状都不相交（不只是标题）。"""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = _deck(tmp / "src.pptx", pages=2)
            for tid in APPLIED:
                with self.subTest(theme=tid):
                    out = tmp / f"{tid}.pptx"
                    T.apply_theme(src, out, theme_name=tid)
                    prs = Presentation(str(out))
                    for slide in prs.slides:
                        decor = [(s.left / IN, s.top / IN,
                                  (s.left + s.width) / IN, (s.top + s.height) / IN)
                                 for s in slide.shapes
                                 if s.name.startswith("wg-furniture:")]
                        content = [(s.left / IN, s.top / IN,
                                    (s.left + s.width) / IN, (s.top + s.height) / IN)
                                   for s in slide.shapes
                                   if not s.name.startswith("wg-furniture:")]
                        for d in decor:
                            for c in content:
                                overlap = (d[0] < c[2] - 0.01 and c[0] < d[2] - 0.01
                                           and d[1] < c[3] - 0.01 and c[1] < d[3] - 0.01)
                                self.assertFalse(overlap, f"{tid} 构件压住了内容: {d} vs {c}")

    def test_furniture_may_touch_itself(self):
        """角标与竖轨是一套图形，允许相接——否则 L 形角标会被自己的竖轨删掉。"""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = _deck(tmp / "src.pptx", pages=2)
            out = tmp / "business.pptx"
            stats = T.apply_theme(src, out, theme_name="wengao-business")
            fu = stats["furniture"]
            self.assertEqual(fu["skipped_count"], 0, fu["skipped"])
            self.assertEqual(fu["added"], 2 * fu["parts"])

    def test_gives_up_on_a_full_bleed_page(self):
        """页面上铺满内容时不硬画——宁可没有构件，不可压住东西。"""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = _deck(tmp / "src.pptx", pages=1, full_bleed=True)
            out = tmp / "themed.pptx"
            stats = T.apply_theme(src, out, theme_name="wengao-basic")
            fu = stats["furniture"]
            self.assertEqual(fu["added"], 0)
            self.assertGreater(fu["skipped_count"], 0)
            self.assertEqual(fu["pages_with_furniture"], 0)
            reasons = {s["reason"] for s in fu["skipped"]}
            self.assertEqual(reasons, {"overlaps_existing_shape"})
            # 放弃是记账，不是静默：每条被跳过的构件都带页面与图元序号
            for rec in fu["skipped"]:
                self.assertIn("page", rec)
                self.assertIn("part", rec)

    def test_deterministic_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = _deck(tmp / "src.pptx", pages=2)
            a, b = tmp / "a.pptx", tmp / "b.pptx"
            T.apply_theme(src, a, theme_name="wengao-minimal")
            T.apply_theme(src, b, theme_name="wengao-minimal")
            self.assertEqual(_sha(a), _sha(b), "两次套用同一主题得到了不同字节")

    def test_preserve_and_adaptive_add_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            src = _deck(tmp / "src.pptx", pages=1)
            keep = tmp / "keep.pptx"
            st = T.apply_theme(src, keep, theme_name="wengao-basic", preserve=True)
            self.assertEqual(st["theme_application"], "PRESERVE_ORIGINAL")
            self.assertNotIn("furniture", st)
            self.assertEqual(_sha(src), _sha(keep), "preserve 路径改动了字节")

            ref = tmp / "ref.pptx"
            st2 = T.apply_theme(src, ref, theme_name="wengao-adaptive")
            self.assertEqual(st2["theme_application"], "REFINED_ORIGINAL")
            self.assertNotIn("furniture", st2)


if __name__ == "__main__":
    unittest.main()
