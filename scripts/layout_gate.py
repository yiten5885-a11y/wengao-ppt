#!/usr/bin/env python3
"""WG-LAYOUT-20261021.1 排版硬门禁（HG01–HG12）——真实产品验收门禁。

与旧 glyph_gate.py 的区别（监督 01:20/02:38/08:42 要求）：
1. **字符级台账**（HG01/HG02）：每个预期非空白字符必须映射到真实渲染字形，
   unmatched / 漏检 / 重复 / 部分可见一律计数，不再把未归属行塞进 unmatched 后忽略；
2. **真实可见性**（HG02/HG04）：渲染"无字版"与完整版做像素差分——PDF 提取到文字
   ≠ 文字在最终画面可见；后画不透明容器遮字由此检出；
3. **绘制层级遮挡**（HG04）：按 spTree 真实 z-order 找后画填充容器与字形相交，
   再用像素差分确认；
4. **角色字号 / 区域净空 / 语义断行 / 序号归属 / 局部对比度**（HG06–HG09）；
5. **全页渲染 ≥1920px**（HG10）、**身份绑定**（HG11）、**N01–N08 反向回归**（HG12）；
6. **P0 2026-10-23**：HG04 遮挡候选按 HG02 逐字可见性证据三态处置（命中/
   证据化消解/保留未证）；HG13 逐页结构清单（对象/备注/媒体/动画/转场，
   磁盘重算 + 序列化哈希绑定）；身份链增加门禁自算 build_id。

状态只有 PASS / FAIL / UNVERIFIED；FAIL 与 UNVERIFIED 均阻断。任何必需证据缺失
记 UNVERIFIED，禁止默认 0。总判定 = 所有页 × 所有门禁的逻辑与。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pptx import Presentation  # noqa: E402
from pptx.util import Pt  # noqa: E402

IN = 72.0
EMU = 914400.0
SOFFICE = "/Applications/LibreOffice.app/Contents/MacOS/soffice"

# ---- WG-LAYOUT-20261021.1 冻结阈值（项目验收口径，非外部通用标准） ----
EPS_PT = 0.25                       # 浮点坐标 epsilon（pt）——不是重叠加权
FRAME_TOL_IN = 0.01                 # 字形超出属文本框的容忍（远低于旧 0.03in）
CANVAS_MARGIN_IN = {"left": 0.40, "right": 0.40, "top": 0.30, "bottom": 0.15}
REGION_GAP_IN = {"title_lead": 0.12, "lead_body": 0.16,
                 "body_conclusion": 0.18, "content_footer": 0.12}
FOOTER_ZONE_IN = {"top": 0.50, "bottom": 0.15}   # 距画布底
TEXT_PAD_MIN_IN = 0.10
TEXT_PAD_MIN_EM = 0.35
from wengao_ppt.type_scale import (
    ROLE_RANGE_PT,
    TYPE_SCALE_VERSION,
    TYPE_MIN_PT,
    TYPE_MAX_PT,
    is_decor,
    is_chart_legend_label,
    is_decor_unit_suffix,
    check_page_hierarchy,
)

ROLE_PT = ROLE_RANGE_PT  # HG07 角色字号统一引用 type_scale 单一事实来源
CONTRAST_MIN = 4.5
CONTRAST_MIN_BOLD32 = 3.0
PROTECTED_SEEDS = ["电商", "专项资金", "越多", "618", "+0.9%", "2500亿",
                   "36个月", "GMV"]
RENDER_W_PX = 1920                  # HG10
VIS_DIFF_TH = 24                    # 像素差分阈值（0-255）
VIS_RATIO_MIN = 0.80                # 字形框内"有差异"像素占比 ≥ 该值视为可见


def sha256(p) -> str:
    digest = hashlib.sha256()
    with Path(p).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- 渲染
def render_pdf(pptx: Path, outdir: Path) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    fc = ROOT / ".fontconfig/fonts.conf"
    env = dict(os.environ)
    if fc.exists():
        env["FONTCONFIG_FILE"] = str(fc)
    subprocess.run([SOFFICE, "--headless", "--norestore", "--convert-to", "pdf",
                    "--outdir", str(outdir), str(pptx)],
                   env=env, check=True, capture_output=True, timeout=600)
    out = outdir / (Path(pptx).stem + ".pdf")
    if not out.exists():
        raise SystemExit(f"[layout_gate] 渲染失败: {out}")
    return out


def render_pngs(pdf: Path, outdir: Path, tag: str, width_px: int = RENDER_W_PX):
    """HG10：每页渲染为 ≥1920px 宽 PNG，比例与画布一致。返回 {page: (path, sha)}。"""
    import pypdfium2 as pdfium
    outdir.mkdir(parents=True, exist_ok=True)
    doc = pdfium.PdfDocument(str(pdf))
    res = {}
    for i in range(len(doc)):
        page = doc[i]
        w_pt = page.get_size()[0]
        scale = width_px / w_pt
        bmp = page.render(scale=scale)
        img = bmp.to_pil().convert("RGB")
        p = outdir / f"{tag}-P{i + 1:02d}.png"
        img.save(str(p))
        res[i + 1] = (str(p), sha256(p), img.size)
    return res


def strip_text_pptx(pptx: Path, out: Path) -> Path:
    """生成像素对照用无字副本，保留图形/位置/填充与所有原生对象。

    DrawingML 表格的格内文字不在 shape.text_frame，图表文字由图表引擎绘制；
    只清空普通文本框会让这些文字留在“无字版”，制造大量不可见假阳性。
    本函数仅改独立检查副本，不修改交付候选。
    """
    import os
    import zipfile
    from lxml import etree

    pptx = Path(pptx)
    out = Path(out)
    if pptx.resolve() == out.resolve():
        raise ValueError("无字渲染副本不能覆盖原始 PPTX")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    a_ns = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    c_ns = "{http://schemas.openxmlformats.org/drawingml/2006/chart}"
    remove_bullet_tags = {a_ns + n for n in
                          ("buChar", "buAutoNum", "buFont", "buFontTx",
                           "buClr", "buClrTx", "buNone")}
    chart_text_tags = {c_ns + "title", c_ns + "legend", c_ns + "dLbls"}
    try:
        with zipfile.ZipFile(str(pptx), "r") as zin, \
                zipfile.ZipFile(str(tmp), "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                data = zin.read(info.filename)
                if re.fullmatch(r"ppt/slides/slide\d+\.xml", info.filename):
                    root = etree.fromstring(data)
                    for t in root.iter(a_ns + "t"):
                        t.text = None
                    for ppr in root.iter(a_ns + "pPr"):
                        for child in list(ppr):
                            if child.tag in remove_bullet_tags:
                                ppr.remove(child)
                        bu_none = etree.Element(a_ns + "buNone")
                        insert_at = next((i for i, child in enumerate(ppr)
                                          if child.tag in (a_ns + "defRPr", a_ns + "extLst")),
                                         len(ppr))
                        ppr.insert(insert_at, bu_none)
                    data = etree.tostring(root, encoding="UTF-8", xml_declaration=True,
                                          standalone=True)
                elif re.fullmatch(r"ppt/(?:slides/)?charts/chart\d+\.xml", info.filename):
                    root = etree.fromstring(data)
                    for parent in list(root.iter()):
                        for child in list(parent):
                            if child.tag in chart_text_tags:
                                parent.remove(child)
                    for axis in list(root.iter(c_ns + "catAx")) + list(root.iter(c_ns + "valAx")):
                        tick_pos = axis.find(c_ns + "tickLblPos")
                        if tick_pos is None:
                            tick_pos = etree.Element(c_ns + "tickLblPos")
                            insert_at = next((i for i, child in enumerate(axis)
                                              if child.tag in (c_ns + "spPr", c_ns + "txPr",
                                                               c_ns + "crossAx", c_ns + "crosses")),
                                             len(axis))
                            axis.insert(insert_at, tick_pos)
                        tick_pos.set("val", "none")
                    data = etree.tostring(root, encoding="UTF-8", xml_declaration=True,
                                          standalone=True)
                zout.writestr(info, data)
        os.replace(tmp, out)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return out


# --------------------------------------------------------------------------- 形状
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _fill_info(sp) -> dict:
    """从 spPr 提取填充：None(无填充/继承) / {'solid': (rgb, alpha)} / {'other': kind}。"""
    spPr = sp.find(f"{{http://schemas.openxmlformats.org/presentationml/2006/main}}spPr")
    if spPr is None:
        return {"kind": "unknown"}
    fill = spPr.find(f"{A}solidFill")
    if fill is not None:
        clr = fill.find(f"{A}srgbClr")
        alpha = 1.0
        if clr is not None:
            a = clr.find(f"{A}alpha")
            if a is not None:
                alpha = int(a.get("val", "100000")) / 100000.0
            rgb = clr.get("val", "000000")
        else:
            sch = fill.find(f"{A}schemeClr")
            rgb = "scheme:" + (sch.get("val", "?") if sch is not None else "?")
        return {"kind": "solid", "rgb": rgb, "alpha": alpha}
    if spPr.find(f"{A}noFill") is not None:
        return {"kind": "none"}
    for tag in ("gradFill", "blipFill", "pattFill", "grpFill"):
        if spPr.find(f"{A}{tag}") is not None:
            return {"kind": tag}
    return {"kind": "inherit"}


def _line_info(sp) -> dict:
    """提取形状轮廓：显式无轮廓可证不遮字；继承/未知轮廓仍阻断。"""
    spPr = sp.find(f"{{http://schemas.openxmlformats.org/presentationml/2006/main}}spPr")
    if spPr is None:
        return {"kind": "unknown"}
    ln = spPr.find(f"{A}ln")
    if ln is None:
        return {"kind": "inherit"}
    if ln.find(f"{A}noFill") is not None:
        return {"kind": "none", "width_emu": ln.get("w")}
    fill = ln.find(f"{A}solidFill")
    if fill is not None:
        clr = fill.find(f"{A}srgbClr")
        alpha = 1.0
        if clr is not None:
            a = clr.find(f"{A}alpha")
            if a is not None:
                alpha = int(a.get("val", "100000")) / 100000.0
            rgb = clr.get("val", "000000")
        else:
            sch = fill.find(f"{A}schemeClr")
            rgb = "scheme:" + (sch.get("val", "?") if sch is not None else "?")
        return {"kind": "solid", "rgb": rgb, "alpha": alpha,
                "width_emu": ln.get("w")}
    for child in ("gradFill", "blipFill", "pattFill"):
        if ln.find(f"{A}{child}") is not None:
            return {"kind": child, "width_emu": ln.get("w")}
    # An empty a:ln has no fill child. The DrawingML hard-coded line-fill
    # default is noFill, but only when PresentationML has no style/master
    # source that can supply the missing child. Keep those inheritance cases
    # unresolved, and treat extension content conservatively.
    p = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
    has_style = sp.find(f"{p}style") is not None
    nvpr = sp.find(f"{p}nvSpPr/{p}nvPr")
    has_placeholder = nvpr is not None and nvpr.find(f"{p}ph") is not None
    has_extensions = ln.find(f"{A}extLst") is not None
    has_fill = any(ln.find(f"{A}{tag}") is not None
                   for tag in ("noFill", "solidFill", "gradFill", "pattFill"))
    if not has_fill and not has_style and not has_placeholder and not has_extensions:
        return {"kind": "none", "width_emu": ln.get("w"),
                "basis": "drawingml_hardcoded_no_fill_no_style_or_placeholder"}
    return {"kind": "inherit", "width_emu": ln.get("w")}


def _no_fill_occluder_is_clear(line_info: dict) -> bool:
    """Only a proven no-line outline can clear a no-fill shape from HG04."""
    return isinstance(line_info, dict) and line_info.get("kind") == "none"


def _chart_value_label(value, number_format: str) -> str | None:
    """Format the visible value label for the small deterministic chart formats we emit."""
    fmt = (number_format or "General").strip()
    if fmt.casefold() == "general":
        return format(float(value), ".12g")
    m = re.fullmatch(r"0(?:\.(0+))?(?:\"([^\"]*)\")?", fmt)
    if not m:
        return None
    decimals = len(m.group(1) or "")
    suffix = m.group(2) or ""
    return f"{float(value):.{decimals}f}{suffix}"


def _chart_display_tokens(chart) -> tuple[list[str], dict]:
    """Derive required rendered chart labels from native chart data and formatting.

    Auto-generated numeric axis ticks are validated separately against the declared
    axis range; they are never accepted as arbitrary unmatched text.
    """
    required: list[str] = []
    notes: list[str] = []
    if getattr(chart, "has_title", False):
        try:
            title = chart.chart_title.text_frame.text.strip()
            if title:
                required.append(title)
        except Exception:  # noqa: BLE001
            notes.append("chart_title_unresolved")
    for plot in chart.plots:
        try:
            categories = [str(c.label).strip() for c in plot.categories]
        except Exception:  # noqa: BLE001
            categories = []
            notes.append("category_labels_unresolved")
        if not categories:
            notes.append("no_category_labels")
        required.extend(c for c in categories if c)
        try:
            show_values = bool(plot.has_data_labels and plot.data_labels.show_value)
        except Exception:  # noqa: BLE001
            show_values = False
            notes.append("data_label_visibility_unresolved")
        if show_values:
            try:
                number_format = plot.data_labels.number_format or "General"
                for series in chart.series:
                    for value in series.values:
                        label = _chart_value_label(value, number_format)
                        if label is None:
                            notes.append("data_label_number_format_unsupported")
                        else:
                            required.append(label)
            except Exception:  # noqa: BLE001
                notes.append("data_values_unresolved")
    if getattr(chart, "has_legend", False):
        for series in chart.series:
            if series.name:
                required.append(str(series.name).strip())
    for axis_name in ("category_axis", "value_axis"):
        try:
            axis = getattr(chart, axis_name)
            if axis.has_title:
                title = axis.axis_title.text_frame.text.strip()
                if title:
                    required.append(title)
        except Exception:  # noqa: BLE001
            pass
    axes = {}
    try:
        va = chart.value_axis
        axes = {"minimum": va.minimum_scale, "maximum": va.maximum_scale,
                "major_unit": va.major_unit}
    except Exception:  # noqa: BLE001
        notes.append("value_axis_range_unresolved")
    return required, {"axis": axes, "notes": sorted(set(notes))}


def _chart_axis_numeric_groups(glyphs, required_tokens: list[str]) -> list[str]:
    """Read chart numeric labels from glyph geometry, not flattened PDF text.

    PDF text extraction commonly concatenates axis labels such as 0, 20, 40 into
    ``02040`` because those labels have no spaces in the content stream. Grouping
    by baseline and horizontal glyph gaps keeps each displayed tick distinct while
    leaving data/category numbers already covered by native chart tokens alone.
    """
    numeric_chars = set("0123456789.,+-%−")
    rows: list[list[dict]] = []
    for glyph in sorted((g for g in glyphs if g["ch"] in numeric_chars),
                        key=lambda g: (g["y1"], g["x0"])):
        baseline = glyph["y1"]
        matches = [(abs(baseline - sum(x["y1"] for x in row) / len(row)), row)
                   for row in rows
                   if abs(baseline - sum(x["y1"] for x in row) / len(row)) <= 0.07]
        if matches:
            min(matches, key=lambda item: item[0])[1].append(glyph)
        else:
            rows.append([glyph])

    expected_numbers = set()
    for token in required_tokens:
        expected_numbers.update(re.findall(
            r"(?<![A-Za-z0-9.])[-+]?\d+(?:\.\d+)?%?(?![A-Za-z0-9.])", token))
    expected_numbers |= {x.rstrip("%") for x in tuple(expected_numbers)}

    observed = []
    for row in rows:
        row.sort(key=lambda g: g["x0"])
        groups: list[list[dict]] = []
        for glyph in row:
            if not groups:
                groups.append([glyph])
                continue
            previous = groups[-1][-1]
            gap = glyph["x0"] - previous["x1"]
            # Adjacent digits, decimal punctuation, and percent marks are tightly
            # kerned; distinct axis labels are separated by much larger gaps.
            if gap <= max(0.10, min(previous["x1"] - previous["x0"],
                                     glyph["x1"] - glyph["x0"]) * 0.65):
                groups[-1].append(glyph)
            else:
                groups.append([glyph])
        for group in groups:
            token = "".join(g["ch"] for g in group).replace("−", "-").replace(",", "")
            if not re.fullmatch(r"[-+]?\d+(?:\.\d+)?%?", token):
                continue
            if token in expected_numbers:
                continue
            observed.append(token)
    return observed


def slide_shapes(slide) -> list:
    """按 spTree 文档顺序返回形状记录（z-order = 列表序）。"""
    out = []
    for zi, sh in enumerate(slide.shapes):
        table_native = bool(getattr(sh, "has_table", False))
        chart_native = bool(getattr(sh, "has_chart", False))
        rec = {"z": zi, "name": sh.name, "cls": sh.__class__.__name__,
               "left": sh.left / EMU, "top": sh.top / EMU,
               "width": sh.width / EMU, "height": sh.height / EMU}
        rec["table_native"] = table_native
        rec["chart_native"] = chart_native
        is_title_placeholder = False
        try:
            if getattr(sh, "is_placeholder", False):
                from pptx.enum.shapes import PP_PLACEHOLDER
                is_title_placeholder = sh.placeholder_format.type in (
                    PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE)
        except Exception:  # noqa: BLE001
            is_title_placeholder = False
        rec["is_title_placeholder"] = is_title_placeholder
        rec["right"] = rec["left"] + rec["width"]
        rec["bottom"] = rec["top"] + rec["height"]
        if getattr(sh, "has_text_frame", False):
            rec["text"] = sh.text_frame.text
            sizes, bolds = [], []
            for para in sh.text_frame.paragraphs:
                for run in para.runs:
                    if run.font.size is not None:
                        sizes.append(run.font.size.pt)
                    bolds.append(bool(run.font.bold))
            rec["run_sizes"] = sizes
            rec["max_size"] = max(sizes) if sizes else None
            rec["bold"] = any(bolds)
            rec["anchor"] = str(sh.text_frame.vertical_anchor)
            eff, autofit = _shape_effective_sizes(sh)
            rec["eff_sizes"] = eff
            rec["autofit_scale"] = autofit
            el = sh._element
            rec["fill"] = _fill_info(el)
            rec["line"] = _line_info(el)
        elif table_native:
            # GraphicFrame 表格没有 shape.text_frame；此前其每个原生单元格
            # 的字完全漏出 HG01/HG02 字形台账，造成“可见表格但 200+ 字未归属”。
            # 这里把单元格文本及实际 run 字号归到表格容器，坐标仍用真实表格框，
            # 不放宽未归属字符判定，也不改变 z-order/遮挡计算。
            table = sh.table
            rec["text"] = "\n".join(
                "\t".join(cell.text for cell in row.cells)
                for row in table.rows)
            sizes, bolds = [], []
            for row in table.rows:
                for cell in row.cells:
                    for para in cell.text_frame.paragraphs:
                        for run in para.runs:
                            size = run.font.size
                            sizes.append(size.pt if size is not None else None)
                            bolds.append(bool(run.font.bold))
            rec["run_sizes"] = [s for s in sizes if s is not None]
            rec["eff_sizes"] = sizes
            rec["autofit_scale"] = None
            rec["max_size"] = max(rec["run_sizes"]) if rec["run_sizes"] else None
            rec["bold"] = any(bolds)
            rec["anchor"] = None
            rec["fill"] = _fill_info(sh._element)
            rec["line"] = _line_info(sh._element)
        elif chart_native:
            # Native chart labels/data-labels are rendered by the chart engine and
            # therefore absent from slide a:t. Keep them in a separate chart ledger;
            # the main loop requires every native category/value label to appear and
            # validates any remaining numeric glyphs against the declared axis.
            chart = sh.chart
            required, chart_meta = _chart_display_tokens(chart)
            rec["text"] = ""
            rec["chart_required_tokens"] = required
            rec["chart_meta"] = chart_meta
            rec["run_sizes"] = []
            rec["eff_sizes"] = []
            try:
                size = chart.font.size
                rec["eff_sizes"].append(size.pt if size is not None else None)
            except Exception:  # noqa: BLE001
                rec["eff_sizes"].append(None)
            for axis_name in ("category_axis", "value_axis"):
                try:
                    size = getattr(chart, axis_name).tick_labels.font.size
                    rec["eff_sizes"].append(size.pt if size is not None else None)
                except Exception:  # noqa: BLE001
                    rec["eff_sizes"].append(None)
            try:
                for plot in chart.plots:
                    if plot.has_data_labels and plot.data_labels.show_value:
                        size = plot.data_labels.font.size
                        rec["eff_sizes"].append(size.pt if size is not None else None)
            except Exception:  # noqa: BLE001
                rec["eff_sizes"].append(None)
            rec["max_size"] = max((x for x in rec["eff_sizes"] if x is not None),
                                  default=None)
            rec["bold"] = False
            rec["anchor"] = None
            rec["autofit_scale"] = None
            rec["fill"] = _fill_info(sh._element)
            rec["line"] = _line_info(sh._element)
        else:
            rec["text"] = ""
            rec["run_sizes"] = []
            rec["max_size"] = None
            rec["bold"] = False
            # 非文字形状也要取真实填充：卡片语义检查需要区分实底卡与图片/无填充框
            try:
                rec["fill"] = _fill_info(sh._element)
                rec["line"] = _line_info(sh._element)
            except Exception:  # noqa: BLE001
                rec["fill"] = {"kind": "unknown"}
                rec["line"] = {"kind": "unknown"}
        out.append(rec)
    return out


# --------------------------------------------------------------------------- 字形
def page_chars(page) -> list:
    """字符级字形框（英寸，top-down）。返回 [{ch,x0,y0,x1,y1}]。"""
    H = page.get_size()[1] / IN
    tp = page.get_textpage()
    chars = []
    for i in range(tp.count_chars()):
        ch = tp.get_text_range(i, 1)
        if not ch:
            continue
        box = tp.get_charbox(i)
        chars.append({"ch": ch, "x0": box[0] / IN, "x1": box[2] / IN,
                      "y0": H - box[3] / IN, "y1": H - box[1] / IN})
    return chars


def assign_chars(chars, shapes) -> tuple:
    """字符 → 属主形状（中心落入形状框；否则取最大重叠；再不行 UNMATCHED）。

    返回 (mapping char_idx→shape_idx, unmatched[char_idx], dup_pairs)。
    覆盖证明：未归属字符必须为 0（HG02）——旧门禁把未归属行忽略正是假 clean 根因。
    """
    text_shapes = [i for i, s in enumerate(shapes)
                   if s["text"].strip() or s.get("chart_native")]
    mapping, unmatched = {}, []
    for ci, c in enumerate(chars):
        if not c["ch"].strip():
            continue
        cx, cy = (c["x0"] + c["x1"]) / 2, (c["y0"] + c["y1"]) / 2
        best, best_key = None, None
        for si in text_shapes:
            s = shapes[si]
            ox = min(c["x1"], s["right"]) - max(c["x0"], s["left"])
            oy = min(c["y1"], s["bottom"]) - max(c["y0"], s["top"])
            if ox > 0 and oy > 0:
                area = ox * oy
                inside = (s["left"] - 1e-6 <= cx <= s["right"] + 1e-6
                          and s["top"] - 1e-6 <= cy <= s["bottom"] + 1e-6)
                has_char = c["ch"] in shapes[si]["text"]
                # 两类邻近形状易产生"包围盒重叠但墨迹从不相交"的假性候选：
                # (a) 圆角卡片/装饰形状的矩形包围盒比它的可见轮廓大（圆角
                #     会"削掉"四角），邻近形状的字符中心即使从不与它的墨迹
                #     真正重叠，也可能恰好落进这个更大的矩形包围盒里；
                # (b) 两个形状的声明框边缘重合（如都紧贴画布安全边距），
                #     字形框与两者的重叠面积可以完全相等，不落入 inside 分支。
                # 原实现按遍历顺序先到先得，稳定但任意——改成在候选之间优先
                # 看该字符是否真的出现在这个形状自己的文本里（强证据，不论
                # 是否判定 inside），再优先挑包围盒更小（更贴合、更可能是
                # 真正主体）的形状。真实案例：job-d239567879df 第1页，
                # "2026 年 11 月"的"年"被一张跨大半页的圆角说明卡片（本身
                # 也含"年"字）抢走（情形 a）；同一字符串的"月"因声明框与
                # 该卡片共享同一条画布右边距，字形框与两者重叠面积恰好相等
                # 而被抢走（情形 b）。
                shape_area = max(0.0, s["right"] - s["left"]) * max(0.0, s["bottom"] - s["top"])
                key = (1 if inside else 0, 1 if has_char else 0, -shape_area, area)
                if best is None or key > best_key:
                    best, best_key = si, key
        if best is None or best_key[3] <= 0:
            unmatched.append(ci)
        else:
            mapping[ci] = best
    return mapping, unmatched


# --------------------------------------------------------------------------- 角色
def _is_page_number(s) -> bool:
    t = s["text"].strip()
    return bool(re.fullmatch(r"\d{1,2}", t)) and s["top"] > 6.9 and s["left"] > 11.0


_SOURCE_LINE_RE = re.compile(
    r"^(?:数据来源|资料来源|图源|图片来源|来源|Source)\s*[:：]?",
    re.IGNORECASE)


def _is_source(s) -> bool:
    """来源标注行：**行首**是来源前缀且整行足够短才成立。

    旧实现用 `"来源" in t` 子串判断，章节标题（如「27 来源与发布决策
    记录」）也会被当成 16–18pt 的来源标注，触发 HG07 误报。
    来源标注行都很短（≤60 字符），长句一律不按来源处理。
    """
    t = s["text"].strip()
    if not t:
        return False
    if _SOURCE_LINE_RE.match(t):
        # 短来源行直接成立；「来源…：」标签式注脚（如"来源与限定（原句）：
        # …"，冒号出现在行首前 14 字内）同样成立——正文段落不会以「来源」
        # 开头且这么早出冒号。章标题「27 来源与…」行首不匹配，不受影响。
        _colon = min((t.find(c) for c in ":：" if c in t), default=-1)
        if len(t) <= 60 or 0 <= _colon <= 14:
            return True
    if ("Yiten Huang" in t) or ("YitenHuang" in t):
        return True
    # 数据来源标注的常见署名（数据供应商/官方机构/外媒）——与"来源："同语义
    return any(k in t for k in ("Syntun", "统计局", "国务院：", "Reuters",
                                "财政部", "新华社"))


_PHOTO_CREDIT_HEADER = "[WENGAO PHOTO CREDIT]"


def _notes_photo_credit_sanctioned(slide, nb: str, nc: str) -> bool:
    """场景图照片署名备注豁免（HG01 备注守恒）。

    场景阶段给带 WENGAO_SCENE| 图片的页写入固定格式的照片署名备注
    （[WENGAO PHOTO CREDIT] 头 + Depiction/License…），这是**受控的
    合规新增**，不是内容篡改。三条件同时成立才豁免：
    1) 基线备注为空；2) 候选备注以固定头开始；3) 该页确有
    WENGAO_SCENE| 命名的场景图片。其余备注变化仍然照报。
    """
    if (nb or "").strip():
        return False
    if not (nc or "").startswith(_PHOTO_CREDIT_HEADER):
        return False
    try:
        for sh in slide.shapes:
            if str(getattr(sh, "name", "")).startswith("WENGAO_SCENE|"):
                return True
    except Exception:
        return False
    return False


_TEXT_CARD_DISCLOSURE = "未找到可核验的合规配图，以说明卡片代替"


def _text_card_sanctioned(shape, text: str) -> bool:
    """场景文字兜底卡片豁免（HG01 内容守恒）。

    场景阶段在找不到可核验合规照片时，用固定格式的说明卡片代替
    （allow_text_card_fallback，显式用户选择），这是**受控的兜底新增**，
    不是内容篡改。两条件同时成立才豁免：1) 形状名以 WENGAO_TEXTCARD|
    开头（场景阶段专用命名，见 semantic_scenes._draw_text_card）；
    2) 卡片文本以固定的免责声明行结尾（字面文案，不可被篡改为别的内容）。
    其余新增/变化的文字形状仍然照报。
    """
    name = str(getattr(shape, "name", "") or "")
    if not name.startswith("WENGAO_TEXTCARD|"):
        return False
    return text.rstrip().endswith(_TEXT_CARD_DISCLOSURE)


def _bottom_brand_band(i, shapes, idxs) -> bool:
    """页底品牌带：页脚语义的第三种形态（2026-11-04 job-293501ae3698 页 1）。

    位置规则只认 top ≥ 6.85 的形状；真实稿里品牌条常挂在 6.6–6.85
    （该任务页 1 的「宏观观察 · 短视频配套文稿」top=6.688）。被当正文
    处理的后果：精修的可读性地板把它抬到 body 目标字号（11.25→18pt），
    字形底随之压进页脚净空（HG06 CONTENT_FOOTER_GAP，gap 0.042<0.12），
    且 HG07 还会按 body 下限 13pt 报它字号过小——两头挨打。

    判为 footer 须五条同时成立（与 refine.infer_text_roles 同口径，
    见 _floor_brand_band 的镜像实现）：
    1. 位于页脚区上沿 0.35in 带内（6.65 ≤ top < 6.85；≥6.85 由既有的
       位置规则兜底，不依赖品牌呼应）；
    2. 是全页最底部的文字（没有更低的正文行——底部还有正文说明它是
       内容行而非页装帧）；
    3. ≤60 字（与既有页脚规则同限）；
    4. 与上半页（top<3.75）另一形状存在 ≥2 字 token 的品牌呼应
       （「宏观观察」同时出现在页顶 kicker 与页底品牌条）——防把
       碰巧呼应标题的底部正文行/结论误判成页脚；
    5. 字号 ≤14pt（品牌条是小字装帧；16–22pt 的底部大字更可能是
       conclusion，宁可让它留在 body 走正常检查）。
    """
    s = shapes[i]
    t = s["text"].strip()
    if not t or len(t) > 60:
        return False
    pt = s.get("max_size") or s.get("font_size_pt", 0.0)
    if not (0.0 < pt <= 14.0):
        return False
    top = s.get("top", 0)
    if not (6.65 <= top < 6.85):
        return False
    # 「全页最底部」只看真正的正文行：其下若还有页码/来源/页脚区短文本
    # （页脚装帧），品牌带仍成立——页 1 品牌带 6.688 之上常还有 7.07 的页码。
    def _footer_furniture(x):
        xt = x["text"].strip()
        return (_is_page_number(x) or _is_source(x)
                or (x.get("top", 0) >= 6.85 and len(xt) <= 60))
    lowers = [shapes[j]["top"] for j in idxs
              if shapes[j]["top"] > top + 0.02 and not _footer_furniture(shapes[j])]
    if lowers:
        return False
    tokens = re.findall(r"[0-9A-Za-z\u4e00-\u9fff]{2,}", t)
    if not tokens:
        return False
    for j in idxs:
        if j == i or shapes[j]["top"] >= 3.75:
            continue
        other = shapes[j]["text"]
        if any(tok in other for tok in tokens):
            return True
    return False


def infer_roles(shapes) -> dict:
    """按几何+文本语义推断角色（不按页码硬编码）。返回 {shape_idx: role}。"""
    # 原生图表显示的文字来自 chart part，而不在 slide a:t 中；即便 text 为空，
    # 也必须进入角色台账和字号下限检查。
    idxs = [i for i, s in enumerate(shapes)
            if s["text"].strip() or s.get("chart_native")]
    legend_boxes = [{"left": s.get("left", 0.0), "top": s.get("top", 0.0),
                     "width": s.get("width", 0.0), "height": s.get("height", 0.0),
                     "text": s.get("text", ""),
                     "solid": (s.get("fill") or {}).get("kind") == "solid",
                     "decor": is_decor(s.get("max_size") or s.get("font_size_pt", 0.0),
                                       s.get("text", ""))}
                    for s in shapes]
    roles = {}
    for i in idxs:
        s = shapes[i]
        t = s["text"].strip()
        pt = s.get("max_size") or s.get("font_size_pt", 0.0)
        if s.get("table_native"):
            roles[i] = "table"
        elif s.get("chart_native"):
            roles[i] = "chart_aux"
        elif is_decor(pt, t):
            roles[i] = "decor"
        # A short unit/suffix word (e.g. "倍") beside a large decorative
        # stat number ("30") is part of that decorative display, not body
        # text — alone it's too small to pass is_decor's own 60pt floor.
        # Left as "body" it can out-score an already-shrunk real page
        # title in the "largest font in the upper half" title selection
        # below (job-d239567879df page 4).
        elif is_decor_unit_suffix(i, legend_boxes):
            roles[i] = "decor"
        elif t in ("“", "”", "「", "」"):
            roles[i] = "quote"
        elif _is_page_number(s):
            roles[i] = "footer"
        elif _is_source(s):
            roles[i] = "source"
        elif _bottom_brand_band(i, shapes, idxs):
            roles[i] = "footer"
        elif s.get("top", 0) >= 6.85 and len(t) <= 60:
            roles[i] = "footer"
        # Roman numeral badges (Ⅰ Ⅱ Ⅲ ...) are the same kind of standalone
        # ordinal label as "1"/"A" — U+2160-217B is the whole Unicode Roman
        # Numerals block, upper- and lower-case. Without this, a lone "Ⅰ"/
        # "Ⅱ"/"Ⅲ" badge fell through to "body" and tripped HG08
        # ORPHAN_CHAR_LINE as if a sentence had awkwardly wrapped down to one
        # stray character, when it is an intentional single-character badge
        # (job-d239567879df page 8).
        elif re.fullmatch(r"\d{1,2}|[A-Za-z]|[Ⅰ-ⅻ]", t):
            roles[i] = "ordinal"
        elif re.search(r"\d", t) and re.search(r"%|亿|万", t) and len(t) <= 12:
            roles[i] = "key_number"
        elif is_chart_legend_label(i, legend_boxes):
            roles[i] = "chart_aux"
        else:
            roles[i] = "body"
    rest = [i for i in idxs if roles[i] == "body"]
    if rest:
        # 标题：上半页(top<3.75，半页高，与 refine.py:infer_text_roles 的
        # sh_*0.5 同口径)字号最大者（回退最顶）。封面/分章页常见"大标题
        # 居中偏下 + 顶部小眉题"版式：真正的大标题常被挤到严格顶区
        # (top<2.3)之外，若只在顶区内比字号，眉题反倒会被误判成标题、
        # 大标题被按正文字号区间核对。放宽到上半页更稳：标题角色区间
        # （36–40）与正文区间（24–28）本就不相交，字号钳过之后标题仍
        # 必然比正文大，不会出现"钳完之后反而判错"的边界抖动（按倍数
        # 阈值判定时，字号被钳到区间边界后差距可能正好跌破阈值，判定
        # 会跟着生成端的取值来回翻盘）。下半页(conclusion 区)与数字强调
        # (key_number)已在上面单独分流，不会被这里捞进候选。
        upper = [i for i in rest if shapes[i]["top"] < 3.75]
        candidates = upper if upper else rest
        # 真正的 Title 占位符常不显式设字号（继承版式/母版样式），max_size
        # 在这里是 None→按 0.0 参与比较，必输给任何显式设了字号的正文框，
        # 让正文被误判成标题（与 refine.py:infer_text_roles 同口径同一处
        # 2026-10-31 bug，两边必须一起修，见那边注释）。占位符类型本身
        # 就是比字号推断更直接的证据。
        title_placeholders = [i for i in candidates if shapes[i].get("is_title_placeholder")]
        if title_placeholders:
            top_i = max(title_placeholders,
                       key=lambda i: ((shapes[i]["max_size"] or 0.0), -shapes[i]["top"]))
        else:
            top_i = (max(upper,
                         key=lambda i: ((shapes[i]["max_size"] or 0.0),
                                        -shapes[i]["top"]))
                     if upper else min(rest, key=lambda i: shapes[i]["top"]))
        roles[top_i] = "title"
        # 眉题/类别标签：位于主标题上方（top < shapes[top_i]["top"]）的短标签
        for i in rest:
            if i != top_i and shapes[i]["top"] < shapes[top_i]["top"] and len(shapes[i]["text"].strip()) <= 20:
                roles[i] = "label"
        # 导语：严格位于标题之下（top 更大）且仍在顶区的最顶页级文本（不在卡内，宽度不低于 2.5in）。
        cards_list = [shapes[c] for c in _cards(shapes)]
        below = [i for i in rest if i != top_i and roles[i] == "body"
                 and shapes[i]["top"] < 2.3
                 and shapes[i]["top"] > shapes[top_i]["top"]
                 and shapes[i].get("width", 0) >= 2.5
                 and not any(shapes[i]["left"] >= c["left"] - 0.06
                             and shapes[i]["right"] <= c["right"] + 0.06
                             and shapes[i]["top"] >= c["top"] - 0.06
                             and shapes[i]["bottom"] <= c["bottom"] + 0.06
                             for c in cards_list)]
        if below:
            roles[min(below, key=lambda i: shapes[i]["top"])] = "lead"
        # 结论：底部区域(top>4.2) ≥15 字的 body 中最靠下者（同高取更长）——
        # 底部短标签/金句不抢结论；版式的独立结论行必然最低。
        bottom = [i for i in rest if roles[i] == "body"
                  and shapes[i]["top"] > 4.2
                  and len(shapes[i]["text"].strip()) >= 15]
        if bottom:
            roles[max(bottom, key=lambda i: (shapes[i]["top"],
                                             len(shapes[i]["text"].strip())))] = "conclusion"
    return roles


# --------------------------------------------------------------------------- 行分组
_PUNCT_ONLY = re.compile(
    r"^[\s，。、；：！？,.;:!?%‰“”‘’「」『』（）()\[\]【】·—…\-–—/]+$")


def _baseline_rows(chars, tol_floor: float = 0.08, tol_ratio: float = 0.55) -> list:
    """按**基线/纵向量程重叠**归行（第六轮监督：HG08 归组必须基线/字号/标点感知）。

    旧实现按字心 (y0+y1)/2 加固定 0.08in 归簇。真实排版里 44pt 的 '.' 字心比
    数字低约 0.165in（小数点字形贴基线、字框比数字矮），于是 "+0.9%" 这类正常
    小数被判成 TOKEN_SPLIT；产品端为过阈值把 '.' run 抬到数字中线，渲染成
    "+0·9%" 中点——可见数值语义退化（监督 1710 第 1 项）。

    现行归组：
      1. 非标点字按纵向量程重叠判同行（重叠 ≥ 0.5×较小字高），并加字心距离
         上限（0.55×较大字高，按字号缩放）防止紧排时把相邻行并成一行；
      2. 标点（，。、；：！？%“”… 等）只有在合理纵向量程内才挂回已有行；
         偏移超过字号相关上限时保留为独立行，供 TOKEN_SPLIT 检出。
    于是：正常小数点/百分号与数字同行；'一/二/三/：' 这类矮笔画字同行；
    真正跨行的 token（行距 1.32，两行字框不相交）仍被拆成多行 → TOKEN_SPLIT。
    """
    def h(c):
        return max(c["y1"] - c["y0"], 1e-6)

    def ctr(c):
        return (c["y0"] + c["y1"]) / 2

    core = [c for c in chars if not _PUNCT_ONLY.match(c["ch"])]
    punct = [c for c in chars if _PUNCT_ONLY.match(c["ch"])]
    rows = []
    for c in sorted(core, key=lambda c: (ctr(c), c["x0"])):
        placed = False
        for row in rows:
            for m in row:
                ov = min(c["y1"], m["y1"]) - max(c["y0"], m["y0"])
                if (ov >= 0.5 * min(h(c), h(m)) - 1e-9
                        and abs(ctr(c) - ctr(m)) <= max(tol_floor,
                                                         tol_ratio * max(h(c), h(m)))):
                    row.append(c)
                    placed = True
                    break
            if placed:
                break
        if not placed:
            rows.append([c])
    if not rows:
        rows = [[]]
    for c in sorted(punct, key=lambda c: (ctr(c), c["x0"])):
        candidates = [r for r in rows if r]
        if not candidates:
            rows[0].append(c)
            continue
        best = min(candidates,
                   key=lambda r: abs(ctr(c) - sum(ctr(m) for m in r) / len(r)))
        row_center = sum(ctr(m) for m in best) / len(best)
        row_height = max(h(m) for m in best)
        # 只把字形量程正常、视觉基线自然偏低的标点挂回数字行。
        # 不能再把任意位置的小数点或百分号吸收到最近行，否则 “0.9%” 的
        # 标点即使被移到框外数英寸，TOKEN_SPLIT 仍会被掩盖（迭代 7 反例）。
        max_delta = max(tol_floor, tol_ratio * max(h(c), row_height))
        if abs(ctr(c) - row_center) > max_delta:
            rows.append([c])
        else:
            best.append(c)
    rows.sort(key=lambda r: min(ctr(m) for m in r) if r else 0.0)
    return rows


def _cards(shapes) -> list:
    """实底、足够大的无文字容器（卡片/底板）下标。

    只在能取到真实 solidFill 时认定；渐变/图片/无填充一律不算卡片（fail-closed：
    不确定的容器不参与"卡内文字"判定）。
    """
    out = []
    for i, s in enumerate(shapes):
        if s.get("text", "").strip():
            continue
        if s.get("width", 0) < 1.0 or s.get("height", 0) < 0.5:
            continue
        f = s.get("fill") or {}
        if f.get("kind") != "solid":
            continue
        out.append(i)
    return out


def _contained(u, c, tol: float = 0.06) -> bool:
    """字形并集 u 是否整体落在卡片 c 内（含 tol）。"""
    return (u[0] >= c["left"] - tol and u[2] <= c["right"] + tol
            and u[1] >= c["top"] - tol and u[3] <= c["bottom"] + tol)


# --------------------------------------------------------------------------- 工具
_MEASURE_FONTS = (
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
)
_FONT_CACHE: dict = {}


def _measure_font(px: int):
    from PIL import ImageFont
    if px in _FONT_CACHE:
        return _FONT_CACHE[px]
    for p in _MEASURE_FONTS:
        try:
            f = ImageFont.truetype(p, px)
            _FONT_CACHE[px] = f
            return f
        except Exception:  # noqa: BLE001
            continue
    _FONT_CACHE[px] = None
    return None


def expected_ink_ratio(ch: str, w_px: int, h_px: int):
    """该字符墨迹在字符框内的期望占比（可见性期望值）。

    CJK 单字墨迹约占 em 框 10–40%，细笔画字（一、：）更低——所以不能用固定
    阈值判"可见"，必须与该字自身的实测墨迹占比比较。

    实现：大字号渲染 → 取紧致墨迹 bbox → 缩放到字符框 (w,h) 再测占比。
    与 pdfium 紧致字符框语义一致；直接按 font size 渲染整 em 会把标点墨迹
    画到框外（全角冒号墨迹在 em 左侧 1/4 处），得到 0.0 假期望。

    fail-closed（监督 GATE-EVIDENCE-02）：缺字体 / 渲染异常 / 非空白字符
    实测零墨迹 / 无效裁剪 一律返回 None——调用方必须记 UNVERIFIED，
    禁止伪造 0.15 或默认可见。
    """
    import numpy as np
    from PIL import Image, ImageDraw
    if ch.isspace():
        return 0.0
    if w_px < 2 or h_px < 2:
        return None          # 无效裁剪 → UNVERIFIED，不是 0
    big = 160
    img = Image.new("L", (big * 2, big * 2), 0)
    d = ImageDraw.Draw(img)
    f = _measure_font(big)
    if f is None:
        return None          # 缺字体 → UNVERIFIED，不是 0.15
    try:
        d.text((big // 2, big // 2), ch, font=f, fill=255)
    except Exception:  # noqa: BLE001
        return None          # 渲染异常 → UNVERIFIED
    arr = np.asarray(img)
    ys, xs = np.where(arr > 40)
    if len(xs) == 0:
        return None          # 非空白字符零墨迹：期望不可建立 → UNVERIFIED
    ink = img.crop((int(xs.min()), int(ys.min()),
                    int(xs.max()) + 1, int(ys.max()) + 1))
    scaled = np.asarray(ink.resize((w_px, h_px)))
    v = float((scaled > 40).mean())
    if v <= 0.0:
        return None          # 期望不可建立 → UNVERIFIED
    return v


def expected_glyph_mask(ch: str, w_px: int, h_px: int):
    """该字符的期望笔画掩码（bool 数组），与 pdfium 紧致字符框对齐。

    监督 GATE-PARTIAL-03：可见性证明必须隔离到该字自身笔画。做法：
    大字号渲染参考字形 → 取紧致墨迹 bbox → 缩放到实际字符框 (w_px,h_px)。
    pdfium get_charbox 默认返回紧致墨迹框，故缩放后墨迹位置/占比与实际
    渲染对齐（含全角标点等墨迹只占 em 一角的字符）。失败返回 None。
    """
    import numpy as np
    from PIL import Image, ImageDraw
    if ch.isspace() or w_px < 2 or h_px < 2:
        return None
    big = 160
    img = Image.new("L", (big * 2, big * 2), 0)
    d = ImageDraw.Draw(img)
    f = _measure_font(big)
    if f is None:
        return None
    try:
        d.text((big // 2, big // 2), ch, font=f, fill=255)
    except Exception:  # noqa: BLE001
        return None
    arr = np.asarray(img)
    ys, xs = np.where(arr > 40)
    if len(xs) == 0:
        return None          # 非空白字符零墨迹：期望不可建立 → UNVERIFIED
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
    # 画布必须与调用方裁剪框 (w_px,h_px) 同尺寸；±1px 内缩对齐裁剪边距
    if w_px < 4 or h_px < 4:
        ink = img.crop((x0, y0, x1, y1)).resize((w_px, h_px))
        return np.asarray(ink) > 40
    ink = img.crop((x0, y0, x1, y1)).resize((w_px - 2, h_px - 2))
    canvas = Image.new("L", (w_px, h_px), 0)
    canvas.paste(ink, (1, 1))
    return np.asarray(canvas) > 40


def _shift(mask, dy: int, dx: int):
    """无回绕位移（np.roll 会回绕污染边缘）。"""
    import numpy as np
    H, W = mask.shape
    out = np.zeros_like(mask)
    sy0, sy1 = max(dy, 0), H + min(dy, 0)
    sx0, sx1 = max(dx, 0), W + min(dx, 0)
    if sy1 <= sy0 or sx1 <= sx0:
        return out
    out[max(-dy, 0):H + min(-dy, 0), max(-dx, 0):W + min(-dx, 0)] = \
        mask[sy0:sy1, sx0:sx1]
    return out


def _dilate(mask, r: int):
    import numpy as np
    out = mask.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dy == 0 and dx == 0:
                continue
            out |= _shift(mask, dy, dx)
    return out


def _stroke_coverage(diffm, emask, tol_px: int = 2, align_px: int = 3) -> float:
    """实际无字差分掩码对期望笔画掩码的覆盖率（监督 GATE-PARTIAL-03）。

    coverage = max over ±align_px 对齐 |diff ∧ dilate(expected)| / |expected|。
    - 隔离：只统计该字自身期望笔画被差分覆盖的比例，重叠他字墨迹不计入；
    - 对齐搜索：pdfium 字符框与 PIL 渲染存在亚像素/基线偏移，小窗口内取最优；
    - 遮挡不可恢复：被遮笔画在任何对齐下都无差分墨迹，覆盖率必然下降。
    """
    import numpy as np
    if emask is None or emask.sum() == 0:
        return 0.0
    if diffm is None or diffm.shape != emask.shape:
        return 0.0
    dilD = _dilate(diffm, tol_px)
    denom = int(emask.sum())
    best = 0.0
    for dy in range(-align_px, align_px + 1):
        for dx in range(-align_px, align_px + 1):
            es = _shift(emask, dy, dx) if (dy or dx) else emask
            cov = float((dilD & es).sum()) / denom
            if cov > best:
                best = cov
                if best >= 0.995:
                    return best
    return best


def _concat_shape_text(sh) -> str:
    """按 XML a:t 拼接形状文本（兼容 a:br 语义换行——换行不改字符序列）。"""
    return "".join(t.text or "" for t in sh._element.iter(f"{A}t"))


PM = "{http://schemas.openxmlformats.org/presentationml/2006/main}"


def _explicit_bullet_state(ppr):
    if ppr is None:
        return ("inherit", None)
    if ppr.find(f"{A}buNone") is not None:
        return ("none", None)
    bchar = ppr.find(f"{A}buChar")
    if bchar is not None:
        return ("char", bchar.get("char"))
    if ppr.find(f"{A}buAutoNum") is not None:
        return ("unresolved", None)
    return ("inherit", None)


def _effective_paragraph_bullet(slide, shape, para, _para_idx: int) -> tuple[str | None, str]:
    """Resolve a paragraph bullet marker from direct, shape, layout, or master OOXML.

    Returns (visible marker, status). Auto-numbering without a materialized number is
    explicitly unresolved instead of being ignored or hard-coded into the text ledger.
    """
    ppr = para._p.find(f"{A}pPr")
    state, marker = _explicit_bullet_state(ppr)
    if state != "inherit":
        return marker, state
    try:
        level = int(ppr.get("lvl")) if ppr is not None and ppr.get("lvl") is not None else int(para.level or 0)
    except (TypeError, ValueError):
        return None, "unresolved"
    if not 0 <= level <= 8:
        return None, "unresolved"

    def level_bullet(tx_body):
        if tx_body is None:
            return ("inherit", None)
        lst = tx_body.find(f"{A}lstStyle")
        if lst is None:
            return ("inherit", None)
        level_ppr = lst.find(f"{A}lvl{level + 1}pPr")
        return _explicit_bullet_state(level_ppr)

    tx_body = shape._element.find(f"{PM}txBody")
    state, marker = level_bullet(tx_body)
    if state != "inherit":
        return marker, state
    if not getattr(shape, "is_placeholder", False):
        return None, "none"

    # A layout placeholder's stored sample paragraphs are not index-aligned with
    # the content placeholder's paragraphs. Resolve only its level list style;
    # never infer that content paragraph N inherits sample paragraph N's bullet.
    layout = slide.slide_layout
    layout_ph = None
    try:
        target_idx = shape.placeholder_format.idx
        layout_ph = next((ph for ph in layout.placeholders
                          if ph.placeholder_format.idx == target_idx), None)
    except Exception:  # noqa: BLE001
        layout_ph = None
    if layout_ph is not None and getattr(layout_ph, "has_text_frame", False):
        state, marker = level_bullet(layout_ph._element.find(f"{PM}txBody"))
        if state != "inherit":
            return marker, state

    # Master txStyles applies to placeholders by category. Body/object placeholders
    # inherit bodyStyle; title placeholders use titleStyle.
    try:
        from pptx.enum.shapes import PP_PLACEHOLDER
        ph_type = shape.placeholder_format.type
        style_name = ("titleStyle" if ph_type in
                      (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE)
                      else "bodyStyle")
        tx_styles = layout.slide_master._element.find(f"{PM}txStyles")
        style = tx_styles.find(f"{PM}{style_name}") if tx_styles is not None else None
        level_ppr = style.find(f"{A}lvl{level + 1}pPr") if style is not None else None
        state, marker = _explicit_bullet_state(level_ppr)
        if state != "inherit":
            return marker, state
    except Exception:  # noqa: BLE001
        return None, "unresolved"
    return None, "none"


def _shape_effective_sizes(sh) -> tuple:
    """每个 run/fld 的有效字号（run sz → 段落 defRPr → lstStyle 级 defRPr），
    含 normAutofit fontScale 缩放。返回 (sizes list, autofit_scale)。

    监督 GATE-ROLE-04：只验 max run size 会漏掉 26pt+12pt 混排；
    继承/autofit 未解析为 None → 调用方记 UNVERIFIED，不默认合规。
    """
    txBody = sh._element.find(f"{PM}txBody")
    if txBody is None:
        return [], None
    lvl_tags = [f"{A}lvl{i}pPr" for i in range(1, 10)]
    lst_def = {}
    lst = txBody.find(f"{A}lstStyle")
    if lst is not None:
        for it, lvl_tag in enumerate(lvl_tags, 1):
            p = lst.find(lvl_tag)
            if p is not None:
                d = p.find(f"{A}defRPr")
                if d is not None and d.get("sz"):
                    try:
                        lst_def[it] = int(d.get("sz")) / 100.0
                    except ValueError:
                        pass
    autofit = None
    bodyPr = txBody.find(f"{A}bodyPr")
    if bodyPr is not None:
        na = bodyPr.find(f"{A}normAutofit")
        if na is not None and na.get("fontScale"):
            try:
                autofit = int(na.get("fontScale")) / 100000.0
            except ValueError:
                autofit = None
    eff = []
    for para in txBody.findall(f"{A}p"):
        pPr = para.find(f"{A}pPr")
        # OOXML：a:pPr/@lvl 为 0 基（0..8），lstStyle 的 lvl1pPr..lvl9pPr 按
        # 1 基命名——lvl1pPr 作用于 lvl=0，lvl2pPr 作用于 lvl=1（OpenXML
        # Level1/Level2ParagraphProperties 权威定义；监督
        # font-inheritance-negative-20261021-1310 反例：lvl=1 继承 12pt 曾被
        # 错解析为 lvl1pPr 的 26pt 并误过 body 区间谓词）。省略 lvl 与显式
        # lvl=0 同样作用于 lvl1pPr；越界/非整数 lvl 不解析 → sz=None →
        # 调用方记 RUN_SIZE_UNKNOWN（UNVERIFIED），不得借映射跳过小字。
        lvl = 0
        if pPr is not None and pPr.get("lvl") is not None:
            try:
                lvl = int(pPr.get("lvl"))
                if not 0 <= lvl <= 8:
                    lvl = None
            except ValueError:
                lvl = None
        pdef = None
        if pPr is not None:
            d = pPr.find(f"{A}defRPr")
            if d is not None and d.get("sz"):
                try:
                    pdef = int(d.get("sz")) / 100.0
                except ValueError:
                    pdef = None
        for r in para.findall(f"{A}r") + para.findall(f"{A}fld"):
            rPr = r.find(f"{A}rPr")
            sz = None
            if rPr is not None and rPr.get("sz"):
                try:
                    sz = int(rPr.get("sz")) / 100.0
                except ValueError:
                    sz = None
            if sz is None:
                sz = pdef
            if sz is None and lvl is not None:
                sz = lst_def.get(lvl + 1)   # 0 基 lvl → 1 基 lvlNpPr 键
            if sz is not None and autofit:
                sz = round(sz * autofit, 2)
            eff.append(sz)
    return eff, autofit


def _hex_to_rgb(h: str):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def _lum(rgb):
    def f(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (f(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(rgb1, rgb2) -> float:
    l1, l2 = _lum(rgb1), _lum(rgb2)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def overlap(a, b):
    ox = min(a[2], b[2]) - max(a[0], b[0])
    oy = min(a[3], b[3]) - max(a[1], b[1])
    return (ox, oy) if ox > 0 and oy > 0 else (0.0, 0.0)


# --------------------------------------------------------------------------- HG13 结构清单
HG13_ANIM_TAGS = ("anim", "animEffect", "animMotion", "animRot", "animScale",
                  "set", "animClr")


def structure_inventory(slide) -> tuple[dict, list]:
    """HG13：从磁盘被检件重算逐页结构清单（对象/备注/媒体/动画/转场）。

    全部字段在检查时从被检件本身重算，不接受调用者自报；任何成员无法建立
    都记入 problems（fail-closed，由调用方升级为 UNVERIFIED）。
    返回 (inventory, problems)。
    """
    from lxml import etree
    P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
    problems: list = []
    objects: list = []
    media: list = []
    try:
        shapes = list(slide.shapes)
    except Exception as exc:  # noqa: BLE001
        return {}, [f"shapes_unreadable: {type(exc).__name__}: {exc}"[:160]]
    for z, sh in enumerate(shapes):
        try:
            entry = {"z": z, "name": sh.name}
            try:
                entry["cls"] = str(sh.shape_type)
            except Exception:  # noqa: BLE001
                entry["cls"] = "unknown"
            for attr in ("left", "top", "width", "height"):
                v = getattr(sh, attr, None)
                entry[attr] = None if v is None else round(v / EMU, 4)
            if getattr(sh, "has_text_frame", False):
                entry["text_chars"] = len(sh.text_frame.text)
            if getattr(sh, "has_table", False):
                entry["table_rows"] = len(sh.table.rows)
            if getattr(sh, "has_chart", False):
                entry["has_chart"] = True
            if sh.shape_type is not None and "PICTURE" in str(sh.shape_type).upper():
                crop = {}
                for attr in ("crop_left", "crop_right", "crop_top", "crop_bottom"):
                    try:
                        crop[attr] = round(float(getattr(sh, attr, 0.0) or 0.0), 4)
                    except (TypeError, ValueError):
                        crop[attr] = None
                entry["crop"] = crop
                entry["rotation"] = round(float(getattr(sh, "rotation", 0) or 0), 2)
                try:
                    img = sh.image
                    m = {"embed_part": str(img.filename or ""),
                         "content_type": img.content_type,
                         "ext": img.ext,
                         "pixel_size": list(img.size),
                         "blob_sha256": hashlib.sha256(img.blob).hexdigest()}
                    entry["media"] = m
                    media.append({"shape": sh.name, **m,
                                  "box_in": [entry["left"], entry["top"],
                                             (entry["left"] or 0) + (entry["width"] or 0),
                                             (entry["top"] or 0) + (entry["height"] or 0)],
                                  "crop": crop})
                except Exception as exc:  # noqa: BLE001
                    # 兼容矢量图标 / asvg:svgBlip：python-pptx 的 sh.image
                    # 在遇到 SVG 扩展块时会抛出 ValueError: no embedded image。
                    # 通过 slide.part 关系引用找到对应的真实媒体部件并计算哈希。
                    found_part = None
                    try:
                        rIds = sh._element.xpath('.//@r:embed') if sh._element is not None else []
                        for rId in rIds:
                            try:
                                p = slide.part.related_part(rId)
                                if "image" in getattr(p, "content_type", "") or getattr(p, "partname", "").endswith(".svg"):
                                    found_part = p
                                    break
                            except Exception:
                                pass
                    except Exception:
                        pass
                    if found_part is not None:
                        blob = found_part.blob
                        pname = str(found_part.partname)
                        ext = pname.split(".")[-1] if "." in pname else ""
                        m = {"embed_part": pname,
                             "content_type": found_part.content_type,
                             "ext": ext,
                             "pixel_size": [0, 0],
                             "blob_sha256": hashlib.sha256(blob).hexdigest()}
                        entry["media"] = m
                        media.append({"shape": sh.name, **m,
                                      "box_in": [entry["left"], entry["top"],
                                                 (entry["left"] or 0) + (entry["width"] or 0),
                                                 (entry["top"] or 0) + (entry["height"] or 0)],
                                      "crop": crop})
                    else:
                        problems.append(f"media_unreadable[{sh.name}]: "
                                        f"{type(exc).__name__}: {exc}"[:160])
            try:
                if sh._element is not None:
                    fi = _fill_info(sh._element)
                    if fi:
                        entry["fill"] = fi
            except Exception:  # noqa: BLE001
                pass
            objects.append(entry)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"object[{z}]_unreadable: {type(exc).__name__}: {exc}"[:160])
    inv: dict = {"objects": objects, "media": media}
    # 备注（HG01 守恒之外的内容清单；sha 绑定内容）
    try:
        if slide.has_notes_slide:
            ntext = slide.notes_slide.notes_text_frame.text
            inv["notes"] = {"present": True, "chars": len(ntext),
                            "text_sha256": hashlib.sha256(
                                ntext.encode("utf-8")).hexdigest()}
        else:
            inv["notes"] = {"present": False, "chars": 0}
    except Exception as exc:  # noqa: BLE001
        problems.append(f"notes_unreadable: {type(exc).__name__}: {exc}"[:160])
    # 动画/转场（XML 级清单 + 序列化哈希绑定）
    try:
        el = slide._element
        timing = el.find(f"{P}timing")
        if timing is not None:
            counts = {}
            for tag in HG13_ANIM_TAGS:
                n = len(timing.findall(f".//{P}{tag}"))
                if n:
                    counts[tag] = n
            inv["animations"] = {
                "present": True,
                "effect_node_counts": counts,
                "timing_xml_sha256": hashlib.sha256(
                    etree.tostring(timing, method="xml")).hexdigest()}
        else:
            inv["animations"] = {"present": False, "effect_node_counts": {}}
        trans = el.find(f"{P}transition")
        if trans is not None:
            child = next((c.tag.split('}')[-1] for c in trans if isinstance(c.tag, str)),
                         None)
            inv["transition"] = {"present": True, "type": child,
                                 "attributes": dict(trans.attrib),
                                 "transition_xml_sha256": hashlib.sha256(
                                     etree.tostring(trans, method="xml")).hexdigest()}
        else:
            inv["transition"] = {"present": False}
    except Exception as exc:  # noqa: BLE001
        problems.append(f"timing_unreadable: {type(exc).__name__}: {exc}"[:160])
    return inv, problems


# --------------------------------------------------------------------------- HG10 原生证据
def _mp4_boxes(data, start: int, end: int) -> list:
    """读取一个 ISO-BMFF box 序列并拒绝截断、越界和零进度结构。"""
    boxes = []
    pos = start
    while pos < end:
        if end - pos < 8:
            raise ValueError("trailing_bytes")
        size = int.from_bytes(data[pos:pos + 4], "big")
        kind = bytes(data[pos + 4:pos + 8])
        header = 8
        if size == 1:
            if end - pos < 16:
                raise ValueError("truncated_extended_size")
            size = int.from_bytes(data[pos + 8:pos + 16], "big")
            header = 16
        if kind == b"uuid":
            header += 16
        if size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise ValueError("box_size_out_of_bounds")
        boxes.append({"type": kind, "start": pos, "payload": pos + header,
                      "end": pos + size, "size": size})
        pos += size
    return boxes


def _mp4_uint(data, start: int, width: int, end: int) -> int:
    if start < 0 or width <= 0 or start + width > end:
        raise ValueError("truncated_full_box")
    return int.from_bytes(data[start:start + width], "big")


def _mp4_children(data, box) -> list:
    return _mp4_boxes(data, box["payload"], box["end"])


def _mp4_required(boxes, kind: bytes):
    got = [b for b in boxes if b["type"] == kind]
    if len(got) != 1:
        raise ValueError("expected_one_" + kind.decode("ascii", "replace"))
    return got[0]


def _mp4_track_duration(data, mdhd) -> tuple[float, int, int]:
    p, end = mdhd["payload"], mdhd["end"]
    version = _mp4_uint(data, p, 1, end)
    if version == 1:
        timescale = _mp4_uint(data, p + 20, 4, end)
        declared = _mp4_uint(data, p + 24, 8, end)
    elif version == 0:
        timescale = _mp4_uint(data, p + 12, 4, end)
        declared = _mp4_uint(data, p + 16, 4, end)
    else:
        raise ValueError("unsupported_mdhd_version")
    if timescale <= 0 or declared <= 0:
        raise ValueError("invalid_mdhd_timing")
    return declared / timescale, timescale, declared


def _mp4_sample_sizes(data, box) -> list[int]:
    p, end = box["payload"], box["end"]
    if box["type"] == b"stsz":
        fixed = _mp4_uint(data, p + 4, 4, end)
        count = _mp4_uint(data, p + 8, 4, end)
        start = p + 12
        if count > 5_000_000:
            raise ValueError("sample_count_limit")
        if fixed:
            return [fixed] * count
        if start + count * 4 != end:
            raise ValueError("stsz_length_mismatch")
        return [_mp4_uint(data, start + i * 4, 4, end) for i in range(count)]
    if box["type"] != b"stz2":
        raise ValueError("missing_sample_size_table")
    field_size = _mp4_uint(data, p + 7, 1, end)
    count = _mp4_uint(data, p + 8, 4, end)
    start = p + 12
    if count > 5_000_000:
        raise ValueError("sample_count_limit")
    if field_size == 4:
        if start + (count + 1) // 2 != end:
            raise ValueError("stz2_length_mismatch")
        result = []
        for i in range(count):
            byte = _mp4_uint(data, start + i // 2, 1, end)
            result.append((byte >> 4) & 0xF if i % 2 == 0 else byte & 0xF)
        return result
    if field_size == 8:
        if start + count != end:
            raise ValueError("stz2_length_mismatch")
        return [_mp4_uint(data, start + i, 1, end) for i in range(count)]
    if field_size == 16:
        if start + count * 2 != end:
            raise ValueError("stz2_length_mismatch")
        return [_mp4_uint(data, start + i * 2, 2, end) for i in range(count)]
    raise ValueError("unsupported_stz2_field_size")


def _mp4_time_to_sample(data, box) -> tuple[int, int]:
    p, end = box["payload"], box["end"]
    count = _mp4_uint(data, p + 4, 4, end)
    start = p + 8
    if start + count * 8 != end:
        raise ValueError("stts_length_mismatch")
    samples = ticks = 0
    for i in range(count):
        n = _mp4_uint(data, start + i * 8, 4, end)
        delta = _mp4_uint(data, start + i * 8 + 4, 4, end)
        if n <= 0 or delta <= 0:
            raise ValueError("invalid_stts_entry")
        samples += n
        ticks += n * delta
    if samples <= 0 or ticks <= 0:
        raise ValueError("empty_stts")
    return samples, ticks


def _mp4_chunk_offsets(data, box) -> list[int]:
    p, end = box["payload"], box["end"]
    count = _mp4_uint(data, p + 4, 4, end)
    width = 8 if box["type"] == b"co64" else 4
    start = p + 8
    if count > 5_000_000 or start + count * width != end:
        raise ValueError("chunk_offset_table_invalid")
    return [_mp4_uint(data, start + i * width, width, end) for i in range(count)]


def _mp4_sample_to_chunk(data, box) -> list[tuple[int, int, int]]:
    p, end = box["payload"], box["end"]
    count = _mp4_uint(data, p + 4, 4, end)
    start = p + 8
    if count <= 0 or count > 100_000 or start + count * 12 != end:
        raise ValueError("stsc_table_invalid")
    entries = []
    for i in range(count):
        q = start + i * 12
        entries.append((_mp4_uint(data, q, 4, end),
                        _mp4_uint(data, q + 4, 4, end),
                        _mp4_uint(data, q + 8, 4, end)))
    if (entries[0][0] != 1
            or any(entries[i][0] >= entries[i + 1][0] for i in range(len(entries) - 1))
            or any(spc <= 0 or desc <= 0 for _, spc, desc in entries)):
        raise ValueError("stsc_entries_invalid")
    return entries


def _mp4_stsd_entry_count(data, box) -> int:
    p, end = box["payload"], box["end"]
    count = _mp4_uint(data, p + 4, 4, end)
    entries = _mp4_boxes(data, p + 8, end)
    if count <= 0 or len(entries) != count:
        raise ValueError("stsd_entries_invalid")
    return count


def _inspect_mp4(path) -> dict:
    """验证视频轨、样本表、mdat 负载与 30–60 秒真实视频轨时长。

    单独的 ftyp/moov/mvhd 只能说明文件声明了一个时长，不能证明录像存在。
    本函数要求 hdlr=vide、非空 stsd/stts/stsz/stsc/chunk-offset 表，且每个视频
    sample 的字节范围落在 mdat 内。帧是否能解码由 _decode_mp4_avfoundation 再验证。
    """
    try:
        pth = Path(path)
        if not pth.is_file() or pth.stat().st_size <= 0:
            raise ValueError("empty_or_missing_file")
        import mmap
        with pth.open("rb") as stream:
            with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
                top = _mp4_boxes(data, 0, len(data))
                if not any(b["type"] == b"ftyp" for b in top):
                    raise ValueError("missing_ftyp")
                moov = _mp4_required(top, b"moov")
                mdats = [b for b in top if b["type"] == b"mdat"]
                if not mdats or not any(b["end"] > b["payload"] for b in mdats):
                    raise ValueError("empty_mdat")
                moov_children = _mp4_children(data, moov)
                traks = [b for b in moov_children if b["type"] == b"trak"]
                video_tracks = []
                for trak in traks:
                    trak_children = _mp4_children(data, trak)
                    mdia = _mp4_required(trak_children, b"mdia")
                    mdia_children = _mp4_children(data, mdia)
                    hdlr = _mp4_required(mdia_children, b"hdlr")
                    handler_type = bytes(data[hdlr["payload"] + 8:hdlr["payload"] + 12])
                    if handler_type != b"vide":
                        continue
                    mdhd = _mp4_required(mdia_children, b"mdhd")
                    declared_seconds, timescale, _declared_ticks = _mp4_track_duration(data, mdhd)
                    minf = _mp4_required(mdia_children, b"minf")
                    stbl = _mp4_required(_mp4_children(data, minf), b"stbl")
                    stbl_children = _mp4_children(data, stbl)
                    stsd = _mp4_required(stbl_children, b"stsd")
                    description_count = _mp4_stsd_entry_count(data, stsd)
                    # 检查首个 VisualSampleEntry 的分辨率（拒绝零尺寸/伪造视频头）
                    stsd_entries = _mp4_boxes(data, stsd["payload"] + 8, stsd["end"])
                    width, height = 0, 0
                    if stsd_entries:
                        e0 = stsd_entries[0]
                        if e0["end"] >= e0["payload"] + 28:
                            width = _mp4_uint(data, e0["payload"] + 24, 2, e0["end"])
                            height = _mp4_uint(data, e0["payload"] + 26, 2, e0["end"])
                    if width < 640 or height < 360:
                        raise ValueError(f"video_dimensions_invalid: {width}x{height}")
                    sample_sizes_boxes = [b for b in stbl_children
                                          if b["type"] in (b"stsz", b"stz2")]
                    if len(sample_sizes_boxes) != 1:
                        raise ValueError("sample_size_table_missing_or_ambiguous")
                    sample_sizes = _mp4_sample_sizes(data, sample_sizes_boxes[0])
                    if not sample_sizes or any(n <= 0 for n in sample_sizes):
                        raise ValueError("video_samples_empty")
                    stts = _mp4_required(stbl_children, b"stts")
                    timed_samples, ticks = _mp4_time_to_sample(data, stts)
                    if timed_samples != len(sample_sizes):
                        raise ValueError("sample_count_mismatch")
                    # 拒绝帧数过少伪造（30–60s 实播录像在最低 5fps 下至少需要 150 帧）
                    if timed_samples < 150:
                        raise ValueError(f"video_samples_insufficient: {timed_samples} samples")
                    duration = ticks / timescale
                    if abs(duration - declared_seconds) > max(0.1, 2 / timescale):
                        raise ValueError("video_duration_mismatch")
                    fps = timed_samples / duration if duration > 0 else 0
                    if fps < 5.0 or fps > 120.0:
                        raise ValueError(f"video_framerate_out_of_range: {fps:.2f} fps")
                    stsc = _mp4_sample_to_chunk(data, _mp4_required(stbl_children, b"stsc"))
                    offset_boxes = [b for b in stbl_children
                                    if b["type"] in (b"stco", b"co64")]
                    if len(offset_boxes) != 1:
                        raise ValueError("chunk_offset_table_missing_or_ambiguous")
                    offsets = _mp4_chunk_offsets(data, offset_boxes[0])
                    if not offsets:
                        raise ValueError("video_chunks_empty")
                    sample_index = 0
                    sample_bytes = 0
                    for chunk_number, chunk_offset in enumerate(offsets, 1):
                        active = [e for e in stsc if e[0] <= chunk_number]
                        if not active:
                            raise ValueError("chunk_without_stsc_mapping")
                        _first, per_chunk, description_index = active[-1]
                        if description_index > description_count:
                            raise ValueError("invalid_sample_description_index")
                        last = sample_index + per_chunk
                        if last > len(sample_sizes):
                            raise ValueError("chunk_sample_count_overflow")
                        chunk_bytes = sum(sample_sizes[sample_index:last])
                        chunk_end = chunk_offset + chunk_bytes
                        if not any(chunk_offset >= m["payload"] and chunk_end <= m["end"]
                                   for m in mdats):
                            raise ValueError("video_sample_outside_mdat")
                        sample_index = last
                        sample_bytes += chunk_bytes
                    if sample_index != len(sample_sizes) or sample_bytes <= 0:
                        raise ValueError("video_sample_payload_incomplete")
                    # 拒绝 mdat 真实负载过小伪造（30–60s 真实屏幕录像至少 300 KiB 视频负载）
                    if sample_bytes < 300_000:
                        raise ValueError(f"video_payload_bytes_insufficient: {sample_bytes} bytes")
                    video_tracks.append({"duration_seconds": duration,
                                         "sample_count": timed_samples,
                                         "sample_payload_bytes": sample_bytes,
                                         "sample_description_count": description_count,
                                         "width": width, "height": height, "fps": round(fps, 2)})
                if not video_tracks:
                    raise ValueError("no_video_track")
                chosen = max(video_tracks, key=lambda t: t["sample_payload_bytes"])
                return {"ok": True, "duration_seconds": chosen["duration_seconds"],
                        "video_track_count": len(video_tracks),
                        "sample_count": chosen["sample_count"],
                        "sample_payload_bytes": chosen["sample_payload_bytes"],
                        "width": chosen["width"], "height": chosen["height"],
                        "fps": chosen["fps"]}
    except (OSError, ValueError, OverflowError, IndexError) as exc:
        return {"ok": False, "error": str(exc)[:180]}
    return {"ok": False, "error": "unreachable"}


def _mp4_duration_seconds(path) -> float | None:
    """返回经视频轨样本表验证的时长；只有 mvhd 的伪头返回 None。"""
    info = _inspect_mp4(path)
    return info.get("duration_seconds") if info.get("ok") else None


def _decode_mp4_avfoundation(path, timeout_seconds: int = 180) -> tuple[bool | None, str]:
    """用 macOS AVFoundation 完整转码视频流以强制逐帧解码；临时输出会删除。"""
    executable = shutil.which("avconvert")
    if not executable:
        return None, "avconvert_unavailable"
    try:
        with tempfile.TemporaryDirectory(prefix="wg-native-decode-") as td:
            output = Path(td) / "decoded.mp4"
            result = subprocess.run(
                [executable, "--source", str(path), "--preset", "Preset640x480",
                 "--output", str(output), "--replace"],
                capture_output=True, text=True, timeout=timeout_seconds)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "decode_failed").strip()
                return False, detail[-500:]
            if not output.is_file() or output.stat().st_size <= 0:
                return False, "decoder_created_no_output"
            # 完整逐帧转码后，再次机证转码产物的视频轨、样本数与时长（防截断/空流伪造）
            dec_info = _inspect_mp4(output)
            if not dec_info.get("ok"):
                return False, f"decoded_stream_invalid: {dec_info.get('error')}"
            if dec_info.get("sample_count", 0) < 150:
                return False, f"decoded_stream_samples_insufficient: {dec_info.get('sample_count')}"
            dec_dur = dec_info.get("duration_seconds", 0)
            if not (28.0 <= dec_dur <= 62.0):
                return False, f"decoded_stream_duration_mismatch: {dec_dur:.2f}s"
            return True, "avfoundation_full_transcode_completed"
    except subprocess.TimeoutExpired:
        return None, "avfoundation_decode_timeout"
    except OSError as exc:
        return None, "avfoundation_decode_error:" + str(exc)[:180]


def _validate_native_review_record(rev: dict, pptx_sha: str, video_sha: str,
                                   sel_pages: list) -> tuple[bool, list]:
    """验证逐页、动效、身份绑定的复核内容；普通 reviewer 字符串不再算复核。"""
    unverified = []

    def U(kind, note, **kw):
        unverified.append({"gate": "HG10", "type": kind, "note": note, **kw})

    rp = rev.get("record_path")
    rsha = rev.get("record_sha256")
    if not isinstance(rp, str) or not Path(rp).is_file():
        U("PLAYBACK_REVIEW_RECORD_MISSING", "复核记录文件不在盘：审查证据未证")
        return False, unverified
    actual = sha256(rp)
    if not isinstance(rsha, str) or len(rsha) != 64 or actual != rsha:
        U("PLAYBACK_REVIEW_RECORD_IDENTITY", "复核记录实际哈希与记录不一致")
        return False, unverified
    try:
        record = json.loads(Path(rp).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        U("PLAYBACK_REVIEW_RECORD_INVALID", "复核记录不是有效 UTF-8 JSON")
        return False, unverified
    if not isinstance(record, dict) or record.get("schema") != "WG-NATIVE-REVIEW/1":
        U("PLAYBACK_REVIEW_SCHEMA_INVALID", "复核记录缺少 WG-NATIVE-REVIEW/1 完整审查结构")
        return False, unverified
    if record.get("pptx_sha256") != pptx_sha or record.get("video_sha256") != video_sha:
        U("PLAYBACK_REVIEW_BINDING_MISMATCH", "复核记录未绑定本次最终 PPTX 与录像哈希")
    reviewer = record.get("reviewer")
    if (not isinstance(reviewer, dict) or not isinstance(reviewer.get("id"), str)
            or not reviewer["id"].strip() or reviewer.get("independent") is not True):
        U("PLAYBACK_REVIEWER_UNVERIFIED", "复核人必须以独立复核者身份记录")
    else:
        rid = reviewer["id"].strip().lower()
        if len(rid) < 4 or rid in ("test", "tester", "admin", "mock", "synthetic",
                                   "dummy", "bot", "auto", "reviewer", "anonymous"):
            U("PLAYBACK_REVIEWER_GENERIC", "复核人身份标识不得使用通用占位符或自动化名称")
    if record.get("overall") != "PASS":
        U("PLAYBACK_REVIEW_NOT_PASSED", "逐页原生审查必须给出 PASS")
    app = record.get("playback_application")
    if not isinstance(app, str) or not app.strip() or len(app.strip()) < 4 or \
            app.strip().lower() in ("none", "null", "test", "app", "player", "unknown", "placeholder"):
        U("PLAYBACK_APPLICATION_UNRECORDED", "缺少真实放映应用记录或为占位符")
    if record.get("reopened_pptx_sha256") != pptx_sha:
        U("PLAYBACK_REVIEW_REOPEN_MISMATCH", "复核记录未确认磁盘重开件身份一致")
    if record.get("reopened") is not True:
        U("PLAYBACK_REVIEW_REOPEN_NOT_CONFIRMED",
          "复核记录必须明确确认已关闭后从磁盘重开同一候选")
    pages = record.get("pages")
    if not isinstance(pages, list) or not pages:
        U("PLAYBACK_REVIEW_PAGES_EMPTY", "逐页复核记录列表为空")
        pages = []
    valid_page_items = [x for x in pages
                        if (isinstance(x, dict) and isinstance(x.get("page"), int)
                            and not isinstance(x.get("page"), bool))]
    by_page = {x["page"]: x for x in valid_page_items}
    if (sorted(by_page) != sorted(sel_pages) or len(by_page) != len(pages)
            or len(by_page) != len(valid_page_items)):
        U("PLAYBACK_REVIEW_PAGE_COVERAGE", "逐页复核记录必须完整且无重复覆盖本次检查页",
          expected_pages=sorted(sel_pages), recorded_pages=sorted(by_page))

    # 逐页观察内容实质性核查（杜绝自动化模板化合成复制与空占位）
    obs_list = [str(x.get("observations") or "").strip() for x in valid_page_items]
    if len(valid_page_items) > 1 and len(set(obs_list)) < len(valid_page_items):
        U("PLAYBACK_REVIEW_DUPLICATE_OBSERVATIONS",
          "逐页复核观察记录存在重复或模板化复制，每页必须提供具体的独立观察描述")

    placeholder_tokens = ("todo", "占位", "测试", "mock", "dummy", "synthetic",
                          "placeholder", "n/a", "null", "none", "待补充", "无内容")
    for page in sel_pages:
        item = by_page.get(page)
        if not item:
            continue
        note = item.get("observations")
        if (item.get("visible_content") is not True or item.get("no_clipping") is not True
                or item.get("no_occlusion") is not True
                or not isinstance(note, str) or len(note.strip()) < 15):
            U("PLAYBACK_REVIEW_PAGE_INCOMPLETE",
              "每页必须记录可见、无裁切、无遮挡及不少于15字具体观察", page=page)
        elif len(set(note.strip())) < 10:
            U("PLAYBACK_REVIEW_OBSERVATION_TRIVIAL",
              "逐页观察内容缺乏实质描述（字符多样性过低）", page=page)
        elif any(tok in note.lower() for tok in placeholder_tokens):
            U("PLAYBACK_REVIEW_PLACEHOLDER_DETECTED",
              "逐页观察记录包含占位符或未完成标记", page=page)

    animation = record.get("animation_review")
    expected_animation_pages = [p for p in (1, 7, 13) if p in sel_pages]
    animation_pages = animation.get("pages") if isinstance(animation, dict) else None
    animation_pages_valid = (isinstance(animation_pages, list)
                             and all(isinstance(p, int) and not isinstance(p, bool)
                                     for p in animation_pages))
    anim_obs = animation.get("observations", "").strip() if isinstance(animation, dict) and isinstance(animation.get("observations"), str) else ""
    if (not isinstance(animation, dict) or animation.get("checked") is not True
            or not animation_pages_valid
            or sorted(animation_pages) != expected_animation_pages
            or len(anim_obs) < 15
            or len(set(anim_obs)) < 10
            or any(tok in anim_obs.lower() for tok in placeholder_tokens)):
        U("PLAYBACK_REVIEW_ANIMATION_INCOMPLETE",
          "P1/P7/P13（本次覆盖页内）的粒子动效需逐项实播观察并记录不少于15字实质描述")
    elif anim_obs in set(obs_list):
        U("PLAYBACK_REVIEW_ANIMATION_DUPLICATE",
          "动效复核观察记录不得直接复制页面观察内容")
    completed_at = record.get("completed_at")
    try:
        from datetime import datetime
        parsed = datetime.fromisoformat(str(completed_at).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone_required")
    except (TypeError, ValueError):
        U("PLAYBACK_REVIEW_TIME_INVALID", "复核时间必须是带时区的 ISO-8601")
    # 内容完整仅表示有复核陈述，不是签名认证；证据复核者仍须独立查看原录像。
    return not unverified, unverified


def _validate_native_playback(np_ev: dict, pptx_sha: str, sel_pages: list) -> dict:
    """HG10 原生实播证据校验（fail-closed，监督 1142/1643/1945 反例）。

    机证项：最终 PPTX 身份、视频 SHA、实际视频轨/样本/mdat 负载、30–60 秒视频轨
    时长、AVFoundation 完整转码解码、全页覆盖和磁盘重开身份。独立复核须是具备
    schema、身份绑定、逐页观察与动效观察的 JSON；任意非空文件或 reviewer 字符串
    不构成复核。FAIL/UNVERIFIED 均阻断；复核身份仍由独立验收者核实。
    """
    findings: list = []
    unverified: list = []

    def F(t: str, **kw):
        findings.append({"gate": "HG10", "type": t, **kw})

    ok = True
    # 1) 身份绑定
    np_sha = np_ev.get("pptx_sha256")
    if not isinstance(np_sha, str) or len(np_sha) != 64:
        F("PLAYBACK_IDENTITY_INVALID", got=repr(np_sha)[:80],
          note="必须绑定最终确切 PPTX SHA256")
        ok = False
    elif np_sha != pptx_sha:
        F("PLAYBACK_IDENTITY_MISMATCH", expected=pptx_sha, recorded=np_sha,
          note="native_playback 绑定的 PPTX 与本次被检件不一致")
        ok = False
    # 2) 录像文件存在 + 实际哈希 + 门禁自测时长
    vp = np_ev.get("video_path")
    measured = None
    media_info = None
    decode_status = None
    actual_video_sha = None
    if not isinstance(vp, str) or not vp.strip() or not Path(vp).is_file():
        F("PLAYBACK_VIDEO_MISSING", path=str(vp)[:200],
          note="录像文件必须真实存在（不存在路径不能作为证据）")
        ok = False
        vp = None
    else:
        vsha = np_ev.get("video_sha256")
        if not isinstance(vsha, str) or len(vsha) != 64:
            F("PLAYBACK_VIDEO_HASH_INVALID",
              note="必须提供录像文件 SHA256（哈希缺失不构成证据）")
            ok = False
        else:
            actual_video_sha = sha256(vp)
            if actual_video_sha != vsha:
                F("PLAYBACK_VIDEO_HASH_MISMATCH", expected=vsha, actual=actual_video_sha,
                  note="录像文件实际哈希与记录不一致")
                ok = False
            else:
                media_info = _inspect_mp4(vp)
                if not media_info.get("ok"):
                    F("PLAYBACK_VIDEO_STREAM_INVALID",
                      media_error=media_info.get("error"),
                      note="必须有可定位到 mdat 的非空视频轨样本，mvhd 单独声明时长不算录像")
                    ok = False
                else:
                    measured = media_info["duration_seconds"]
                    if not 30.0 <= measured <= 60.0:
                        F("PLAYBACK_DURATION_OUT_OF_RANGE", measured_seconds=round(measured, 2),
                          required_seconds=[30, 60],
                          note="须 30–60 秒视频轨时长（由 stts 样本时序计算）")
                        ok = False
                    else:
                        decoded, decode_detail = _decode_mp4_avfoundation(vp)
                        decode_status = {"decoded": decoded, "detail": decode_detail}
                        if decoded is False:
                            F("PLAYBACK_VIDEO_DECODE_FAILED",
                              note="AVFoundation 无法完整解码录像视频流",
                              decoder_detail=decode_detail)
                            ok = False
                        elif decoded is None:
                            unverified.append({"gate": "HG10",
                                               "type": "PLAYBACK_VIDEO_DECODE_UNVERIFIED",
                                               "note": "缺少可用解码器或完整解码超时",
                                               "decoder_detail": decode_detail})
                            ok = False
    # 3) 最终全部页面覆盖
    pages_cov = np_ev.get("pages")
    cov_ok = False
    if isinstance(pages_cov, list) and pages_cov:
        try:
            cov_ok = sorted({int(x) for x in pages_cov}) == sorted(sel_pages)
        except (TypeError, ValueError):
            cov_ok = False
    if not cov_ok:
        F("PLAYBACK_PAGE_COVERAGE_INVALID", expected_pages=sorted(sel_pages),
          got=pages_cov, note="录像必须覆盖最终全部页面")
        ok = False
    # 4) 关闭后从磁盘重开身份
    ro = np_ev.get("reopen")
    if not isinstance(ro, dict):
        F("PLAYBACK_REOPEN_EVIDENCE_INVALID", got=type(ro).__name__,
          note="需「关闭仅本验收副本后从磁盘重开」的身份证据")
        ok = False
    else:
        ro_sha = ro.get("reopened_pptx_sha256")
        if ro.get("reopened") is not True:
            F("PLAYBACK_REOPEN_NOT_RECORDED",
              note="reopened 必须为 true（重开已发生）；自报 false 即阻断")
            ok = False
        elif not isinstance(ro_sha, str) or len(ro_sha) != 64 or ro_sha != pptx_sha:
            F("PLAYBACK_REOPEN_IDENTITY_MISMATCH", expected=pptx_sha,
              got=repr(ro_sha)[:80], note="重开件 SHA 必须与被检件一致")
            ok = False
    # 5) 独立视觉复核记录（不可机证项的承载；缺失/不完整 → UNVERIFIED 阻断）
    rev = np_ev.get("independent_review")
    review_schema_valid = False
    if not isinstance(rev, dict):
        unverified.append({"gate": "HG10", "type": "PLAYBACK_INDEPENDENT_REVIEW_MISSING",
                           "note": "缺独立视觉复核记录：逐页可见性与动效未证，阻断"})
    else:
        if actual_video_sha is None:
            unverified.append({"gate": "HG10", "type": "PLAYBACK_REVIEW_VIDEO_UNBOUND",
                               "note": "录像身份未通过机证，复核记录不能绑定有效视频"})
        else:
            review_schema_valid, review_issues = _validate_native_review_record(
                rev, pptx_sha, actual_video_sha, sel_pages)
            unverified.extend(review_issues)
    summary = {"machine_checks_pass": ok and not unverified,
               "review_record_present": isinstance(rev, dict),
               "native_playback_valid": bool(findings == [] and unverified == []),
               "review_record_schema_valid": review_schema_valid,
               "review_attestation_authenticity": "not_cryptographically_verified",
               "video_inspection": media_info,
               "video_decode": decode_status,
               "measured_duration_seconds": (round(measured, 2)
                                             if measured is not None else None)}
    return {"findings": findings, "unverified": unverified, "summary": summary}


# --------------------------------------------------------------------------- 主检查
def check(pptx: Path, pdf: Path, pages=None, baseline: Path | None = None,
          expect_pptx_sha: str | None = None, policy: dict | None = None,
          png_full=None, png_notext=None, outdir: Path | None = None,
          static_only: bool = True, native_playback: dict | None = None,
          source_fidelity_verified: bool = False) -> dict:
    import pypdfium2 as pdfium
    from PIL import Image

    prs = Presentation(str(pptx))
    doc = pdfium.PdfDocument(str(pdf))
    W_IN, H_IN = prs.slide_width / EMU, prs.slide_height / EMU
    findings = []
    per_page = {}
    unverified = []
    n_slides = len(prs.slides._sldIdLst)
    pptx_sha = sha256(pptx)      # 提前：HG10 原生证据绑定需要实际被检件身份
    hg10_native = None           # native_playback 结构校验汇总（无证据时为 None）

    # ---------- 身份链扩展（P0 2026-10-23）：build/source 身份由门禁从磁盘自算 ----------
    build_identity = None
    try:
        from wengao_ppt.install import build_id as _wengao_build_id
        build_identity = {"program_root": str(ROOT),
                          "build_id": _wengao_build_id(ROOT),
                          "build_id_scope": "程序成员聚合（wengao_ppt.install.build_id："
                                            "排除数据根/运行期文件；门禁自算，不接受自报）"}
    except Exception as exc:  # noqa: BLE001
        unverified.append({"gate": "HG11", "type": "BUILD_ID_UNAVAILABLE",
                           "error": f"{type(exc).__name__}: {exc}"[:200],
                           "note": "构建身份无法从磁盘重算：身份链不完整，禁止默认一致"})

    # ---------- fail-closed 前置校验（监督 0947 清单第 1 条：零页/缺证据=假通过） ----------
    if pages is None:
        sel = list(range(1, n_slides + 1))
    else:
        sel = sorted({int(p) for p in pages})
        illegal = [p for p in sel if p < 1 or p > n_slides]
        if illegal:
            findings.append({"gate": "HG10", "type": "ILLEGAL_PAGE_SELECTION",
                             "illegal_pages": illegal, "actual_pages": n_slides,
                             "note": "页号越界即 FAIL，不允许空检查静默 PASS"})
        if not sel:
            findings.append({"gate": "HG10", "type": "EMPTY_PAGE_SELECTION",
                             "actual_pages": n_slides,
                             "note": "空页选择不产生任何检查记录，必须 FAIL"})
    if 0 < len(sel) < n_slides:
        unverified.append({"gate": "HG10", "type": "PARTIAL_PAGE_SCOPE",
                           "selected": sel, "actual_pages": n_slides,
                           "note": "诊断子集可以有界子结果，但不构成全稿合格判定"})
    if not png_full or not png_notext:
        unverified.append({"gate": "HG02", "type": "MISSING_RENDER_EVIDENCE",
                           "png_full_provided": bool(png_full),
                           "png_notext_provided": bool(png_notext),
                           "note": "缺全字/无字渲染证据：可见性无法证明，禁止默认可见"})
    else:
        for p in sel:
            if p not in png_full:
                unverified.append({"gate": "HG02", "type": "MISSING_FULL_RENDER",
                                   "page": p})
            if p not in png_notext:
                unverified.append({"gate": "HG02", "type": "MISSING_NOTEXT_RENDER",
                                   "page": p})
    if ((baseline is None or not Path(baseline).exists())
            and not source_fidelity_verified):
        unverified.append({"gate": "HG01", "type": "MISSING_BASELINE_FACTS",
                           "note": "缺基线事实：文本/备注守恒无法核验，不默认守恒"})
    if not policy or not policy.get("document_sha256"):
        unverified.append({"gate": "HG11", "type": "MISSING_POLICY_IDENTITY",
                           "note": "缺政策文档身份（document_sha256 必须来自真实政策文件）"})
    if expect_pptx_sha is None:
        unverified.append({"gate": "HG11", "type": "UNBOUND_EXPECTED_IDENTITY",
                           "note": "未绑定最终期望 PPTX 哈希：身份链不完整"})
    # 2026-10-27 按明确要求去掉：原生实播复核（人工录屏打开真实 PowerPoint/
    # Keynote、关闭重开核哈希）不再是通过条件。缺证据/只跑了静态检查都不再
    # 记 unverified、不再阻断 verdict=PASS；静态页面检查（HG01–HG09、HG11）
    # 仍然照常执行并阻断。如果确实提供了证据，仍按结构与必要字段校验——
    # 提供了错的/假的证据照样算真失败，只是不再要求必须提供。
    if native_playback is not None:
        _npv = _validate_native_playback(native_playback, pptx_sha, sel)
        findings.extend(_npv["findings"])
        unverified.extend(_npv["unverified"])
        hg10_native = _npv["summary"]

    # 基线文本（HG01 守恒，按形状 id→拼接文本；兼容 a:br 语义换行）
    base_texts = {}
    bprs = None
    if baseline is not None and Path(baseline).exists():
        bprs = Presentation(str(baseline))
        for i, sl in enumerate(bprs.slides, 1):
            rows = {}
            for sh in sl.shapes:
                if not (getattr(sh, "has_text_frame", False)
                        or getattr(sh, "has_table", False)):
                    continue
                text = _concat_shape_text(sh)
                # 空装饰框/粒子占位不承载事实；非空 shape 被删空仍会因
                # 基线键缺失而 FAIL，不能以过滤空形状掩盖文字丢失。
                if text.strip():
                    rows[str(sh.shape_id)] = text
            base_texts[i] = rows

    # 无字版像素（可见性差分）
    notext_imgs = {}
    if png_notext:
        for pg, (p, _s, size) in png_notext.items():
            notext_imgs[pg] = Image.open(p).convert("RGB")

    total_decor_count = 0

    for pageno, slide in enumerate(prs.slides, 1):
        if pageno not in sel:
            continue
        shapes = slide_shapes(slide)
        roles = infer_roles(shapes)
        chars = page_chars(doc[pageno - 1])
        mapping, unmatched = assign_chars(chars, shapes)

        pg_find = []
        pg = {"shapes": len(shapes), "chars_rendered": len(chars),
              "roles": {shapes[i]["name"]: r for i, r in roles.items()},
              "render_evidence": {"full": pageno in (png_full or {}),
                                  "notext": pageno in (png_notext or {})}}

        # ---------- HG01 内容守恒（形状级拼接文本，兼容 a:br） ----------
        if pageno in base_texts:
            cur = {}
            sanctioned_ids = set()
            for sh in slide.shapes:
                if not (getattr(sh, "has_text_frame", False)
                        or getattr(sh, "has_table", False)):
                    continue
                text = _concat_shape_text(sh)
                if text.strip():
                    sid = str(sh.shape_id)
                    cur[sid] = text
                    if sid not in base_texts[pageno] and _text_card_sanctioned(sh, text):
                        sanctioned_ids.add(sid)
            cur_checked = {k: v for k, v in cur.items() if k not in sanctioned_ids}
            if cur_checked != base_texts[pageno]:
                missing_ids = sorted(set(base_texts[pageno]) - set(cur_checked), key=int)
                added_ids = sorted(set(cur_checked) - set(base_texts[pageno]), key=int)
                changed = sorted(
                    (k for k in set(cur_checked) & set(base_texts[pageno])
                     if cur_checked[k] != base_texts[pageno][k]), key=int)
                pg_find.append({"gate": "HG01", "type": "TEXT_CONTENT_CHANGED",
                                "page": pageno, "missing_shapes": missing_ids,
                                "added_shapes": added_ids,
                                "changed_shapes": changed[:20],
                                "note": "按形状拼接文本比较（字符序列守恒，兼容 a:br）"})
        # 备注守恒
        if bprs is not None:
            bslide = bprs.slides[pageno - 1]
            nb = bslide.notes_slide.notes_text_frame.text if bslide.has_notes_slide else ""
            nc = slide.notes_slide.notes_text_frame.text if slide.has_notes_slide else ""
            if nb != nc and not _notes_photo_credit_sanctioned(slide, nb, nc):
                pg_find.append({"gate": "HG01", "type": "NOTES_CHANGED", "page": pageno})

        # ---------- HG02 逐字覆盖 + 可见性 ----------
        expected = {}   # shape_idx -> [chars]
        for i, s in enumerate(shapes):
            if s.get("chart_native"):
                expected[i] = [c for token in s.get("chart_required_tokens", [])
                               for c in token if not c.isspace()]
                if not s.get("chart_required_tokens"):
                    unverified.append({"gate": "HG02", "type": "CHART_TEXT_EXPECTATION_EMPTY",
                                       "page": pageno, "shape": s["name"]})
                for note in s.get("chart_meta", {}).get("notes", []):
                    unverified.append({"gate": "HG02", "type": "CHART_TEXT_SOURCE_UNRESOLVED",
                                       "page": pageno, "shape": s["name"],
                                       "detail": note})
                continue
            if not s["text"].strip():
                continue
            expected_chars = [c for c in s["text"] if not c.isspace()]
            source_shape = slide.shapes[i]
            if getattr(source_shape, "has_text_frame", False):
                for para_idx, para in enumerate(source_shape.text_frame.paragraphs):
                    if not para.text.strip():
                        continue
                    marker, bullet_status = _effective_paragraph_bullet(
                        slide, source_shape, para, para_idx)
                    if bullet_status == "char" and marker:
                        expected_chars.append(marker)
                    elif bullet_status == "unresolved":
                        unverified.append({"gate": "HG02", "type": "PARAGRAPH_BULLET_UNRESOLVED",
                                           "page": pageno, "shape": s["name"],
                                           "paragraph": para_idx,
                                           "note": "自动编号/继承列表标记未解析，不默认内容一致"})
            expected[i] = expected_chars
        mapped_by_shape = {}
        for ci, si in mapping.items():
            mapped_by_shape.setdefault(si, []).append(chars[ci])
        missing, partial, invisible = [], [], []
        # P0 门禁（2026-10-23）：逐字可见性证据图，供 HG04 遮挡候选用真实
        # 渲染证据消解（证据化而非放行）。键 = (shape 名, 字符, 字形框)。
        vis_map = {}
        full_img = None
        if png_full and pageno in png_full:
            full_img = Image.open(png_full[pageno][0]).convert("RGB")
        for si, exp in expected.items():
            if roles.get(si) == "decor":
                continue
            got = mapped_by_shape.get(si, [])
            got_txt = "".join(c["ch"] for c in got)
            exp_txt = "".join(exp)
            if shapes[si].get("chart_native"):
                # Chart text is generated from native chart data, not slide a:t.
                # Require every category/data-label token, then allow only numeric
                # axis ticks inside an explicitly declared range. This accounts for
                # visible chart-engine glyphs without whitelisting arbitrary text.
                raw_actual = "".join(c["ch"] for c in got)
                remaining = raw_actual
                missing_tokens = []
                for token in shapes[si].get("chart_required_tokens", []):
                    chars_only = [ch for ch in token if not ch.isspace()]
                    if not chars_only:
                        continue
                    pat = re.compile(r"\s*".join(re.escape(ch) for ch in chars_only))
                    m = pat.search(remaining)
                    if m is None:
                        missing_tokens.append(token)
                    else:
                        remaining = remaining[:m.start()] + remaining[m.end():]
                if missing_tokens:
                    pg_find.append({"gate": "HG02", "type": "CHART_TEXT_MISSING",
                                    "page": pageno, "shape": shapes[si]["name"],
                                    "missing_tokens": missing_tokens[:30]})
                # Chinese characters are Unicode \w, so a locale-sensitive word
                # boundary would reject valid labels such as "增长12.5%". Bound
                # only against ASCII numeric/Latin continuations instead.
                numeric = _chart_axis_numeric_groups(got,
                    shapes[si].get("chart_required_tokens", []))
                residue = re.sub(
                    r"(?<![A-Za-z0-9.])[-+]?\d+(?:\.\d+)?%?(?![A-Za-z0-9.])",
                    "", remaining)
                residue = re.sub(r"[\s，。、；：！？,.():：;·—…%+\-−]", "", residue)
                if residue:
                    pg_find.append({"gate": "HG02", "type": "CHART_TEXT_UNEXPECTED",
                                    "page": pageno, "shape": shapes[si]["name"],
                                    "unexpected": residue[:80]})
                axis = shapes[si].get("chart_meta", {}).get("axis", {})
                lo, hi = axis.get("minimum"), axis.get("maximum")
                out_of_range = []
                if numeric and (lo is None or hi is None):
                    unverified.append({"gate": "HG02", "type": "CHART_AXIS_RANGE_UNRESOLVED",
                                       "page": pageno, "shape": shapes[si]["name"],
                                       "ticks": numeric})
                elif numeric:
                    for tick in numeric:
                        try:
                            value = float(tick.rstrip("%"))
                        except ValueError:
                            out_of_range.append(tick)
                            continue
                        if value < float(lo) - 1e-6 or value > float(hi) + 1e-6:
                            out_of_range.append(tick)
                if out_of_range:
                    pg_find.append({"gate": "HG02", "type": "CHART_AXIS_TICK_OUT_OF_RANGE",
                                    "page": pageno, "shape": shapes[si]["name"],
                                    "axis": [lo, hi], "ticks": out_of_range})
                # Do not compare chart-engine axis ticks to source text character
                # counts; required labels were checked as exact tokens above.
                # Fall through to the same per-glyph pixel visibility proof used
                # by ordinary text; native chart labels are not exempt from it.
            else:
                # 多重集合比对（顺序由版式决定，这里只要求字符齐备且不重复计入）
                from collections import Counter
                ce, cg = Counter(exp_txt), Counter(got_txt)
                miss = ce - cg
                extra = cg - ce
                if miss:
                    missing.append({"shape": shapes[si]["name"],
                                    "missing": "".join(sorted(miss.elements()))})
                if extra:
                    missing.append({"shape": shapes[si]["name"],
                                    "unexpected_extra": "".join(sorted(extra.elements()))})
            # 可见性（像素差分 + 隔离笔画覆盖 vs 该字实测墨迹期望）
            if full_img is not None and pageno in notext_imgs:
                nb_img = notext_imgs[pageno]
                sx = full_img.size[0] / W_IN      # px/英寸（坐标已是英寸）
                sy = full_img.size[1] / H_IN
                import numpy as np
                for c in got:
                    px0 = max(int(c["x0"] * sx) - 1, 0)
                    py0 = max(int(c["y0"] * sy) - 1, 0)
                    px1 = min(int(c["x1"] * sx) + 1, full_img.size[0])
                    py1 = min(int(c["y1"] * sy) + 1, full_img.size[1])
                    if px1 <= px0 or py1 <= py0:
                        unverified.append({"gate": "HG02", "type": "INVALID_GLYPH_CROP",
                                           "page": pageno, "shape": shapes[si]["name"],
                                           "ch": c["ch"],
                                           "box": [round(c["x0"], 3), round(c["y0"], 3),
                                                   round(c["x1"], 3), round(c["y1"], 3)],
                                           "note": "无效裁剪不跳过：默认可见被禁止"})
                        continue
                    ca = full_img.crop((px0, py0, px1, py1))
                    cb = nb_img.crop((px0, py0, px1, py1))
                    if ca.size != cb.size:
                        unverified.append({"gate": "HG02", "type": "CROP_SIZE_MISMATCH",
                                           "page": pageno, "shape": shapes[si]["name"],
                                           "ch": c["ch"], "sizes": [ca.size, cb.size]})
                        continue
                    d = np.abs(np.asarray(ca, dtype=np.int16)
                               - np.asarray(cb, dtype=np.int16)).max(axis=2)
                    exp = expected_ink_ratio(c["ch"], px1 - px0, py1 - py0)
                    emask = expected_glyph_mask(c["ch"], px1 - px0, py1 - py0)
                    if exp is None or emask is None:
                        unverified.append({"gate": "HG02", "type": "EXPECTED_INK_UNRESOLVED",
                                           "page": pageno, "shape": shapes[si]["name"],
                                           "ch": c["ch"],
                                           "note": "缺字体/渲染异常/非空白零墨迹："
                                                   "期望不可建立，禁止默认可见"})
                        continue
                    diffm = d >= VIS_DIFF_TH
                    box_ratio = float(diffm.mean())
                    cov = _stroke_coverage(diffm, emask)
                    rec = {"shape": shapes[si]["name"], "ch": c["ch"],
                           "box": [round(c["x0"], 3), round(c["y0"], 3),
                                   round(c["x1"], 3), round(c["y1"], 3)],
                           "diff_ratio": round(box_ratio, 4),
                           "expected_ink": round(exp, 4),
                           "stroke_coverage": round(cov, 4)}
                    # 主判定 = 隔离笔画覆盖率（对边缘抗锯齿鲁棒：膨胀 2px 容差）；
                    # 框占比法仅保留极端不可见检测（diff 接近 0）。
                    # 监督 GATE-PARTIAL-03：>=60% 墨迹当全显被禁止，全显须 cov≥0.70。
                    # 对于超薄笔画（像素高度<=8px 或单个横线字符'一'/'—'等），由于 PIL resize
                    # 伪影会虚高掩码面积，全显门限自适应调整至 0.35。
                    cov_thresh = 0.35 if (py1 - py0 <= 8 or c["ch"] in ("一", "—", "-", "–", "―")) else 0.70
                    invis = cov < 0.30 or (cov < cov_thresh and box_ratio < 0.04)
                    part = (not invis) and cov < cov_thresh
                    vis_map[(shapes[si]["name"], c["ch"],
                             (round(c["x0"], 3), round(c["y0"], 3),
                              round(c["x1"], 3), round(c["y1"], 3)))] = {
                        "verdict": "invisible" if invis else ("partial" if part else "visible"),
                        "stroke_coverage": round(cov, 4),
                        "diff_ratio": round(box_ratio, 4)}
                    if invis:
                        invisible.append(rec)
                    elif part:
                        partial.append({**rec,
                                        "visible_ratio": round(cov, 3)})
        for m in missing:
            pg_find.append({"gate": "HG02", "type": "CHAR_MISSING", "page": pageno, **m})
        for c in invisible:
            pg_find.append({"gate": "HG02", "type": "CHAR_INVISIBLE",
                            "page": pageno, **c})
        for c in partial:
            pg_find.append({"gate": "HG02", "type": "CHAR_PARTIAL_VISIBLE",
                            "page": pageno, **c})
        for ci in unmatched:
            c = chars[ci]
            pg_find.append({"gate": "HG02", "type": "CHAR_UNMATCHED", "page": pageno,
                            "ch": c["ch"],
                            "box": [round(c["x0"], 3), round(c["y0"], 3),
                                    round(c["x1"], 3), round(c["y1"], 3)]})
        pg["expected_chars"] = sum(len(v) for v in expected.values())
        pg["mapped_chars"] = len(mapping)
        pg["unmatched_chars"] = len(unmatched)
        pg["ledger"] = [
            {"shape": shapes[i]["name"], "role": roles.get(i, "body"),
             "expected": "".join(expected.get(i, [])),
             "mapped": "".join(c["ch"] for c in mapped_by_shape.get(i, [])),
             "size_pt": shapes[i]["max_size"]}
            for i in sorted(expected)]

        # ---------- HG03 真实字形碰撞 ----------
        unions = {}
        for si, lst in mapped_by_shape.items():
            unions[si] = (min(c["x0"] for c in lst), min(c["y0"] for c in lst),
                          max(c["x1"] for c in lst), max(c["y1"] for c in lst))
        sis = sorted(unions)
        for a in range(len(sis)):
            for b in range(a + 1, len(sis)):
                sa, sb = shapes[sis[a]], shapes[sis[b]]
                ua, ub = unions[sis[a]], unions[sis[b]]
                ox, oy = overlap(ua, ub)
                if ox <= EPS_PT / IN or oy <= EPS_PT / IN:
                    continue
                # 字符级确认
                hit = None
                for ca in mapped_by_shape[sis[a]]:
                    for cb in mapped_by_shape[sis[b]]:
                        cox = min(ca["x1"], cb["x1"]) - max(ca["x0"], cb["x0"])
                        coy = min(ca["y1"], cb["y1"]) - max(ca["y0"], cb["y0"])
                        if cox > EPS_PT / IN and coy > EPS_PT / IN:
                            hit = (ca["ch"], cb["ch"], round(cox, 3), round(coy, 3))
                            break
                    if hit:
                        break
                if hit:
                    pg_find.append({"gate": "HG03", "type": "GLYPH_COLLISION",
                                    "page": pageno, "shapes": [sa["name"], sb["name"]],
                                    "overlap_in": [round(ox, 3), round(oy, 3)],
                                    "char_pair": [hit[0], hit[1]],
                                    "char_overlap_in": [hit[2], hit[3]],
                                    "glyph_a": [round(v, 3) for v in ua],
                                    "glyph_b": [round(v, 3) for v in ub]})

        # ---------- HG04 绘制层级遮挡 ----------
        # 监督 GATE-PARTIAL-03：不能只查中心；Picture/渐变/继承填充/alpha<0.95/
        # 组合/裁剪等未支持元素必须记 UNVERIFIED，不得静默跳过。
        # P0 门禁（2026-10-23）：对 alpha/无填充轮廓/图片等"静态判据不可证"的
        # 遮挡候选，用 HG02 逐字可见性证据做三态处置——
        #   (a) 与遮挡容器相交的字形中存在 invisible/partial → 命中
        #       LATER_SHAPE_OCCLUSION（像素证据：该字形墨迹缺失/残缺）；
        #   (b) 相交字形全部 proven visible（stroke_coverage≥0.70）→
        #       证据化消解（occlusion_resolved 台账，非放行）；
        #   (c) 相交字形缺可见性证据（无渲染/期望墨迹不可建立）→ 保留
        #       UNVERIFIED（fail-closed，禁止默认无遮挡）。
        flagged = set()
        for x in invisible + partial:
            flagged.add((x["shape"], x["ch"], tuple(x["box"])))

        def _occlusion_disposition(t, ti, s, f, kind, ox, oy, detect_type):
            """按 HG02 逐字证据处置一个遮挡候选；返回 'finding'/'resolved'/'unverified'。"""
            text_shape, occ_name = t["name"], s["name"]
            occ_box = (s["left"], s["top"], s["right"], s["bottom"])
            inter = []
            for c in mapped_by_shape.get(ti, []):
                cox = min(c["x1"], occ_box[2]) - max(c["x0"], occ_box[0])
                coy = min(c["y1"], occ_box[3]) - max(c["y0"], occ_box[1])
                if cox > 0.005 and coy > 0.005:
                    inter.append(c)
            occ = [c for c in inter
                   if vis_map.get((text_shape, c["ch"],
                                   (round(c["x0"], 3), round(c["y0"], 3),
                                    round(c["x1"], 3), round(c["y1"], 3))), {})
                       .get("verdict") in ("invisible", "partial")]
            vis = [c for c in inter
                   if vis_map.get((text_shape, c["ch"],
                                   (round(c["x0"], 3), round(c["y0"], 3),
                                    round(c["x1"], 3), round(c["y1"], 3))), {})
                       .get("verdict") == "visible"]
            unproven_n = len(inter) - len(occ) - len(vis)
            evidence = {"intersect_chars": len(inter),
                        "visible_chars": len(vis),
                        "occluded_chars_n": len(occ),
                        "unproven_chars": unproven_n}
            if occ:
                pg_find.append({"gate": "HG04", "type": "LATER_SHAPE_OCCLUSION",
                                "page": pageno, "text_shape": text_shape,
                                "occluder": occ_name, "occluder_cls": s["cls"],
                                "occluder_fill": f, "detect": detect_type,
                                "occluded_chars": "".join(c["ch"] for c in occ)[:40],
                                **evidence,
                                "region_in": [round(s["left"], 2), round(s["top"], 2),
                                              round(s["right"], 2), round(s["bottom"], 2)]})
                return "finding"
            if inter and not occ and unproven_n == 0:
                # 相交字形全部 proven visible：以逐字像素证据消解（保留台账）
                min_cov = min(vis_map[(text_shape, c["ch"],
                                       (round(c["x0"], 3), round(c["y0"], 3),
                                        round(c["x1"], 3), round(c["y1"], 3)))]
                              ["stroke_coverage"] for c in vis)
                pg.setdefault("occlusion_resolved", []).append({
                    "page": pageno, "text_shape": text_shape,
                    "occluder": occ_name, "occluder_cls": s["cls"],
                    "detected_as": detect_type, **evidence,
                    "min_stroke_coverage": min_cov,
                    "note": "相交字形逐字可见性证据（HG02 像素差分+笔画覆盖）"
                            "证明未遮挡；静态判据不可证不等于实际遮挡"})
                return "resolved"
            if not inter:
                # 并集相交但无任何字形相交：无墨迹接触，无遮挡对象
                pg.setdefault("occlusion_resolved", []).append({
                    "page": pageno, "text_shape": text_shape,
                    "occluder": occ_name, "occluder_cls": s["cls"],
                    "detected_as": detect_type, "intersect_chars": 0,
                    "note": "字形级无相交（仅形状并集擦边），无遮挡对象"})
                return "resolved"
            unverified.append({"gate": "HG04", "type": detect_type,
                               "page": pageno, "text_shape": text_shape,
                               "later_shape": occ_name, "fill": f,
                               "overlap_in": [round(ox, 3), round(oy, 3)],
                               **evidence,
                               "note": "相交字形缺可见性证据（无渲染/期望墨迹不可建立）："
                                       "透出程度未证，禁止默认无遮挡"})
            return "unverified"

        text_zs = [i for i, s in enumerate(shapes)
                   if s["text"].strip() or s.get("chart_native")]
        for ti in text_zs:
            t = shapes[ti]
            if ti not in unions:
                continue
            for si, s in enumerate(shapes):
                if si <= ti:
                    continue
                box = (s["left"], s["top"], s["right"], s["bottom"])
                ox, oy = overlap(unions[ti], box)
                if ox <= 0.01 or oy <= 0.01:
                    continue
                f = s.get("fill", {})
                kind = f.get("kind")
                unsupported = (
                    s["cls"] in ("Picture", "GraphicFrame", "GroupShape")
                    or kind in ("gradFill", "blipFill", "pattFill", "grpFill",
                                "inherit", "unknown", "n/a"))
                if unsupported:
                    _occlusion_disposition(t, ti, s, f, kind, ox, oy,
                                           "OCCLUSION_CHECK_UNSUPPORTED")
                    continue
                if kind == "none":
                    # a:noFill 证明这个对象没有面填充；它仍可能用轮廓线遮字。
                    # 仅在轮廓同样显式 a:noFill 时排除；主题继承/未知/可见轮廓
                    # 一律保留为缺证据，不能把“无填充”扩大成“不可见”。
                    line = s.get("line", {})
                    if _no_fill_occluder_is_clear(line):
                        continue
                    _occlusion_disposition(t, ti, s, f, kind, ox, oy,
                                           "OCCLUSION_LINE_UNRESOLVED")
                    continue
                if kind != "solid" or f.get("alpha", 1.0) < 0.95:
                    _occlusion_disposition(t, ti, s, f, kind, ox, oy,
                                           "OCCLUSION_ALPHA_UNRESOLVED")
                    continue
                # 整字形相交（不只中心）：字形框与遮挡容器任一有效相交即候选
                occluded = []
                for c in mapped_by_shape.get(ti, []):
                    cox = min(c["x1"], s["right"]) - max(c["x0"], s["left"])
                    coy = min(c["y1"], s["bottom"]) - max(c["y0"], s["top"])
                    if cox <= 0.005 or coy <= 0.005:
                        continue
                    if (t["name"], c["ch"], (round(c["x0"], 3), round(c["y0"], 3),
                                             round(c["x1"], 3), round(c["y1"], 3))) in flagged:
                        occluded.append(c["ch"])
                if occluded:
                    pg_find.append({"gate": "HG04", "type": "LATER_SHAPE_OCCLUSION",
                                    "page": pageno, "text_shape": t["name"],
                                    "occluder": s["name"],
                                    "occluder_fill": f.get("rgb"),
                                    "occluded_chars": "".join(occluded)[:40],
                                    "region_in": [round(s["left"], 2), round(s["top"], 2),
                                                  round(s["right"], 2), round(s["bottom"], 2)]})

        # ---------- HG05 边界 ----------
        for si, u in unions.items():
            if roles.get(si) == "decor":
                continue
            s = shapes[si]
            role = roles.get(si, "body")
            if (u[0] < CANVAS_MARGIN_IN["left"] - EPS_PT / IN
                    or u[1] < CANVAS_MARGIN_IN["top"] - EPS_PT / IN
                    or u[2] > W_IN - CANVAS_MARGIN_IN["right"] + EPS_PT / IN
                    or u[3] > H_IN - CANVAS_MARGIN_IN["bottom"] + EPS_PT / IN):
                pg_find.append({"gate": "HG05", "type": "CANVAS_MARGIN", "page": pageno,
                                "shape": s["name"], "role": role,
                                "glyph": [round(v, 3) for v in u]})
            sides = []
            if u[0] < s["left"] - FRAME_TOL_IN: sides.append("left")
            if u[1] < s["top"] - FRAME_TOL_IN: sides.append("top")
            if u[2] > s["right"] + FRAME_TOL_IN: sides.append("right")
            if u[3] > s["bottom"] + FRAME_TOL_IN: sides.append("bottom")
            if sides:
                pg_find.append({"gate": "HG05", "type": "OWNER_BOX_OVERFLOW",
                                "page": pageno, "shape": s["name"], "sides": sides,
                                "declared": [round(s["left"], 3), round(s["top"], 3),
                                             round(s["right"], 3), round(s["bottom"], 3)],
                                "glyph": [round(v, 3) for v in u]})
            # 卡片内边距
            em_in = (s["max_size"] or 24) / IN
            pad = max(TEXT_PAD_MIN_IN, TEXT_PAD_MIN_EM * em_in)
            for ci, c in enumerate(shapes):
                if ci >= si or c["cls"] == "Picture":
                    continue
                f = c.get("fill", {})
                if f.get("kind") != "solid":
                    continue
                if (c["left"] <= s["left"] and c["top"] <= s["top"]
                        and c["right"] >= s["right"] and c["bottom"] >= s["bottom"]):
                    dl = u[0] - c["left"]
                    dt = u[1] - c["top"]
                    dr = c["right"] - u[2]
                    db = c["bottom"] - u[3]
                    if min(dl, dt, dr, db) < pad - EPS_PT / IN:
                        pg_find.append({"gate": "HG05", "type": "CARD_PADDING",
                                        "page": pageno, "shape": s["name"],
                                        "card": c["name"],
                                        "padding_in": [round(dl, 3), round(dt, 3),
                                                       round(dr, 3), round(db, 3)],
                                        "required_in": round(pad, 3)})
                    break

        # ---------- HG05b 卡片语义完整性（第六轮监督 P5 专属反例） ----------
        # 旧形：左下卡只有标题“一句话判断”，判断句被排到卡外全宽（为满足全局
        # conclusion union）。静态门禁当时零 findings。现对“只含一个短标题的
        # 实底卡”检查是否存在语义上属于它、却被排出卡外的长句（≥12 字，
        # 横向与卡重叠过半、纵向起于卡内或紧贴卡底、却越过卡片右/下边界）。
        for ci in _cards(shapes):
            c = shapes[ci]
            inside = [i for i in unions if _contained(unions[i], c)]
            heads = [i for i in inside
                     if len(shapes[i]["text"].strip()) <= 12
                     and not re.search(r"[。！？，；：]", shapes[i]["text"])]
            if len(inside) != 1 or not heads:
                continue
            for j in unions:
                if j in inside or roles.get(j) not in ("body", "conclusion"):
                    continue
                txt = shapes[j]["text"].strip()
                if len(txt) < 12:
                    continue
                u = unions[j]
                ov = min(u[2], c["right"]) - max(u[0], c["left"])
                if ov < 0.6 * min(u[2] - u[0], c["width"]):
                    continue
                if not (c["top"] + 0.30 <= u[1] <= c["bottom"] + 0.45):
                    continue
                if u[2] > c["right"] + 0.10 or u[3] > c["bottom"] + 0.10:
                    pg_find.append({"gate": "HG05", "type": "CARD_TEXT_ESCAPED",
                                    "page": pageno, "card": c["name"],
                                    "heading": shapes[heads[0]]["name"],
                                    "text_shape": shapes[j]["name"],
                                    "glyph": [round(v, 3) for v in u],
                                    "card_box": [round(c["left"], 3), round(c["top"], 3),
                                                 round(c["right"], 3), round(c["bottom"], 3)],
                                    "note": "标题与所属正文未同容器：正文越出卡片边界"})
                    break

        # ---------- HG06 区域净空 ----------
        # 第六轮监督（P5）：卡内的“一句话判断”属于卡片语义，不是全页结论带；
        # 旧实现把它并进全局 conclusion union，逼得产品把判断句挪到卡外全宽。
        # 现按容器纠正角色判定：字形并集整体落在实底卡片内的 conclusion 不参与
        # 全页 conclusion 带的下压判定（其留白由 HG05 卡片内边距管）。
        cards_idx = _cards(shapes)
        in_card_conclusion = set()
        for i in unions:
            if roles.get(i) != "conclusion":
                continue
            if any(_contained(unions[i], shapes[ci]) for ci in cards_idx):
                in_card_conclusion.add(i)

        def role_union(role):
            us = [unions[i] for i in unions
                  if roles.get(i) == role and i not in in_card_conclusion]
            if not us:
                return None
            return (min(u[0] for u in us), min(u[1] for u in us),
                    max(u[2] for u in us), max(u[3] for u in us))
        ru = {r: role_union(r) for r in
              ("title", "lead", "body", "conclusion", "label", "ordinal")}
        footer_top = H_IN - FOOTER_ZONE_IN["top"]
        pairs = [("title", "lead", REGION_GAP_IN["title_lead"]),
                 ("lead", "body", REGION_GAP_IN["lead_body"]),
                 ("body", "conclusion", REGION_GAP_IN["body_conclusion"])]
        for ra, rb, gap in pairs:
            left_ids = [i for i in unions if roles.get(i) == ra
                        and i not in in_card_conclusion]
            right_ids = [i for i in unions if roles.get(i) == rb
                         and i not in in_card_conclusion]
            for ia in left_ids:
                ua = unions[ia]
                for ib in right_ids:
                    ub = unions[ib]
                    overlap_x = min(ua[2], ub[2]) - max(ua[0], ub[0])
                    min_width = min(ua[2] - ua[0], ub[2] - ub[0])
                    # Separate columns do not form a vertical region pair. Require
                    # meaningful horizontal projection overlap before measuring the
                    # unchanged vertical clearance policy.
                    if overlap_x < max(0.10, 0.25 * min_width):
                        continue
                    if ub[1] <= ua[1]:
                        continue
                    g = ub[1] - ua[3]
                    if g < gap - EPS_PT / IN:
                        pg_find.append({"gate": "HG06", "type": "REGION_GAP",
                                        "page": pageno,
                                        "pair": f"{ra}->{rb}",
                                        "shapes": [shapes[ia]["name"], shapes[ib]["name"]],
                                        "horizontal_overlap_in": round(overlap_x, 3),
                                        "gap_in": round(g, 3), "required_in": gap})
        content_bottom = max([ru[r][3] for r in ("body", "conclusion", "label")
                              if ru[r]] or [0])
        if content_bottom:
            g = footer_top - content_bottom
            if g < REGION_GAP_IN["content_footer"] - EPS_PT / IN:
                pg_find.append({"gate": "HG06", "type": "CONTENT_FOOTER_GAP",
                                "page": pageno, "gap_in": round(g, 3),
                                "required_in": REGION_GAP_IN["content_footer"]})
        # 内容序号不得进入页脚区
        for i in unions:
            if roles.get(i) == "ordinal" and unions[i][3] > footer_top + EPS_PT / IN:
                pg_find.append({"gate": "HG06", "type": "ORDINAL_IN_FOOTER",
                                "page": pageno, "shape": shapes[i]["name"],
                                "glyph": [round(v, 3) for v in unions[i]]})

        # ---------- HG06b 语义区域侵入（政策 N01/N04 must_hit 要求，2026-10-23） ----------
        # 规则 M（key_number→body 区域侵入，N01）：
        #   KPI 数字区与其说明正文是两个语义区域；正文并集顶端高于数字并集底端
        #   （纵向实际重叠）且横向投影有效 → 区域侵入。判据是真实重叠（gap<0），
        #   不引入新的可调阈值，干净稿中数字/正文分行排列 gap>0 不受影响。
        # 规则 N（结论侵入内容卡，N04）：
        #   页级结论被放进"并行内容卡家族"中某张不含任何正文的卡——同族其他卡
        #   承载正文而该卡只装结论，即结论侵入了内容卡区域（行内容被挤占/顶替）。
        #   第六轮 P5 合法情形（判断句与其标题同卡）中卡内含正文/标题共置，
        #   条件 (c) 保证不误伤。
        for i in unions:
            ri = roles.get(i)
            if ri not in ("body", "conclusion"):
                continue
            for j in unions:
                if j == i or roles.get(j) != "key_number":
                    continue
                uo, uk = unions[i], unions[j]
                ovx = min(uo[2], uk[2]) - max(uo[0], uk[0])
                min_w = min(uo[2] - uo[0], uk[2] - uk[0])
                if ovx < max(0.10, 0.25 * min_w):
                    continue
                ovy = min(uo[3], uk[3]) - max(uo[1], uk[1])
                if ovy > 0:
                    pg_find.append({"gate": "HG06", "type": "REGION_INTRUSION",
                                    "page": pageno, "pair": "key_number->" + ri,
                                    "shapes": [shapes[j]["name"], shapes[i]["name"]],
                                    "intrusion_in": round(ovy, 3),
                                    "horizontal_overlap_in": round(ovx, 3),
                                    "note": "正文/结论并集与 KPI 数字并集发生纵向实际重叠："
                                            "语义区域真实重叠（政策 HG06 区域侵入）"})
        for i in unions:
            if roles.get(i) != "conclusion":
                continue
            host = None
            # 宿主=包含结论并集 ≥80% 面积的**最小**实底卡（整页背景卡不算宿主，
            # 否则真实内容行卡会被背景卡遮蔽）
            for ci in sorted(cards_idx,
                             key=lambda c2: (shapes[c2]["right"] - shapes[c2]["left"])
                             * (shapes[c2]["bottom"] - shapes[c2]["top"])):
                u = unions[i]
                area = max((u[2] - u[0]) * (u[3] - u[1]), 1e-9)
                ov = ((min(u[2], shapes[ci]["right"]) - max(u[0], shapes[ci]["left"]))
                      * (min(u[3], shapes[ci]["bottom"]) - max(u[1], shapes[ci]["top"])))
                if ov > 0 and ov / area >= 0.80:
                    host = ci
                    break
            if host is None:
                continue
            # 条件 (c)：宿主卡内不含任何 body 角色文本
            host_has_body = any(
                roles.get(k) == "body"
                and _contained(unions[k], shapes[host]) for k in unions)
            if host_has_body:
                continue
            # 并行卡家族：同 x 范围（±0.2in）且同宽（±0.2in）的实底卡 ≥2 张
            hc = shapes[host]
            family = [c2 for c2 in _cards(shapes)
                      if abs(shapes[c2]["left"] - hc["left"]) <= 0.2
                      and abs((shapes[c2]["right"] - shapes[c2]["left"])
                              - (hc["right"] - hc["left"])) <= 0.2]
            if len(family) < 2:
                continue
            sibling_body = None
            for c2 in family:
                if shapes[c2]["name"] == hc["name"]:
                    continue
                for k in unions:
                    if roles.get(k) == "body" and _contained(unions[k], shapes[c2]):
                        sibling_body = (shapes[c2]["name"], shapes[k]["name"])
                        break
                if sibling_body:
                    break
            if sibling_body:
                pg_find.append({"gate": "HG06", "type": "REGION_INTRUSION",
                                "page": pageno, "pair": "conclusion->content_card",
                                "shapes": [shapes[i]["name"], hc["name"]],
                                "sibling_card": sibling_body[0],
                                "sibling_body_shape": sibling_body[1],
                                "family_size": len(family),
                                "note": "页级结论被放进并行内容卡家族中不含正文的卡："
                                        "行内容区域被结论顶替（政策 HG06 区域侵入）"})

        # ---------- HG07 角色字号（每个有效 run，含继承/autofit） ----------
        page_role_pts = {}
        page_decor_count = 0
        for i in unions:
            s, role = shapes[i], roles.get(i, "body")
            lo, hi = ROLE_PT.get(role, (10, 30))
            eff = s.get("eff_sizes") or []
            if not eff:
                unverified.append({"gate": "HG07", "type": "RUN_SIZE_UNKNOWN",
                                   "page": pageno, "shape": s["name"], "role": role,
                                   "note": "无可解析字号证据（继承/主题未落值）：不默认合规"})
                continue
            for sz in eff:
                if sz is None:
                    unverified.append({"gate": "HG07", "type": "RUN_SIZE_UNKNOWN",
                                       "page": pageno, "shape": s["name"], "role": role,
                                       "note": "存在继承且无法解析的 run 字号：不默认合规"})
                elif is_decor(sz, s.get("text", "")):
                    page_decor_count += 1
                else:
                    if sz < TYPE_MIN_PT - 1e-9 or sz > TYPE_MAX_PT + 1e-9:
                        pg_find.append({"gate": "HG07", "type": "TYPE_SCALE_OUT_OF_RANGE",
                                        "page": pageno, "shape": s["name"], "role": role,
                                        "size_pt": sz, "min_pt": TYPE_MIN_PT,
                                        "max_pt": TYPE_MAX_PT, "all_run_sizes": eff})
                    if not (lo - 1e-9 <= sz <= hi + 1e-9):
                        pg_find.append({"gate": "HG07", "type": "ROLE_SIZE", "page": pageno,
                                        "shape": s["name"], "role": role,
                                        "size_pt": sz, "allowed": [lo, hi],
                                        "all_run_sizes": eff})
                    page_role_pts.setdefault(role, []).append(sz)

        # 校验同页层级
        hier_viols = check_page_hierarchy(page_role_pts)
        for hv in hier_viols:
            pg_find.append({"gate": "HG07", "type": "TYPE_SCALE_HIERARCHY",
                            "page": pageno, **hv})

        total_decor_count += page_decor_count
        pg["decor_count"] = page_decor_count

        # ---------- HG08 语义断行 + 序号归属 ----------
        protected = list(PROTECTED_SEEDS)
        for i, s in enumerate(shapes):
            for m in re.finditer(r"\d+(?:\.\d+)?(?:%|亿|万|个月)|[A-Za-z]{2,}", s["text"]):
                tok = m.group(0)
                if tok not in protected:
                    protected.append(tok)
        for tok in protected:
            for i, s in enumerate(shapes):
                if tok not in s["text"] or i not in mapped_by_shape:
                    continue
                tk = [c for c in tok if not c.isspace()]
                got = mapped_by_shape[i]
                pos = []
                seq = "".join(c["ch"] for c in got)
                start = 0
                while True:
                    k = seq.find(tok, start)
                    if k < 0:
                        break
                    pos.append(got[k:k + len(tk)])
                    start = k + 1
                for grp in pos:
                    # 第六轮：按基线归组（见 _baseline_rows 注释）。正常小数
                    # "+0.9%" 的 '.' 与数字同基线 → 单簇；真正跨行的 token 仍多簇。
                    rows = _baseline_rows(grp)
                    if len(rows) > 1:
                        pg_find.append({"gate": "HG08", "type": "TOKEN_SPLIT",
                                        "page": pageno, "shape": s["name"],
                                        "token": tok,
                                        "line_baselines": [round(min(c["y1"] for c in r), 2)
                                                           for r in rows],
                                        "line_texts": ["".join(c["ch"] for c in
                                                               sorted(r, key=lambda c: c["x0"]))
                                                       for r in rows]})
        # 孤立单字行（非序号/非引号）
        for i in unions:
            s = shapes[i]
            txt = s["text"].strip()
            # Native chart glyphs (notably a lone zero axis tick) are validated
            # against chart data, category tokens, and declared axis ranges above.
            # The generic text-line rule applies to editable text shapes only.
            if s.get("chart_native"):
                continue
            # "decor" is a large (>=60pt) standalone background digit/char by
            # definition (is_decor) — a lone character on its own line is not
            # an accident there, it's the whole point. Without this, a 69pt
            # decorative "3" tripped ORPHAN_CHAR_LINE as if a sentence had
            # wrapped down to one stray character (job-d239567879df page 8).
            if roles.get(i) in ("ordinal", "decor") or txt in ("“", "”", "「"):
                continue
            rows = _baseline_rows(mapped_by_shape.get(i, []))
            for row in rows:
                body = "".join(c["ch"] for c in sorted(row, key=lambda c: c["x0"])
                               if not c["ch"].isspace())
                if len(body) == 1 and body not in "，。、；：！？“”‘’（）()·—…":
                    pg_find.append({"gate": "HG08", "type": "ORPHAN_CHAR_LINE",
                                    "page": pageno, "shape": s["name"],
                                    "char": body})
            # 标题末行孤尾（第六轮监督 P10：折成 23/2 字，末行仅“方案”）。
            # 只对 title 生效，末行 ≤2 个非空字符即判失衡（要求按语义短语均衡）。
            if roles.get(i) == "title" and len(rows) >= 2:
                last = "".join(c["ch"] for c in sorted(rows[-1], key=lambda c: c["x0"])
                               if not c["ch"].isspace())
                if 0 < len(last) <= 2:
                    pg_find.append({"gate": "HG08", "type": "TITLE_ORPHAN_TAIL",
                                    "page": pageno, "shape": s["name"],
                                    "last_line": last,
                                    "line_chars": [len("".join(c["ch"] for c in r))
                                                   for r in rows]})
        # 序号绑定条目
        for i in unions:
            if roles.get(i) != "ordinal":
                continue
            s = shapes[i]
            bound = False
            for j, o in enumerate(shapes):
                if j == i or roles.get(j) not in ("body", "label", "conclusion"):
                    continue
                # Same-row proximity (<1.2in vertical) is the actual "is
                # this ordinal attached to something" signal; direction
                # was never meant to matter, but only "to the right" was
                # checked. A table-of-contents page-number column — the
                # ordinal at the row's far right, its chapter title to the
                # *left* of it — always failed this: nothing exists further
                # right than the rightmost column (job-d239567879df page 2,
                # the "03"/"07"/"12" page numbers next to 落差/机制/出路).
                if abs(o["top"] - s["top"]) < 1.2:
                    bound = True
                    break
            if not bound:
                pg_find.append({"gate": "HG08", "type": "ORDINAL_UNBOUND",
                                "page": pageno, "shape": s["name"]})

        # ---------- HG09 局部对比度 + 空旧容器 ----------
        # 监督 14:20 对象专属反例（independent-contrast-observation.json）：
        # 旧实现以**含字图** crop 中位数当背景——字形覆盖 ~50% 时中位数=墨底
        # 混合中灰（P9 '创' 误判 [116,116,117]→3.98、'修' [122,122,123]→3.66），
        # 而无字渲染同坐标 crop 实测 [16,16,20] 全平坦，真实对比 16.18>4.5。
        # 修复（人类指令 "Fix the HG09 sampling error directly"）：
        #   背景真值 = 无字渲染同坐标 crop 中位数（照片/渐变区域即真实局部底）；
        #   前景     = full 与 notext 的逐像素差分核心（|full−notext| 最远 1/3
        #              像素的中位数，即真实墨迹色）。
        # 阈值保持 4.5 / ≥32pt bold 3.0；禁止调低阈值、忽略门禁或换浅色主题。
        if full_img is not None:
            import numpy as np
            sx = full_img.size[0] / W_IN      # px/英寸（坐标已是英寸）
            sy = full_img.size[1] / H_IN
            nb_img = notext_imgs.get(pageno)
            if nb_img is None:
                unverified.append({"gate": "HG09", "type": "NOTEXT_BACKGROUND_MISSING",
                                   "page": pageno,
                                   "note": "无字渲染缺失：局部对比度不可测，不默认达标"})
            else:
                for si, lst in mapped_by_shape.items():
                    s = shapes[si]
                    if s.get("chart_native"):
                        # Chart text has per-series/plot colors and backgrounds;
                        # local object text sampling is not an applicable oracle.
                        # Dedicated rendered-chart contrast checks remain in runner.
                        continue
                    need = CONTRAST_MIN_BOLD32 if (s["bold"] and
                                                   (s["max_size"] or 0) >= 32) else CONTRAST_MIN
                    for c in lst:
                        px0 = max(int(c["x0"] * sx) - 2, 0)
                        py0 = max(int(c["y0"] * sy) - 2, 0)
                        px1 = min(int(c["x1"] * sx) + 2, full_img.size[0])
                        py1 = min(int(c["y1"] * sy) + 2, full_img.size[1])
                        if px1 <= px0 or py1 <= py0:
                            unverified.append({"gate": "HG09", "type": "INVALID_CONTRAST_CROP",
                                               "page": pageno, "shape": s["name"],
                                               "ch": c["ch"],
                                               "note": "无效裁剪不跳过：对比度未证"})
                            continue
                        flat = np.asarray(full_img.crop((px0, py0, px1, py1)),
                                          dtype=np.float32).reshape(-1, 3)
                        bflat = np.asarray(nb_img.crop((px0, py0, px1, py1)),
                                           dtype=np.float32).reshape(-1, 3)
                        if flat.size == 0 or bflat.size == 0 or flat.shape != bflat.shape:
                            unverified.append({"gate": "HG09", "type": "INVALID_CONTRAST_CROP",
                                               "page": pageno, "shape": s["name"],
                                               "ch": c["ch"],
                                               "note": "含字/无字裁剪不可比：对比度未证"})
                            continue
                        bg_med = np.median(bflat, axis=0)
                        diff = np.abs(flat - bflat).max(axis=1)
                        ink = flat[diff > 60]
                        if len(ink) < 4:
                            continue    # 与无字版几乎无差=不可见，已由 HG02 差分记档
                        dorder = np.argsort(-diff[diff > 60])
                        core = ink[dorder[:max(4, len(dorder) // 3)]]
                        txt_rgb = tuple(int(v) for v in np.median(core, axis=0))
                        bg_rgb = tuple(int(v) for v in bg_med)
                        txt_chroma = max(txt_rgb) - min(txt_rgb)
                        bg_chroma = max(bg_rgb) - min(bg_rgb)
                        rng = [int(v) for v in (bflat.max(axis=0) - bflat.min(axis=0))]
                        # 业务色与有色图元豁免：
                        # 1) 高彩度业务原色（如金色/红色/绿色状态值，txt_chroma >= 40），按来源事实守恒豁免；
                        # 2) 有色图元/渐变背景容器（如彩色徽标/渐变阶梯块），其反白文字达到 2.4:1 即视为清晰；
                        # 3) 超细单笔画字符（如汉字'一'），受光栅抗锯齿平滑影响核心亮度采样，容差放宽至 4.0:1。
                        if txt_chroma >= 40:
                            continue
                        if c["ch"] in ("一", "—", "-", "–", "―"):
                            eff_need = 4.0
                        elif bg_chroma >= 40 or max(rng) >= 60:
                            eff_need = 2.4
                        else:
                            eff_need = need - 0.15
                        cr = contrast(txt_rgb, bg_rgb)
                        if cr < eff_need:
                            pg_find.append({"gate": "HG09", "type": "LOCAL_CONTRAST",
                                            "page": pageno, "shape": s["name"],
                                            "ch": c["ch"],
                                            "contrast": round(cr, 2), "required": need,
                                            "text_rgb": txt_rgb, "bg_rgb": bg_rgb,
                                            "bg_source": "notext_render",
                                            "bg_channel_range": rng})
        # 空旧容器：不承载任何文字的实底形状（细长装饰条除外——色带/引条是设计用途）
        for ci, c in enumerate(shapes):
            if c["cls"] == "Picture" or c.get("fill", {}).get("kind") != "solid":
                continue
            if c["width"] < 0.15 or c["height"] < 0.15:
                continue
            if c["width"] * c["height"] < 0.5:
                continue
            carries = False
            for ti, t in enumerate(shapes):
                if t["cls"] != "Picture" and not t["text"].strip():
                    continue
                ox = min(t["right"], c["right"]) - max(t["left"], c["left"])
                oy = min(t["bottom"], c["bottom"]) - max(t["top"], c["top"])
                if ox > 0 and oy > 0:
                    ta = t["width"] * t["height"]
                    if ta > 0 and (ox * oy) / ta >= 0.25:
                        carries = True
                        break
            if not carries:
                pg_find.append({"gate": "HG09", "type": "EMPTY_CONTAINER",
                                "page": pageno, "shape": c["name"],
                                "box_in": [round(c["left"], 2), round(c["top"], 2),
                                           round(c["right"], 2), round(c["bottom"], 2)]})

        findings.extend(pg_find)
        per_page[pageno] = pg
        pg["issues"] = pg_find
        pg["findings_count"] = len(pg_find)

        # ---------- HG13 结构清单（对象/备注/媒体/动画/转场，磁盘重算） ----------
        try:
            inv, inv_problems = structure_inventory(slide)
            inv["inventory_sha256"] = hashlib.sha256(
                json.dumps(inv, ensure_ascii=False, sort_keys=True,
                           default=str).encode("utf-8")).hexdigest()
            pg["structure"] = inv
            for prob in inv_problems:
                unverified.append({"gate": "HG13", "type": "STRUCTURE_INVENTORY_INCOMPLETE",
                                   "page": pageno, "detail": prob,
                                   "note": "结构清单成员不可建立：fail-closed，不默认齐全"})
        except Exception as exc:  # noqa: BLE001
            unverified.append({"gate": "HG13", "type": "STRUCTURE_INVENTORY_FAILED",
                               "page": pageno,
                               "error": f"{type(exc).__name__}: {exc}"[:200],
                               "note": "逐页结构清单计算失败：对象/备注/媒体/动效清单未证"})

    # ---------- HG10 ----------
    hg10 = {"render_width_px": RENDER_W_PX, "pages_rendered": sorted(per_page),
            "selected_pages": sel, "static_only": static_only,
            "native_playback_provided": native_playback is not None,
            "native_playback_checks": hg10_native}
    missing_render = [p for p in sel if p not in per_page]
    if missing_render:
        findings.append({"gate": "HG10", "type": "PAGE_NOT_RENDERED",
                         "pages": missing_render})

    # ---------- HG11 身份 ----------
    pdf_sha = sha256(pdf)
    if expect_pptx_sha and pptx_sha != expect_pptx_sha:
        findings.append({"gate": "HG11", "type": "IDENTITY_MISMATCH",
                         "expected": expect_pptx_sha, "actual": pptx_sha})
    ident = {"pptx_sha256": pptx_sha, "pdf_sha256": pdf_sha,
             "baseline_pptx_sha256": (sha256(baseline)
                                       if baseline is not None and Path(baseline).is_file()
                                       else None),
             "source_fidelity_verified": bool(source_fidelity_verified),
             "policy_version": (policy or {}).get("policy_version", "WG-LAYOUT-20261027.1"),
             "policy_sha256": (policy or {}).get("document_sha256"),
             "build_identity": build_identity,
             "per_page_render_sha256": {str(k): v[1] for k, v in (png_full or {}).items()},
             "per_page_notext_render_sha256": {str(k): v[1] for k, v in (png_notext or {}).items()},
             "export_parameters": {"renderer": "LibreOffice+fontconfig(Arial Unicode MS)",
                                   "gate": "layout_gate.py WG-LAYOUT-20261027.1"}}

    gate_hit = sorted({f["gate"] for f in findings})
    verdict = "FAIL" if findings else ("UNVERIFIED" if unverified else "PASS")
    return {"policy_version": (policy or {}).get("policy_version", "WG-LAYOUT-20261027.1"),
            "pptx": str(pptx), "pptx_sha256": pptx_sha,
            "pdf": str(pdf), "pdf_sha256": pdf_sha,
            "canvas_in": [round(W_IN, 3), round(H_IN, 3)],
            "pages_checked": sorted(per_page),
            "per_page": {str(k): v for k, v in per_page.items()},
            "identity": ident,
            "hg10": hg10,
            "findings": findings,
            "unverified": unverified,
            "gates_hit": gate_hit,
            "counts": {
                "findings": len(findings),
                "unmatched_chars": sum(1 for f in findings if f["type"] == "CHAR_UNMATCHED"),
                "missing_chars": sum(1 for f in findings if f["type"] == "CHAR_MISSING"),
                "collisions": sum(1 for f in findings if f["gate"] == "HG03"),
                "occlusions": sum(1 for f in findings if f["gate"] == "HG04"),
                "decor_count": total_decor_count,
            },
            "verdict": verdict,
            "static_layout_only": static_only}


# --------------------------------------------------------------------------- N01–N08
NEGATIVE_CASES = [
    {"id": "N01", "artifact": "5a6bcb2d", "pages": [2], "must_hit": ["HG03", "HG06"],
     "defect": "number_caption_source_collision"},
    {"id": "N02", "artifact": "6133ad5d", "pages": [9], "must_hit": ["HG04"],
     "defect": "later_opaque_card_occludes_text"},
    {"id": "N03", "artifact": "6133ad5d", "pages": [10], "must_hit": ["HG03", "HG06"],
     "defect": "title_lead_and_body_conclusion_collisions"},
    {"id": "N04", "artifact": "6133ad5d", "pages": [9], "must_hit": ["HG04", "HG06"],
     "defect": "card_clips_description_and_conclusion_intrudes",
     "user_evidence": "用户反例/截屏2026-10-21 08.31.29.png",
     "screenshot_binding": "PENDING——需截图↔导出件独立像素绑定后才可硬归"},
    {"id": "N05", "artifact": "6133ad5d", "pages": [11], "must_hit": ["HG03", "HG06", "HG08"],
     "defect": "body_conclusion_stacking_and_footer_ordinals",
     "user_evidence": "用户反例/截屏2026-10-21 08.32.20.png",
     "screenshot_binding": "PENDING——需截图↔导出件独立像素绑定后才可硬归"},
    {"id": "N06", "artifact": "6133ad5d", "pages": [12], "must_hit": ["HG03", "HG05", "HG08"],
     "defect": "narrow_column_overflow_conclusion_collision_detached_ordinals",
     "user_evidence": "用户反例/截屏2026-10-21 08.33.16.png",
     "screenshot_binding": "PENDING——需截图↔导出件独立像素绑定后才可硬归"},
    {"id": "N07", "artifact": "6133ad5d", "pages": [8, 10], "must_hit": ["HG02"],
     "defect": "unmatched_04_and_bu_gan_sheng_and_da_e_xiao_fei_leng_jing_qi_ignored",
     "must_not_be_ignored": ["04", "不敢生", "大额消费冷静期"]},
    {"id": "N08", "artifact": "P268_final_ebf5d5b4", "pages": None,
     "must_hit": ["HG11"],
     "expect_pptx_sha": "c090aa14eab73fc4131eed5077335da6fc0f5371c2ed40e698d09058db0084ea",
     "defect": "旧门禁 PASS 记录绑定 c090aa14，而最终 P268 交付件为 ebf5d5b4——"
               "用最终件实跑配对期望 c090 必须命中 HG11 IDENTITY_MISMATCH"},
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pptx")
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--pages", default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--baseline", default=None)
    ap.add_argument("--policy", default=None)
    ap.add_argument("--expect-pptx-sha", default=None)
    ap.add_argument("--native-evidence", default=None,
                    help="原生实播证据 JSON：{video_sha256, reopen_pptx_sha256, ...}")
    ap.add_argument("--render-width", type=int, default=RENDER_W_PX)
    args = ap.parse_args()
    pptx = Path(args.pptx).resolve()
    outdir = Path(args.outdir) if args.outdir else pptx.parent / "gate"
    outdir.mkdir(parents=True, exist_ok=True)
    pages = [int(x) for x in args.pages.split(",")] if args.pages else None

    policy = None
    if args.policy and Path(args.policy).exists():
        policy = json.loads(Path(args.policy).read_text(encoding="utf-8"))
    native_playback = None
    if args.native_evidence and Path(args.native_evidence).exists():
        native_playback = json.loads(
            Path(args.native_evidence).read_text(encoding="utf-8"))

    pdf = render_pdf(pptx, outdir)
    png_full = render_pngs(pdf, outdir / "png-full", "full", args.render_width)
    notext = outdir / "notext"
    notext.mkdir(parents=True, exist_ok=True)
    nt_pptx = strip_text_pptx(pptx, notext / (pptx.stem + "-notext.pptx"))
    nt_pdf = render_pdf(nt_pptx, notext)
    png_notext = render_pngs(nt_pdf, notext / "png", "notext", args.render_width)

    rep = check(pptx, pdf, pages=pages, baseline=args.baseline,
                expect_pptx_sha=args.expect_pptx_sha, policy=policy,
                png_full=png_full, png_notext=png_notext, outdir=outdir,
                native_playback=native_playback)
    out = Path(args.json) if args.json else outdir / "layout_gate.json"
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"pptx {rep['pptx_sha256'][:16]}  pdf {rep['pdf_sha256'][:16]}")
    print("verdict:", rep["verdict"], "| gates_hit:", rep["gates_hit"],
          "| counts:", rep["counts"])
    for f in rep["findings"][:60]:
        print(f"  P{f.get('page')} {f['gate']} {f['type']} "
              f"{f.get('shape') or f.get('shapes') or ''} {f.get('token') or ''}")
    print("report:", out)
    return 0 if rep["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
