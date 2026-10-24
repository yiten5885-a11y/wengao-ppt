#!/usr/bin/env python3
"""页级定向布局修复（P2/P6/P8）——工作台管线的内建修复，不再只存在于构建脚本。

来源：full4/full5/full6 已验证的三项修复（监督方 02:45 前后的目视缺陷），
本轮（03:27 督改）移植进候选包并对**真实原稿**生效。

设计原则：
- **检测式**：每条修复先检测缺陷几何（叠压/遮挡）是否真实存在，存在才动手；
  对本就正常的版面（如原稿 P6）是 no-op，同一代码对两种输入都安全；
- **不改文字**：只动位置/尺寸/字号，内容守恒由 validate 独立复核；
- **可审计**：每个动作写进 acts（rule + 页码 + 明细），随 visual_plan.json 落盘。

三项修复：
- fix_p2  指标卡白字折行压来源行：等宽重排 + 真实字体度量 + 按需加高；
- fix_p6  右列 bullet 文本同位叠压（中间稿遗留；原稿本就正常 → no-op）：
          检测到叠压时按列内圆点位置重排文本；
- fix_p8  引用条整块盖住 05 卡（原稿真实存在）：检测到覆盖才移到左下空区。
"""
from __future__ import annotations

from .visual import _in, is_text_shape, real_text_h_in, reflow_cards

IN = 914400.0

#: 计划器保留区：修复完成后这些矩形被文字占用，配图不得进入（页码 → [rect]）
RESERVED_RECTS = {8: [(0.9, 5.55, 6.3, 1.3)]}

_P2_CARDS = ["Shape 3", "Shape 7", "Shape 11", "Shape 15"]


def _intersects(a, b) -> bool:
    al, at, aw, ah = a
    bl, bt, bw, bh = b
    return (min(al + aw, bl + bw) - max(al, bl)) > 0.05 and \
           (min(at + ah, bt + bh) - max(at, bt)) > 0.05


def fix_p2(slide, acts):
    """P2 指标卡：等宽重排 + 标签 10.5→12pt + 真实度量防折行压来源。

    4 张卡名全部存在才动手（其他版面 no-op）。
    """
    names = {sh.name for sh in slide.shapes}
    if not all(n in names for n in _P2_CARDS):
        return
    reflow_cards(slide, _P2_CARDS, left=0.72, right=12.67, gap=0.25,
                 pad=0.14, set_pt={10.5: 12.0}, max_bottom=4.1,
                 acts=acts, page=2)
    if acts is not None:
        acts.append({"rule": "fix_p2", "page": 2, "cards": _P2_CARDS})


def fix_p6(slide, acts):
    """P6 右列 bullet：检测同位叠压（≥2 条文本 top 差 <0.06in）才重排。

    重排方式：列内小圆点按 top 排序与文本一一配对，文本 top = 圆点 top-0.08
    （与原稿几何一致的偏移）。原稿 bullet 本就分离 → 检测不命中 → no-op。
    """
    bullets, dots = [], []
    for sh in slide.shapes:
        l, t = _in(sh.left), _in(sh.top)
        w, h = _in(sh.width), _in(sh.height)
        if l < 6.8 or not (2.0 < t < 5.6):
            continue
        if is_text_shape(sh) and sh.text_frame.text.strip() and h < 0.45:
            bullets.append(sh)
        elif not is_text_shape(sh) and w < 0.2 and h < 0.2:
            dots.append(sh)
    if len(bullets) < 3 or len(dots) < len(bullets):
        return
    tops = sorted(round(_in(b.top), 2) for b in bullets)
    stacked = any(tops[i + 1] - tops[i] < 0.06 for i in range(len(tops) - 1))
    if not stacked:
        return
    from pptx.util import Inches
    bullets.sort(key=lambda s: _in(s.top))
    dots.sort(key=lambda s: _in(s.top))
    moved = 0
    for b, d in zip(bullets, dots):
        nt = round(_in(d.top) - 0.08, 3)
        if abs(_in(b.top) - nt) > 0.01:
            b.top = Inches(nt)
            moved += 1
    if acts is not None:
        acts.append({"rule": "fix_p6_unstack", "page": 6,
                     "stacked": len(bullets), "moved": moved})


def fix_p8(slide, acts):
    """P8 引用条：检测到「宽引用框盖住带两位编号的卡片」才移到左下空区。

    原稿 Shape 28 [4.95,4.10,7.00,1.25] 与 05 卡 [4.95,4.10,3.55,1.25] 完全
    重合 → 触发；目的区 (0.9,5.55,6.3,1.3) 在页脚带之上、卡区之下，为空。
    """
    shapes = list(slide.shapes)
    quote = next((s for s in shapes if s.name == "Shape 28"), None)
    txt = next((s for s in shapes if s.name == "Text 30"), None)
    mark = next((s for s in shapes if s.name == "Text 29"), None)
    if quote is None or txt is None:
        return
    q = (_in(quote.left), _in(quote.top), _in(quote.width), _in(quote.height))
    if q[2] < 5.5:            # 引用框特征不符
        return
    covers_card = False
    for s in shapes:
        if s is quote or is_text_shape(s):
            continue
        w, h = _in(s.width), _in(s.height)
        if 3.0 <= w <= 4.2 and 1.0 <= h <= 1.5 and _intersects(q, (_in(s.left), _in(s.top), w, h)):
            covers_card = True
            break
    if not covers_card:
        return
    from pptx.util import Inches
    quote.left, quote.top = Inches(0.9), Inches(5.55)
    quote.width, quote.height = Inches(6.3), Inches(1.3)
    if mark is not None:
        mark.left, mark.top = Inches(1.1), Inches(5.68)
    txt.left, txt.top = Inches(1.48), Inches(5.78)
    txt.width = Inches(5.5)
    txt.height = Inches(round(real_text_h_in(txt.text_frame.text, 15.0, 5.44), 3))
    if acts is not None:
        acts.append({"rule": "fix_p8_move_quote", "page": 8,
                     "from": [round(v, 2) for v in q],
                     "to": [0.9, 5.55, 6.3, 1.3]})


#: 页码 → 修复函数（visual_deck 的 fixes 钩子签名：f(slide, acts)）
PAGE_FIXES = {2: fix_p2, 6: fix_p6, 8: fix_p8}
