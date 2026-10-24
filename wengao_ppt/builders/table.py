"""W2-D2　原生表格构建器。

- python-pptx `add_table`（graphicFrame 原生表格，可编辑）；
- 字号 = table 档（20pt），表头加粗、底色 substrate；行高按内容估算；
- 列宽按比例分配（调用方给 ratios 或均分）；
- 回环校验：`wengao_ppt.tables.extract_tables_pptx` 抽出后逐格核对。
"""
from __future__ import annotations

from pptx.dml.color import RGBColor
from pptx.util import Emu, Pt

from ..layout import tokens

_T_PT = int(tokens.TYPE_ROLES["table"].pt)
_H_PT = int(tokens.TYPE_ROLES["table"].pt)


def _hex(theme: tokens.Theme, key: str) -> RGBColor:
    return RGBColor.from_string(theme.roles()[key].lstrip("#"))


def add_native_table(slide, rect, headers: list[str], rows: list[list],
                     theme: tokens.Theme, *, col_ratios: list[float] | None = None,
                     header: bool = True):
    """添加原生表格。返回 graphicFrame。

    headers/rows 的内容只来自调用方（台账 / 表格块 payload），构建器不补格、
    不加合计行。行数 > page_catalog 容量上限由调用方（模板/规划器）负责分页。
    """
    n_rows, n_cols = len(rows) + (1 if header else 0), len(headers)
    if n_rows < 1 or n_cols < 1:
        raise ValueError("表格至少要有 1 行 1 列")
    gf = slide.shapes.add_table(n_rows, n_cols, Emu(rect.x), Emu(rect.y),
                                Emu(rect.w), Emu(rect.h))
    gf.name = "w2:table"          # 表头/表体均在 20pt（table 档）
    tbl = gf.table
    if col_ratios:
        if len(col_ratios) != n_cols:
            raise ValueError("col_ratios 长度必须等于列数")
        s = sum(col_ratios)
        for i, r in enumerate(col_ratios):
            tbl.columns[i].width = Emu(int(rect.w * r / s))
    body_color = _hex(theme, "body")
    for ci, h in enumerate(headers):
        cell = tbl.cell(0, ci)
        cell.text = str(h)
        p = cell.text_frame.paragraphs[0]
        for run in p.runs:
            run.font.size = Pt(_H_PT)
            run.font.bold = True
            run.font.color.rgb = _hex(theme, "ink")
            tokens.apply_font_slots(run.font, tokens.script_for_text(str(h)))
        if header:
            cell.fill.solid()
            cell.fill.fore_color.rgb = _hex(theme, "substrate")
    ri0 = 1 if header else 0
    for ri, row in enumerate(rows):
        for ci in range(n_cols):
            val = row[ci] if ci < len(row) else ""
            cell = tbl.cell(ri + ri0, ci)
            cell.text = str(val)
            for run in cell.text_frame.paragraphs[0].runs:
                run.font.size = Pt(_T_PT)
                run.font.color.rgb = body_color
                tokens.apply_font_slots(run.font, tokens.script_for_text(str(val)))
    return gf
