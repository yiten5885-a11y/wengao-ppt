"""wengao_ppt.media.mask: 蒙版资产规范与工具函数（W1 域与 W2 版式对接）。

蒙版资产规范（Full Bleed with Mask）：
1. 目的：全出血图片覆盖整个画布（13.333in x 7.5in）时，原图明暗与色彩不可控，
   必须叠加半透明蒙版层以满足 WCAG 对比度标准（正文 ≥ 4.5:1，标题 ≥ 3.0:1）。
2. 规范定义：
   - scrim_solid: 纯色半透明遮罩，全画幅覆盖，深色填充（#000000 或主题暗色），
     alpha 默认 55%（55000）。
   - scrim_bottom_gradient: 底部文字区渐变蒙版，保护底部标题与图注。
3. 接口：
   - apply_bleed_mask(slide, rect, kind="solid_dark", alpha_pct=55): 在幻灯片上绘制蒙版层。
"""
from __future__ import annotations

from typing import Any

from pptx.enum.shapes import MSO_SHAPE
from pptx.oxml.ns import qn
from pptx.util import Emu
from pptx.dml.color import RGBColor


def apply_bleed_mask(
    slide: Any,
    rect: Any,
    kind: str = "solid_dark",
    alpha_pct: int = 55,
    rgb_hex: str = "000000",
) -> Any:
    """在图片之上、文字之下添加蒙版层。

    Args:
        slide: python-pptx Slide 对象。
        rect: engine.Rect 区域。
        kind: 'solid_dark' 或 'gradient_dark'。
        alpha_pct: 遮罩不透明度百分比 (0-100)，默认 55%。
        rgb_hex: 蒙版颜色，默认黑色 '000000'。

    Returns:
        蒙版 Shape 对象。
    """
    shp = slide.shapes.add_shape(
        MSO_SHAPE.RECTANGLE,
        Emu(rect.x),
        Emu(rect.y),
        Emu(rect.w),
        Emu(rect.h),
    )
    shp.name = f"w1:mask_{kind}"
    shp.fill.solid()
    shp.fill.fore_color.rgb = RGBColor.from_string(rgb_hex.lstrip("#"))
    shp.line.fill.background()
    shp.shadow.inherit = False

    # 设置 XML 级别的透明度 (a:alpha: 100000 表示 100% 不透明，55000 表示 55% 不透明)
    spPr = shp._element.spPr
    solidFill = spPr.find(qn("a:solidFill"))
    if solidFill is not None:
        clr = solidFill.find(qn("a:srgbClr"))
        if clr is not None:
            # 清除既有 alpha 标签
            for old in clr.findall(qn("a:alpha")):
                clr.remove(old)
            # 添加新 alpha 标签
            from pptx.oxml import parse_xml
            alpha_val = int(alpha_pct * 1000)
            al = parse_xml(f'<a:alpha xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" val="{alpha_val}"/>')
            clr.append(al)

    return shp
