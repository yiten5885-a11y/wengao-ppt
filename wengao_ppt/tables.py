"""P2：原生规则表格的结构化提取与关系核验。

核心立场（来自规格 §8.2 / §8.3）：
- 表身份不能只看文本：要综合页位置、结构位置、表头语义来判断是不是同一张表；
- 单元格身份不能只靠文本匹配：重复表头、值相同的单元格都会骗过纯文本比对；
- 关系核验必须针对「主体—值」绑定，不能退化成整表文本集合比较
  （主体与数值对调、两个单元格交换后，多重集合完全不变）。

本模块只处理**规则表**：矩形、无合并（或仅表头横向合并）的表。
遇到不规则结构如实记为 irregular 并拒绝给出"已核验"，不猜测。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

from .core import conservative_norm

NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)*")
UNIT_RE = re.compile(r"(万元|亿元|元|万单|单|%|个百分点|人|万人|天|月|次)")

# 对外错误码取自 contracts/error_codes_v1.json；细粒度判定原因放 reason_code。
CONTRACT_CODES = {
    "native_missing": "EDIT.TABLE_RASTERIZED",
    "empty_decoy": "EDIT.DECOY",
    "subject_value_swap": "FIDELITY.SUBJECT_VALUE_MISMATCH",
    "cell_value_mismatch": "FIDELITY.TABLE_CELL_MISMATCH",
    "unit_mismatch": "FIDELITY.NUMBER_UNIT_MISMATCH",
    "number_mismatch": "FIDELITY.NUMBER_UNIT_MISMATCH",
    "structure_changed": "FIDELITY.TABLE_CELL_MISMATCH",
    "irregular": "REVIEW.SEMANTIC_AMBIGUITY",
}


def _finding(key, severity, reason=None, **fields):
    """统一构造 finding：code 用契约码，reason_code 保留细粒度判定原因。"""
    f = {"code": CONTRACT_CODES[key], "reason_code": f"TABLE.{key.upper()}" if not reason else reason,
         "severity": severity, "origin": "table"}
    f.update(fields)
    return f


@dataclass
class TableModel:
    """一张规则表的结构化模型。"""
    table_id: str
    page: int
    shape_id: str
    source_index: int = -1        # 来源侧表序号（DOCX：d.tables 的下标），用于显式来源映射
    headers: list = field(default_factory=list)        # 第 0 行（表头）
    rows: list = field(default_factory=list)           # 数据行，每行是单元格文本列表
    row_labels: list = field(default_factory=list)     # 每行第 0 列（主体）
    geometry: dict = field(default_factory=dict)       # 位置与尺寸（用于结构身份）
    group_path: str = ""
    irregular: bool = False
    irregular_reason: str = ""
    visible: bool = True
    source_ref: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)

    def bindings(self):
        """主体—值绑定集合：[(行主体, 列表头, 数值原文, 单位)]。

        这是关系核验的最小单元；两个单元格对调后多重集合不变，但绑定会变。
        """
        out = []
        for ri, row in enumerate(self.rows):
            label = conservative_norm(row[0]).strip() if row else ""
            for ci in range(1, len(row)):
                head = conservative_norm(self.headers[ci]).strip() if ci < len(self.headers) else f"col{ci}"
                cell = conservative_norm(row[ci]).strip()
                nums = NUM_RE.findall(cell)
                unit = UNIT_RE.search(cell)
                out.append({
                    "row_index": ri + 1, "col_index": ci, "subject": label,
                    "column_header": head, "cell_text": cell,
                    "numbers": nums, "unit": unit.group(1) if unit else None,
                })
        return out


# ---------------------------------------------------------------- 提取

def extract_tables_pptx(prs, path: str):
    """从 python-pptx 的 Presentation 提取原生规则表。"""
    tables = []
    for idx, slide in enumerate(prs.slides, start=1):
        for shp in slide.shapes:
            if not (getattr(shp, "has_table", False) and shp.has_table):
                continue
            tbl = shp.table
            grid = []
            for row in tbl.rows:
                grid.append([c.text for c in row.cells])
            headers = grid[0] if grid else []
            rows = grid[1:] if len(grid) > 1 else []
            irregular = False
            reason = ""
            widths = {len(r) for r in grid}
            if len(widths) > 1:
                irregular, reason = True, f"行列长度不一致：{sorted(widths)}"
            try:
                geom = {"left": shp.left, "top": shp.top, "width": shp.width, "height": shp.height}
                shape_id = f"slide{idx}/shape[{shp.shape_id}]"
            except Exception:  # noqa: BLE001
                geom, shape_id = {}, f"slide{idx}/shape[?]"
            t = TableModel(
                table_id=f"p{idx}_tbl_{shape_id}",
                page=idx, shape_id=shape_id, headers=list(headers), rows=[list(r) for r in rows],
                row_labels=[r[0] if r else "" for r in rows], geometry=geom,
                irregular=irregular, irregular_reason=reason,
                source_ref={"file": path, "slide": idx, "part": "table", "object": shape_id,
                            "row_count": len(grid),
                            "col_count": len(headers)},
            )
            tables.append(t)
    return tables


def extract_tables_docx(d, path: str):
    """从 python-docx 的 Document 提取原生规则表（含表头行）。"""
    tables = []
    for ti, tbl in enumerate(d.tables):
        grid = [[c.text for c in row.cells] for row in tbl.rows]
        headers = grid[0] if grid else []
        rows = grid[1:] if len(grid) > 1 else []
        widths = {len(r) for r in grid}
        irregular = len(widths) > 1
        tables.append(TableModel(
            table_id=f"docx_tbl{ti}", page=0, shape_id=f"table[{ti}]", source_index=ti,
            headers=list(headers), rows=[list(r) for r in rows],
            row_labels=[r[0] if r else "" for r in rows],
            irregular=irregular,
            irregular_reason=f"行列长度不一致：{sorted(widths)}" if irregular else "",
            source_ref={"file": path, "part": "table", "table_index": ti,
                        "row_count": len(grid), "col_count": len(headers)},
        ))
    return tables


# ---------------------------------------------------------------- 关系核验

def _key(t):
    """表格身份键：页 + 表头集合（语义），不依赖文本顺序。"""
    return (t.page, tuple(conservative_norm(h).strip() for h in t.headers))


def compare_tables(src_tables, out_tables, pptx_path=None, mapping=None):
    """比较来源表与输出表，返回 findings 列表。

    检测（关闭自动修复）：
      EDIT.TABLE_RASTERIZED（TABLE.NATIVE_MISSING）      —— 来源有规则表，输出里找不到原生表
      EDIT.DECOY（TABLE.EMPTY_DECOY）                    —— 输出存在原生表对象但单元格全空
      FIDELITY.SUBJECT_VALUE_MISMATCH（…SUBJECT_VALUE_SWAP）—— 主体与数值的绑定被破坏（对调/交换）
      FIDELITY.TABLE_CELL_MISMATCH（…CELL_VALUE_MISMATCH）—— 同一绑定的单元格文本不同
      FIDELITY.NUMBER_UNIT_MISMATCH（…UNIT_MISMATCH）    —— 单位变化（数值不变）
      REVIEW.SEMANTIC_AMBIGUITY（TABLE.IRREGULAR）       —— 结构不规则，拒绝给出已核验

    mapping：显式来源映射（DOCX 表序号 → 输出页/形状）。有映射时优先按映射配对，
    不再依赖"表头文本相同"这种会随内容变化而失效的猜测。
    """
    # 结构位置索引：同页内第 k 张表。表头被改、单元格被清空时表身份不能只看文本，
    # 必须靠结构位置兜底，否则会把"表头被改"误报成"表不存在"。
    def _ordinal_map(tables):
        m, cnt = {}, {}
        for t in tables:
            k = cnt.get(t.page, 0)
            m[(t.page, k)] = t
            cnt[t.page] = k + 1
        return m

    src_pos = _ordinal_map(src_tables)
    out_pos = _ordinal_map(out_tables)

    findings = []
    matched = set()
    map_by_src = {}
    if mapping:
        for m in mapping:
            try:
                map_by_src[int(m.get("source_table_index", -1))] = m
            except (TypeError, ValueError):
                continue
    for si, st in enumerate(src_tables):
        # 身份匹配：⓪ 显式来源映射；① 页 + 表头；② 页 + 同页内第 k 张（结构位置）；③ 仅表头
        cand = []
        m = map_by_src.get(st.source_index)
        if m is not None:
            cand = [t for j, t in enumerate(out_tables)
                    if j not in matched
                    and str(t.page) == str(m.get("output_slide"))
                    and (not m.get("output_shape_id") or t.shape_id == m.get("output_shape_id"))]
            if not cand and m.get("output_slide") is not None:
                # 形状 id 在保存后可能变化，退到"该页第 k 张表"
                k = int(m.get("output_table_ordinal", 0) or 0)
                same_page = [t for t in out_tables if str(t.page) == str(m.get("output_slide"))]
                if k < len(same_page):
                    cand = [same_page[k]]
        if not cand:
            cand = [t for j, t in enumerate(out_tables)
                    if j not in matched and _key(t) == _key(st)]
        if not cand:
            # 该源表在同页中的序号
            same_page = [x for x in src_tables if x.page == st.page]
            k = same_page.index(st)
            pos_t = out_pos.get((st.page, k))
            if pos_t is not None and out_tables.index(pos_t) not in matched:
                cand = [pos_t]
        if not cand:
            cand = [t for j, t in enumerate(out_tables)
                    if j not in matched
                    and tuple(conservative_norm(h).strip() for h in t.headers)
                    == tuple(conservative_norm(h).strip() for h in st.headers)]
        if not cand:
            findings.append(_finding(
                "native_missing", "BLOCKING",
                slide=st.page, table_id=st.table_id,
                expected=f"原生规则表（{len(st.rows)} 行 × {len(st.headers)} 列）",
                actual="输出中未找到对应原生表",
                detail="来源里的规则表在输出中不作为原生表存在（疑似被栅格化为图片）。"
                       "栅格化后表格文字仍可在图片里，但原生编辑性已丢失。",
                source_ref=st.source_ref))
            continue
        ot = cand[0]
        matched.add(out_tables.index(ot))

        if ot.irregular:
            findings.append(_finding(
                "irregular", "REVIEW", slide=ot.page, table_id=ot.table_id,
                detail=f"输出表结构不规则：{ot.irregular_reason}",
                expected="行列长度一致", actual=ot.irregular_reason))
            continue

        non_empty = sum(1 for r in ot.rows for c in r if c.strip())
        if non_empty == 0 and any(c.strip() for r in st.rows for c in r):
            findings.append(_finding(
                "empty_decoy", "BLOCKING", slide=ot.page, table_id=ot.table_id,
                expected=f"有内容的原生表（{len(st.rows)} 行数据）", actual="单元格全空",
                detail="输出里存在原生表对象但没有任何内容，属于空表诱饵："
                       "对象类型正确不等于内容存在。",
                source_ref=st.source_ref))
            continue

        if tuple(conservative_norm(h).strip() for h in ot.headers) != \
                tuple(conservative_norm(h).strip() for h in st.headers):
            findings.append(_finding(
                "structure_changed", "BLOCKING", slide=ot.page, table_id=ot.table_id,
                expected=list(st.headers), actual=list(ot.headers),
                detail="表头不一致，表结构已改变。"))
            continue

        if len(ot.rows) != len(st.rows):
            findings.append(_finding(
                "structure_changed", "BLOCKING", slide=ot.page, table_id=ot.table_id,
                expected=f"{len(st.rows)} 行数据", actual=f"{len(ot.rows)} 行数据",
                detail="数据行数与来源不一致。"))
            continue

        # ---- 关系核验：按 (主体, 列表头) 绑定比较，而不是按整表文本集合 ----
        sb = {(b["subject"], b["column_header"]): b for b in st.bindings()}
        ob = {(b["subject"], b["column_header"]): b for b in ot.bindings()}
        s_subjects = {b["subject"] for b in st.bindings()}
        o_subjects = {b["subject"] for b in ot.bindings()}

        # 主体集合本身变了（行标签消失/新增）→ 绑定被破坏
        if s_subjects != o_subjects:
            findings.append(_finding(
                "subject_value_swap", "BLOCKING", slide=ot.page, table_id=ot.table_id,
                expected=sorted(s_subjects), actual=sorted(o_subjects),
                detail="表格的主体（行标签）集合发生变化，绑定关系被破坏。"))
            continue

        # 关键判据：同一列下「主体→值」的映射是否变化。
        # - 该列的值多重集合不变、但映射变了  → 值在主体之间搬家（对调 / 交换）；
        # - 该列的值多重集合也变了            → 值本身被改（单位 / 符号 / 数值）。
        # 只看整表文本集合会把这两类完全漏掉（多重集合相同）。
        cols = sorted({b["column_header"] for b in st.bindings()} |
                      {b["column_header"] for b in ot.bindings()})
        swap_cols, value_diffs = [], []
        for col in cols:
            smap = {k[0]: v["cell_text"] for k, v in sb.items() if k[1] == col}
            omap = {k[0]: v["cell_text"] for k, v in ob.items() if k[1] == col}
            if smap == omap:
                continue
            smultiset = sorted(str(v) for v in smap.values())
            omultiset = sorted(str(v) for v in omap.values())
            if smultiset == omultiset:
                swap_cols.append({"column": col, "source": smap, "output": omap})
            else:
                for subj in sorted(set(smap) | set(omap)):
                    if smap.get(subj) != omap.get(subj):
                        value_diffs.append({"column": col, "subject": subj,
                                            "source": smap.get(subj), "output": omap.get(subj)})

        for sc in swap_cols:
            findings.append(_finding(
                "subject_value_swap", "BLOCKING", slide=ot.page, table_id=ot.table_id,
                column_header=sc["column"], expected=sc["source"], actual=sc["output"],
                detail=f"列「{sc['column']}」的值在主体之间搬家：整表文本多重集合不变，"
                       "只有「主体—值」绑定被破坏（对调或单元格交换）。"))
        for vd in value_diffs:
            sbind = sb.get((vd["subject"], vd["column"]), {})
            obind = ob.get((vd["subject"], vd["column"]), {})
            # C05 与 C06 的分野（按语义，不按输出反推）：
            #   来源值搬到了**别的主体**下   → 主体—值对调（C05）
            #   来源值搬到了**同一主体的别列** → 行列/单元格关系错配（C06）
            #   来源值在输出里根本不存在      → 值本身被改写（C04/C03）
            src_val = vd["source"]
            moved_to_other_subject = any(
                k[0] != vd["subject"] and v.get("cell_text") == src_val
                for k, v in ob.items())
            moved_to_other_column = (not moved_to_other_subject) and any(
                k[0] == vd["subject"] and k[1] != vd["column"] and v.get("cell_text") == src_val
                for k, v in ob.items())
            if moved_to_other_subject:
                findings.append(_finding(
                    "subject_value_swap", "BLOCKING", slide=ot.page, table_id=ot.table_id,
                    subject=vd["subject"], column_header=vd["column"],
                    expected=src_val, actual=vd["output"],
                    detail=f"主体「{vd['subject']}」在列「{vd['column']}」下的值"
                           f"（{src_val}）出现在别的主体下：主体—值绑定被对调。"))
                continue
            if moved_to_other_column:
                other_cols = sorted({k[1] for k, v in ob.items()
                                     if k[0] == vd["subject"] and k[1] != vd["column"]
                                     and v.get("cell_text") == src_val})
                findings.append(_finding(
                    "cell_value_mismatch", "BLOCKING", slide=ot.page, table_id=ot.table_id,
                    subject=vd["subject"], column_header=vd["column"],
                    expected=src_val, actual=vd["output"],
                    reason="TABLE.COLUMN_RELATION_MISMATCH",
                    detail=f"同一主体「{vd['subject']}」的值从列「{vd['column']}」"
                           f"搬到了列「{'/'.join(other_cols)}」：行列关系错配，"
                           "不是主体对调，也不是数值被改写。"))
                continue
            unit_changed = obind.get("unit") != sbind.get("unit")
            s_first = (sbind.get("numbers") or [""])[0]
            o_first = (obind.get("numbers") or [""])[0]
            sign_changed = s_first.startswith("-") != o_first.startswith("-")
            # 数值不变、只有单位变 → 单位失真（C04-V2/V3）
            numbers_same = (sbind.get("numbers") or []) == (obind.get("numbers") or [])
            detail = "同一主体—表头绑定下的单元格文本不同。"
            if unit_changed:
                detail += f" 单位由 {sbind.get('unit')} 变为 {obind.get('unit')}。"
            if sign_changed:
                detail += " 正负号发生变化。"
            if numbers_same and unit_changed:
                # 单位失真：数值没动，单位被换（万元→亿元、%→个百分点）
                key = "unit_mismatch"
            elif not numbers_same and not sign_changed:
                # 数值失真：数量级/小数点被改而单位与符号没变（C04-V1）。
                # 与"符号丢失"分开：符号丢失归 C03（SIGN_MISMATCH），数值失真归 C04。
                key = "number_mismatch"
                detail += " 数值本身被改写（小数点/数量级变化），单位与符号未变。"
            else:
                key = "cell_value_mismatch"
            findings.append(_finding(
                key, "BLOCKING", slide=ot.page, table_id=ot.table_id,
                subject=vd["subject"], column_header=vd["column"],
                expected=vd["source"], actual=vd["output"],
                unit_expected=sbind.get("unit"), unit_actual=obind.get("unit"),
                sign_changed=sign_changed, detail=detail))
    return findings


def table_edit_probe(pptx_path, out_path):
    """原生可编辑性实证：真实改一个数据单元格，保存重开确认。

    返回 (ok, detail)。照片/表格栅格化后这里会拿不到可编辑单元格，直接失败。
    """
    from pptx import Presentation
    prs = Presentation(str(pptx_path))
    target = None
    for slide in prs.slides:
        for shp in slide.shapes:
            if getattr(shp, "has_table", False) and shp.has_table and len(shp.table.rows) > 1:
                target = shp.table
                break
        if target is not None:
            break
    if target is None:
        return False, {"reason": "输出中找不到可编辑的原生表（可能已被栅格化）"}
    cell = target.cell(1, 1)
    before = cell.text
    cell.text = before + "（已改）"
    prs.save(str(out_path))
    prs2 = Presentation(str(out_path))
    after = None
    for slide in prs2.slides:
        for shp in slide.shapes:
            if getattr(shp, "has_table", False) and shp.has_table:
                after = shp.table.cell(1, 1).text
                break
        if after is not None:
            break
    return (after == before + "（已改）"), {"cell_before": before, "cell_after": after,
                                           "saved_and_reopened": True}
