"""P2 原生图表：来源提取、工作簿与缓存核对、系列—类别—数值关系核验、原生编辑。

声明支持范围（首发核心图表，规格 §3）：
    二维柱状图（COLUMN_CLUSTERED）、条形图（BAR_CLUSTERED）、折线图（LINE / LINE_MARKERS）。
    饼图经专项验证后另开；组合图、外部数据、复杂多轴不在承诺范围。

三层数据（规格 §8.5）：
    1. 可见缓存  —— chartN.xml 的 c:numCache / c:strCache，渲染器实际看到的值；
    2. 内嵌工作簿 —— ppt/embeddings/*.xlsx，用户在 PowerPoint 里"编辑数据"打开的原始表；
    3. 结构视图  —— python-pptx 对象模型（图表类型、系列名、类别名）。

冲突判定（C11）：工作簿与缓存不一致 → INPUT.CHART_SOURCE_CONFLICT（REVIEW → NEEDS_REVIEW）。
    必须保留双方原始值与位置，不得静默选择一份当权威。该核对发生在美化之前。

关系核验（C07）：按「系列—类别—数值」三元组比对，多重集合不变而绑定变化
    （系列标签对调、类别数值搬家）必须报 FIDELITY.CHART_BINDING_MISMATCH。

原生编辑实证（N05 / E02）：改单点 → 保存 → 重开 → 独立核对；图表被图片替代
    报 EDIT.CHART_RASTERIZED（图片可移动不等于图表数据可编辑）。
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
C = "http://schemas.openxmlformats.org/drawingml/2006/chart"

# 声明支持的图表类型（规范化名 → XL_CHART_TYPE 名）
SUPPORTED_CHART_TYPES = {
    "column": "二维柱状图（COLUMN_CLUSTERED）",
    "bar": "条形图（BAR_CLUSTERED）",
    "line": "折线图（LINE / LINE_MARKERS）",
}
_TYPE_MAP = {
    "COLUMN_CLUSTERED": "column", "COLUMN_STACKED": "column", "COLUMN_STACKED_100": "column",
    "BAR_CLUSTERED": "bar", "BAR_STACKED": "bar", "BAR_STACKED_100": "bar",
    "LINE": "line", "LINE_MARKERS": "line", "LINE_STACKED": "line", "LINE_MARKERS_STACKED": "line",
}

CONTRACT_CODES = {
    "binding_mismatch": "FIDELITY.CHART_BINDING_MISMATCH",
    "rasterized": "EDIT.CHART_RASTERIZED",
    "decoy": "EDIT.DECOY",
    "source_conflict": "INPUT.CHART_SOURCE_CONFLICT",
}


def _norm_type(raw) -> str:
    name = str(raw).split(".")[-1].split(" ")[0].upper()
    return _TYPE_MAP.get(name, name.lower())


def _num_equal(a, b) -> bool:
    """数值相等：两边都能解析为数值时按数值比（容忍 350 与 350.0），否则按字符串比。"""
    try:
        fa, fb = float(str(a).replace(",", "")), float(str(b).replace(",", ""))
        return abs(fa - fb) < 1e-9
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


def _multiset(values):
    """多重集合的规范化键：能解析为数值的统一按浮点比，避免 18 与 18.0 被当成不同值。

    图表缓存里同一个值可能写作 "18"（python-pptx 初始写入）或 "18.0"（replace_data
    重写后）；用原始字符串比较多重集合会把"对调"误判成"改写"。
    """
    out = []
    for v in values:
        try:
            out.append(("n", round(float(str(v).replace(",", "")), 9)))
        except (TypeError, ValueError):
            out.append(("s", str(v).strip()))
    return sorted(out, key=repr)


@dataclass
class ChartModel:
    """一个原生图表的完整模型：结构 + 缓存值 + 工作簿值。"""
    chart_id: str
    page: int
    shape_id: str
    chart_type: str                 # 规范化：column / bar / line
    raw_type: str
    series: list = field(default_factory=list)
    categories: list = field(default_factory=list)
    cache: dict = field(default_factory=dict)      # series -> {category: value}
    workbook: dict = field(default_factory=dict)   # series -> {category: value}
    workbook_part: str = ""
    geometry: dict = field(default_factory=dict)   # left/top/width/height（EMU）
    source_ref: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    def to_dict(self):
        return {
            "chart_id": self.chart_id, "page": self.page, "shape_id": self.shape_id,
            "chart_type": self.chart_type, "raw_type": self.raw_type,
            "series": self.series, "categories": self.categories,
            "cache": self.cache, "workbook": self.workbook,
            "workbook_part": self.workbook_part, "geometry": self.geometry,
            "source_ref": self.source_ref, "warnings": self.warnings,
        }


# ---------------------------------------------------------------- 提取

def _chart_part_caches(zf, part_name):
    """直接读 chartN.xml：系列名、类别名、缓存值（c:strCache / c:numCache）。"""
    from lxml import etree
    root = etree.fromstring(zf.read(part_name))
    series, categories, cache = [], [], {}
    for ser in root.iter(f"{{{C}}}ser"):
        name = ""
        tx = ser.find(f"{{{C}}}tx/{{{C}}}strRef/{{{C}}}strCache")
        if tx is not None:
            pts = tx.findall(f"{{{C}}}pt")
            name = "".join((p.find(f"{{{C}}}v").text or "") for p in pts
                           if p.find(f"{{{C}}}v") is not None) or (tx.findtext(f"{{{C}}}v") or "")
        else:
            v = ser.find(f"{{{C}}}tx/{{{C}}}v")
            name = (v.text or "") if v is not None else ""
        cats = []
        cat = ser.find(f"{{{C}}}cat")
        if cat is not None:
            for cache_el in cat.iter(f"{{{C}}}strCache"):
                for p in cache_el.findall(f"{{{C}}}pt"):
                    v = p.find(f"{{{C}}}v")
                    cats.append((v.text or "") if v is not None else "")
                break
            if not cats:
                for cache_el in cat.iter(f"{{{C}}}numCache"):
                    for p in cache_el.findall(f"{{{C}}}pt"):
                        v = p.find(f"{{{C}}}v")
                        cats.append((v.text or "") if v is not None else "")
                    break
        vals = {}
        val = ser.find(f"{{{C}}}val")
        if val is not None:
            for cache_el in val.iter(f"{{{C}}}numCache"):
                for p in cache_el.findall(f"{{{C}}}pt"):
                    v = p.find(f"{{{C}}}v")
                    idx = int(p.get("idx", "0"))
                    vals[idx] = (v.text or "") if v is not None else ""
                break
        series.append(name)
        if not categories:
            categories = cats
        cache[name] = {cats[i] if i < len(cats) else f"#{i}": v
                       for i, v in sorted(vals.items())}
    return series, categories, cache


def _read_workbook(zf, embeddings):
    """读内嵌工作簿：按「首行系列名 + 首列类别」定位，返回 series -> {category: value}。

    只取第一张工作表；无法定位的系列/类别登记 warnings，不猜。
    """
    if not embeddings:
        return {}, ""
    try:
        import openpyxl
    except ImportError:  # pragma: no cover
        return {"__error__": "openpyxl 不可用"}, ""
    part = sorted(embeddings)[0]
    import tempfile
    try:
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=True) as tmp:
            tmp.write(zf.read(part))
            tmp.flush()
            wb = openpyxl.load_workbook(tmp.name, data_only=True)
            ws = wb.worksheets[0]
            grid = [["" if c is None else str(c) for c in row]
                    for row in ws.iter_rows(values_only=True)]
    except Exception as e:  # noqa: BLE001
        return {"__error__": f"{type(e).__name__}: {e}"}, part
    if not grid:
        return {}, part
    header = grid[0]
    out = {}
    for ci, name in enumerate(header):
        if ci == 0 or not str(name).strip():
            continue
        col = {}
        for ri in range(1, len(grid)):
            row = grid[ri]
            cat = row[0] if row else ""
            if ci < len(row) and str(cat).strip():
                col[str(cat)] = "" if row[ci] is None else str(row[ci])
        out[str(name)] = col
    return out, part


class _BytesReader:
    """openpyxl 接受的极简 file-like（保留备用；主路径用临时文件）。"""

    def __init__(self, data):
        import io
        self._f = io.BytesIO(data)

    def read(self, *a):
        return self._f.read(*a)

    def seek(self, *a):
        return self._f.seek(*a)

    def tell(self):
        return self._f.tell()

    def close(self):
        self._f.close()


def _chart_workbook_part(zf, chart_part):
    """按 chartN.xml 的 rels 找到它自己的内嵌工作簿（不是全局第一个）。"""
    rels_name = chart_part.replace("charts/", "charts/_rels/") + ".rels"
    if rels_name not in zf.namelist():
        return None
    from lxml import etree
    rels = etree.fromstring(zf.read(rels_name))
    for r in rels:
        tgt = (r.get("Target") or "")
        if "embeddings" in tgt:
            return "ppt/" + tgt.replace("../", "").lstrip("/")
    return None


def extract_charts_pptx(prs, path) -> list:
    """从 Presentation 提取全部原生图表（含嵌套在组合里的）。"""
    path = str(path)
    zf = zipfile.ZipFile(path)
    names = zf.namelist()
    embeddings = sorted(n for n in names if n.startswith("ppt/embeddings/"))
    # chart part -> 所属 slide / shape（按 slideN.xml 的 rels 反查）
    part_owner = {}
    for sn in sorted(n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)):
        rels_name = sn.replace("slides/", "slides/_rels/") + ".rels"
        if rels_name not in names:
            continue
        from lxml import etree
        rels = etree.fromstring(zf.read(rels_name))
        for r in rels:
            tgt = (r.get("Target") or "").replace("../", "ppt/")
            if re.match(r"ppt/charts/chart\d+\.xml$", tgt):
                part_owner.setdefault(tgt, []).append(sn)

    out = []
    for si, slide in enumerate(prs.slides, start=1):
        ordinal = 0

        def walk(shapes, prefix):
            """递归遍历 slide.shapes / group.shapes。

            D8 修复：此前只遍历 slide.shapes 顶层，组合内部的原生图表既不进
            doc.charts 也不进图表检查链的分母——删掉组合里的图表不会被发现。
            现递归进 GroupShape.shapes，与 extractors._iter_shapes /
            parsers_xml._xml_walk 的组遍历口径一致。
            """
            nonlocal ordinal
            from pptx.enum.shapes import MSO_SHAPE_TYPE
            for shp in shapes:
                obj_ref = f"{prefix}shape[{shp.shape_id}]" if prefix \
                    else f"slide{si}/shape[{shp.shape_id}]"
                if shp.shape_type == MSO_SHAPE_TYPE.GROUP:
                    walk(shp.shapes, f"{prefix}grpSp[{shp.shape_id}]/")
                    continue
                if not (getattr(shp, "has_chart", False) and shp.has_chart):
                    continue
                ch = shp.chart
                raw_type = str(ch.chart_type)
                ctype = _norm_type(raw_type)
                part = getattr(getattr(ch, "part", None), "partname", "")
                part_name = str(part).lstrip("/")
                try:
                    s_xml, cats_xml, cache = _chart_part_caches(zf, part_name)
                except Exception as e:  # noqa: BLE001
                    s_xml, cats_xml, cache = [], [], {"__error__": f"{type(e).__name__}: {e}"}
                wb_part = _chart_workbook_part(zf, part_name)
                workbook, _ = _read_workbook(zf, [wb_part] if wb_part else [])
                model = ChartModel(
                    chart_id=f"slide{si}/chart[{shp.shape_id}]", page=si,
                    shape_id=str(shp.shape_id), chart_type=ctype, raw_type=raw_type,
                    series=s_xml, categories=cats_xml, cache=cache, workbook=workbook,
                    workbook_part=wb_part,
                    geometry={"left": shp.left, "top": shp.top,
                              "width": shp.width, "height": shp.height},
                    source_ref={"file": path, "slide": si, "part": part_name,
                                "object": obj_ref,
                                "group_path": prefix.rstrip("/") if prefix else "",
                                "chart_ordinal": ordinal},
                )
                if ctype not in SUPPORTED_CHART_TYPES:
                    model.warnings.append(
                        f"图表类型 {raw_type} 不在声明支持范围（{'/'.join(SUPPORTED_CHART_TYPES)}）；"
                        "不承诺其数据关系核验与原生编辑。")
                out.append(model)
                ordinal += 1

        walk(slide.shapes, "")
    return out


# ---------------------------------------------------------------- C11：工作簿 vs 缓存

def check_chart_source_consistency(chart: ChartModel):
    """C11：内嵌工作簿与可见缓存冲突 → INPUT.CHART_SOURCE_CONFLICT（REVIEW）。

    保留双方原始值与位置；不选择权威来源。在美化之前调用。
    """
    findings = []
    if "__error__" in chart.workbook:
        findings.append({"code": CONTRACT_CODES["source_conflict"], "severity": "REVIEW",
                         "origin": "chart_source", "slide": chart.page,
                         "chart_id": chart.chart_id,
                         "detail": f"内嵌工作簿无法读取（{chart.workbook['__error__']}）；"
                                   "无法与显示缓存核对，不得声称数据一致。",
                         "expected": "可读取的内嵌工作簿", "actual": chart.workbook["__error__"]})
        return findings
    if not chart.workbook:
        chart.warnings.append("未找到内嵌工作簿；无可核对的第二来源。")
        return findings
    diffs = []
    all_series = sorted(set(chart.cache) | set(chart.workbook))
    for s in all_series:
        c_map = chart.cache.get(s, {})
        w_map = chart.workbook.get(s, {})
        for cat in sorted(set(c_map) | set(w_map)):
            cv, wv = c_map.get(cat), w_map.get(cat)
            if cv is None or wv is None:
                diffs.append({"series": s, "category": cat,
                              "cache": cv, "workbook": wv,
                              "kind": "单侧缺失"})
            elif not _num_equal(cv, wv):
                diffs.append({"series": s, "category": cat,
                              "cache": cv, "workbook": wv, "kind": "值不一致"})
    for d in diffs:
        findings.append({
            "code": CONTRACT_CODES["source_conflict"], "severity": "REVIEW",
            "reason_code": "CHART.SOURCE_CONFLICT",
            "origin": "chart_source", "slide": chart.page, "chart_id": chart.chart_id,
            "series": d["series"], "category": d["category"],
            "expected": {"source": "内嵌工作簿", "value": d["workbook"]},
            "actual": {"source": "显示缓存", "value": d["cache"]},
            "detail": f"系列「{d['series']}」类别「{d['category']}」的{d['kind']}："
                      f"工作簿={d['workbook']!r}，缓存={d['cache']!r}。"
                      "双方原始值均已保留；未指定以哪份为准前不得美化。",
        })
    return findings


def check_chart_source_conflicts(charts):
    findings = []
    for c in charts:
        findings += check_chart_source_consistency(c)
    return findings


# ---------------------------------------------------------------- E02：原生图表在不在

def _match_out_chart(src: ChartModel, out_charts):
    """来源图表 ↔ 输出图表配对：同页 + 同图表序号；序号对不上再按系列签名兜底。"""
    same_page = [c for c in out_charts if c.page == src.page]
    for c in same_page:
        if c.source_ref.get("chart_ordinal") == src.source_ref.get("chart_ordinal"):
            return c
    for c in same_page:
        if set(c.series) == set(src.series) and set(c.categories) == set(src.categories):
            return c
    return None


def check_chart_native_present(src_charts, out_charts):
    """E02：来源里的原生图表在输出里必须还是原生图表。"""
    findings = []
    for src in src_charts:
        if src.chart_type not in SUPPORTED_CHART_TYPES:
            continue  # 不支持类型的图表不做原生性判定（已在提取时登记）
        out = _match_out_chart(src, out_charts)
        if out is None:
            findings.append({"code": CONTRACT_CODES["rasterized"], "severity": "BLOCKING",
                             "reason_code": "CHART.NATIVE_MISSING",
                             "origin": "chart_native", "slide": src.page,
                             "chart_id": src.chart_id, "chart_type": src.chart_type,
                             "expected": f"原生图表对象（{src.chart_type}，"
                                         f"{len(src.series)} 系列 × {len(src.categories)} 类别）",
                             "actual": "输出中无对应原生图表（疑似被图片替代）",
                             "detail": "可见图表不再由原生图表对象承载；"
                                       "图片可移动不等于图表数据可编辑。"})
            continue
        if not out.series or not out.categories:
            findings.append({"code": CONTRACT_CODES["decoy"], "severity": "BLOCKING",
                             "reason_code": "CHART.EMPTY_DECOY",
                             "origin": "chart_native", "slide": out.page,
                             "chart_id": out.chart_id,
                             "expected": "有系列与类别的原生图表",
                             "actual": "图表对象存在但无系列或无类别",
                             "detail": "原生图表对象在但数据为空，属于空壳诱饵。"})
    return findings


# ---------------------------------------------------------------- C07：系列—类别—数值绑定

def check_chart_binding(src_charts, out_charts):
    """C07：按「系列—类别—数值」三元组核对；多重集合不变而绑定变化必须被发现。"""
    findings = []
    for src in src_charts:
        if src.chart_type not in SUPPORTED_CHART_TYPES:
            continue
        out = _match_out_chart(src, out_charts)
        if out is None:
            continue  # 原生性缺失已由 check_chart_native_present 报告
        base = {"origin": "chart_binding", "slide": out.page, "chart_id": out.chart_id,
                "chart_type": src.chart_type}
        if set(out.series) != set(src.series):
            findings.append({"code": CONTRACT_CODES["binding_mismatch"], "severity": "BLOCKING",
                             "reason_code": "CHART.SERIES_SET_CHANGED", **base,
                             "expected": sorted(src.series), "actual": sorted(out.series),
                             "detail": "系列集合发生变化。"})
            continue
        if set(out.categories) != set(src.categories):
            findings.append({"code": CONTRACT_CODES["binding_mismatch"], "severity": "BLOCKING",
                             "reason_code": "CHART.CATEGORY_SET_CHANGED", **base,
                             "expected": sorted(src.categories), "actual": sorted(out.categories),
                             "detail": "类别集合发生变化。"})
            continue
        diffs, swaps = [], []
        for s in src.series:
            cmap = src.cache.get(s, {})
            omap = out.cache.get(s, {})
            for cat in src.categories:
                cv = cmap.get(cat)
                ov = omap.get(cat)
                if cv is None or ov is None:
                    diffs.append((s, cat, cv, ov))
                elif not _num_equal(cv, ov):
                    diffs.append((s, cat, cv, ov))
        # 判定特征（与表格关系核验同构）：
        #   整图数值多重集合不变 + 存在绑定差异 → 值在绑定之间搬家（对调/交换）；
        #   多重集合本身变化 → 数值被改写。
        # 纯按单元格逐个判断会把"对调"误报成"改写"，因为对调后每个格子的值
        # 都能在输出里找到（只是不在原绑定里），必须按多重集合先分型。
        src_multiset = _multiset(v for m in src.cache.values() for v in m.values())
        out_multiset = _multiset(v for m in out.cache.values() for v in m.values())
        multiset_same = src_multiset == out_multiset
        if diffs and multiset_same:
            for s, cat, cv, ov in diffs:
                findings.append({"code": CONTRACT_CODES["binding_mismatch"],
                                 "severity": "BLOCKING",
                                 "reason_code": "CHART.SERIES_CATEGORY_SWAP", **base,
                                 "series": s, "category": cat,
                                 "expected": cv, "actual": ov,
                                 "detail": f"系列「{s}」类别「{cat}」的值搬去了其他绑定："
                                           "整图数值多重集合不变，只有系列—类别—数值绑定被破坏"
                                           "（系列标签对调或类别数值互换）。"})
            continue
        if not diffs and not multiset_same:
            findings.append({"code": CONTRACT_CODES["binding_mismatch"], "severity": "BLOCKING",
                             "reason_code": "CHART.VALUE_MISMATCH", **base,
                             "expected": src_multiset, "actual": out_multiset,
                             "detail": "图表数值多重集合与来源不一致。"})
            continue
        for s, cat, cv, ov in diffs:
            findings.append({"code": CONTRACT_CODES["binding_mismatch"],
                             "severity": "BLOCKING",
                             "reason_code": "CHART.VALUE_MISMATCH", **base,
                             "series": s, "category": cat,
                             "expected": cv, "actual": ov,
                             "detail": f"系列「{s}」类别「{cat}」的数值与来源不一致"
                                       "（非绑定搬家）。"})
    return findings


# ---------------------------------------------------------------- N05：原生编辑实证

def chart_edit_probe(pptx_in, pptx_out, series_idx=0, category_idx=0, new_value=999,
                     page=None):
    """原生编辑实证：改一个数据点 → 保存 → 重开 → 独立核对。

    走 chart.replace_data：它同时改写 chartN.xml 的缓存与内嵌工作簿，
    正是 N05 要求的"更新一个数据点并同步其可见图表与相关数据"。
    page 指定第几页的图表（1 起）；None 表示取第一个。
    返回 (ok, detail)；detail 含改前/改后/未改动点核对/工作簿同步核对。
    """
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData

    prs = Presentation(str(pptx_in))
    target = None
    for idx, slide in enumerate(prs.slides, start=1):
        if page is not None and idx != page:
            continue
        for shp in slide.shapes:
            if getattr(shp, "has_chart", False) and shp.has_chart:
                target = shp.chart
                break
        if target is not None:
            break
    if target is None:
        return False, {"error": f"未找到原生图表（page={page}）"}

    plot = target.plots[0]
    cats = [str(c) for c in plot.categories]
    names = [s.name for s in plot.series]
    before = [[v for v in s.values] for s in plot.series]

    cd = CategoryChartData()
    cd.categories = cats
    for si, s in enumerate(plot.series):
        vals = list(s.values)
        if si == series_idx and category_idx < len(vals):
            vals[category_idx] = new_value
        cd.add_series(str(s.name), vals,
                      number_format=getattr(s, "number_format", None) or "General")
    target.replace_data(cd)

    out = Path(str(pptx_out))
    out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out))

    # 保存重开：独立核对改动落盘、其余点未动、工作簿同步
    prs2 = Presentation(str(out))
    ch2 = None
    for idx, slide in enumerate(prs2.slides, start=1):
        if page is not None and idx != page:
            continue
        for shp in slide.shapes:
            if getattr(shp, "has_chart", False) and shp.has_chart:
                ch2 = shp.chart
                break
        if ch2 is not None:
            break
    if ch2 is None:
        return False, {"error": "重开后找不到图表"}
    after = [[v for v in s.values] for s in ch2.plots[0].series]
    names2 = [s.name for s in ch2.plots[0].series]
    edited_ok = (after[series_idx][category_idx] == new_value
                 if series_idx < len(after) and category_idx < len(after[series_idx]) else False)
    others_ok = all(
        after[i][j] == before[i][j]
        for i in range(len(before)) for j in range(len(before[i]))
        if not (i == series_idx and j == category_idx))
    # 工作簿同步核对：重读**本图表自己的**内嵌工作簿，改动的点必须同步。
    # （曾错读全局第一个 embeddings，单系列图表的核对因此恒假。）
    zf_out = zipfile.ZipFile(str(out))
    own_wb_part = None
    for idx, slide in enumerate(prs.slides, start=1):
        if page is not None and idx != page:
            continue
        for shp in slide.shapes:
            if getattr(shp, "has_chart", False) and shp.has_chart:
                own_wb_part = _chart_workbook_part(
                    zf_out, str(getattr(shp.chart.part, "partname", "")).lstrip("/"))
                break
        if own_wb_part:
            break
    wb_after, _ = _read_workbook(zf_out, [own_wb_part] if own_wb_part else [])
    sname = names[series_idx] if series_idx < len(names) else None
    cat = cats[category_idx] if category_idx < len(cats) else None
    wb_val = (wb_after.get(sname, {}) or {}).get(cat) if sname and cat else None
    wb_ok = wb_val is not None and _num_equal(wb_val, new_value)
    detail = {
        "series": sname, "category": cat,
        "before": before[series_idx][category_idx] if edited_ok or True else None,
        "after": after[series_idx][category_idx],
        "new_value": new_value,
        "edited_ok": edited_ok, "other_points_unchanged": others_ok,
        "workbook_synced": wb_ok, "workbook_value_after": wb_val,
        "series_names_preserved": names2 == names,
        "categories_preserved": [str(c) for c in ch2.plots[0].categories] == cats,
        "saved_and_reopened": True,
    }
    ok = edited_ok and others_ok and wb_ok and names2 == names
    return ok, detail


def set_chart_cache_only(pptx_in, pptx_out, series_idx=0, category_idx=0, new_value=999):
    """故障注入：只改 chartN.xml 的显示缓存，不动内嵌工作簿（制造 C11 冲突）。"""
    from pptx import Presentation
    prs = Presentation(str(pptx_in))
    ch = None
    for slide in prs.slides:
        for shp in slide.shapes:
            if getattr(shp, "has_chart", False) and shp.has_chart:
                ch = shp.chart
                break
        if ch is not None:
            break
    if ch is None:
        return False
    plot = ch.plots[0]
    ser = plot.series[series_idx]
    # 直接改 XML 缓存（绕过 replace_data，工作簿保持原值）
    from lxml import etree
    val_el = ser._element.find(f"{{{C}}}val")
    num_cache = val_el.find(f"{{{C}}}numRef/{{{C}}}numCache")
    if num_cache is None:
        return False
    pts = num_cache.findall(f"{{{C}}}pt")
    done = False
    for p in pts:
        if int(p.get("idx", "-1")) == category_idx:
            v = p.find(f"{{{C}}}v")
            if v is None:
                v = etree.SubElement(p, f"{{{C}}}v")
            v.text = str(new_value)
            done = True
    if not done:
        return False
    prs.save(str(pptx_out))
    return True
