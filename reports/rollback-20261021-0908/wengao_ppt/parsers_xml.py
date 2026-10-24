"""第二解析器：直接读 PPTX 包内原始 XML，不经 python-pptx 对象模型。

目的（规格 §8.1）：两个独立解析器比较**对象集合、类型、引用关系**，而不是只比总数。
总数相同也可能一个漏 A、一个多 B。共享实现导致的共同盲区需要独立样本与原稿核对发现；
双解析不能宣称"绝对无遗漏"。

本解析器只做结构普查与表格单元格读取，不做语义解释：
- 按 presentation.xml 的 sldIdLst 顺序解析每页；
- 统计每页的图形对象类型族（sp / pic / graphicFrame / grpSp / cxnSp）；
- 读取 graphicFrame 内的 a:tbl 单元格文本（按行/列索引）；
- 记录绘图对象总数与 rels 引用的媒体/图表部件。
"""
from __future__ import annotations

import re
import zipfile

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

TEXT_TAG = f"{{{A}}}t"

# 对象类型族：XML 标签名 ↔ python-pptx 类名。用于把两个视角的"类型"放到同一口径上比较，
# 否则一边是 sp/pic/graphicFrame，另一边是 Shape/Picture/GraphicFrame，永远不一致。
_KIND_TAGS = ("sp", "pic", "graphicFrame", "grpSp", "cxnSp")
_CLASS_TO_KIND = {
    "Shape": "sp", "AutoShape": "sp", "TextBox": "sp",
    "Picture": "pic",
    "GraphicFrame": "graphicFrame", "Table": "graphicFrame", "Chart": "graphicFrame",
    "GroupShape": "grpSp",
    "Connector": "cxnSp",
}


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _kind_of_class(cls_name: str) -> str:
    return _CLASS_TO_KIND.get(cls_name, cls_name)


def parse_pptx_xml(path):
    """返回 {slides:[{index, objects:[{kind, tag, name}], tables:[{rows, cells}]}], media:[...]}。"""
    from lxml import etree

    z = zipfile.ZipFile(str(path))
    names = z.namelist()

    # 正确的页序：presentation.xml 的 sldIdLst + rels 映射
    order = []
    try:
        pres = etree.fromstring(z.read("ppt/presentation.xml"))
        rels = etree.fromstring(z.read("ppt/_rels/presentation.xml.rels"))
        rid2target = {r.get("Id"): r.get("Target") for r in rels}
        for sld in pres.iter(f"{{{P}}}sldId"):
            rid = sld.get(f"{{{R}}}id")
            tgt = rid2target.get(rid, "")
            tgt = tgt.lstrip("/")
            if not tgt.startswith("ppt/"):
                tgt = "ppt/" + tgt.replace("../", "")
            order.append(tgt)
    except Exception:  # noqa: BLE001
        order = sorted(n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n))

def _xml_shape_children(el):
    """spTree / grpSp 的直接子元素中属于图形对象族（_KIND_TAGS）的，按 XML 原序返回。

    非图形对象（nvGrpSpPr / grpSpPr 等容器属性）不在此列；它们由调用方按
    uncatalogued 单独登记，不静默丢弃。
    """
    for child in el:
        if _local(child.tag) in _KIND_TAGS:
            yield child


def _xml_walk(el, prefix, objects, tables):
    """递归遍历 spTree / grpSp，登记对象身份与表格。

    路径口径（与 parse_pptx_xml_via_pptx 对齐，便于 compare_parsers 不因路径格式
    误报差异）：
      - 顶层对象 i：path = "[i]"，nested = False；
      - 组合内直接子对象（组合对象索引为 j）：path = "[j]/kind[child_idx]"，
        child_idx 为该组合直接子图形对象的 0 基序号，nested = True。
    组合套组合按同一规则递归。
    """
    child_idx = 0
    for child in _xml_shape_children(el):
        k = _local(child.tag)
        nv = child.find(f".//{{{P}}}cNvPr")
        if nv is None:
            nv = child.find(f".//{{{A}}}cNvPr")
        name = nv.get("name") if nv is not None else None
        hidden = bool(nv is not None and nv.get("hidden") == "1")
        obj_idx = len(objects)
        if prefix:
            path = f"{prefix}/{k}[{child_idx}]"
            nested = True
        else:
            path = f"[{obj_idx}]"
            nested = False
        objects.append({"kind": k, "name": name, "nested": nested, "path": path,
                        "hidden": hidden})
        if k == "grpSp":
            _xml_walk(child, f"[{obj_idx}]", objects, tables)
        else:
            # 表格：graphicFrame 内的 a:tbl。
            # 组合的表格由递归收集，这里不 iter grpSp 的后代 a:tbl，避免重复计数。
            for tbl in child.iter(f"{{{A}}}tbl"):
                rows = []
                for tr in tbl.findall(f"{{{A}}}tr"):
                    cells = []
                    for tc in tr.findall(f"{{{A}}}tc"):
                        txt = "".join(t.text or "" for t in tc.iter(TEXT_TAG))
                        cells.append(txt)
                    rows.append(cells)
                tables.append({"rows": rows, "row_count": len(rows),
                               "col_count": max((len(r) for r in rows), default=0)})
        child_idx += 1


def parse_pptx_xml(path):
    """返回 {slides:[{index, objects:[{kind, tag, name}], tables:[{rows, cells}]}], media:[...]}。"""
    from lxml import etree

    z = zipfile.ZipFile(str(path))
    names = z.namelist()

    # 正确的页序：presentation.xml 的 sldIdLst + rels 映射
    order = []
    try:
        pres = etree.fromstring(z.read("ppt/presentation.xml"))
        rels = etree.fromstring(z.read("ppt/_rels/presentation.xml.rels"))
        rid2target = {r.get("Id"): r.get("Target") for r in rels}
        for sld in pres.iter(f"{{{P}}}sldId"):
            rid = sld.get(f"{{{R}}}id")
            tgt = rid2target.get(rid, "")
            tgt = tgt.lstrip("/")
            if not tgt.startswith("ppt/"):
                tgt = "ppt/" + tgt.replace("../", "")
            order.append(tgt)
    except Exception:  # noqa: BLE001
        order = sorted(n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n))

    slides = []
    for idx, part in enumerate(order, start=1):
        if part not in names:
            continue
        root = etree.fromstring(z.read(part))
        objects, tables, uncatalogued = [], [], []
        tree = root.find(f".//{{{P}}}cSld/{{{P}}}spTree")
        if tree is None:
            slides.append({"index": idx, "part": part, "objects": [], "tables": [],
                           "uncatalogued": [], "note": "spTree 未找到"})
            continue
        # 非图形对象的 spTree 直接子元素单独登记（不静默丢弃）
        for child in tree:
            if _local(child.tag) not in _KIND_TAGS:
                nv = child.find(f".//{{{P}}}cNvPr")
                if nv is None:
                    nv = child.find(f".//{{{A}}}cNvPr")
                uncatalogued.append({"tag": _local(child.tag),
                                     "name": nv.get("name") if nv is not None else None})
        _xml_walk(tree, "", objects, tables)
        slides.append({"index": idx, "part": part, "objects": objects, "tables": tables,
                       "uncatalogued": uncatalogued})

    media = sorted(n for n in names if n.startswith("ppt/media/"))
    charts = sorted(n for n in names if re.match(r"ppt/charts/chart\d+\.xml$", n))
    embedded = sorted(n for n in names if re.match(r"ppt/embeddings/", n))
    return {"slides": slides, "media": media, "charts": charts, "embedded": embedded,
            "part_count": len(names)}


def compare_parsers(doc_a, doc_b):
    """比较两个视角的对象集合与表格结构，返回差异清单（不是只比总数）。

    doc_a：原始 XML 直读（含组合内部对象）；doc_b：python-pptx 对象模型。

    比较维度（R09 要求"不只比总数"）：
      1. 页集合；
      2. 每页对象类型多重集合（漏对象/误列对象都会被发现）；
      3. 每页对象身份多重集合 (kind, name, hidden) —— 数量相同但成员不同也能发现；
      4. 每页嵌套路径签名 —— 同一对象在一边是顶层、在一边藏在组合里，能发现；
      5. 每页表数量、单元格集合、行列形状。
    """
    diffs = []
    sa = {s["index"]: s for s in doc_a["slides"]}
    sb = {s["index"]: s for s in doc_b["slides"]}
    if set(sa) != set(sb):
        diffs.append({"what": "页集合", "page": None,
                      "only_in_first": sorted(set(sa) - set(sb)),
                      "only_in_second": sorted(set(sb) - set(sa))})
    for i in sorted(set(sa) & set(sb)):
        ka = sorted(o["kind"] for o in sa[i]["objects"])
        kb = sorted(o["kind"] for o in sb[i]["objects"])
        if ka != kb:
            diffs.append({"what": f"第 {i} 页对象类型多重集合", "page": i,
                          "first": ka, "second": kb})
        ia = sorted((o["kind"], o.get("name") or "", bool(o.get("hidden")))
                    for o in sa[i]["objects"])
        ib = sorted((o["kind"], o.get("name") or "", bool(o.get("hidden")))
                    for o in sb[i]["objects"])
        if ia != ib:
            diffs.append({"what": f"第 {i} 页对象身份集合（类型/名称/隐藏）", "page": i,
                          "first": ia, "second": ib,
                          "only_in_first": sorted(set(ia) - set(ib)),
                          "only_in_second": sorted(set(ib) - set(ia)),
                          "detail": "数量可能相同但成员不同：一边漏掉对象 A 并误列对象 B。"})
        pa = sorted((o.get("name") or "", o.get("nested"), o.get("path") or "")
                    for o in sa[i]["objects"])
        pb = sorted((o.get("name") or "", o.get("nested"), o.get("path") or "")
                    for o in sb[i]["objects"])
        if pa != pb:
            diffs.append({"what": f"第 {i} 页对象嵌套路径签名", "page": i,
                          "first": pa, "second": pb,
                          "detail": "同一对象在一边位于顶层、在一边位于组合内部（组路径不同）。"})
        ta = sa[i]["tables"]
        tb = sb[i]["tables"]
        if len(ta) != len(tb):
            diffs.append({"what": f"第 {i} 页表数量", "page": i, "first": len(ta),
                          "second": len(tb),
                          "detail": "XML 视角看得见的表格，对象模型视角看不到（常见于组合内部的表格）。"})
            continue
        for k, (x, y) in enumerate(zip(ta, tb)):
            ca = [c for r in x["rows"] for c in r]
            cb = [c for r in y["rows"] for c in r]
            if sorted(ca) != sorted(cb):
                diffs.append({"what": f"第 {i} 页第 {k} 表单元格集合", "page": i,
                              "only_in_first": sorted(set(ca) - set(cb)),
                              "only_in_second": sorted(set(cb) - set(ca))})
            if x["row_count"] != y["row_count"] or x["col_count"] != y["col_count"]:
                diffs.append({"what": f"第 {i} 页第 {k} 表形状", "page": i,
                              "first": [x["row_count"], x["col_count"]],
                              "second": [y["row_count"], y["col_count"]]})
    return diffs


def _pptx_walk(shapes, prefix, objects, tables):
    """递归遍历 slide.shapes / group.shapes，与 _xml_walk 路径口径对齐。

    D2 修复：此前只遍历 slide.shapes 顶层，组合内部对象本视角看不到——
    任何含 grpSp 的输入都会被判 GATE.PARSER_DIVERGENCE（误伤合法组合）。
    现递归进 GroupShape.shapes，让两视角看到同一批对象。
    """
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    from wengao_ppt.core import shape_hidden
    child_idx = 0
    for shp in shapes:
        cls = type(shp).__name__
        kind = _kind_of_class(cls)
        obj_idx = len(objects)
        if prefix:
            path = f"{prefix}/{kind}[{child_idx}]"
            nested = True
        else:
            path = f"[{obj_idx}]"
            nested = False
        objects.append({"kind": kind, "name": shp.name, "nested": nested,
                        "path": path, "hidden": shape_hidden(shp)})
        if shp.shape_type == MSO_SHAPE_TYPE.GROUP:
            _pptx_walk(shp.shapes, f"[{obj_idx}]", objects, tables)
        if getattr(shp, "has_table", False) and shp.has_table:
            rows = [[c.text for c in row.cells] for row in shp.table.rows]
            tables.append({"rows": rows, "row_count": len(rows),
                           "col_count": max((len(r) for r in rows), default=0)})
        child_idx += 1


def parse_pptx_xml_via_pptx(path):
    """第二视角：python-pptx 对象模型。

    诚实说明：它读的还是同一批 XML，因此不是完全独立的实现；它的价值是暴露
    "对象模型与 XML 直读在对象身份/类型/组路径/表格上的差异"。共同盲区要靠
    与原稿的来源核对发现，本视角不承诺覆盖。类型名统一折算到 XML 标签族后再比较。

    D2 修复后：递归进 GroupShape.shapes，组合内部对象与视角 A 一致可见，
    合法组合不再被误判为 PARSER_DIVERGENCE。真实的解析差异（对象集合/类型/
    组路径真不同）仍由 compare_parsers 拦截（见 R09-V2/V3）。
    """
    from pptx import Presentation
    prs = Presentation(str(path))
    slides = []
    for idx, slide in enumerate(prs.slides, start=1):
        objects, tables = [], []
        _pptx_walk(slide.shapes, "", objects, tables)
        slides.append({"index": idx, "objects": objects, "tables": tables})
    return {"slides": slides}
