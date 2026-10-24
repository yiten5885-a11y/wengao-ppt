"""输入提取器：PPTX 文字、UTF-8 文本、Markdown、DOCX 正文。

每种输入都产出带 source_ref 的独立标注（不来自生成器），并保留：
- PPTX：页、部件/对象路径、备注、隐藏页、表格单元格身份、图表系列绑定
- 文本/Markdown：原始行号区间、标题层级、块关系
- DOCX：正文段落顺序、标题层级、来源定位

规则表格与图表的完整关系提取按规格在 P2 完成；本模块对 DOCX 只做正文，
不把"正文能读"宣称为"所有 DOCX 已支持"。
"""
from __future__ import annotations

import re
from pathlib import Path

from .core import ExtractedDoc, TextUnit, sha256_file, conservative_norm, shape_visible

try:
    from pptx import Presentation
    from pptx.util import Emu
except Exception:  # pragma: no cover
    Presentation = None

try:
    import docx
except Exception:  # pragma: no cover
    docx = None


# ---------------------------------------------------------------- PPTX

def extract_pptx(path) -> ExtractedDoc:
    if Presentation is None:
        raise RuntimeError("python-pptx 未安装")
    path = Path(path)
    prs = Presentation(str(path))
    doc = ExtractedDoc(input_path=str(path), input_sha256=sha256_file(path), input_kind="pptx")

    for idx, slide in enumerate(prs.slides, start=1):
        hidden = _slide_hidden(slide)
        page = {"page_no": idx, "hidden": hidden, "title": "", "units": [], "notes": ""}
        uid = 0
        # 组合内对象必须一并登记：python-pptx 的 slide.shapes 只给顶层形状，
        # 只遍历顶层会让"组合里的文本框被删掉"完全逃过内容保真核验。
        # 这里按 (形状, 组路径) 递归遍历，组路径进入 source_ref 供结构绑定使用。
        for shp, gpath in _iter_shapes(slide.shapes):
            obj_path = _shape_path(shp, idx)
            doc.objects.append({"object_id": obj_path, "type": _shape_type(shp), "page": idx,
                                "path": obj_path, "group_path": gpath,
                                "visible": _shape_visible(shp)})
            if getattr(shp, "has_text_frame", False) and shp.has_text_frame:
                txt = shp.text_frame.text
                if txt.strip():
                    role = "title" if shp == _first_title(slide) else "body"
                    page["units"].append(TextUnit(
                        id=f"p{idx}_u{uid}", text=txt, role=role,
                        source_ref={"file": str(path), "slide": idx, "part": "slide",
                                    "object": obj_path, "group_path": gpath},
                        locked=False))
                    uid += 1
                    if role == "title" and not page["title"]:
                        page["title"] = txt.strip()
            if getattr(shp, "has_table", False) and shp.has_table:
                tbl = shp.table
                for ri, row in enumerate(tbl.rows):
                    for ci, cell in enumerate(row.cells):
                        if cell.text.strip():
                            page["units"].append(TextUnit(
                                id=f"p{idx}_tbl_r{ri}c{ci}", text=cell.text, role="table_cell",
                                source_ref={"file": str(path), "slide": idx, "part": "table",
                                            "object": obj_path, "row": ri, "col": ci,
                                            "header": tbl.cell(0, ci).text}))
            if getattr(shp, "has_chart", False) and shp.has_chart:
                ch = shp.chart
                for plot in ch.plots:
                    cats = [str(c) for c in plot.categories]
                    for si, series in enumerate(plot.series):
                        for ci, val in enumerate(series.values):
                            page["units"].append(TextUnit(
                                id=f"p{idx}_cht_s{si}c{ci}", text=f"{series.name} {cats[ci] if ci < len(cats) else ci} {val}",
                                role="chart_value",
                                source_ref={"file": str(path), "slide": idx, "part": "chart",
                                            "object": obj_path, "series": str(series.name),
                                            "category": cats[ci] if ci < len(cats) else None,
                                            "value": val}))
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text
            page["notes"] = notes
            if notes.strip():
                page["units"].append(TextUnit(
                    id=f"p{idx}_notes", text=notes, role="note",
                    source_ref={"file": str(path), "slide": idx, "part": "notes"}))
        doc.pages.append(page)

    # P2：规则表结构模型（关系核验的分母来自来源表，不来自输出）
    from .tables import extract_tables_pptx
    doc.tables = [t.to_dict() for t in extract_tables_pptx(prs, str(path))]
    # P2：原生图表模型（结构 + 显示缓存 + 内嵌工作簿，供 C07/C11/E02 核验）
    from .charts import extract_charts_pptx
    doc.charts = [c.to_dict() for c in extract_charts_pptx(prs, str(path))]
    return doc


def _slide_hidden(slide) -> bool:
    """隐藏页判定：OOXML 里 `show="0"` 才是隐藏，`show="1"` 是显式显示。

    旧实现写的是 bool(get("show"))，会把显式 show="1" 也当成隐藏——
    于是"隐藏页被改成可见"这类变更会被判成"仍是隐藏"，页数与导出页数的
    比对也会随之错判。
    """
    try:
        return slide._element.get("show") == "0"
    except Exception:  # noqa: BLE001
        return False


def _shape_type(shp) -> str:
    try:
        return str(shp.shape_type)
    except Exception:  # noqa: BLE001
        return type(shp).__name__


def _shape_path(shp, slide_no: int) -> str:
    try:
        return f"slide{slide_no}/shape[{shp.shape_id}]"
    except Exception:  # noqa: BLE001
        return f"slide{slide_no}/shape[?]"


def _group_path(shp) -> str:
    parts = []
    cur = shp
    while cur is not None:
        try:
            cur = cur._parent if hasattr(cur, "_parent") else None
        except Exception:  # noqa: BLE001
            cur = None
        if cur is None:
            break
        name = getattr(cur, "name", None)
        if name:
            parts.append(str(name))
        if len(parts) > 8:
            break
    return "/".join(reversed(parts)) if parts else ""


def _shape_visible(shp) -> bool:
    return shape_visible(shp)


def _iter_shapes(shapes, prefix=""):
    """递归遍历形状树，产出 (形状, 组路径)。

    组合（grpSp）内部的表格、图表、文本框与顶层对象同等对待——
    否则"藏在组里的对象"既进不了对象台账，也进不了内容保真的分母。
    """
    for shp in shapes:
        name = ""
        try:
            name = str(getattr(shp, "name", "") or "")
        except Exception:  # noqa: BLE001
            name = ""
        try:
            sid = str(getattr(shp, "shape_id", "?"))
        except Exception:  # noqa: BLE001
            sid = "?"
        gpath = f"{prefix}/{name or sid}" if prefix else ""
        is_group = False
        try:
            is_group = bool(getattr(shp, "shapes", None)) and \
                "GROUP" in str(getattr(shp, "shape_type", "")).upper()
        except Exception:  # noqa: BLE001
            is_group = False
        if is_group:
            inner_prefix = f"{prefix}/{name or sid}" if prefix else (name or sid)
            yield from _iter_shapes(shp.shapes, inner_prefix)
            continue
        yield shp, gpath


def _first_title(slide):
    for shp in slide.shapes:
        if getattr(shp, "has_text_frame", False) and shp.has_text_frame and shp.text_frame.text.strip():
            return shp
    return None


# ---------------------------------------------------------------- 文本 / Markdown

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def extract_text_or_markdown(path) -> ExtractedDoc:
    path = Path(path)
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    lines = text.splitlines()
    is_md = path.suffix.lower() in (".md", ".markdown")
    doc = ExtractedDoc(input_path=str(path), input_sha256=sha256_file(path),
                       input_kind="markdown" if is_md else "text")

    # 分块：Markdown 按标题分节；纯文本按空行分段
    blocks = []           # (start_line, end_line, level, heading, text)
    if is_md:
        cur_start, cur_level, cur_head, buf = 1, 0, "", []
        for i, line in enumerate(lines, start=1):
            m = _HEADING_RE.match(line)
            if m:
                if buf:
                    blocks.append((cur_start, i - 1, cur_level, cur_head, "\n".join(buf)))
                cur_start, cur_level, cur_head, buf = i, len(m.group(1)), m.group(2).strip(), []
                doc.heading_index.append({"line": i, "level": cur_level, "text": cur_head})
            else:
                buf.append(line)
        if buf:
            blocks.append((cur_start, len(lines), cur_level, cur_head, "\n".join(buf)))
    else:
        cur_start, buf = 1, []
        for i, line in enumerate(lines, start=1):
            if not line.strip() and buf:
                blocks.append((cur_start, i - 1, 0, "", "\n".join(buf)))
                cur_start, buf = i + 1, []
            else:
                buf.append(line)
        if buf:
            blocks.append((cur_start, len(lines), 0, "", "\n".join(buf)))

    pages, uid = [], 0
    for n, (s, e, level, head, body) in enumerate(blocks, start=1):
        body_text = body.strip()
        # 纯标题分节（标题后无正文）不能静默丢弃：标题本身就是内容。
        # 曾因 `if not body_text: continue` 丢掉这类分节，被提取对照实测抓出。
        if not body_text and not head:
            continue
        units = []
        if head:
            units.append(TextUnit(id=f"b{n}_h", text=head, role="heading",
                                  source_ref={"file": str(path), "line_start": s, "line_end": s,
                                              "heading_level": level}))
        for para in [p for p in body_text.split("\n") if p.strip()]:
            units.append(TextUnit(id=f"b{n}_p{uid}", text=para.strip(), role="body",
                                  source_ref={"file": str(path), "line_start": s, "line_end": e,
                                              "heading_level": level, "heading": head}))
            uid += 1
        pages.append({"page_no": n, "title": head, "units": units, "notes": "",
                      "line_start": s, "line_end": e, "heading_level": level})
    doc.pages = pages
    return doc


# ---------------------------------------------------------------- DOCX 正文

def extract_docx_body(path) -> ExtractedDoc:
    if docx is None:
        raise RuntimeError("python-docx 未安装")
    path = Path(path)
    d = docx.Document(str(path))
    doc = ExtractedDoc(input_path=str(path), input_sha256=sha256_file(path), input_kind="docx")
    pages, uid, page_no = [], 0, 1
    cur = None

    def flush():
        nonlocal cur
        if cur and cur["units"]:
            pages.append(cur)
        cur = None

    for i, para in enumerate(d.paragraphs):
        style = (para.style.name or "").lower()
        text = para.text
        is_heading = style.startswith("heading") or style.startswith("标题")
        if is_heading or cur is None:
            flush()
            level = 1
            m = re.search(r"(\d+)", style)
            if m:
                level = int(m.group(1))
            cur = {"page_no": page_no, "title": text.strip(), "units": [], "notes": "",
                   "heading_level": level, "paragraph_index": i}
            doc.heading_index.append({"paragraph_index": i, "level": level, "text": text.strip()})
            page_no += 1
        if not text.strip():
            continue
        cur["units"].append(TextUnit(
            id=f"d{cur['page_no']}_p{uid}", text=text, role="heading" if is_heading else "body",
            source_ref={"file": str(path), "paragraph_index": i, "style": para.style.name,
                        "heading_level": cur["heading_level"]}))
        uid += 1
    flush()
    doc.pages = pages
    # P2：DOCX 规则表提取（表头行 + 数据行 + 结构异常登记）
    from .tables import extract_tables_docx
    doc.tables = [t.to_dict() for t in extract_tables_docx(d, str(path))]
    doc.warnings.append(
        f"DOCX 已提取正文段落、标题层级与 {len(doc.tables)} 张规则表（P2）；"
        "合并单元格等不规则结构与图表关系仍不在本阶段承诺范围。")
    return doc


def extract_any(path) -> ExtractedDoc:
    p = Path(path)
    suf = p.suffix.lower()
    if suf == ".pptx":
        return extract_pptx(p)
    if suf in (".md", ".markdown"):
        return extract_text_or_markdown(p)
    if suf in (".txt", ".text"):
        return extract_text_or_markdown(p)
    if suf == ".docx":
        return extract_docx_body(p)
    raise ValueError(f"不支持的输入类型: {p.suffix}")
