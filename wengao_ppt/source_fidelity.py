"""原材料来源侧保真核对（产品模块）。

与 runner 既有检查的区别（这是它必须独立成模块的原因）：
- 既有检查以**候选自身**或其直接上游为基线；
- 本模块的期望集合是**原材料在模型调用之前冻结的必讲事实台账**，
  候选不能决定"应该有什么内容"，也不能反向修订期望。

核对内容不止数字：主体、指标、数值原写法、单位、时间、条件、
否定/比较关系，以及计划页型与实际原生对象的绑定。

模型返回只当数据；本模块不执行任何模型输出内容。
检测阶段关闭自动修复：不写回候选、不改原材料、不改台账。
"""
from __future__ import annotations

import re
import unicodedata

# 契约既有码（不新造）
CODE_MISSING = "FIDELITY.REQUIRED_TEXT_MISSING"
CODE_SUBJECT = "FIDELITY.SUBJECT_VALUE_MISMATCH"
CODE_SIGN = "FIDELITY.SIGN_MISMATCH"
CODE_COVERAGE = "GATE.FACT_COVERAGE_INCOMPLETE"
CODE_BINDING = "SCOPE.UNREQUESTED_CHANGE"

NEG_CUES = ("下降", "减少", "降低", "下滑", "负增长", "未达", "低于", "不足", "亏损")
COND_CUES = ("同比", "环比", "较上年", "目标", "计划", "承诺", "预计", "截至",
             "上半年", "下半年", "每季度", "每月")


def normalize(text: str) -> str:
    """保守规范化：NFC + 折叠空白。

    **不删除数值负号、单位、百分号**，也不做数字改写。
    """
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", text or ""))


def _structural_subject(subject: str) -> bool:
    """结构化占位主体不是语义主体，拿它做"错绑"判定会产生假阳性。

    涵盖两类：
    - 纯文本无标题层级时产生的「第 N 段」；
    - 章节标题（如「二、资产分布」「第三章 结论」）——它们是**结构位置/导航**，
      按规格「结构位置用于定位，语义主体用于含义」，不得当作业务主体。
    """
    s = (subject or "").strip()
    core = re.sub(r"^[一二三四五六七八九十、\s]+", "", s)
    if re.fullmatch(r"(第\s*\d+\s*段|unknown|)", core or ""):
        return True
    if re.fullmatch(r"[一二三四五六七八九十]+[、.．].*", s):
        return True
    if re.fullmatch(r"第\s*[一二三四五六七八九十\d]+\s*[章节部分].*", s):
        return True
    return False


def candidate_texts(pptx_path) -> list[str]:
    from pptx import Presentation
    prs = Presentation(str(pptx_path))
    pages = []
    for slide in prs.slides:
        parts = []
        for sh in slide.shapes:
            if getattr(sh, "has_text_frame", False) and sh.has_text_frame:
                parts.append(sh.text_frame.text)
            if getattr(sh, "has_table", False) and sh.has_table:
                for row in sh.table.rows:
                    for cell in row.cells:
                        parts.append(cell.text)
        pages.append("\n".join(parts))
    return pages


def candidate_chart_values(pptx_path) -> list[float]:
    """收集原生图表的系列数值——数值画成图表同样是真实承载。"""
    from pptx import Presentation
    prs = Presentation(str(pptx_path))
    vals = []
    for slide in prs.slides:
        for sh in slide.shapes:
            if getattr(sh, "has_chart", False) and sh.has_chart:
                try:
                    for ser in sh.chart.series:
                        for v in ser.values:
                            if v is not None:
                                vals.append(float(v))
                except Exception:  # noqa: BLE001
                    pass
    return vals


def candidate_visible_meta(pptx_path) -> str:
    """图表上**可见**但不在文本框里的语义信息：系列名、数值格式（单位）、轴标题。

    原生图表可以把单位放在系列名（「指标（台）」）或数值格式（`0.0"台"`）里，
    这些同样是可见标签，单位核对必须把它们算进去，否则会误杀正确图表。
    """
    parts: list[str] = []
    for p in _chart_xml_parts(pptx_path):
        parts.append(p.get("series_name") or "")
    try:
        import re as _re2
        import zipfile
        z = zipfile.ZipFile(str(pptx_path))
        for n in z.namelist():
            if _re2.fullmatch(r"ppt/charts/chart\d+\.xml", n):
                x = z.read(n).decode("utf-8", "ignore")
                parts += _re2.findall(r"<c:formatCode>(.*?)</c:formatCode>", x)
                parts += _re2.findall(r"<a:t>(.*?)</a:t>", x)
    except Exception:  # noqa: BLE001
        pass
    return normalize("\n".join(parts))


def _page_has_chart(slide) -> bool:
    return any(getattr(sh, "has_chart", False) and sh.has_chart for sh in slide.shapes)


def _page_has_table(slide) -> bool:
    return any(getattr(sh, "has_table", False) and sh.has_table for sh in slide.shapes)


def _page_has_picture(slide) -> bool:
    for sh in slide.shapes:
        try:
            if "PICTURE" in str(sh.shape_type).upper():
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


# ---------------------------------------------------------------------------
# 数值/单位关联核对（替代裸子串判定）
# ---------------------------------------------------------------------------
# 已知不能用子串的原因（实测反例）：
#   台账「7 台」 vs 候选「17 台」 → "7 台" 是 "17 台" 的子串 → 裸子串会假通过；
#   台账「70%」  vs 候选「170%」 → 同上。
# 因此数值必须按**数字边界**切分，再核对符号、精度、百分号/百分点与单位关联。

# 注意：alternation 必须放在非捕获组内，否则 `|` 优先级会把左右边界断言
# 只作用于其中一个分支（曾导致 "99.9" 在 "99.95" 里被错误切分）。
_NUM_TOKEN = re.compile(
    r"(?<![\d.])"                                  # 左侧不能紧邻数字或小数点
    r"(?:[-+−]?\d{1,3}(?:,\d{3})+|[-+−]?\d+(?:\.\d+)?)"  # 千分位 或 普通数值
    r"(?![\d])"                                    # 右侧不能紧邻数字
)

#: 单位 → 是否属于"百分比类"（% 与 个百分点 语义不同，不能互换）
_PCT_UNITS = {"%", "％"}
_PP_UNITS = {"个百分点", "百分点", "个基点", "pp"}
#: 多字符单位必须排在单字符单位之前（正则最长匹配优先）
_UNIT_ALT = (
    "个百分点|百分点|个基点|小时|分钟|季度|千瓦时|万吨|亿元|万元|人次|"
    "台|个|件|项|次|天|周|月|年|秒|人|家|元|亿|万|%|％|吨|套|批|倍"
)


def _num_tokens(text: str) -> list[str]:
    """按数字边界切出全部数值 token（保留原始符号与千分位）。"""
    return _NUM_TOKEN.findall(text or "")


def _canon_num(tok: str) -> tuple[float, int, str]:
    """把数值 token 规范化成 (数值, 小数位数, 符号)。

    小数位数用于判定精度是否被改动；符号区分正负（负号丢失必须拦截）。
    """
    s = (tok or "").strip().replace(",", "").replace("−", "-")
    sign = "-" if s.startswith("-") else ("+" if s.startswith("+") else "")
    s2 = s.lstrip("+-")
    try:
        val = float(s2)
    except ValueError:
        return (float("nan"), 0, sign)
    dec = len(s2.split(".")[1]) if "." in s2 else 0
    return (val, dec, sign)


def _unit_at(text: str, start: int, tok_len: int) -> str:
    """取**指定位置**的数值 token 之后的紧邻单位。

    必须由 tokenize 时给出的位置调用：朴素 `text.find(tok)` 会落在
    另一个数字内部（实测"99.9"会命中"99.95"里的前 4 个字符）。
    """
    tail = text[start + tok_len:]
    m = re.match(r"\s*(" + _UNIT_ALT + ")", tail)
    return m.group(1) if m else ""


def _unit_of_token(text: str, tok: str) -> str:
    """兼容入口：按 token 边界定位后再取单位（不再用裸 find 的首个命中）。"""
    for m in _NUM_TOKEN.finditer(text or ""):
        if m.group(0) == tok:
            return _unit_at(text, m.start(), m.end() - m.start())
    # 退化：token 不在边界切分结果里时，用带边界的搜索
    m = re.search(r"(?<![\d.])" + re.escape(tok) + r"(?![\d])", text or "")
    if m:
        return _unit_at(text, m.start(), len(tok))
    return ""


def value_matches(fact: dict, page_text: str) -> dict:
    """在**单页文本**内按冻结规则核对一条事实的数值与单位关联。

    返回 {"ok": bool, "why": str, "mode": "adjacent_unit"|"column_unit"|None}

    判定层次（任一成立即通过，但都必须是**明确关联**）：
    1. 数值按数字边界匹配 + 符号一致 + 精度一致 + 单位紧邻；
    2. 数值在**表格列**内，且该列的表头/行首声明了同一单位（明确关联）；
    3. 裸数值且无任何单位关联 → 不通过。
    """
    raw = str(fact.get("raw_value_text") or "").strip()
    if not raw:
        return {"ok": False, "why": "台账条目无数值 token", "mode": None}
    want = str(fact.get("value") or "").strip()
    want_unit = str(fact.get("unit_class") or "").strip()
    try:
        want_val = float(want.replace(",", ""))
    except ValueError:
        want_val = None
    want_pct = want_unit in _PCT_UNITS
    want_sign = "-" if raw.lstrip().startswith(("-", "−")) else ""

    for m in _NUM_TOKEN.finditer(page_text or ""):
        tok = m.group(0)
        val, dec, sign = _canon_num(tok)
        if val != val:  # NaN
            continue
        if want_val is None:
            if tok != raw:
                continue
        elif abs(val - want_val) > 1e-9:
            continue
        # 数值匹配 → 现在核对符号、精度与单位关联
        if want_sign and sign != want_sign:
            return {"ok": False,
                    "why": f"数值 {tok!r} 的符号与冻结写法 {raw!r} 不一致",
                    "mode": None, "seen": tok}
        if want_sign and not sign:
            return {"ok": False,
                    "why": f"数值 {tok!r} 丢失了冻结写法 {raw!r} 的负号",
                    "mode": None, "seen": tok}
        if not want_sign and sign and want_val is not None and val > 0:
            # 候选写成 -N 而冻结为正：可能是符号被改
            return {"ok": False,
                    "why": f"数值 {tok!r} 的符号与冻结写法 {raw!r} 不一致",
                    "mode": None, "seen": tok}
        # 精度：冻结写法有小数位时，候选不得改成整数或不同精度
        _, want_dec, _ = _canon_num(want or raw)
        if want_dec and dec == 0:
            return {"ok": False,
                    "why": f"数值 {tok!r} 的精度被改动（冻结为 {want_dec} 位小数）",
                    "mode": None, "seen": tok}
        if not want_unit or want_unit == "unknown":
            return {"ok": True, "why": "数值一致（台账未声明单位）",
                    "mode": None, "seen": tok}
        seen_unit = _unit_at(page_text, m.start(), m.end() - m.start())
        if seen_unit == want_unit:
            return {"ok": True, "why": f"{tok}{seen_unit} 数值与单位均一致",
                    "mode": "adjacent_unit", "seen": tok}
        # % 与 个百分点 不可互换
        if want_pct and seen_unit in _PP_UNITS:
            return {"ok": False,
                    "why": f"单位被改动：{seen_unit} 与冻结的 {want_unit} 语义不同",
                    "mode": None, "seen": tok}
        if want_unit in _PP_UNITS and seen_unit in _PCT_UNITS:
            return {"ok": False,
                    "why": f"单位被改动：{seen_unit} 与冻结的 {want_unit} 语义不同",
                    "mode": None, "seen": tok}
    return {"ok": False,
            "why": f"未找到与冻结值 {raw!r} 按数字边界一致且单位关联明确的数值",
            "mode": None}


def value_matches_in_tables(fact: dict, pptx_path) -> dict:
    """表格列内核对：值在单元格，且**同列表头或同行行首声明同一单位**。

    这是规格允许的「明确关联」情形，例如「数量（台）」列里的 7。
    不依赖把整表拼成一段文本后做子串匹配。
    """
    from pptx import Presentation
    want_unit = str(fact.get("unit_class") or "").strip()
    if not want_unit or want_unit == "unknown":
        return {"ok": False, "why": "台账未声明单位", "mode": None}
    try:
        want_val = float(str(fact.get("value") or "").replace(",", ""))
    except ValueError:
        return {"ok": False, "why": "台账无数值", "mode": None}
    try:
        prs = Presentation(str(pptx_path))
    except Exception:  # noqa: BLE001
        return {"ok": False, "why": "候选无法解析", "mode": None}
    subj_core = re.sub(r"^[一二三四五六七八九十、\s]+", "",
                       str(fact.get("subject") or "").strip())
    subj_core = re.sub(r"^(项目|指标|类别)\s*", "", subj_core)[:6]
    for slide in prs.slides:
        for sh in slide.shapes:
            if not (getattr(sh, "has_table", False) and sh.has_table):
                continue
            tbl = sh.table
            n_rows, n_cols = len(tbl.rows), len(tbl.columns)
            grid = [[tbl.cell(r, c).text for c in range(n_cols)] for r in range(n_rows)]
            for r in range(n_rows):
                for c in range(n_cols):
                    cell = grid[r][c]
                    for tok in _num_tokens(cell):
                        val, _dec, _sign = _canon_num(tok)
                        if val != val or abs(val - want_val) > 1e-9:
                            continue
                        # **关系核对**：命中行必须自身承载冻结主体。
                        # 否则「数值集合不变、主体对调」会被放过。
                        row_other = normalize(
                            "".join(grid[r][j] for j in range(n_cols) if j != c))
                        if (subj_core and not _structural_subject(subj_core)
                                and subj_core not in row_other):
                            continue
                        # 同列表头声明同一单位 → 明确关联
                        for hr in range(r):
                            if want_unit in normalize(grid[hr][c]):
                                return {"ok": True,
                                        "why": f"表格 [{hr + 1},{c + 1}] 列声明单位 "
                                               f"{want_unit}，第 {r + 1} 行主体 "
                                               f"{subj_core or '（未声明）'}，"
                                               f"单元格值 {tok} 一致",
                                        "mode": "column_unit", "seen": tok}
                        # 同行行首声明同一单位
                        for cc in range(c):
                            if want_unit in normalize(grid[r][cc]):
                                return {"ok": True,
                                        "why": f"表格第 {r + 1} 行行首声明单位 "
                                               f"{want_unit} 与主体 {subj_core or '（未声明）'}，"
                                               f"单元格值 {tok} 一致",
                                        "mode": "row_header_unit", "seen": tok}
    return {"ok": False, "why": "表格内未找到带明确单位关联的相同数值", "mode": None}


# ---------------------------------------------------------------------------
# 原生图表关系核对：类别/系列/值/单位/期间 + 嵌入工作簿与缓存
# ---------------------------------------------------------------------------
# 只比较"数值集合"或"chart part 数量"是不够的：实测三份候选的图表类别是
#   重复的同一标签（4 个点同叫「续约预警覆盖率」）、
#   整句原句（「跨区域数据同步存在 3 分钟的延迟」）、
#   被截断的碎片（「业务系统中有·4个仍使」），
# 而数值集合本身"看起来"都对。已知类别错误不得记作关系检查通过。

CODE_CHART_CAT_DUP = "FIDELITY.CHART_CATEGORY_NOT_DISCRIMINATING"
CODE_CHART_CAT_NUM = "FIDELITY.CHART_CATEGORY_IS_NUMBER"
CODE_CHART_CAT_TRUNC = "FIDELITY.CHART_CATEGORY_TRUNCATED"
CODE_CHART_CAT_UNBOUND = "FIDELITY.CHART_CATEGORY_NOT_BOUND_TO_SUBJECT"
CODE_CHART_SERIES_GENERIC = "FIDELITY.CHART_SERIES_GENERIC"
CODE_CHART_WB_MISMATCH = "FIDELITY.CHART_WORKBOOK_CACHE_MISMATCH"
CODE_CHART_DUP_POINT = "FIDELITY.CHART_DUPLICATE_POINT"

_GENERIC_SERIES = {"数值", "值", "value", "values", "series", "序列", "数据"}


def _chart_xml_parts(pptx_path) -> list[dict]:
    """从 PPTX 包里读出每张原生图表的类别/系列/值与嵌入工作簿。

    直接读 OOXML（`c:cat`/`c:val` 的缓存 + `c:f` 公式范围 + `ppt/embeddings/*.xlsx`），
    因为"可见标签"与"工作簿里真正的数据"必须分别核对。
    """
    import io
    import re as _re
    import zipfile
    out: list[dict] = []
    try:
        z = zipfile.ZipFile(str(pptx_path))
    except Exception:  # noqa: BLE001
        return out
    names = z.namelist()
    chart_names = sorted(n for n in names
                         if _re.fullmatch(r"ppt/charts/chart\d+\.xml", n))
    # 每张图表有**自己的**嵌入工作簿：必须经它自己的 rels 解析，
    # 不能把"找到的第一个 embeddings/*.xlsx"分配给所有图表（实测会误报不一致）。
    def _workbook_for(cn: str):
        rels = f"ppt/charts/_rels/{cn.split('/')[-1]}.rels"
        if rels not in names:
            return None
        try:
            rx = z.read(rels).decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            return None
        m = _re.search(r'Target="([^"]*embeddings/[^"]+)"', rx)
        if not m:
            return None
        tgt = m.group(1)
        if tgt.startswith("/"):
            path = tgt.lstrip("/")
        else:
            path = "ppt/charts/" + tgt
        path = path.replace("\\", "/")
        # 规范化 ../ 与 ./ 片段
        parts = []
        for seg in path.split("/"):
            if seg == "..":
                if parts:
                    parts.pop()
            elif seg and seg != ".":
                parts.append(seg)
        return "/".join(parts)

    for cn in chart_names:
        try:
            xml = z.read(cn).decode("utf-8")
        except Exception:  # noqa: BLE001
            continue
        wb_name = _workbook_for(cn)
        title_block = _re.search(r"<c:title>(.*?)</c:title>", xml, _re.S)
        chart_title = ""
        if title_block:
            from html import unescape as _html_unescape
            chart_title = "".join(_html_unescape(t) for t in
                                   _re.findall(r"<a:t>(.*?)</a:t>",
                                               title_block.group(1), _re.S)).strip()
        series = _re.findall(r"<c:ser>(.*?)</c:ser>", xml, _re.S)
        for si, sxml in enumerate(series):
            name_m = _re.search(r"<c:tx>.*?<c:strCache>.*?<c:v>(.*?)</c:v>", sxml, _re.S)
            cat_block = _re.search(r"<c:cat>(.*?)</c:cat>", sxml, _re.S)
            val_block = _re.search(r"<c:val>(.*?)</c:val>", sxml, _re.S)
            cats = _re.findall(r"<c:v>(.*?)</c:v>",
                               (cat_block.group(1) if cat_block else ""), _re.S)
            vals = _re.findall(r"<c:v>(.*?)</c:v>",
                               (val_block.group(1) if val_block else ""), _re.S)
            cat_f = _re.search(r"<c:cat>.*?<c:f>(.*?)</c:f>", sxml, _re.S)
            val_f = _re.search(r"<c:val>.*?<c:f>(.*?)</c:f>", sxml, _re.S)
            out.append({"chart": cn, "series_idx": si,
                        "chart_title": chart_title,
                        "series_name": (name_m.group(1) if name_m else ""),
                        "categories": cats, "values": vals,
                        "cat_formula": (cat_f.group(1) if cat_f else ""),
                        "val_formula": (val_f.group(1) if val_f else ""),
                        "workbook": wb_name, "workbook_categories": [],
                        "workbook_values": []})
    # 按每张图表自己的工作簿读取单元格（一次读出，多图表时分别匹配）
    wb_cache: dict = {}
    for p in out:
        wbn = p.get("workbook")
        if not wbn or wbn not in names:
            continue
        if wbn not in wb_cache:
            try:
                from openpyxl import load_workbook
                wb = load_workbook(io.BytesIO(z.read(wbn)), data_only=True)
                ws = wb.worksheets[0]
                rows = [[c.value for c in r] for r in ws.iter_rows()]
                wb_cache[wbn] = (
                    [str(r[0]) for r in rows[1:] if len(r) > 1 and r[1] is not None],
                    [float(r[1]) for r in rows[1:] if len(r) > 1 and r[1] is not None],
                )
            except Exception:  # noqa: BLE001
                wb_cache[wbn] = ([], [])
        cats_wb, vals_wb = wb_cache[wbn]
        p["workbook_categories"] = list(cats_wb)
        p["workbook_values"] = list(vals_wb)
    return out


def _is_bare_number(label: str) -> bool:
    return bool(re.fullmatch(r"[-+]?\d[\d,]*(?:\.\d+)?\s*(?:%|％|个百分点)?",
                             (label or "").strip()))


def _looks_truncated(label: str) -> bool:
    t = (label or "").strip()
    return t.endswith("…") or t.endswith("...")


def _truncates_mid_word(label: str, sentence: str) -> bool:
    """标签是原句的前缀、且原句在该处**并未**在标点/空格边界结束 → 被截断。

    例：「业务系统中有·4个仍使」，原句后面还有「用默认口令」→ 断在词中。
    """
    lab = (label or "").strip()
    sent = (sentence or "").strip()
    if not lab or not sent or lab not in sent:
        return False
    i = sent.find(lab) + len(lab)
    if i >= len(sent):
        return False          # 正好到句尾 → 完整
    nxt = sent[i]
    return bool(re.match(r"[\u4e00-\u9fffA-Za-z0-9]", nxt))


def check_chart_relations(fact_ledger: dict, pptx_path,
                          label_for_fact=None) -> list[dict]:
    """逐数据点核对图表的类别/系列/值/单位/工作簿缓存关系。"""
    findings: list[dict] = []
    parts = _chart_xml_parts(pptx_path)
    if not parts:
        return findings
    facts = fact_ledger.get("facts") or []
    # 数值 → 冻结事实（同一数值可能有多个事实；保留全部以检测主体对调）
    by_val: dict[float, list[dict]] = {}
    for f in facts:
        try:
            v = float(str(f.get("value") or "").replace(",", ""))
        except (TypeError, ValueError):
            continue
        by_val.setdefault(v, []).append(f)

    for p in parts:
        cats, vals = p["categories"], p["values"]
        sn = (p["series_name"] or "").strip()
        if sn in _GENERIC_SERIES or not sn:
            findings.append({
                "code": CODE_CHART_SERIES_GENERIC, "severity": "BLOCKING",
                "origin": "source_coverage", "fact_id": None,
                "detail": f"图表 {p['chart']} 的系列名 {sn!r} 是通用词，"
                          "未承载指标/单位语义。"})
        # 工作簿与缓存一致性
        if p["workbook"] and p["workbook_values"]:
            try:
                cache_v = [float(x) for x in vals]
            except (TypeError, ValueError):
                cache_v = []
            if cache_v and [round(x, 6) for x in cache_v] != \
                    [round(x, 6) for x in p["workbook_values"][:len(cache_v)]]:
                findings.append({
                    "code": CODE_CHART_WB_MISMATCH, "severity": "BLOCKING",
                    "origin": "source_coverage", "fact_id": None,
                    "detail": f"图表 {p['chart']} 的显示缓存 {cache_v} 与嵌入工作簿 "
                              f"{p['workbook']} 的值 "
                              f"{p['workbook_values'][:len(cache_v)]} 不一致。"})
            wb_cats = p["workbook_categories"]
            if wb_cats and [normalize(x) for x in wb_cats[:len(cats)]] != \
                    [normalize(x) for x in cats]:
                findings.append({
                    "code": CODE_CHART_WB_MISMATCH, "severity": "BLOCKING",
                    "origin": "source_coverage", "fact_id": None,
                    "detail": f"图表 {p['chart']} 的类别缓存 {cats} 与嵌入工作簿 "
                              f"{wb_cats[:len(cats)]} 不一致。"})
        # 逐点：类别可靠性 + 重复 + 主体绑定
        seen_label: dict[str, list[str]] = {}
        for i, lab in enumerate(cats):
            lab = (lab or "").strip()
            val = vals[i] if i < len(vals) else None
            seen_label.setdefault(lab, []).append(val)
            if _is_bare_number(lab):
                findings.append({
                    "code": CODE_CHART_CAT_NUM, "severity": "BLOCKING",
                    "origin": "source_coverage", "fact_id": None,
                    "detail": f"图表 {p['chart']} 第 {i + 1} 个类别的可见标签是裸数字 "
                              f"{lab!r}，不能充当类别。"})
            elif _looks_truncated(lab):
                findings.append({
                    "code": CODE_CHART_CAT_TRUNC, "severity": "BLOCKING",
                    "origin": "source_coverage", "fact_id": None,
                    "detail": f"图表 {p['chart']} 第 {i + 1} 个类别 {lab!r} 是被截断的"
                              "片段，来源描述不完整。"})
            elif len(lab) > 24 and any(t in lab for t in _num_tokens(lab)):
                findings.append({
                    "code": CODE_CHART_CAT_TRUNC, "severity": "REVIEW",
                    "origin": "source_coverage", "fact_id": None,
                    "detail": f"图表 {p['chart']} 第 {i + 1} 个类别是一整句原句 "
                              f"{lab!r}，未收敛为可读类别（进复核）。"})
        for lab, vs in seen_label.items():
            if len(vs) > 1 and len(set(vs)) > 1:
                findings.append({
                    "code": CODE_CHART_CAT_DUP, "severity": "BLOCKING",
                    "origin": "source_coverage", "fact_id": None,
                    "detail": f"图表 {p['chart']} 的类别 {lab!r} 同时对应多个不同数值 "
                              f"{vs}：类别无法区分数据点。"})
        # 重复点：同一 (类别, 数值) 出现多次 → 图表被灌水
        pairs = [(cats[i], vals[i]) for i in range(min(len(cats), len(vals)))]
        dup = {x for x in pairs if pairs.count(x) > 1}
        for lab, v in sorted(dup):
            findings.append({
                "code": CODE_CHART_DUP_POINT, "severity": "BLOCKING",
                "origin": "source_coverage", "fact_id": None,
                "detail": f"图表 {p['chart']} 重复绘制了同一数据点（{lab!r}, {v}）；"
                          "重复点不得用来凑覆盖。"})
        # 每个数值是否绑定到"该数值真正对应的事实"的类别
        for i, lab in enumerate(cats):
            if i >= len(vals):
                break
            try:
                v = float(vals[i])
            except (TypeError, ValueError):
                continue
            owners = by_val.get(v) or []
            if not owners:
                continue          # 非冻结数值由别的检查（编造数字）负责
            # 截断判定只针对**兜底来源描述**（可靠短标签不会"截断"）。
            # 可靠名词标签（如「存量接口」）在句中本来就可能出现在词中位置。
            labels = {}
            if label_for_fact is not None:
                for f in owners:
                    try:
                        labels[id(f)] = label_for_fact(f)
                    except Exception:  # noqa: BLE001
                        labels[id(f)] = None
            for f in owners:
                r = labels.get(id(f)) or {}
                if r.get("reliable", True):
                    continue
                sent = (f.get("source_ref") or {}).get("sentence") or ""
                if _truncates_mid_word(lab, sent):
                    findings.append({
                        "code": CODE_CHART_CAT_TRUNC, "severity": "BLOCKING",
                        "origin": "source_coverage", "fact_id": f.get("fact_id"),
                        "detail": f"图表 {p['chart']} 第 {i + 1} 个类别 {lab!r} 是对事实 "
                                  f"{f.get('fact_id')} 来源描述的截断（断在词中），"
                                  "来源描述不完整。"})
                    break
            if label_for_fact is None:
                continue
            want = {str((labels.get(id(f)) or {}).get("label") or "").strip() for f in owners}
            core = {re.sub(r"^(第\s*\d+\s*段|unknown)$", "", x) for x in want}
            core = {x for x in core if x}
            if not core:
                continue
            role_labels = {"当前", "目标", "改善前", "改善后", "同比", "环比",
                           "同期", "预算", "实际", "去年", "今年", "上年", "本年"}
            is_role_only = normalize(lab) in {normalize(x) for x in role_labels}
            chart_title_norm = normalize(p.get("chart_title") or "")
            title_binds_subject = bool(chart_title_norm) and all(
                normalize(x[:6]) in chart_title_norm for x in core)
            # Role-only categories (e.g. 当前/目标) are acceptable only when a
            # visible native chart title carries every bound fact subject. A
            # caller-supplied relation record or a generic chart title is ignored.
            category_bound = (title_binds_subject if is_role_only else
                              any(x and normalize(x[:6]) in normalize(lab) for x in core))
            if not category_bound:
                findings.append({
                    "code": CODE_CHART_CAT_UNBOUND, "severity": "BLOCKING",
                    "origin": "source_coverage", "fact_id": owners[0].get("fact_id"),
                    "detail": f"图表 {p['chart']} 第 {i + 1} 个类别 {lab!r} 与数值 {v} "
                              f"所对应的事实主体 {sorted(core)} 不匹配；角色型类别需由原生图表标题绑定主体（疑似错绑/类别对调）。"})
    return findings


def check_fact_coverage(fact_ledger: dict, pptx_path, plan: dict | None = None) -> list[dict]:
    """逐条核对冻结事实是否在候选中成立。返回 finding 列表（空 = 通过）。

    fact_ledger: {"facts": [...], "frozen_before_model_call": true, ...}
    """
    facts = fact_ledger.get("facts") or []
    findings: list[dict] = []
    if not facts:
        return findings

    pages = candidate_texts(pptx_path)
    chart_nums = candidate_chart_values(pptx_path)
    full = normalize("\n".join(pages))
    # 图表上可见但不进文本框的语义（系列名/数值格式单位/轴标题）一并计入**单位**核对
    full_units = full + candidate_visible_meta(pptx_path)
    all_neg = any(c in full for c in NEG_CUES)
    all_cond = any(c in full for c in COND_CUES)

    # 图表关系单独核对（类别/系列/工作簿缓存/重复点）。数值"出现在图表里"本身
    # 不足以证明这条事实成立——类别错了就不能记作关系检查通过。
    try:
        from . import label_extract as _label_mod
        chart_findings = check_chart_relations(fact_ledger, pptx_path,
                                               label_for_fact=_label_mod.label_for_fact)
    except Exception as e:  # noqa: BLE001
        chart_findings = [{"code": "GATE.CHECKER_CRASH", "severity": "BLOCKING",
                           "origin": "source_coverage", "slide": None,
                           "detail": f"图表关系核对异常：{type(e).__name__}: {e}"}]
    findings += chart_findings
    chart_blocking = any(f.get("severity") == "BLOCKING" for f in chart_findings)

    for fact in facts:
        tok = normalize(fact.get("raw_value_text") or "")
        if not tok:
            # 定性必讲句（无数字 token）：覆盖判据是**完整原句**出现在候选中
            # （监督 2026-10-20：不能只保 57 个数字；半句/截断不算覆盖）。
            if fact.get("qualitative"):
                sent = normalize((fact.get("source_ref") or {}).get("sentence") or "")
                core = sent.rstrip("。！？")
                if core and core not in full:
                    findings.append({
                        "code": CODE_MISSING, "severity": "BLOCKING",
                        "origin": "source_coverage",
                        "fact_id": fact.get("fact_id"),
                        "raw_value_text": None,
                        "detail": f"定性必讲句 {fact.get('fact_id')} 未完整出现在候选中"
                                  f"（完整原句，非数字 token）："
                                  f"「{(fact.get('source_ref') or {}).get('sentence', '')[:36]}…」。",
                    })
            continue
        fid = fact.get("fact_id")
        val = str(fact.get("value") or "")
        unit = fact.get("unit_class") or ""
        subject = fact.get("subject") or ""
        sent = (fact.get("source_ref") or {}).get("sentence") or ""

        # 值 + 单位关联：按数字边界核对，不使用裸子串。
        # （裸子串会让「7 台」被「17 台」满足、「70%」被「170%」满足。）
        hit = None
        for pg in pages:
            r = value_matches(fact, pg)
            if r["ok"]:
                hit = {"mode": r["mode"], "why": r["why"]}
                break
        if hit is None:
            r = value_matches_in_tables(fact, pptx_path)
            if r["ok"]:
                hit = {"mode": r["mode"], "why": r["why"]}
        if hit is not None:
            hit_mode = hit["mode"]
        else:
            in_chart = False
            try:
                fv = float(val.replace(",", ""))
                in_chart = any(abs(x - fv) < 1e-9 for x in chart_nums)
            except (TypeError, ValueError):
                in_chart = False
            if in_chart:
                # 图表承载：数值一致，但**类别/系列/单位/期间关系**由
                # check_chart_relations 单独核对；类别错则本条不得记作完整通过。
                hit = {"mode": "chart_value",
                       "why": "数值出现在原生图表系列中"
                              + ("（但图表类别关系存在问题）" if chart_blocking else "")}
                if chart_blocking:
                    findings.append({
                        "code": CODE_CHART_CAT_UNBOUND, "severity": "BLOCKING",
                        "origin": "source_coverage", "fact_id": fid,
                        "raw_value_text": fact.get("raw_value_text"),
                        "detail": f"必讲事实 {fid}（{fact.get('raw_value_text')!r}）由图表承载，"
                                  "但图表的类别/系列关系核对未通过，不得记作完整覆盖。"})
            else:
                # 给出可诊断的原因：优先报告"找到同数字但关联不符"
                diag = value_matches(fact, "\n".join(pages))
                detail = diag.get("why") or "既不在候选文本中，也不在原生图表数据中"
                findings.append({
                    "code": CODE_MISSING, "severity": "BLOCKING",
                    "origin": "source_coverage",
                    "fact_id": fid, "raw_value_text": fact.get("raw_value_text"),
                    "detail": f"必讲事实 {fid}（{fact.get('raw_value_text')!r}）"
                              f"未按冻结规则成立：{detail}。",
                })
                continue

        # 单位：值出现但单位整体缺失 → 关系不完整
        if unit and unit != "unknown" and normalize(unit) not in full_units:
            findings.append({
                "code": CODE_MISSING, "severity": "BLOCKING", "origin": "source_coverage",
                "fact_id": fid, "raw_value_text": fact.get("raw_value_text"),
                "detail": f"必讲事实 {fid} 的单位 {unit!r} 未在候选中保留。",
            })

        # 否定/比较关系
        if any(c in sent for c in NEG_CUES) and not (all_neg or re.search(r"-\d", full)):
            findings.append({
                "code": CODE_SIGN, "severity": "BLOCKING", "origin": "source_coverage",
                "fact_id": fid, "raw_value_text": fact.get("raw_value_text"),
                "detail": f"必讲事实 {fid} 的否定/比较关系可能丢失（原句含下降/减少等）。",
            })

        # 时间/限定条件
        if any(c in sent for c in COND_CUES) and not all_cond:
            findings.append({
                "code": CODE_MISSING, "severity": "BLOCKING", "origin": "source_coverage",
                "fact_id": fid, "raw_value_text": fact.get("raw_value_text"),
                "detail": f"必讲事实 {fid} 的时间/限定条件线索缺失。",
            })

        # 主体：语义主体未出现 → 可能错绑（结构化占位/章节标题不属于语义主体）
        core = re.sub(r"^[一二三四五六七八九十、\s]+", "", subject.strip())
        if (core and not _structural_subject(core) and core != "unknown"
                and not _structural_subject(subject)):
            # 线索必须与候选同一规范化口径：normalize 会删除全部空白
            # （2026-10-27 实证：主体"27 来源与发布决策记录"的线索带空格，
            # 全文去空格后永远匹配不上 → 13 条假阳性"疑似错绑"）。
            head = normalize(core)[:6]
            if head and head not in full:
                findings.append({
                    "code": CODE_SUBJECT, "severity": "BLOCKING", "origin": "source_coverage",
                    "fact_id": fid, "raw_value_text": fact.get("raw_value_text"),
                    "detail": f"必讲事实 {fid} 的主体线索 {head!r} 未出现在候选中（疑似错绑）。",
                })

    # 半句门禁（监督 2026-10-12→20 实证）：must_state 条目是某冻结原句的
    # **截断前缀**（以逗号/顿号中断），且完整原句不在计划/候选中 → 不得记作
    # 覆盖。P12 的「…基础设施稳定性、」数字 token 齐全，此前的数值核对全过，
    # 但原句后半"接口治理、续约预警三项改进措施绑定推进"整段丢失。
    if plan:
        plan_text = normalize("\n".join(
            str(x) for s in (plan.get("slides") or []) if isinstance(s, dict)
            for x in (s.get("must_state") or [])))
        sent_pool = [normalize((f.get("source_ref") or {}).get("sentence") or "")
                     .rstrip("。！？") for f in facts]
        sent_pool = [s for s in sent_pool if len(s) >= 10]
        reported: set = set()
        for s in (plan.get("slides") or []):
            if not isinstance(s, dict):
                continue
            for x in (s.get("must_state") or []):
                item = normalize(str(x)).rstrip("。！？")
                if len(item) < 8:
                    continue
                for sent in sent_pool:
                    if item != sent and sent.startswith(item) and len(item) < len(sent):
                        if sent in plan_text or sent in full:
                            break          # 完整原句已在计划/候选中：共同覆盖
                        key = (s.get("page"), item[:24])
                        if key in reported:
                            break
                        reported.add(key)
                        findings.append({
                            "code": CODE_MISSING, "severity": "BLOCKING",
                            "origin": "source_coverage",
                            "fact_id": None, "raw_value_text": None,
                            "detail": f"第 {s.get('page')} 页 must_state 条目是冻结原句的"
                                      f"截断半句（以逗号/顿号中断）：「{str(x)[:30]}…」；"
                                      f"完整原句未在计划或候选中出现，不得记作覆盖。",
                        })
                        break

    # 计划页型 → 实际原生对象绑定（图表被换成文字/图片必须被拦）
    if plan:
        from pptx import Presentation
        prs = Presentation(str(pptx_path))
        slides = list(prs.slides)
        for spec in plan.get("slides", []) or []:
            idx = int(spec.get("page") or 0) - 1
            if idx < 0 or idx >= len(slides):
                findings.append({
                    "code": CODE_MISSING, "severity": "BLOCKING", "origin": "source_coverage",
                    "fact_id": None, "raw_value_text": None,
                    "detail": f"计划第 {spec.get('page')} 页在候选中不存在。",
                })
                continue
            slide = slides[idx]
            t = spec.get("page_type")
            if t == "chart" and not _page_has_chart(slide):
                findings.append({
                    "code": CODE_BINDING, "severity": "BLOCKING", "origin": "source_coverage",
                    "fact_id": None, "raw_value_text": None,
                    "detail": f"计划第 {spec.get('page')} 页要求原生 chart，"
                              "实际没有 chart 对象（被替换为文字或图片）。",
                })
            if t == "table" and not _page_has_table(slide):
                findings.append({
                    "code": CODE_BINDING, "severity": "BLOCKING", "origin": "source_coverage",
                    "fact_id": None, "raw_value_text": None,
                    "detail": f"计划第 {spec.get('page')} 页要求原生表格，实际没有表格对象。",
                })
            if t == "image_caption" and not _page_has_picture(slide):
                findings.append({
                    "code": CODE_BINDING, "severity": "BLOCKING", "origin": "source_coverage",
                    "fact_id": None, "raw_value_text": None,
                    "detail": f"计划第 {spec.get('page')} 页要求 image_caption，"
                              "实际没有受控图片对象（不得以纯文字页冒充）。",
                })
    return findings
