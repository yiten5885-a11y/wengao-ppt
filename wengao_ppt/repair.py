"""D1：有限确定性修复（首批白名单）。

规格要求"检测与修复分测，最多两轮"。本模块只做**白名单内**的确定性修复：

  WHITELIST.text_unique_source       普通静态文本的唯一来源恢复（首批）
  WHITELIST.table_cell_unique        规则原生表格的唯一单元格恢复（首批）
  WHITELIST.table_relation_restore   绑定搬家的多单元格协同恢复（第二批）
  WHITELIST.text_soft_break_restore  含软换行静态文本的按源事件序列恢复（第二批）
  WHITELIST.text_event_sequence_restore
                                     事件级恢复：检测侧直接给出源事件序列，
                                     候选段落事件序列须与 actual 逐项吻合才写回（第二批）

第二批仍遵守同样的铁律：来源唯一、置换可证、独立候选、保存后独立重读。
**布局修复不在白名单**：几何**检测**是有的——`checks.py:299 check_geometry_bounds()`
（注册为 `object.actual_geometry`，属 `runner.ALWAYS_REQUIRED_CHECKS`，每次验收真实调用），
但它只判定"对象完全位于页面之外"这一种确定事实，**不是**"候选 vs 源"的位置/尺寸偏差检测。
因此目前缺三样：① 布局偏差的检测与 finding；② 可引用的可信真值来源；③ 布局修复器。
按规格"不为了凑自动修复扩展未经验证的排版器"，本模块不实现布局修复，也不为此新增排版器。

（口径更正 2026-10-13：上一版曾据一次返回异常的空 grep 写成"项目没有任何几何检查"，
**该结论错误已撤回**，详见 `reports/closeout-20261012/d1_20261012/D1_SCOPE_RECHECK_20261013.md`。）

铁律（与 AGENTS.md / 任务书一致）：
  1. **修复计划先行**：`plan_repairs` 在任何写盘之前产出计划；计划里没有的动作绝不做。
  2. **最多两轮**：`max_rounds` 默认 2；超过即停并记 REPAIR.FORBIDDEN_CONTENT_CHANGE。
  3. **独立候选**：`apply_repairs` 从不原地改输入候选；先复制到新文件再改。
  4. **保存后独立重读**：`reread_verify` 用全新的 Presentation 打开保存后的文件逐条核对。
  5. **正常稿不误修**：没有 finding 就不产出任何修复动作。
  6. **不确定来源拒修**：来源不唯一/结构不纯/定位不唯一/表格不规则 → 记 refused，不做。
  7. **副作用与过期结果不得提交**：本模块不写 candidate/、不写 validation.json、不提交结果；
     只产出独立候选文件与报告，由 runner.repair 决定是否采纳并重新走验收入口。
  8. **检测与修复分开计分**：`score_split` 分别汇总检测结论与修复结论。

已知边界（如实记录，不声称完美）：
  - 文本恢复保留候选项落的 run 数量与首个 run 的 rPr，把原文写进首个 run、其余 run 置空；
    因此"run 数量"不变但"run 内文本分布"会变。候选段落含 a:fld / a:br / OMML 时**拒修**
    （那属于结构修复，不在首批白名单）。
  - 表格只恢复"单个单元格文本被改坏"（CELL/NUMBER/UNIT 三类）；主体对调、值搬到别列
    这类**关系错配**不在首批白名单（需多单元格协同修复），一律拒修。
  - 表格定位用递归遍历（含组合内表格），与 charts.py 的 D8 修复同口径；
    但 `tables.extract_tables_pptx` 本身只遍历顶层，那是另一处已登记缺口，不在本轮范围。
"""
from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

WHITELIST = ("text_unique_source", "table_cell_unique",
             "table_relation_restore", "text_soft_break_restore",
             "text_event_sequence_restore")
MAX_ROUNDS_DEFAULT = 2

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"

# 首批白名单接受的 finding 码
_TEXT_CODES = {"FIDELITY.REQUIRED_TEXT_MISSING", "FIDELITY.LOCKED_CONTEXT_MISSING"}
# 只列**检查器实际会发出**且在契约字典内的码：
#   tables.py/checks.py 的映射为 cell_value_mismatch→FIDELITY.TABLE_CELL_MISMATCH、
#   unit_mismatch→FIDELITY.NUMBER_UNIT_MISMATCH、number_mismatch→FIDELITY.NUMBER_UNIT_MISMATCH。
# 曾经列入的 FIDELITY.NUMBER_MISMATCH 是**实现私造码**（字典中不存在、检查器也从不发出），
# 契约校验 error_codes.implementation_codes_in_dictionary 已判 FAIL，故移除，不补进字典。
_TABLE_CELL_CODES = {
    "FIDELITY.TABLE_CELL_MISMATCH": "TABLE.CELL_VALUE_MISMATCH",
    "FIDELITY.NUMBER_UNIT_MISMATCH": "TABLE.UNIT_MISMATCH",
}
# 关系错配类：首批**不在**白名单（需多单元格协同，单独修会制造新错误）。
# 第二批以"置换可证"为硬前提纳入：源与候选在该恢复单元上的取值多重集合必须相同，
# 证明这是"值在单元内搬家"而不是"数值被改写"；否则一律拒修。
_TABLE_RELATION_REASONS = {
    "TABLE.SUBJECT_VALUE_SWAP": "主体与数值绑定被破坏（对调/交换）",
    "TABLE.COLUMN_RELATION_MISMATCH": "值被搬到别的列",
}
# 第二批关系修复接受的（code, reason）组合
_RELATION_SWAP = ("FIDELITY.SUBJECT_VALUE_MISMATCH", "TABLE.SUBJECT_VALUE_SWAP")
_RELATION_COLUMN = ("FIDELITY.TABLE_CELL_MISMATCH", "TABLE.COLUMN_RELATION_MISMATCH")


@dataclass
class RepairAction:
    kind: str
    round_no: int
    finding_code: str
    finding_reason: str
    slide: int
    locator: dict
    expected: str
    source_identity: dict
    note: str = ""
    finding_index: int = -1      # 溯源：对应 findings 列表中的第几条（铁律 6）
    cells: list = field(default_factory=list)   # 多单元格协同：(row_index, col_index, 期望值)
    unit: dict = field(default_factory=dict)    # 恢复单元（整列 / 整行）描述
    source_events: list = field(default_factory=list)  # 含软换行文本的源事件序列
    candidate_actual_events: list = field(default_factory=list)  # 计划期读到的候选事件序列


@dataclass
class RepairPlan:
    actions: list = field(default_factory=list)
    refused: list = field(default_factory=list)
    rounds_allowed: int = MAX_ROUNDS_DEFAULT
    normal_input: bool = False

    def to_dict(self):
        return {"actions": [a.__dict__ for a in self.actions],
                "refused": self.refused,
                "rounds_allowed": self.rounds_allowed,
                "normal_input": self.normal_input,
                "whitelist": list(WHITELIST)}


# ---------------------------------------------------------------- 定位辅助
def _census_shape_map(src_cc):
    """(slide, shape_id) → census object_path；供 source_ref 与 census 台账桥接。

    census 的 para_events 键用 "sp[2]" / "grpSp[900]/sp[5]" 这类 XML 路径，
    而 finding 的 source_ref.object 用 "slide1/shape[2]"（形状 id）。两者按
    (页, 形状 id) 对齐——形状 id 是两者共有的稳定定位符。
    """
    out = {}
    for s in src_cc.get("slides", []) or []:
        si = s.get("index")
        for o in s.get("objects", []) or []:
            oid = o.get("id")
            if oid is None:
                continue
            kind = o.get("kind", "sp")
            gpath = o.get("group_path") or ""
            path = f"{gpath}/{kind}[{oid}]" if gpath else f"{kind}[{oid}]"
            out[(si, str(oid))] = path
    return out


def _locate_shape_by_id(prs, slide_no, shape_id):
    """按 (页, 形状 id) 定位；返回 [shape, ...]（含组合内）。"""
    from wengao_ppt.extractors import _iter_shapes
    if slide_no < 1 or slide_no > len(prs.slides):
        return []
    out = []
    for shp, _g in _iter_shapes(prs.slides[slide_no - 1].shapes):
        try:
            if str(getattr(shp, "shape_id", "")) == str(shape_id):
                out.append(shp)
        except Exception:  # noqa: BLE001
            continue
    return out


def _locate_shape_by_name(prs, slide_no, ref):
    """按 (页, 组路径, 形状名) 回退定位；返回 [shape, ...]。"""
    from wengao_ppt.extractors import _iter_shapes
    if slide_no < 1 or slide_no > len(prs.slides):
        return []
    obj = (ref or {}).get("object") or ""
    want_name = obj.split("/")[-1] if "/" in obj else obj
    gpath_want = (ref or {}).get("group_path", "")
    out = []
    for shp, g in _iter_shapes(prs.slides[slide_no - 1].shapes):
        try:
            nm = str(getattr(shp, "name", "") or "")
        except Exception:  # noqa: BLE001
            continue
        if nm == want_name and (not gpath_want or g == gpath_want):
            out.append(shp)
    return out


def _shape_id_from_ref(ref):
    obj = (ref or {}).get("object") or ""
    i = obj.rfind("shape[")
    if i < 0 or not obj.endswith("]"):
        return None
    return obj[i + 6:-1]


def _iter_text_shapes(shapes, prefix=""):
    """递归产出 (shape, 组路径)，与 extractors._iter_shapes 同口径。"""
    from wengao_ppt.extractors import _iter_shapes
    yield from _iter_shapes(shapes)


def _iter_tables(shapes, page_no, prefix=""):
    """递归产出 (table_shape, grid, 组路径, shape_id)。"""
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    for shp in shapes:
        try:
            sid = str(getattr(shp, "shape_id", "?"))
            name = str(getattr(shp, "name", "") or "")
        except Exception:  # noqa: BLE001
            continue
        if shp.shape_type == MSO_SHAPE_TYPE.GROUP:
            inner = f"{prefix}/{name or sid}" if prefix else (name or sid)
            yield from _iter_tables(shp.shapes, page_no, inner)
            continue
        if getattr(shp, "has_table", False) and shp.has_table:
            grid = [[c.text for c in row.cells] for row in shp.table.rows]
            yield shp, grid, prefix, f"slide{page_no}/shape[{sid}]"


def _shape_path_of(shp, gpath):
    try:
        name = str(getattr(shp, "name", "") or "")
        sid = str(getattr(shp, "shape_id", "?"))
    except Exception:  # noqa: BLE001
        return None
    leaf = name or sid
    return f"{gpath}/{leaf}" if gpath else leaf


def _para_static_only(events):
    return bool(events) and all(e.get("kind") == "text" for e in events)


def _cand_para_pure_runs(para_el):
    for ch in para_el:
        ln = ch.tag.split("}", 1)[-1] if "}" in ch.tag else ch.tag
        ns = ch.tag.split("}", 1)[0][1:] if "}" in ch.tag else ""
        if ln in ("fld", "br"):
            return False
        if ns == M:
            return False
    return True


def _build_table_index(prs):
    """统一的原生表索引（源与候选同口径；递归含组合内）。"""
    out = []
    for si, slide in enumerate(prs.slides, start=1):
        for shp, grid, gpath, sid in _iter_tables(slide.shapes, si):
            headers = grid[0] if grid else []
            rows = grid[1:] if len(grid) > 1 else []
            widths = {len(r) for r in grid}
            out.append({
                "shape": shp, "page": si, "grid": grid, "headers": headers,
                "rows": rows, "row_labels": [r[0] if r else "" for r in rows],
                "shape_id": sid, "group_path": gpath,
                "irregular": len(widths) > 1,
                "irregular_reason": f"行列长度不一致：{sorted(widths)}" if len(widths) > 1 else "",
            })
    return out


def _para_events_brief(p_el):
    """按 census 同一口径从段落 XML 直接取事件摘要（不依赖 census 全局台账）。"""
    out = []
    for ch in p_el:
        ln = ch.tag.split("}", 1)[-1] if "}" in ch.tag else ch.tag
        ns = ch.tag.split("}", 1)[0][1:] if "}" in ch.tag else ""
        if ln == "br":
            out.append({"kind": "br"})
        elif ln == "r" and ns != M:
            t = ch.find(f"{{{A}}}t")
            out.append({"kind": "text", "text": (t.text or "") if t is not None else ""})
        else:
            out.append({"kind": "other"})
    return out


def _brief_key(evs):
    return [(e.get("kind"), e.get("text", "")) for e in evs
            if isinstance(e, dict)]


def _para_event_pattern(p_el):
    """段落子元素模式：r→"t"（文本），br→"b"，其他→"x"。"""
    pat = []
    for ch in p_el:
        ln = ch.tag.split("}", 1)[-1] if "}" in ch.tag else ch.tag
        ns = ch.tag.split("}", 1)[0][1:] if "}" in ch.tag else ""
        if ln == "br":
            pat.append("b")
        elif ln == "r" and ns != M:
            pat.append("t")
        else:
            pat.append("x")
    return pat


def _events_pattern(events):
    pat = []
    for e in events:
        k = e.get("kind")
        pat.append("b" if k == "br" else ("t" if k == "text" else "x"))
    return pat


def _norm(s):
    from wengao_ppt.tables import conservative_norm
    return conservative_norm(s or "").strip()


# ---------------------------------------------------------------- 计划
def plan_repairs(findings, src_pptx, cand_pptx, src_cc=None, round_no=1,
                 max_rounds=MAX_ROUNDS_DEFAULT):
    """修复计划先行：只做白名单内、来源唯一的修复；其余记 refused。"""
    plan = RepairPlan(rounds_allowed=max_rounds)
    if not findings:
        plan.normal_input = True
        return plan

    from wengao_ppt.census import census_xml_content
    cc = src_cc if src_cc is not None else census_xml_content(src_pptx)
    src_events = cc.get("para_events", {}) or {}
    shape_map = _census_shape_map(cc)
    # 来源唯一性索引：按 (slide, 形状id) → [(census object_path, para_idx, 文本, 纯静态?)]
    src_by_shape = {}
    for key, evs in src_events.items():
        slide, obj, para = key.split("|", 2)
        leaf = obj.rsplit("/", 1)[-1]              # "sp[2]" / "grpSp[900]/sp[5]" 的末段
        i = leaf.rfind("[")
        oid = leaf[i + 1:-1] if i >= 0 and leaf.endswith("]") else None
        txt = "".join(e.get("text", "") for e in evs if e.get("kind") == "text")
        src_by_shape.setdefault((int(slide), oid), []).append({
            "object_path": obj, "para_idx": int(para), "text": txt,
            "static_only": _para_static_only(evs), "n_events": len(evs),
            "events": evs})

    from pptx import Presentation
    cand_prs = Presentation(str(cand_pptx))
    # 源侧原生表真值（第二批关系修复的依据；与候选同口径）
    src_prs = Presentation(str(src_pptx))
    src_tables = _build_table_index(src_prs)
    cand_tables = _build_table_index(cand_prs)

    for _fi, f in enumerate(findings):
        code = f.get("code", "")
        reason = f.get("reason_code", "")
        ref = f.get("source_ref") or {}
        _tag = {"finding_index": _fi}
        expected = f.get("expected")
        if not isinstance(expected, str):
            expected = "" if expected is None else str(expected)

        # ---- 第二批：事件级恢复（检测侧给出源事件序列）----
        # 只认检查器实际发出且在契约字典内的码：FIDELITY.REQUIRED_TEXT_MISSING
        # + 细因 TEXT.STATIC_CHANGED（checks._FIELD_REASON["text"]）。
        if code == "FIDELITY.REQUIRED_TEXT_MISSING" \
                and reason == "TEXT.STATIC_CHANGED":
            act, why = _plan_event_sequence(f, cand_prs, round_no, _fi, src_cc=cc)
            if act is None:
                plan.refused.append({"code": code, "reason": reason,
                                     "finding_index": _fi, "why": why})
            else:
                plan.actions.append(act)
            continue

        # ---- 白名单 1：普通静态文本唯一来源恢复 ----
        if code in _TEXT_CODES:
            slide = ref.get("slide")
            if slide is None:
                slide = f.get("slide")
            if slide is None:
                plan.refused.append({"code": code, "reason": reason, "finding_index": _fi,
                                     "why": "finding 无页码定位，拒修"})
                continue
            shape_id = _shape_id_from_ref(ref)
            if shape_id is None:
                plan.refused.append({"code": code, "reason": reason, "finding_index": _fi,
                                     "why": "source_ref.object 不是 slide<n>/shape[<id>] 形式，拒修"})
                continue
            # 软换行显示符 \x0b：census 把 <a:br> 记成**事件**、拼接文本不含它，
            # 而 python-pptx / 内容保真检查把 <a:br> 记成显示文本 "\x0b"。
            # 因此当源段落**含 br** 时，用去掉 \x0b 的显示文本与来源拼接文本对齐；
            # 结构仍由"候选 r/br 模式 == 源事件模式"单独把关，不靠这一步放宽。
            _norm_exp = expected.replace("\x0b", "")
            exact = [c for c in src_by_shape.get((int(slide), shape_id), [])
                     if c["text"] == expected]
            cands = exact or [c for c in src_by_shape.get((int(slide), shape_id), [])
                              if c["text"] == _norm_exp
                              and any(e.get("kind") == "br" for e in c["events"])]
            if len(cands) == 0:
                plan.refused.append({"code": code, "reason": reason, "finding_index": _fi,
                                     "why": ("原始输入该形状下没有段落等于 expected"
                                             "（expected 可能是整形状拼接文本，无法归因到唯一段落），"
                                             "来源不确定，拒修")})
                continue
            if len(cands) > 1:
                plan.refused.append({"code": code, "reason": reason, "finding_index": _fi,
                                     "why": (f"原始输入该形状下有 {len(cands)} 个段落等于 "
                                             f"expected，来源不唯一，拒修")})
                continue
            src = cands[0]
            evs = src["events"]
            kinds = {e.get("kind") for e in evs}
            soft_break = kinds <= {"text", "br"} and "br" in kinds
            if not src["static_only"] and not soft_break:
                plan.refused.append({"code": code, "reason": reason, "finding_index": _fi,
                                     "why": "来源段落含字段/公式/OMML 等非静态结构，拒修"})
                continue
            if not src["text"].strip():
                plan.refused.append({"code": code, "reason": reason, "finding_index": _fi,
                                     "why": "来源文本为空，无可恢复内容，拒修"})
                continue
            # 候选中按 (页, 形状 id) 定位；id 变了则按名称 + 组路径回退，仍须唯一
            matches = _locate_shape_by_id(cand_prs, int(slide), shape_id)
            if not matches:
                nm = (ref.get("object") or "")
                matches = _locate_shape_by_name(cand_prs, int(slide), ref)
            if len(matches) != 1:
                plan.refused.append({"code": code, "reason": reason, "finding_index": _fi,
                                     "why": f"候选中形状定位不唯一（命中 {len(matches)} 个），拒修"})
                continue
            shp = matches[0]
            if not getattr(shp, "has_text_frame", False):
                plan.refused.append({"code": code, "reason": reason, "finding_index": _fi,
                                     "why": "候选目标不是文本形状，拒修"})
                continue
            paras = shp.text_frame.paragraphs
            if src["para_idx"] >= len(paras):
                plan.refused.append({"code": code, "reason": reason, "finding_index": _fi,
                                     "why": "候选段落数少于来源段落序，拒修"})
                continue
            p_el = paras[src["para_idx"]]._p
            if soft_break:
                # ---- 第二批：含软换行的静态文本，按源事件序列重建 ----
                pat = _para_event_pattern(p_el)
                if "x" in pat:
                    plan.refused.append({"code": code, "reason": reason, "finding_index": _fi,
                                         "why": "候选段落含字段/公式/OMML，超出软换行恢复范围，拒修"})
                    continue
                if pat != _events_pattern(evs):
                    plan.refused.append({"code": code, "reason": reason, "finding_index": _fi,
                                         "why": (f"候选段落结构模式 {''.join(pat)} 与源事件模式 "
                                                 f"{''.join(_events_pattern(evs))} 不一致，"
                                                 "无法按源序列无损恢复，拒修")})
                    continue
                texts = [e.get("text", "") for e in evs if e.get("kind") == "text"]
                plan.actions.append(RepairAction(
                    kind="text_soft_break_restore", round_no=round_no,
                    finding_code=code, finding_reason=reason, slide=int(slide),
                    locator={"shape_id": shape_id, "object": ref.get("object"),
                             "group_path": ref.get("group_path", ""),
                             "paragraph_index": src["para_idx"],
                             "census_object_path": src["object_path"],
                             "source_para_key": (
                                 f"{int(slide)}|{src['object_path']}|{src['para_idx']}")},
                    expected="".join(texts),
                    source_identity={"source_event_count": len(evs),
                                     "source_soft_break_count": sum(
                                         1 for e in evs if e.get("kind") == "br"),
                                     "source_event_pattern": "".join(_events_pattern(evs)),
                                     "source_text_sha256": hashlib.sha256(
                                         src["text"].encode("utf-8")).hexdigest(),
                                     "source_events_sha256": hashlib.sha256(
                                         json.dumps(_brief_key(evs), ensure_ascii=False,
                                                    sort_keys=True).encode("utf-8")).hexdigest(),
                                     "source_para_key": (
                                         f"{int(slide)}|{src['object_path']}|{src['para_idx']}"),
                                     "source_basis": "src_cc.para_events（原始台账）",
                                     "unique_match": True},
                    note=("含软换行的静态文本：真值取自原始台账 src_cc 的事件序列，"
                          "按源序列逐段写回，软换行位置不变"),
                    finding_index=_fi,
                    source_events=[{"kind": e.get("kind"), "text": e.get("text", "")}
                                   for e in evs],
                    candidate_actual_events=[{"kind": k, "text": t}
                                             for k, t in _brief_key(
                                                 _para_events_brief(p_el))]))
                continue
            if not _cand_para_pure_runs(p_el):
                plan.refused.append({"code": code, "reason": reason, "finding_index": _fi,
                                     "why": "候选段落含字段/软换行/公式，恢复纯文本会破坏结构，拒修"})
                continue
            plan.actions.append(RepairAction(
                kind="text_unique_source", round_no=round_no,
                finding_code=code, finding_reason=reason, slide=int(slide),
                locator={"shape_id": shape_id, "object": ref.get("object"),
                         "group_path": ref.get("group_path", ""),
                         "paragraph_index": src["para_idx"],
                         "census_object_path": src["object_path"]},
                expected=src["text"],
                source_identity={"source_para_event_count": src["n_events"],
                                 "source_text_sha256": hashlib.sha256(
                                     src["text"].encode("utf-8")).hexdigest(),
                                 "unique_match": True},
                note="普通静态文本：原文取自原始输入唯一来源，按 (页,形状id,段落序) 定位恢复",
                finding_index=_fi))
            continue

        # ---- 第二批：绑定搬家的多单元格协同恢复 ----
        is_swap = (code, reason) == _RELATION_SWAP
        is_colrel = (code, reason) == _RELATION_COLUMN
        if is_swap or is_colrel:
            slide = f.get("slide")
            column = f.get("column_header")
            subject = f.get("subject")
            if slide is None or not column:
                plan.refused.append({"code": code, "reason": reason, "finding_index": _fi,
                                     "why": "关系类 finding 缺少 页/列头 定位，拒修"})
                continue
            act, why = _plan_table_relation(f, src_tables, cand_tables, int(slide),
                                            "column" if is_swap else "row",
                                            round_no, _fi)
            if act is None:
                plan.refused.append({"code": code, "reason": reason, "finding_index": _fi, "why": why})
            else:
                plan.actions.append(act)
            continue

        # ---- 白名单 2：规则原生表格唯一单元格恢复 ----
        if code in _TABLE_CELL_CODES:
            slide = f.get("slide")
            subject = f.get("subject")
            column = f.get("column_header")
            if slide is None or not subject or not column:
                plan.refused.append({"code": code, "reason": reason, "finding_index": _fi,
                                     "why": "表格 finding 缺少 页/主体/列头 定位，拒修"})
                continue
            act, why = _plan_table_cell(f, cand_tables, int(slide),
                                        str(subject), str(column), round_no, _fi)
            if act is None:
                plan.refused.append({"code": code, "reason": reason, "finding_index": _fi, "why": why})
            else:
                plan.actions.append(act)
            continue

        # ---- 关系错配与其它：明确拒修 ----
        why = _TABLE_RELATION_REASONS.get(reason)
        if why:
            plan.refused.append({"code": code, "reason": reason, "finding_index": _fi,
                                 "why": f"{why}：属关系错配，需多单元格协同修复，不在首批白名单，拒修"})
        else:
            plan.refused.append({"code": code, "reason": reason, "finding_index": _fi,
                                 "why": "不在首批白名单（仅普通静态文本与规则表格单单元格），拒修"})
    return plan


def _locate_text(cand_prs, slide_no, obj_path, para_idx):
    from wengao_ppt.extractors import _iter_shapes
    if slide_no < 1 or slide_no > len(cand_prs.slides):
        return []
    out = []
    for shp, gpath in _iter_shapes(cand_prs.slides[slide_no - 1].shapes):
        path = _shape_path_of(shp, gpath)
        if path is None:
            continue
        if path != obj_path and not path.endswith("/" + obj_path) \
                and not obj_path.endswith("/" + path):
            continue
        if not getattr(shp, "has_text_frame", False):
            continue
        paras = shp.text_frame.paragraphs
        if para_idx < len(paras):
            out.append((shp, paras[para_idx]._p))
    return out


def _plan_event_sequence(f, cand_prs, round_no, finding_index=-1, src_cc=None):
    """事件级恢复：**可信来源核对**。

    三条证据缺一不可：
      1. **可信来源**：从已绑定冻结原始输入的 `src_cc`（原始台账）按 (页, object_path,
         段落序) 唯一取得原始事件序列；**不使用** finding.expected 或候选重建、补填台账；
      2. **expected 交叉核对**：`finding.expected` 必须与第 1 步的原始事件序列一致，
         不一致即拒修（**不静默修正 expected 后继续**）；
      3. **候选定位**：候选段落事件序列必须与 `finding.actual` 逐项吻合。

    只写回 `a:r` 文本，不动 `a:br` 的位置；含字段/OMML 或事件模式不一致一律拒修。
    """
    exp, act = f.get("expected"), f.get("actual")
    if not (isinstance(exp, list) and isinstance(act, list)) or not exp or not act:
        return None, "事件级 finding 的 expected/actual 不是事件序列，拒修"
    if not all(isinstance(e, dict) for e in exp + act):
        return None, "事件序列含非对象项，拒修"
    exp_kinds = [e.get("kind") for e in exp]
    act_kinds = [e.get("kind") for e in act]
    if set(exp_kinds) - {"text", "br"} or set(act_kinds) - {"text", "br"}:
        return None, "事件序列含字段/公式/OMML，超出第二批范围，拒修"
    if "br" not in exp_kinds:
        return None, "不涉及软换行（属首批普通静态文本路径），不在本动作范围，拒修"
    if exp_kinds != act_kinds:
        return None, (f"源/候选事件模式不一致（{exp_kinds} vs {act_kinds}）："
                      "结构已变，无法按序列无损恢复，拒修")

    slide = f.get("slide")
    obj_path = f.get("object_path")
    para = f.get("para_idx")
    if slide is None or not obj_path or para is None:
        return None, "事件级 finding 缺少 页/对象路径/段落序，拒修"

    # ---- 1) 可信来源：从冻结原始台账取原始事件序列（不用 expected / 不用候选重建）----
    if src_cc is None:
        return None, ("缺少已绑定冻结原始输入的原始台账（src_cc）："
                      "无法做来源核对，按拒修处理")
    try:
        key = f"{int(slide)}|{obj_path}|{int(para)}"
    except (TypeError, ValueError):
        return None, "事件级 finding 的 页/段落序 不可解析，拒修"
    src_map = src_cc.get("para_events", {}) or {}
    if key not in src_map:
        return None, (f"原始台账中不存在该段落事件（key={key}）："
                      "来源定位失败，无法用可信来源核对，拒修")
    # 对象映射唯一：同一 (页, object_path) 在原始台账里只能对应一个对象
    obj_paths = list(_census_shape_map(src_cc).values())
    if obj_paths.count(obj_path) != 1:
        return None, (f"原始台账中对象路径「{obj_path}」命中 {obj_paths.count(obj_path)} 个对象："
                      "对象映射不唯一，拒修")
    src_events = src_map[key]
    src_key = _brief_key(src_events)
    # ---- 2) expected 与原始事件交叉核对（不静默修正）----
    if _brief_key(exp) != src_key:
        return None, ("finding.expected 与冻结原始台账的事件序列不一致："
                      "不以 expected 为真值，不修正后继续，拒修")
    leaf = str(obj_path).rsplit("/", 1)[-1]
    i = leaf.rfind("[")
    oid = leaf[i + 1:-1] if (i >= 0 and leaf.endswith("]")) else None
    if oid is None:
        return None, f"无法从对象路径 {obj_path} 解析形状 id，拒修"
    matches = _locate_shape_by_id(cand_prs, int(slide), oid)
    if len(matches) != 1:
        return None, f"候选中形状定位不唯一（命中 {len(matches)} 个），拒修"
    shp = matches[0]
    if not getattr(shp, "has_text_frame", False):
        return None, "候选目标不是文本形状，拒修"
    try:
        pi = int(para)
    except (TypeError, ValueError):
        return None, "段落序不可解析，拒修"
    paras = shp.text_frame.paragraphs
    if pi >= len(paras):
        return None, "候选段落数不足，拒修"
    p_el = paras[pi]._p
    got = _brief_key(_para_events_brief(p_el))
    if got != _brief_key(act):
        return None, ("候选段落事件序列与 finding.actual 不一致：定位不成立，"
                      "不能确认这就是被检出那一段，拒修")
    texts = [e.get("text", "") for e in exp if e.get("kind") == "text"]
    return RepairAction(
        kind="text_event_sequence_restore", round_no=round_no,
        finding_code=f.get("code", ""), finding_reason=f.get("reason_code", ""),
        slide=int(slide),
        locator={"shape_id": oid, "object_path": obj_path,
                 "paragraph_index": pi, "locator_basis": "finding.object_path+para_idx",
                 "source_para_key": key},
        expected="".join(texts),
        source_identity={"source_event_kinds": exp_kinds,
                         "source_event_count": len(exp),
                         "soft_break_count": exp_kinds.count("br"),
                         "expected_sha256": hashlib.sha256(
                             "".join(texts).encode("utf-8")).hexdigest(),
                         "source_para_key": key,
                         "source_events_sha256": hashlib.sha256(
                             json.dumps(src_key, ensure_ascii=False,
                                        sort_keys=True).encode("utf-8")).hexdigest(),
                         "source_cross_checked": True,
                         "candidate_actual_matched": True},
        note=("事件级恢复：expected 已与冻结原始台账交叉核对一致；候选段落事件序列与 actual "
              "逐项吻合；只写回文本，软换行位置不变"),
        finding_index=finding_index,
        source_events=[{"kind": e.get("kind"), "text": e.get("text", "")} for e in exp],
        candidate_actual_events=[{"kind": e.get("kind"), "text": e.get("text", "")}
                                 for e in act]), None


def _plan_table_relation(f, src_tables, cand_tables, slide_no, unit, round_no,
                         finding_index=-1):
    """绑定搬家的**多单元格协同**恢复；返回 (action, None) 或 (None, 拒修原因)。

    硬前提（置换可证）：恢复单元（整列 for 主体—值对调 / 整行 for 列关系错配）内，
    候选取值的多重集合必须**等于**源取值的多重集合。这证明损坏是"值在单元内搬家"，
    而不是"数值被改写"；多重集合不同一律拒修（那属于 C03/C04 数值失真，不在白名单）。

    行/列映射按 (主体行标签, 列头) 建立，要求两侧标签与列头各自唯一，且表结构规则。
    """
    column = str(f.get("column_header") or "")
    subject = f.get("subject")
    col_norm = _norm(column)

    cand_same = [t for t in cand_tables if t["page"] == slide_no]
    if not cand_same:
        return None, f"第 {slide_no} 页候选中没有原生表格，拒修"
    cand_by_col = [t for t in cand_same
                   if any(_norm(h) == col_norm for h in t["headers"])]
    if len(cand_by_col) != 1:
        return None, (f"候选中含列头「{column}」的表格不唯一"
                      f"（{len(cand_by_col)} 张），拒修")
    ct = cand_by_col[0]
    if ct["irregular"]:
        return None, f"候选表格结构不规则（{ct['irregular_reason']}），拒修"
    cis = [i for i, h in enumerate(ct["headers"]) if _norm(h) == col_norm]
    if len(cis) != 1:
        return None, f"列头「{column}」在候选表中不唯一（{len(cis)} 列），拒修"
    ci = cis[0]

    src_same = [t for t in src_tables if t["page"] == slide_no]
    if not src_same:
        return None, f"第 {slide_no} 页原始输入中没有原生表格，缺少真值来源，拒修"
    cand_labels = [_norm(x) for x in ct["row_labels"]]
    if len(set(cand_labels)) != len(cand_labels):
        return None, "候选表主体行标签重复，无法建立唯一的行映射，拒修"
    cand_headers_sig = sorted(_norm(h) for h in ct["headers"])

    if unit == "column":
        src_cand = []
        for st in src_same:
            if st["irregular"]:
                continue
            if sorted(_norm(h) for h in st["headers"]) != cand_headers_sig:
                continue
            st_labels = [_norm(x) for x in st["row_labels"]]
            if sorted(st_labels) != sorted(cand_labels):
                continue
            if len(set(st_labels)) != len(st_labels):
                continue
            src_cand.append(st)
        if len(src_cand) != 1:
            return None, (f"原始输入中表头与主体集合同时匹配的表格不唯一"
                          f"（{len(src_cand)} 张），来源不确定，拒修")
        st = src_cand[0]
        st_headers_n = [_norm(h) for h in st["headers"]]
        if st_headers_n.count(col_norm) != 1:
            return None, f"列头「{column}」在源表中不唯一，拒修"
        sci = st_headers_n.index(col_norm)
        src_label_pos = {_norm(x): i for i, x in enumerate(st["row_labels"])}
        cells = []
        for ri, lab in enumerate(ct["row_labels"]):
            j = src_label_pos.get(_norm(lab))
            if j is None:
                return None, f"候选主体「{lab}」在源表中找不到对应行，拒修"
            if ri >= len(ct["rows"]) or j >= len(st["rows"]) \
                    or sci >= len(st["rows"][j]):
                return None, "表格行列越界，拒修"
            cells.append((ri, ci, st["rows"][j][sci]))
        cand_vals = sorted(ct["rows"][ri][ci] for ri in range(len(ct["rows"])))
        src_vals = sorted(c[2] for c in cells)
        unit_desc = {"unit": "column", "column_header": column,
                     "column_index": ci, "cell_count": len(cells)}
        unit_cn = "整列"
    else:
        if not subject:
            return None, "列关系错配 finding 缺少主体定位，拒修"
        subj_norm = _norm(str(subject))
        hits = [i for i, lab in enumerate(ct["row_labels"]) if _norm(lab) == subj_norm]
        if len(hits) != 1:
            return None, (f"主体行「{subject}」在候选表中定位不唯一"
                          f"（{len(hits)} 行），拒修")
        ri = hits[0]
        src_cand = []
        for st in src_same:
            if st["irregular"]:
                continue
            if sorted(_norm(h) for h in st["headers"]) != cand_headers_sig:
                continue
            st_labels = [_norm(x) for x in st["row_labels"]]
            if len([1 for x in st_labels if x == subj_norm]) != 1:
                continue
            if len(set(st_labels)) != len(st_labels):
                continue
            src_cand.append(st)
        if len(src_cand) != 1:
            return None, (f"原始输入中表头一致且主体「{subject}」唯一匹配的表格不唯一"
                          f"（{len(src_cand)} 张），来源不确定，拒修")
        st = src_cand[0]
        st_labels_n = [_norm(x) for x in st["row_labels"]]
        sr = st_labels_n.index(subj_norm)
        st_headers_n = [_norm(h) for h in st["headers"]]
        cand_headers_n = [_norm(h) for h in ct["headers"]]
        if len(set(st_headers_n)) != len(st_headers_n):
            return None, "源表列头重复，无法建立唯一的列映射，拒修"
        if len(set(cand_headers_n)) != len(cand_headers_n):
            return None, "候选表列头重复，无法建立唯一的列映射，拒修"
        if not ct["rows"][ri] or not st["rows"][sr]:
            return None, "表格行越界，拒修"
        if _norm(ct["rows"][ri][0]) != _norm(st["rows"][sr][0]):
            return None, "候选主体标签与源不一致（不是列关系错配），拒修"
        cells = []
        for cj in range(1, len(cand_headers_n)):
            sj = st_headers_n.index(cand_headers_n[cj])
            if cj >= len(ct["rows"][ri]) or sj >= len(st["rows"][sr]):
                return None, "表格行列越界，拒修"
            cells.append((ri, cj, st["rows"][sr][sj]))
        cand_vals = sorted(ct["rows"][ri][cj]
                           for cj in range(1, min(len(cand_headers_n),
                                                  len(ct["rows"][ri]))))
        src_vals = sorted(c[2] for c in cells)
        unit_desc = {"unit": "row", "subject": str(subject),
                     "row_index": ri, "cell_count": len(cells)}
        unit_cn = "整行"

    if not cells:
        return None, "恢复单元内没有可写单元格，拒修"
    if cand_vals != src_vals:
        return None, ("恢复单元内候选与源的取值多重集合不同：属于数值被改写（C03/C04），"
                      "不是绑定搬家，不在白名单，拒修")

    return RepairAction(
        kind="table_relation_restore", round_no=round_no,
        finding_code=f.get("code", ""), finding_reason=f.get("reason_code", ""),
        slide=slide_no,
        locator={"table_shape_id": ct["shape_id"], "group_path": ct["group_path"],
                 "table_ordinal_on_page": cand_same.index(ct)},
        expected=f"多单元格协同恢复（{unit_cn}，{len(cells)} 格）",
        source_identity={"unit": unit,
                         "permutation_proof": {"candidate_multiset": cand_vals,
                                               "source_multiset": src_vals,
                                               "equal": True}},
        note=("绑定搬家：源与候选在恢复单元内取值多重集合相同（置换可证），"
              "按 (主体行, 列头) 映射逐格写回源值；不改动表头与主体标签"),
        finding_index=finding_index,
        cells=[{"row_index": r, "col_index": c, "expected": v} for r, c, v in cells],
        unit=unit_desc), None


def _plan_table_cell(f, cand_tables, slide_no, subject, column, round_no,
                     finding_index=-1):
    """规则原生表格唯一单元格恢复计划；返回 (action, None) 或 (None, 拒修原因)。"""
    expected = f.get("expected")
    if not isinstance(expected, str):
        expected = "" if expected is None else str(expected)
    # 同页候选表
    same_page = [t for t in cand_tables if t["page"] == slide_no]
    if not same_page:
        return None, f"第 {slide_no} 页候选中没有原生表格，拒修"
    # 列头匹配（表头集合）
    col_norm = _norm(column)
    by_header = [t for t in same_page
                 if any(_norm(h) == col_norm for h in t["headers"])]
    if not by_header:
        return None, f"候选中找不到含列头「{column}」的表格，拒修"
    # 主体行匹配
    subj_norm = _norm(subject)
    with_row = []
    for t in by_header:
        hits = [i for i, lab in enumerate(t["row_labels"]) if _norm(lab) == subj_norm]
        if len(hits) == 1:
            with_row.append((t, hits[0]))
    if len(with_row) != 1:
        return None, (f"主体行「{subject}」在候选中定位不唯一"
                      f"（{len(with_row)} 张表各命中唯一行），拒修")
    t, ri = with_row[0]
    if t["irregular"]:
        return None, f"表格结构不规则（{t['irregular_reason']}），拒修"
    cis = [ci for ci, h in enumerate(t["headers"]) if _norm(h) == col_norm]
    if len(cis) != 1:
        return None, f"列头「{column}」在候选中定位不唯一（{len(cis)} 列），拒修"
    ci = cis[0]
    if ri >= len(t["rows"]) or ci >= len(t["rows"][ri]):
        return None, "候选表格行列越界，拒修"
    return RepairAction(
        kind="table_cell_unique", round_no=round_no,
        finding_code=f.get("code", ""), finding_reason=f.get("reason_code", ""),
        slide=slide_no,
        locator={"table_shape_id": t["shape_id"], "group_path": t["group_path"],
                 "row_index": ri, "col_index": ci,
                 "subject": subject, "column_header": column,
                 "table_ordinal_on_page": same_page.index(t)},
        expected=expected,
        source_identity={"row_label": subject, "column_header": column,
                         "candidate_cell_before": t["rows"][ri][ci],
                         "expected_sha256": hashlib.sha256(
                             expected.encode("utf-8")).hexdigest()},
        note="规则原生表格单单元格：原文取自原始输入，按 页+主体行+列头 唯一定位恢复",
        finding_index=finding_index), None


# ---------------------------------------------------------------- 应用前复查（§5.2）
def verify_before_apply(plan, cand_pptx, src_cc=None, cand_sha_at_plan=None):
    """**应用动作前**的复查（不只是生成计划时查一次）。

    逐项核对：
      1. 候选哈希与计划时一致（计划生成后候选被改动即拒绝）；
      2. 文本类动作的**来源身份**：`src_cc` 原始台账该段落的事件序列仍等于计划里的源序列；
      3. 文本类动作的**候选 actual**：候选段落事件序列仍等于计划期读到的序列；
      4. 结构授权：候选段落仍不含字段/OMML（"x"）。

    返回 (ok, reason)。调用方（runner）据此走 PLAN_STALE，不把旧值写进新候选。
    """
    if cand_sha_at_plan is not None:
        cur = hashlib.sha256(Path(cand_pptx).read_bytes()).hexdigest()
        if cur != cand_sha_at_plan:
            return False, ("候选字节在计划生成后被改动（哈希不一致）；"
                           "拒绝应用旧计划（PLAN_STALE）")
    text_kinds = {"text_unique_source", "text_soft_break_restore",
                  "text_event_sequence_restore"}
    if not any(a.kind in text_kinds for a in plan.actions):
        return True, ""
    from pptx import Presentation
    prs = Presentation(str(cand_pptx))
    for a in plan.actions:
        if a.kind not in text_kinds:
            continue
        loc = a.locator
        key = loc.get("source_para_key")
        # 2) 来源身份仍成立
        if src_cc is not None and key:
            evs = (src_cc.get("para_events", {}) or {}).get(key)
            if not evs:
                return False, f"应用前来源核对失败：原始台账已无该段落（{key}）"
            if _brief_key(evs) != _brief_key(a.source_events):
                return False, (f"应用前来源核对失败：原始台账事件序列与计划时不同（{key}）")
        # 3) 候选 actual 仍成立
        matches = _locate_shape_by_id(prs, a.slide, loc.get("shape_id"))
        if len(matches) != 1:
            matches = _locate_shape_by_name(prs, a.slide, loc)
        if len(matches) != 1:
            return False, "应用前候选定位不唯一（形状已变化）"
        shp = matches[0]
        if not getattr(shp, "has_text_frame", False):
            return False, "应用前候选目标不是文本形状"
        pi = loc.get("paragraph_index", 0)
        paras = shp.text_frame.paragraphs
        if pi >= len(paras):
            return False, "应用前候选段落数不足"
        p_el = paras[pi]._p
        if "x" in _para_event_pattern(p_el):
            return False, "应用前候选段落含字段/公式/OMML，未授权结构"
        if a.candidate_actual_events and \
                _brief_key(_para_events_brief(p_el)) != _brief_key(a.candidate_actual_events):
            return False, "应用前候选段落事件序列与计划期读到的不一致（候选已被改动）"
    return True, ""


# ---------------------------------------------------------------- 应用（独立候选）
def apply_repairs(plan, cand_pptx, out_pptx):
    """把计划应用到**独立候选**：先复制再改，绝不原地改输入候选。"""
    out_pptx = Path(out_pptx)
    out_pptx.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(cand_pptx, out_pptx)          # 独立候选：新文件
    from pptx import Presentation
    prs = Presentation(str(out_pptx))
    applied = []
    for a in plan.actions:
        if a.kind == "text_unique_source":
            ok = _apply_text(prs, a)
        elif a.kind == "table_cell_unique":
            ok = _apply_table_cell(prs, a)
        elif a.kind == "table_relation_restore":
            ok = _apply_table_relation(prs, a)
        elif a.kind in ("text_soft_break_restore", "text_event_sequence_restore"):
            ok = _apply_text_soft_break(prs, a)
        else:
            ok = False
        applied.append({"kind": a.kind, "slide": a.slide,
                        "locator": a.locator, "expected": a.expected,
                        "applied": ok, "cells": a.cells, "unit": a.unit,
                        "source_events": a.source_events})
        if not ok:
            # 计划内动作应用失败 → 整个独立候选作废，交回调用方判定
            return applied, False
    prs.save(str(out_pptx))                        # 保存
    return applied, True


def _apply_text(prs, action):
    """把原文写回候选段落：保留 run 数量与首个 run 的 rPr。"""
    slide_no = action.slide
    loc = action.locator
    para_idx = loc["paragraph_index"]
    if slide_no < 1 or slide_no > len(prs.slides):
        return False
    matches = _locate_shape_by_id(prs, slide_no, loc.get("shape_id"))
    if len(matches) != 1:
        matches = _locate_shape_by_name(prs, slide_no, loc)
    if len(matches) != 1:
        return False
    shp = matches[0]
    if not getattr(shp, "has_text_frame", False):
        return False
    paras = shp.text_frame.paragraphs
    if para_idx >= len(paras):
        return False
    para_el = paras[para_idx]._p
    if not _cand_para_pure_runs(para_el):
        return False
    runs = para_el.findall(f"{{{A}}}r")
    if not runs:
        return False
    first_t = runs[0].find(f"{{{A}}}t")
    if first_t is None:
        return False
    first_t.text = action.expected
    for r in runs[1:]:
        t = r.find(f"{{{A}}}t")
        if t is not None:
            t.text = ""
    return True


def _apply_table_relation(prs, action):
    """多单元格协同写入：在保存后的独立文件里重新定位表，逐格写回源值。"""
    slide_no = action.slide
    if slide_no < 1 or slide_no > len(prs.slides):
        return False
    cand_tables = _build_table_index(prs)
    same = [t for t in cand_tables if t["page"] == slide_no]
    target = None
    for t in same:
        if t["shape_id"] == action.locator.get("table_shape_id"):
            target = t
            break
    if target is None:
        idx = action.locator.get("table_ordinal_on_page", 0)
        if idx < len(same):
            target = same[idx]
    if target is None:
        return False
    for c in action.cells:
        ri, ci, val = c["row_index"], c["col_index"], c["expected"]
        try:
            cell = target["shape"].table.cell(ri + 1, ci)   # +1：第 0 行是表头
        except Exception:  # noqa: BLE001
            return False
        cell.text = val
    return True


def _apply_text_soft_break(prs, action):
    """按源事件序列写回文本；`a:br` 保持原位不动，只改 `a:r` 的文本。"""
    slide_no = action.slide
    loc = action.locator
    pi = loc["paragraph_index"]
    if slide_no < 1 or slide_no > len(prs.slides):
        return False
    matches = _locate_shape_by_id(prs, slide_no, loc.get("shape_id"))
    if len(matches) != 1:
        matches = _locate_shape_by_name(prs, slide_no, loc)
    if len(matches) != 1:
        return False
    shp = matches[0]
    if not getattr(shp, "has_text_frame", False):
        return False
    paras = shp.text_frame.paragraphs
    if pi >= len(paras):
        return False
    p_el = paras[pi]._p
    if "x" in _para_event_pattern(p_el):
        return False
    texts = [e.get("text", "") for e in action.source_events
             if e.get("kind") == "text"]
    ri = 0
    for ch in p_el:
        ln = ch.tag.split("}", 1)[-1] if "}" in ch.tag else ch.tag
        if ln != "r":
            continue
        t = ch.find(f"{{{A}}}t")
        if t is None:
            return False
        t.text = texts[ri] if ri < len(texts) else ""
        ri += 1
    return ri == len(texts)


def _apply_table_cell(prs, action):
    slide_no = action.slide
    loc = action.locator
    ri, ci = loc["row_index"], loc["col_index"]
    if slide_no < 1 or slide_no > len(prs.slides):
        return False
    # 重新定位（保存后的独立文件里重新找，不信任计划期的对象引用）
    cand_tables = []
    for si, slide in enumerate(prs.slides, start=1):
        for shp, grid, gpath, sid in _iter_tables(slide.shapes, si):
            cand_tables.append({"shape": shp, "page": si, "grid": grid,
                                "shape_id": sid, "group_path": gpath})
    same = [t for t in cand_tables if t["page"] == slide_no]
    target = None
    for t in same:
        if t["shape_id"] == loc["table_shape_id"]:
            target = t
            break
    if target is None:
        # 形状 id 可能变：退到 组路径 + 同页序号
        idx = loc.get("table_ordinal_on_page", 0)
        if idx < len(same):
            target = same[idx]
    if target is None:
        return False
    try:
        cell = target["shape"].table.cell(ri + 1, ci)   # +1：第 0 行是表头
    except Exception:  # noqa: BLE001
        return False
    cell.text = action.expected
    return True


# ---------------------------------------------------------------- 独立重读
def reread_verify(out_pptx, applied):
    """保存后用**全新的 Presentation** 打开，逐条核对修复是否落地。"""
    from pptx import Presentation
    prs = Presentation(str(out_pptx))
    results = []
    for rec in applied:
        if not rec.get("applied"):
            results.append({**rec, "reread_ok": False,
                            "reread_note": "应用阶段即失败"})
            continue
        loc = rec["locator"]
        if rec["kind"] == "text_unique_source":
            matches = _locate_shape_by_id(prs, rec["slide"], loc.get("shape_id"))
            if len(matches) != 1:
                matches = _locate_shape_by_name(prs, rec["slide"], loc)
            if len(matches) != 1:
                results.append({**rec, "reread_ok": False,
                                "reread_note": f"重读定位不唯一（{len(matches)}）"})
                continue
            shp = matches[0]
            pi = loc.get("paragraph_index", 0)
            paras = shp.text_frame.paragraphs
            got = paras[pi].text if pi < len(paras) else None
            ok = (got == rec["expected"])
            results.append({**rec, "reread_ok": ok,
                            "reread_got": got,
                            "reread_note": "重读文本与原文一致" if ok else "重读文本与原文不一致"})
        elif rec["kind"] in ("text_soft_break_restore", "text_event_sequence_restore"):
            # 独立重读：用检测侧同一套 census 重新提取事件序列，与源序列逐项比对
            from wengao_ppt.census import census_xml_content
            cc = census_xml_content(out_pptx)
            smap = _census_shape_map(cc)
            obj_path = loc.get("object_path") or smap.get(
                (rec["slide"], str(loc.get("shape_id"))))
            pi = loc.get("paragraph_index", 0)
            evs = (cc.get("para_events", {}) or {}).get(
                f"{rec['slide']}|{obj_path}|{pi}") if obj_path else None
            got = [(e.get("kind"), e.get("text", "")) for e in (evs or [])]
            want = [(e.get("kind"), e.get("text", "")) for e in rec.get("source_events", [])]
            ok = bool(evs) and got == want
            results.append({**rec, "reread_ok": ok,
                            "reread_got": "".join(g[1] for g in got),
                            "reread_note": ("重读事件序列与源序列一致（含软换行位置）" if ok
                                            else f"重读事件序列与源序列不一致：{got} != {want}")})
        elif rec["kind"] == "table_relation_restore":
            cand = []
            for si, slide in enumerate(prs.slides, start=1):
                for shp, grid, gpath, sid in _iter_tables(slide.shapes, si):
                    if sid == loc.get("table_shape_id"):
                        cand.append((shp, grid))
            if len(cand) != 1:
                results.append({**rec, "reread_ok": False,
                                "reread_note": f"重读表格定位不唯一（{len(cand)}）"})
                continue
            _shp, grid = cand[0]
            bad = []
            for c in rec.get("cells", []):
                ri, ci, want_v = c["row_index"], c["col_index"], c["expected"]
                try:
                    got_v = grid[ri + 1][ci]
                except Exception:  # noqa: BLE001
                    got_v = None
                if got_v != want_v:
                    bad.append({"row_index": ri, "col_index": ci,
                                "expected": want_v, "got": got_v})
            ok = not bad
            results.append({**rec, "reread_ok": ok, "reread_checked": len(rec.get("cells", [])),
                            "reread_mismatch": bad,
                            "reread_note": ("多单元格逐格重读与源值一致" if ok
                                            else f"{len(bad)} 格重读不一致")})
        else:
            ri, ci = loc["row_index"], loc["col_index"]
            cand = []
            for si, slide in enumerate(prs.slides, start=1):
                for shp, grid, gpath, sid in _iter_tables(slide.shapes, si):
                    if sid == loc["table_shape_id"]:
                        cand.append((shp, grid))
            if len(cand) != 1:
                results.append({**rec, "reread_ok": False,
                                "reread_note": f"重读表格定位不唯一（{len(cand)}）"})
                continue
            shp, grid = cand[0]
            try:
                got = grid[ri + 1][ci]
            except Exception:  # noqa: BLE001
                got = None
            ok = (got == rec["expected"])
            results.append({**rec, "reread_ok": ok, "reread_got": got,
                            "reread_note": "重读单元格与原文一致" if ok else "重读单元格与原文不一致"})
    return results


# ---------------------------------------------------------------- 计分（检测与修复分开）
def score_split(detection_findings, plan, applied, reread, verdict_after,
                rounds_used, max_rounds):
    """检测与修复**分开计分**，不合并成一个结论。"""
    det_codes = {}
    for f in detection_findings:
        det_codes[f.get("code", "?")] = det_codes.get(f.get("code", "?"), 0) + 1
    applied_n = sum(1 for a in applied if a.get("applied"))
    reread_ok_n = sum(1 for r in reread if r.get("reread_ok"))
    return {
        "detection": {
            "findings_total": len(detection_findings),
            "by_code": det_codes,
            "note": "检测结论只由检测器产出，与修复结果无关。",
        },
        "repair": {
            "whitelist": list(WHITELIST),
            "planned": len(plan.actions),
            "refused": len(plan.refused),
            "applied": applied_n,
            "reread_verified": reread_ok_n,
            "rounds_used": rounds_used,
            "rounds_allowed": max_rounds,
            "rounds_exceeded": rounds_used > max_rounds,
            "verdict_after_repair": verdict_after,
            "note": "修复结论只由修复链（计划→独立候选→保存→独立重读→重新验收）产出。",
        },
    }
