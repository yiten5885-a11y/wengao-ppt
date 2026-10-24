"""W2-D4　时间线构建器（原生图形）。

横向：轴线 + 里程碑圆点，标签上下交替；纵向：左侧轴线 + 右侧标签。
时间点文本必须来自调用方（绝对时间或明确相对量），构建器不生成时间。
"""
from __future__ import annotations

from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN
from pptx.util import Emu, Pt

from ..layout import tokens

_LBL_PT = int(tokens.TYPE_ROLES["chart_aux"].pt)
_DOT = int(0.14 * tokens.EMU_PER_IN)


def _hex(theme: tokens.Theme, key: str) -> RGBColor:
    return RGBColor.from_string(theme.roles()[key].lstrip("#"))


def _label(slide, x, y, w, h, text, theme, align=PP_ALIGN.CENTER, bold=False):
    tb = slide.shapes.add_textbox(Emu(x), Emu(y), Emu(w), Emu(h))
    tb.name = "w2:chart_aux"
    tf = tb.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = 0
    tf.text = str(text)
    for run in tf.paragraphs[0].runs:
        run.font.size = Pt(_LBL_PT)
        run.font.bold = bold
        run.font.color.rgb = _hex(theme, "body")
        tokens.apply_font_slots(run.font, tokens.script_for_text(str(text)))
    tf.paragraphs[0].alignment = align
    return tb


def add_timeline(slide, rect, milestones: list[dict], theme: tokens.Theme, *,
                 direction: str = "h"):
    """添加时间线。milestones: [{"time": str, "label": str}, ...]，顺序即时间序。

    返回 shape 列表。构建器不排序、不补时间。
    """
    if len(milestones) < 2:
        raise ValueError("时间线至少 2 个节点")
    shapes = []
    line_c = _hex(theme, "hairline")
    dot_c = _hex(theme, "accent")
    if direction == "h":
        axis_y = rect.y + rect.h // 2
        bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Emu(rect.x + _DOT),
                                     Emu(axis_y - int(0.02 * tokens.EMU_PER_IN)),
                                     Emu(rect.w - 2 * _DOT),
                                     Emu(int(0.04 * tokens.EMU_PER_IN)))
        bar.fill.solid(); bar.fill.fore_color.rgb = line_c
        bar.line.fill.background(); bar.shadow.inherit = False
        shapes.append(bar)
        n = len(milestones)
        seg = rect.w // n
        for i, m in enumerate(milestones):
            cx = rect.x + seg * i + seg // 2 - _DOT // 2
            dot = slide.shapes.add_shape(MSO_SHAPE.OVAL, Emu(cx),
                                         Emu(axis_y - _DOT // 2), Emu(_DOT), Emu(_DOT))
            dot.fill.solid(); dot.fill.fore_color.rgb = dot_c
            dot.line.fill.background(); dot.shadow.inherit = False
            shapes.append(dot)
            above = (i % 2 == 0)
            lw = seg - int(0.06 * tokens.EMU_PER_IN)
            ly = axis_y - _DOT // 2 - int(0.75 * tokens.EMU_PER_IN) if above \
                else axis_y + _DOT // 2 + int(0.12 * tokens.EMU_PER_IN)
            _label(slide, cx - lw // 2 + _DOT // 2, ly, lw,
                   int(0.75 * tokens.EMU_PER_IN),
                   f"{m.get('time', '')}\n{m.get('label', '')}", theme,
                   bold=True)
    elif direction == "v":
        axis_x = rect.x + int(0.5 * tokens.EMU_PER_IN)
        bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,
                                     Emu(axis_x - int(0.02 * tokens.EMU_PER_IN)),
                                     Emu(rect.y), Emu(int(0.04 * tokens.EMU_PER_IN)),
                                     Emu(rect.h))
        bar.fill.solid(); bar.fill.fore_color.rgb = line_c
        bar.line.fill.background(); bar.shadow.inherit = False
        shapes.append(bar)
        n = len(milestones)
        seg = rect.h // n
        for i, m in enumerate(milestones):
            cy = rect.y + seg * i + seg // 2 - _DOT // 2
            dot = slide.shapes.add_shape(MSO_SHAPE.OVAL, Emu(axis_x - _DOT // 2),
                                         Emu(cy), Emu(_DOT), Emu(_DOT))
            dot.fill.solid(); dot.fill.fore_color.rgb = dot_c
            dot.line.fill.background(); dot.shadow.inherit = False
            shapes.append(dot)
            tb = _label(slide, axis_x + _DOT, cy - int(0.2 * tokens.EMU_PER_IN),
                        rect.x + rect.w - axis_x - _DOT,
                        int(0.55 * tokens.EMU_PER_IN),
                        f"{m.get('time', '')}　{m.get('label', '')}", theme,
                        align=PP_ALIGN.LEFT, bold=True)
            shapes.append(tb)
    else:
        raise ValueError("direction 只能是 h 或 v")
    return shapes
