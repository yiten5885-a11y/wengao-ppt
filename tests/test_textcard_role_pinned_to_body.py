#!/usr/bin/env python3
"""WENGAO_TEXTCARD 卡片角色恒为 body（门禁与精修两侧同口径）。

背景 job-624391145aa3 页 8：页底说明卡（top>4.2 且 ≥15 字）被"结论=底部
最靠下 body"位置启发式判成 conclusion，卡内按 ROLE_RANGE_PT["body"] 下限
（13pt）生成的辅助行随即撞 conclusion 16pt 下限 → HG07 ROLE_SIZE ×2。
修复：两侧角色推断都按名字前缀把说明卡钉成 body，且不参与二次改判。
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from wengao_ppt.refine import infer_text_roles  # noqa: E402
from wengao_ppt.refine import Sh  # noqa: E402


class _FakeShape:
    """minimal python-pptx-like shape for Sh() construction"""

    def __init__(self, name, text, left, top, width, height, sizes):
        self.name = name
        self.left, self.top = left, top
        self.width, self.height = width, height
        self.has_text_frame = True
        self._tf = _FakeTF(text, sizes)
        self.has_table = False
        self.has_chart = False
        self.is_placeholder = False
        self.shape_type = None

    @property
    def text_frame(self):
        return self._tf

    @property
    def fill(self):
        class _F:
            type = None
        return _F()


class _FakeRun:
    def __init__(self, text, pt):
        self.text = text
        self.font = _FakeFont(pt)


class _FakeFont:
    def __init__(self, pt):
        self.size = pt and __import__("pptx.util", fromlist=["Pt"]).Pt(pt)
        self.color = _FakeColor()
        self.bold = False


class _FakeColor:
    type = None

    class rgb:
        pass


class _FakePara:
    def __init__(self, text, pt):
        self.runs = [_FakeRun(text, pt)]


class _FakeTF:
    def __init__(self, text, pt):
        self.paragraphs = [_FakePara(text, pt)]

    @property
    def text(self):
        return self.paragraphs[0].runs[0].text


def _slide_page8_layout():
    """13.33x7.5 稿：页顶标题 + 页中正文 + 页底说明卡（复刻 job 页 8 拓扑）。"""
    return [
        _FakeShape("Title 1", "游戏内经济与付费", 914400, 914400, 6000000, 800000, 36.0),
        _FakeShape("Content 2", "玩家在商店购买体力后，付费曲线出现明显跃升。", 1200000, 2600000, 7000000, 900000, 18.0),
        _FakeShape("WENGAO_TEXTCARD|player_spending_diamonds",
                   "玩家购买体力\n玩家 · 购买体力 · 游戏商店\n未找到可核验的合规配图，以说明卡片代替",
                   1200000, 4600000, 5850000, 2400000, 18.0),
    ]


class TestTextCardRolePinnedToBody(unittest.TestCase):
    def test_gate_infer_roles_bottom_textcard_is_body(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "layout_gate", REPO / "scripts" / "layout_gate.py")
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)

        shapes = []
        for i, sh in enumerate(_slide_page8_layout()):
            sizes = []
            for para in sh.text_frame.paragraphs:
                for run in para.runs:
                    if run.font.size is not None:
                        sizes.append(run.font.size.pt)
            shapes.append({"name": sh.name, "text": sh.text_frame.text,
                           "left": sh.left / 914400.0, "top": sh.top / 914400.0,
                           "width": sh.width / 914400.0, "height": sh.height / 914400.0,
                           "right": (sh.left + sh.width) / 914400.0,
                           "bottom": (sh.top + sh.height) / 914400.0,
                           "max_size": max(sizes), "font_size_pt": sizes[0],
                           "bold": False, "is_title_placeholder": False,
                           "table_native": False, "chart_native": False,
                           "fill": {"kind": None}, "anchor": None})
        roles = gate.infer_roles(shapes)
        card_idx = 2
        self.assertEqual(roles[card_idx], "body",
                         f"页底说明卡必须恒为 body：{roles}")
        self.assertNotEqual(roles[card_idx], "conclusion")

    def test_refine_infer_text_roles_bottom_textcard_is_body(self):
        sw, sh_ = 13.333, 7.5
        shs = []
        for i, sh in enumerate(_slide_page8_layout()):
            shs.append(Sh(None, sh, i, sw, sh_))
        roles = infer_text_roles(shs, sw, sh_)
        card_idx = 2
        self.assertEqual(roles[card_idx], "body",
                         f"精修侧页底说明卡必须恒为 body：{roles}")
        self.assertNotEqual(roles[card_idx], "conclusion")

    def test_real_textcard_sizes_within_body_range_after_pin(self):
        """说明卡实际字号（18/13/13pt）全部落在 body 区间内。"""
        from wengao_ppt.type_scale import ROLE_RANGE_PT
        lo, hi = ROLE_RANGE_PT["body"]
        for pt in (18.0, 13.0, 13.0):
            self.assertGreaterEqual(pt, lo, f"{pt}pt 低于 body 下限 {lo}")
            self.assertLessEqual(pt, hi, f"{pt}pt 超过 body 上限 {hi}")


if __name__ == "__main__":
    unittest.main()
