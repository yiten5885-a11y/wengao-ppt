"""W2-D3　流程构建器（原生图形，不再是编号列表）。

横向：CHEVRON 箭头形串联（步骤顺序不可调换——顺序由调用方数组保证）；
纵向：圆角矩形 + 向下箭头连接线。步骤文字 = body 档（26pt）会太大，
按硬门禁 chart_auxiliary（18–20pt）承载图元标签，正文说明仍用 body。
"""
from __future__ import annotations

from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Emu, Pt

from ..layout import tokens

_STEP_PT = int(tokens.TYPE_ROLES["chart_aux"].pt) + 2   # 20pt
_GAP = int(0.10 * tokens.EMU_PER_IN)


def _hex(theme: tokens.Theme, key: str) -> RGBColor:
    return RGBColor.from_string(theme.roles()[key].lstrip("#"))


def _style_shape(shape, theme: tokens.Theme, *, fill_key: str, text: str,
                 font_pt: int, bold: bool = False, color_key: str = "ink"):
    shape.fill.solid()
    shape.fill.fore_color.rgb = _hex(theme, fill_key)
    shape.line.color.rgb = _hex(theme, "hairline")
    shape.line.width = Pt(tokens.DECOR_RULES["hairline_pt"])
    shape.shadow.inherit = False
    tf = shape.text_frame
    tf.word_wrap = True
    tf.text = str(text)
    p = tf.paragraphs[0]
    p.alignment = 2  # PP_ALIGN.CENTER
    for run in p.runs:
        run.font.size = Pt(font_pt)
        run.font.bold = bold
        run.font.color.rgb = _hex(theme, color_key)
        tokens.apply_font_slots(run.font, tokens.script_for_text(str(text)))


def add_process(slide, rect, steps: list[str], theme: tokens.Theme, *,
                direction: str = "h"):
    """添加流程图。steps 顺序即呈现顺序。返回 shape 列表。"""
    if len(steps) < 2:
        raise ValueError("流程至少 2 步")
    shapes = []
    if direction == "h":
        n = len(steps)
        gap = _GAP * (n - 1)
        w = (rect.w - gap) // n
        x = rect.x
        y = rect.y + (rect.h - min(rect.h, int(0.9 * tokens.EMU_PER_IN))) // 2
        h = min(rect.h, int(0.9 * tokens.EMU_PER_IN))
        for i, s in enumerate(steps):
            shp = slide.shapes.add_shape(MSO_SHAPE.CHEVRON, Emu(x), Emu(y),
                                         Emu(w), Emu(h))
            shp.name = "w2:chart_aux"
            _style_shape(shp, theme, fill_key="substrate" if i % 2 == 0 else "paper",
                         text=s, font_pt=_STEP_PT)
            shapes.append(shp)
            x += w + _GAP
    elif direction == "v":
        n = len(steps)
        gap = _GAP * (n - 1)
        h = (rect.h - gap) // n
        w = min(rect.w, int(4.2 * tokens.EMU_PER_IN))
        x = rect.x + (rect.w - w) // 2
        y = rect.y
        for i, s in enumerate(steps):
            shp = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Emu(x), Emu(y),
                                         Emu(w), Emu(h))
            shp.name = "w2:chart_aux"
            _style_shape(shp, theme, fill_key="substrate", text=s, font_pt=_STEP_PT)
            shapes.append(shp)
            if i < n - 1:
                arrow = slide.shapes.add_shape(
                    MSO_SHAPE.DOWN_ARROW, Emu(x + w // 2 - int(0.06 * tokens.EMU_PER_IN)),
                    Emu(y + h), Emu(int(0.12 * tokens.EMU_PER_IN)), Emu(_GAP))
                arrow.fill.solid()
                arrow.fill.fore_color.rgb = _hex(theme, "neutral")
                arrow.line.fill.background()
                arrow.shadow.inherit = False
                shapes.append(arrow)
            y += h + _GAP
    else:
        raise ValueError("direction 只能是 h 或 v")
    return shapes
