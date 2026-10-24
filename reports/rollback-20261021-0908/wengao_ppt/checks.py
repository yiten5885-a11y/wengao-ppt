"""P1 到期检查器：内容保真（对象级绑定）、出现次数、符号保真、原生可编辑性。

与旧 qa_check.py 的关键差异（本轮实测确认的三个根因）：
1. 不用"整页文本拼接后的子串包含"；每个源单元按其 source_ref 与输出中对应对象绑定比较。
2. 规范化默认仅保守 NFC；绝不剥离行首负号（旧实现 norm('-120') == norm('120')）。
3. 出现次数按来源位置计数，不是全文计数。
4. 原生可编辑性核对"可见内容是否由原生对象承载"，防整页截图与隐藏同文诱饵。
"""
from __future__ import annotations

import re
import unicodedata
import zipfile
from collections import Counter

from .core import conservative_norm, leading_sign, numeric_tokens, shape_visible

# 所有对外错误码一律取自 contracts/error_codes_v1.json（45 码字典）。
# 更细的判定原因放在 finding 的 reason_code 字段里，不另造一本字典。
CODES = {
    "required_text_missing": "FIDELITY.REQUIRED_TEXT_MISSING",
    "occurrence_mismatch": "FIDELITY.OCCURRENCE_MISMATCH",
    "sign_mismatch": "FIDELITY.SIGN_MISMATCH",
    "locked_context_missing": "FIDELITY.LOCKED_CONTEXT_MISSING",
    "notes_missing": "FIDELITY.NOTES_MISSING",
    "visible_not_native": "EDIT.VISIBLE_CONTENT_NOT_NATIVE",
    "hidden_duplicate": "EDIT.DECOY",
    "checker_crash": "GATE.CHECKER_CRASH",
    "content_clipped": "RENDER.REQUIRED_CONTENT_CLIPPED",
}

# ---------------------------------------------------------------- 批准的字符/排版规范化
# N03：白名单里的规范化可以放行；未声明的同款改动必须照旧阻断。
# 白名单只含"字符级/排版级"变换，绝不含删字、删段、改数字。

_ASCII_FW = {chr(0xFF01 + i): chr(0x21 + i) for i in range(94)}   # ！-～ → !-~
_PUNCT_PAIRS = {"。": ".", "，": ",", "；": ";", "：": ":", "（": "(", "）": ")",
                "【": "[", "】": "]", "《": "<", "》": ">"}
_QUOTE_PAIRS = {"“": '"', "”": '"', "‘": "'", "’": "'", "＂": '"'}


def _norm_punct_width(t: str) -> str:
    """全角 ASCII/标点 → 半角（字符级，不动数字与文字）。"""
    return "".join(_ASCII_FW.get(ch, ch) for ch in t)


def _norm_quote_unify(t: str) -> str:
    """各种弯引号 → 直引号。"""
    return "".join(_QUOTE_PAIRS.get(ch, ch) for ch in t)


APPROVED_NORMALIZATIONS = {
    "nfc": lambda t: unicodedata.normalize("NFC", t),
    "punctuation_width": _norm_punct_width,
    "quote_unify": _norm_quote_unify,
}


def apply_approved(text: str, approved):
    """按顺序应用已声明的批准规范化。"""
    t = text
    for name in sorted(approved or []):
        fn = APPROVED_NORMALIZATIONS.get(name)
        if fn:
            t = fn(t)
    return t


def _pages_of(doc):
    """同时接受 dict 形态与 ExtractedDoc 对象。"""
    if isinstance(doc, dict):
        return doc.get("pages", [])
    return getattr(doc, "pages", [])


def _units_of(page):
    if isinstance(page, dict):
        return page.get("units", [])
    return getattr(page, "units", [])


def _find_units_by_ref(output_doc, source_ref):
    """按结构定位在输出中找对应单元（对象级绑定，不是整页子串）。

    刻意不按文件路径匹配：输出是另一个文件，必须靠 slide/object/part/row/col/
    paragraph_index/line_start 等结构定位符绑定。文件级绑定由 baseline/validation 的哈希负责。
    """
    out = []
    for page in _pages_of(output_doc):
        for u in _units_of(page):
            ref = getattr(u, "source_ref", {}) if hasattr(u, "source_ref") else u.get("source_ref", {})
            if source_ref.get("slide") is not None and ref.get("slide") != source_ref["slide"]:
                continue
            if source_ref.get("object") and ref.get("object") != source_ref["object"]:
                continue
            if source_ref.get("part") and ref.get("part") != source_ref["part"]:
                continue
            if source_ref.get("row") is not None and ref.get("row") != source_ref["row"]:
                continue
            if source_ref.get("col") is not None and ref.get("col") != source_ref["col"]:
                continue
            if source_ref.get("paragraph_index") is not None and ref.get("paragraph_index") != source_ref["paragraph_index"]:
                continue
            if source_ref.get("line_start") is not None and ref.get("line_start") != source_ref["line_start"]:
                continue
            out.append((page, u))
    return out


def _text_of(u):
    return u.text if hasattr(u, "text") else u.get("text", "")


def check_content_fidelity(input_doc, output_doc, approved_normalizations=None,
                           src_dynamic=None, out_dynamic=None):
    """内容保真：逐字锁定文本、出现次数、符号、锁定上下文、备注。

    src_dynamic / out_dynamic：{页码: [动态字段显示值]}，来自**原始输入与候选各自的
    原始普查**。动态字段（页码/日期）显示值允许按冻结规则变化，逐字比较前先遮罩。

    返回 findings 列表；每项含 code/severity/origin/source_ref/expected/actual。
    """
    findings = []
    approved = set(approved_normalizations or [])
    src_dynamic = src_dynamic or {}
    out_dynamic = out_dynamic or {}

    # ---- 逐字锁定与必讲文本（按 source_ref 绑定） ----
    for page, unit in input_doc.all_units():
        ref = unit.source_ref
        text = unit.text
        if not text.strip():
            continue
        if unit.role in ("chart_value",):
            continue  # 图表关系在 P2
        matched = _find_units_by_ref(output_doc, ref)
        actual = [_text_of(u) for _, u in matched]
        if unit.role == "note":
            if not actual or not conservative_norm("".join(actual)).strip():
                findings.append({"code": CODES["notes_missing"], "severity": "BLOCKING",
                                 "origin": "content_fidelity", "slide": page.get("page_no"),
                                 "source_ref": ref, "expected": text, "actual": actual,
                                 "detail": "要求保留的备注缺失；正文完整不能替代备注完整。"})
            continue
        if not matched:
            findings.append({"code": CODES["required_text_missing"], "severity": "BLOCKING",
                             "origin": "content_fidelity", "slide": page.get("page_no"),
                             "source_ref": ref, "expected": text, "actual": [],
                             "detail": "按来源定位未找到对应输出单元。"})
            continue
        same = any(conservative_norm(t) == conservative_norm(text) for t in actual)
        if not same:
            # 只有事先声明的批准规范化差异才不算缺失；未声明的一律照旧阻断（N03 反向对照）
            if approved:
                expect_n = conservative_norm(apply_approved(text, approved))
                got_n = conservative_norm(apply_approved("".join(actual), approved))
                if expect_n == got_n:
                    same = True
        if not same:
            # 动态字段（页码/日期）显示值允许按冻结规则变化 → 遮罩后再比一次
            pn = (unit.source_ref or {}).get("slide")
            if pn is not None and (src_dynamic.get(pn) or out_dynamic.get(pn)):
                m_src = conservative_norm(apply_approved(
                    mask_dynamic_text(text, src_dynamic.get(pn)), approved))
                m_out = conservative_norm(apply_approved(
                    mask_dynamic_text("".join(actual), out_dynamic.get(pn)), approved))
                if m_src == m_out:
                    same = True
        if not same:
            if unit.locked:
                code = CODES["locked_context_missing"]
            else:
                code = CODES["required_text_missing"]
            findings.append({"code": code, "severity": "BLOCKING", "origin": "content_fidelity",
                             "slide": page.get("page_no"), "source_ref": ref,
                             "expected": text, "actual": actual,
                             "detail": "输出文本与锁定/必讲原文不一致（未使用批准的规范化）。"})

    # ---- 出现次数（按来源位置，不是全文计数） ----
    # 键只含结构定位符（slide/object/规范化文本），不含文件路径，
    # 因为输入与输出是两个文件；文件身份由 baseline 哈希绑定。
    # 动态字段显示值可能出现在这段文本里，计数口径与逐字口径必须一致地遮罩，
    # 否则会出现"逐字比较放行、出现次数却报缺失"这种自相矛盾的结论。
    in_counter = Counter()
    for page, unit in input_doc.all_units():
        if unit.role == "note":
            continue
        pn = unit.source_ref.get("slide")
        t = conservative_norm(apply_approved(
            mask_dynamic_text(unit.text, src_dynamic.get(pn)), approved))
        key = (unit.source_ref.get("slide"), unit.source_ref.get("object"),
               unit.source_ref.get("paragraph_index"), unit.source_ref.get("line_start"), t)
        in_counter[key] += 1
    out_counter = Counter()
    for page in _pages_of(output_doc):
        for u in _units_of(page):
            ref = getattr(u, "source_ref", {}) if hasattr(u, "source_ref") else u.get("source_ref", {})
            pn = ref.get("slide")
            t = conservative_norm(apply_approved(
                mask_dynamic_text(_text_of(u), out_dynamic.get(pn)), approved))
            key = (ref.get("slide"), ref.get("object"), ref.get("paragraph_index"),
                   ref.get("line_start"), t)
            out_counter[key] += 1
    for key, n in in_counter.items():
        got = out_counter.get(key, 0)
        if got != n:
            findings.append({"code": CODES["occurrence_mismatch"], "severity": "BLOCKING",
                             "origin": "content_fidelity", "slide": key[1],
                             "source_ref": {"file": key[0], "slide": key[1], "object": key[2]},
                             "expected": f"出现 {n} 次", "actual": f"出现 {got} 次",
                             "detail": "同文内容按来源位置计数不符；不得因同文仍存在而放行。"})

    # ---- 符号保真（数值单元） ----
    for page, unit in input_doc.all_units():
        # 动态日期/页码里的数字会随渲染时刻变化，先遮罩再取数值记号
        text = mask_dynamic_text(unit.text, src_dynamic.get(unit.source_ref.get("slide")))
        toks_in = numeric_tokens(text)
        if not toks_in:
            continue
        matched = _find_units_by_ref(output_doc, unit.source_ref)
        if not matched:
            continue  # 已在必讲文本检查中记录
        actual_text = " ".join(mask_dynamic_text(
            _text_of(u), out_dynamic.get(unit.source_ref.get("slide"))) for _, u in matched)
        toks_out = numeric_tokens(actual_text)
        sign_in = {leading_sign(t)[0] for t in toks_in}
        sign_out = {leading_sign(t)[0] for t in toks_out}
        if sign_in != sign_out:
            findings.append({"code": CODES["sign_mismatch"], "severity": "BLOCKING",
                             "origin": "content_fidelity", "slide": page.get("page_no"),
                             "source_ref": unit.source_ref, "expected": sorted(sign_in),
                             "actual": sorted(sign_out),
                             "detail": "关键数值正负号改变；规范化不得删除数值负号。"})
    return findings


def check_native_editability(pptx_path, output_doc):
    """原生可编辑性：可见正文必须由原生文字对象承载。

    - 整页截图承载主要文字 → EDIT.VISIBLE_CONTENT_NOT_NATIVE
    - 原生文字被隐藏、另放可见截图 → EDIT.DECOY
    - 仅结构核验；真实 Office 编辑兼容性在 P5 验证。
    """
    findings = []
    try:
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE
    except Exception:  # noqa: BLE001
        return [{"code": "GATE.CHECKER_CRASH", "severity": "BLOCKING", "origin": "native_editability",
                 "detail": "python-pptx 不可用，检查器无法执行"}]

    prs = Presentation(str(pptx_path))
    for idx, slide in enumerate(prs.slides, start=1):
        native_texts, pictures, hidden_native = [], 0, 0
        for shp in slide.shapes:
            is_pic = shp.shape_type == MSO_SHAPE_TYPE.PICTURE
            if is_pic:
                pictures += 1
            has_text = getattr(shp, "has_text_frame", False) and shp.has_text_frame and shp.text_frame.text.strip()
            if has_text:
                visible = _visible(shp)
                if visible:
                    native_texts.append(shp.text_frame.text)
                else:
                    hidden_native += 1
        # 主要可见文字来自图片：整页只有一个大图且几乎没有可见原生文字
        big_pics = [s for s in slide.shapes
                    if s.shape_type == MSO_SHAPE_TYPE.PICTURE and s.width and s.height
                    and s.width * s.height > 0.6 * prs.slide_width * prs.slide_height]
        if big_pics and not native_texts:
            findings.append({"code": CODES["visible_not_native"], "severity": "BLOCKING",
                             "origin": "native_editability", "slide": idx,
                             "detail": "主要可见内容由整页截图承载，未见可见原生文字对象。"})
        if hidden_native and big_pics and not native_texts:
            findings.append({"code": CODES["hidden_duplicate"], "severity": "BLOCKING",
                             "origin": "native_editability", "slide": idx,
                             "detail": "存在被隐藏的原生文字对象配合可见截图，疑为诱饵。"})
    return findings


def _visible(shp) -> bool:
    return shape_visible(shp)


# ---------------------------------------------------------------- A03：实际 PPTX 几何

def _iter_shapes(shapes, path=""):
    """递归遍历（含组合内部）。几何口径必须与对象遍历口径一致，否则组内容易漏检。"""
    for shp in shapes:
        cur = f"{path}/{shp.shape_id}" if path else str(shp.shape_id)
        yield shp, cur
        try:
            if shp.shape_type == 6:  # MSO_SHAPE_TYPE.GROUP
                yield from _iter_shapes(shp.shapes, cur)
        except Exception:  # noqa: BLE001
            continue


def check_geometry_bounds(pptx_path, out_doc=None):
    """A03：从**实际候选 PPTX** 取几何，不读布局记录。

    审查反例：把实际 PPTX 里的正文移到页面外、保留旧 layout 记录，几何检查仍通过。
    本检查只认定一种确定事实——对象完全位于页面矩形之外；无法确认的遮挡/裁切
    不在此判定，仍走 REVIEW。
    """
    from pptx import Presentation
    prs = Presentation(str(pptx_path))
    W, H = prs.slide_width, prs.slide_height
    findings = []
    for idx, slide in enumerate(prs.slides, start=1):
        for shp, obj_path in _iter_shapes(slide.shapes):
            try:
                left, top = shp.left, shp.top
                w, h = shp.width, shp.height
            except Exception:  # noqa: BLE001
                continue
            if None in (left, top, w, h):
                continue
            # 完全出界：与页面矩形无交集（含负向越界）
            fully_out = (left + w <= 0 or top + h <= 0 or left >= W or top >= H)
            if not fully_out:
                continue
            text = ""
            if getattr(shp, "has_text_frame", False) and shp.has_text_frame:
                text = (shp.text_frame.text or "").strip()
            if not text:
                continue
            findings.append({
                "code": CODES["content_clipped"], "severity": "BLOCKING",
                "origin": "actual_pptx_geometry", "slide": idx,
                "object": f"slide{idx}/shape[{obj_path}]",
                "expected": f"对象位于页面内（页面 {W}×{H} EMU）",
                "actual": f"left={left} top={top} w={w} h={h}，完全位于页面之外",
                "text_preview": text[:60],
                "detail": "从实际候选 PPTX 读到的几何完全出界；布局/计划记录不能代替实际对象取证，"
                          "渲染器退出码 0 也不能代替几何核对。",
            })
    return findings


# ---------------------------------------------------------------- P2：备注 / 隐藏页 / 组 / 图片

def _pages_list(doc):
    return doc.get("pages", []) if isinstance(doc, dict) else getattr(doc, "pages", [])


def check_notes_relations(in_doc, out_doc):
    """备注按页归属核验：备注是页级对象，不能只在正文里找回来就算通过。"""
    findings = []
    sp = {p.get("page_no"): (p.get("notes") or "") for p in _pages_list(in_doc)}
    op = {p.get("page_no"): (p.get("notes") or "") for p in _pages_list(out_doc)}
    for no, txt in sorted(sp.items()):
        if not txt.strip():
            continue
        actual = (op.get(no) or "").strip()
        if not actual:
            findings.append({"code": CODES["notes_missing"], "severity": "BLOCKING",
                             "origin": "notes", "slide": no,
                             "expected": txt[:80], "actual": "",
                             "detail": f"第 {no} 页的备注在输出中不存在；"
                                       "备注属页级对象，正文完整不能代替备注存在。"})
        elif conservative_norm(actual) != conservative_norm(txt):
            findings.append({"code": CODES["notes_missing"], "severity": "BLOCKING",
                             "origin": "notes", "slide": no,
                             "expected": txt[:80], "actual": actual[:80],
                             "detail": f"第 {no} 页备注内容与来源不一致。"})
    return findings


def check_hidden_state(in_doc, out_doc):
    """隐藏页状态：页数相同不等于隐藏/导出策略相同。"""
    findings = []
    sp = {p.get("page_no"): bool(p.get("hidden")) for p in _pages_list(in_doc)}
    op = {p.get("page_no"): bool(p.get("hidden")) for p in _pages_list(out_doc)}
    if set(sp) != set(op):
        findings.append({"code": "SCOPE.UNREQUESTED_CHANGE", "severity": "BLOCKING",
                         "origin": "page_set", "slide": None,
                         "expected": sorted(sp), "actual": sorted(op),
                         "detail": "页集合发生变化；页序与页数属于必须保持项。"})
    for no in sorted(set(sp) & set(op)):
        if sp[no] != op[no]:
            findings.append({"code": "SCOPE.UNREQUESTED_CHANGE", "severity": "BLOCKING",
                             "origin": "hidden_state", "slide": no,
                             "expected": f"hidden={sp[no]}", "actual": f"hidden={op[no]}",
                             "detail": f"第 {no} 页的隐藏状态被改变；"
                                       "相同页数不等于相同隐藏/导出策略。"})
    return findings


def check_group_structure(in_pptx, out_pptx):
    """组结构：来源 → 输出的组路径与组内对象必须一致（同一递归视角比同一递归视角）。

    注意：不能用"递归视角 vs 对象模型视角"互比来判组结构——对象模型默认不进组，
    这样比会把任何正常组合都报成分歧。R09 的双视角分歧另有专门检查；
    这里只回答"组内的东西在美化后还在不在、路径变没变"。
    """
    from .parsers_xml import parse_pptx_xml
    findings = []
    try:
        a = parse_pptx_xml(in_pptx)
        b = parse_pptx_xml(out_pptx)
    except Exception as e:  # noqa: BLE001
        return [{"code": "OBJECT.PARSE_ERROR", "severity": "BLOCKING",
                 "origin": "group_structure", "slide": None,
                 "detail": f"组结构检查的解析失败：{type(e).__name__}: {e}"}]
    sa = {s["index"]: s for s in a["slides"]}
    sb = {s["index"]: s for s in b["slides"]}
    if set(sa) != set(sb):
        findings.append({"code": "SCOPE.UNREQUESTED_CHANGE", "severity": "BLOCKING",
                         "origin": "group_structure", "slide": None,
                         "expected": sorted(sa), "actual": sorted(sb),
                         "detail": "页集合不同，组结构无法逐页比对。"})
        return findings
    for i in sorted(sa):
        ga = [o for o in sa[i]["objects"] if o.get("nested")]
        gb = [o for o in sb[i]["objects"] if o.get("nested")]
        if len(ga) != len(gb):
            findings.append({"code": "SCOPE.UNREQUESTED_CHANGE", "severity": "BLOCKING",
                             "origin": "group_structure", "slide": i,
                             "expected": f"组内对象 {len(ga)} 个",
                             "actual": f"组内对象 {len(gb)} 个",
                             "detail": "来源里的组合在输出中不再包含同样数量的组内对象；"
                                       "组被拆散会让组内内容脱离原父级变换。"})
            continue
        pa = sorted(o.get("path", "") for o in ga)
        pb = sorted(o.get("path", "") for o in gb)
        if pa != pb:
            findings.append({"code": "SCOPE.UNREQUESTED_CHANGE", "severity": "BLOCKING",
                             "origin": "group_structure", "slide": i,
                             "expected": pa[:6], "actual": pb[:6],
                             "detail": "组内对象的路径不一致；组层级或对象顺序被改变。"})
    return findings


def _slide_picture_hashes(pptx_path) -> dict:
    """逐页图片**内容哈希多重集**：{页码: Counter[sha256]}。

    zip + XML 直读（不依赖 python-pptx）：presentation.xml 的 sldIdLst 定页序，
    slide rels 解析 rId→media，slide XML 数 blip 引用次数。add_picture 存原图
    字节、srcRect 只是裁切元数据，因此输出包内 media 哈希 == 计划素材哈希。
    """
    import hashlib
    import re
    from collections import Counter

    out: dict = {}
    with zipfile.ZipFile(str(pptx_path)) as zf:
        names = set(zf.namelist())
        media_hash: dict = {}

        def media_sha(target: str):
            if target not in media_hash:
                media_hash[target] = hashlib.sha256(zf.read(target)).hexdigest()
            return media_hash[target]

        # 页序：presentation.xml sldIdLst → rels → slideN.xml
        slide_order = []
        if "ppt/presentation.xml" in names and "ppt/_rels/presentation.xml.rels" in names:
            rels = zf.read("ppt/_rels/presentation.xml.rels").decode("utf-8", "replace")
            rid_map = dict(re.findall(r'<Relationship[^>]*Id="([^"]+)"[^>]*Target="([^"]+)"', rels))
            pres = zf.read("ppt/presentation.xml").decode("utf-8", "replace")
            for m in re.finditer(r'<p:sldId[^>]*r:id="([^"]+)"', pres):
                tgt = rid_map.get(m.group(1), "")
                tgt = tgt.replace("../", "ppt/")
                if not tgt.startswith("ppt/"):
                    tgt = "ppt/" + tgt.lstrip("/")
                if tgt in names:
                    slide_order.append(tgt)
        if not slide_order:
            slide_order = sorted(
                (n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                key=lambda n: int(re.search(r"(\d+)", n).group(1)))
        for page, sn in enumerate(slide_order, 1):
            rels_name = "ppt/slides/_rels/" + sn.split("/")[-1] + ".rels"
            rid2media = {}
            if rels_name in names:
                rels = zf.read(rels_name).decode("utf-8", "replace")
                for m in re.finditer(r'<Relationship[^>]*Id="([^"]+)"[^>]*Target="([^"]+)"', rels):
                    tgt = m.group(2).replace("../", "ppt/")
                    if not tgt.startswith("ppt/"):
                        tgt = "ppt/" + tgt.lstrip("/")
                    rid2media[m.group(1)] = tgt
            xml = zf.read(sn).decode("utf-8", "replace")
            c = Counter()
            for m in re.finditer(r'r:embed="([^"]+)"', xml):
                tgt = rid2media.get(m.group(1))
                if tgt and tgt.startswith("ppt/media/") and tgt in names:
                    c[media_sha(tgt)] += 1
            if c:
                out[page] = c
    return out


def check_media_binding(pptx_path, in_doc, out_doc, planned_pages=None,
                        in_pptx_path=None, planned_images=None):
    """图片：页属一致 + 包内 media 都有对象引用（不引用即不可见）。

    两条路径：
    - **内容身份比对**（in_pptx_path + planned_images 给出时，推荐）：
      逐页比对「输入图 ∪ 冻结计划图」与输出的 sha256 多重集——
      计划内正确图放行；计划外加图 / 漏图 / 错页 / **同页换素材**全部阻断；
    - 页数计数比对（兜底）：仅比较每页图片对象数量。
    """
    findings = []
    try:
        zf = zipfile.ZipFile(str(pptx_path))
        media = sorted(n for n in zf.namelist() if n.startswith("ppt/media/"))
        rels_text = " ".join(zf.read(n).decode("utf-8", "replace")
                             for n in zf.namelist() if n.endswith(".rels"))
    except Exception as e:  # noqa: BLE001
        return [{"code": "OBJECT.PARSE_ERROR", "severity": "BLOCKING",
                 "origin": "media_binding", "slide": None,
                 "detail": f"图片绑定检查无法读取包：{type(e).__name__}: {e}"}]
    unreferenced = [m for m in media if m.split("/")[-1] not in rels_text]
    if unreferenced:
        findings.append({"code": "EDIT.UNREFERENCED_MEDIA", "severity": "BLOCKING",
                         "origin": "media_binding", "slide": None,
                         "expected": "包内媒体均有对象引用", "actual": unreferenced[:6],
                         "detail": "包内存在没有任何对象引用的媒体；"
                                   "文件在包里不等于内容可见。"})

    if in_pptx_path is not None and planned_images:
        from collections import Counter
        try:
            inp = _slide_picture_hashes(in_pptx_path)
            outp = _slide_picture_hashes(pptx_path)
        except Exception as e:  # noqa: BLE001
            return findings + [{"code": "OBJECT.PARSE_ERROR", "severity": "BLOCKING",
                                "origin": "media_binding", "slide": None,
                                "detail": f"逐页图片身份比对无法读包：{type(e).__name__}: {e}"}]
        expected = {pg: Counter(c) for pg, c in inp.items()}
        for pg, shas in planned_images.items():
            e_ct = expected.setdefault(int(pg), Counter())
            for s in shas:
                e_ct[s] += 1
        diff = {}
        for pg in sorted(set(expected) | set(outp)):
            e_ct, a_ct = expected.get(pg, Counter()), outp.get(pg, Counter())
            miss, extra = e_ct - a_ct, a_ct - e_ct
            if miss or extra:
                diff[str(pg)] = {
                    "missing": {h[:12]: n for h, n in sorted(miss.items())},
                    "extra": {h[:12]: n for h, n in sorted(extra.items())}}
        if diff:
            findings.append({
                "code": "FIDELITY.IMAGE_IDENTITY_MISMATCH", "severity": "BLOCKING",
                "origin": "media_binding", "slide": None,
                "expected": "每页图片内容 = 输入图 ∪ 冻结配图计划（逐页 sha256 多重集一致）",
                "actual": diff,
                "detail": "逐页图片内容身份不一致：missing=该页缺的图（漏配/挪页），"
                          "extra=该页多的图（计划外新增/同页换素材）。"})
        return findings

    def pic_pages(doc):
        out = {}
        for o in (doc.get("objects", []) if isinstance(doc, dict)
                  else getattr(doc, "objects", [])):
            t = str(o.get("type", "")).upper()
            if "PICTURE" in t:
                out.setdefault(o.get("page"), 0)
                out[o.get("page")] += 1
        return out

    pi, po = pic_pages(in_doc), pic_pages(out_doc)
    expected = dict(pi)
    for pg, cnt in (planned_pages or {}).items():
        k = int(pg)
        expected[k] = expected.get(k, 0) + int(cnt)
    if po != expected:
        findings.append({"code": "FIDELITY.IMAGE_PAGE_MISMATCH", "severity": "BLOCKING",
                         "origin": "media_binding", "slide": None,
                         "expected": expected, "actual": po,
                         "detail": "图片在各页的分布与来源（或冻结配图计划）不一致；"
                                   "图片数量相同也不代表页属相同。"})
    return findings


def doc_required_checks(doc) -> list:
    """由**提取后的原始对象台账**推导到期必需检查（W01 的内容侧）。

    与 census.census_required_checks（包侧）互补：表格/备注/图片在包层面没有独立
    part，只能从提取结果判定。返回值与 profile 侧集合取并集。
    """
    req = []
    raw_tables = doc.get("tables", []) if isinstance(doc, dict) else getattr(doc, "tables", [])
    raw_charts = doc.get("charts", []) if isinstance(doc, dict) else getattr(doc, "charts", [])
    pages = doc.get("pages", []) if isinstance(doc, dict) else getattr(doc, "pages", [])
    objs = doc.get("objects", []) if isinstance(doc, dict) else getattr(doc, "objects", [])
    if raw_tables:
        req += ["table.native_present", "table.relation_binding"]
    if raw_charts:
        req += ["chart.native_present", "chart.relation_binding", "chart.source_consistency"]
    if any((p.get("notes") or "").strip() for p in pages):
        req += ["content_fidelity.notes"]
    types = {str(o.get("type", "")) for o in objs}
    if any("PICTURE" in t.upper() for t in types):
        req += ["object.media_binding"]
    return sorted(set(req))


# ---------------------------------------------------------------- P2：规则表格关系

# 与 contracts/error_codes_v1.json 对齐；细粒度判定原因由 tables.CONTRACT_CODES 的
# reason_code 承载，本表保留键名以便回溯。
TABLE_CODES = {
    "native_missing": "EDIT.TABLE_RASTERIZED",
    "empty_decoy": "EDIT.DECOY",
    "subject_value_swap": "FIDELITY.SUBJECT_VALUE_MISMATCH",
    "cell_value_mismatch": "FIDELITY.TABLE_CELL_MISMATCH",
    "unit_mismatch": "FIDELITY.NUMBER_UNIT_MISMATCH",
    "structure_changed": "FIDELITY.TABLE_CELL_MISMATCH",
    "irregular": "REVIEW.SEMANTIC_AMBIGUITY",
}


def _tables_of(doc):
    """把 dict 形态的表还原成 TableModel；doc 既可能是 ExtractedDoc 也可能是 dict。"""
    from .tables import TableModel
    raw = doc.get("tables", []) if isinstance(doc, dict) else getattr(doc, "tables", [])
    out = []
    for t in raw:
        if isinstance(t, TableModel):
            out.append(t)
            continue
        d = dict(t)
        out.append(TableModel(
            table_id=d.get("table_id", "?"), page=d.get("page", 0), shape_id=d.get("shape_id", ""),
            source_index=int(d.get("source_index", -1)),
            headers=list(d.get("headers", [])), rows=[list(r) for r in d.get("rows", [])],
            row_labels=list(d.get("row_labels", [])), geometry=d.get("geometry", {}) or {},
            group_path=d.get("group_path", ""), irregular=bool(d.get("irregular")),
            irregular_reason=d.get("irregular_reason", ""), visible=d.get("visible", True),
            source_ref=d.get("source_ref", {}) or {}))
    return out


def check_table_relations(in_doc, out_doc, mapping=None):
    """规则表关系核验：对象级绑定，不退化成整表文本集合比较。

    mapping 为显式来源映射（DOCX 表序号 → 输出页/形状）时优先按映射配对。
    """
    from .tables import compare_tables
    src = _tables_of(in_doc)
    out = _tables_of(out_doc)
    if not src:
        return []
    return compare_tables(src, out, mapping=mapping)


# ---------------------------------------------------------------- P2：原生图表关系

def _charts_of(doc):
    """把 dict 形态的图表还原成 ChartModel；doc 既可能是 ExtractedDoc 也可能是 dict。"""
    from .charts import ChartModel
    raw = doc.get("charts", []) if isinstance(doc, dict) else getattr(doc, "charts", [])
    out = []
    for t in raw:
        if isinstance(t, ChartModel):
            out.append(t)
            continue
        d = dict(t)
        out.append(ChartModel(
            chart_id=d.get("chart_id", "?"), page=d.get("page", 0),
            shape_id=d.get("shape_id", ""), chart_type=d.get("chart_type", ""),
            raw_type=d.get("raw_type", ""), series=list(d.get("series", [])),
            categories=list(d.get("categories", [])), cache=d.get("cache", {}) or {},
            workbook=d.get("workbook", {}) or {}, workbook_part=d.get("workbook_part", ""),
            geometry=d.get("geometry", {}) or {}, source_ref=d.get("source_ref", {}) or {},
            warnings=list(d.get("warnings", []))))
    return out


def check_chart_relations(in_doc, out_doc):
    """原生图表关系核验：E02 原生性 + C07 系列—类别—数值绑定（关闭自动修复）。"""
    from .charts import check_chart_native_present, check_chart_binding
    src = _charts_of(in_doc)
    out = _charts_of(out_doc)
    if not src:
        return []
    findings = check_chart_native_present(src, out)
    findings += check_chart_binding(src, out)
    return findings


def check_chart_output_sources(out_doc):
    """输出侧图表的工作簿/缓存一致性（C11 在美化后的复验）。"""
    from .charts import check_chart_source_conflicts
    return check_chart_source_conflicts(_charts_of(out_doc))


# ---------------------------------------------------------------- P2：双解析对象集合一致性（R09）

PARSE_CODES = {"divergence": "GATE.PARSER_DIVERGENCE", "parse_error": "OBJECT.PARSE_ERROR"}


def check_parser_agreement(pptx_path):
    """两个独立视角比较同一份 PPTX 的对象集合与表结构，不一致即阻断。

    视角 A：直接读包内原始 XML（presentation.xml 页序 + spTree + a:tbl）；
    视角 B：python-pptx 对象模型。

    诚实边界：B 仍是同一批 XML 上的库封装，两者不是"完全独立实现"；
    能发现的是"对象模型视图看不到的结构"（例如组合内部的表格），
    共同盲区要靠与原稿的来源核对发现，不在本检查的承诺内。
    """
    from .parsers_xml import parse_pptx_xml, parse_pptx_xml_via_pptx, compare_parsers
    try:
        a = parse_pptx_xml(pptx_path)
        b = parse_pptx_xml_via_pptx(pptx_path)
    except Exception as e:  # noqa: BLE001
        return [{"code": PARSE_CODES["parse_error"], "reason_code": "PARSE.EXECUTION_FAILED",
                 "severity": "BLOCKING", "origin": "parser_agreement", "slide": None,
                 "detail": f"解析器执行失败：{type(e).__name__}: {e}"}]
    try:
        diffs = compare_parsers(a, b)
    except Exception as e:  # noqa: BLE001
        return [{"code": PARSE_CODES["divergence"], "reason_code": "PARSE.COMPARE_FAILED",
                 "severity": "BLOCKING", "origin": "parser_agreement", "slide": None,
                 "detail": f"解析器比较异常：{type(e).__name__}: {e}"}]
    findings = []
    for d in diffs:
        findings.append({
            "code": PARSE_CODES["divergence"], "reason_code": "PARSE.OBJECT_SET_DIVERGENCE",
            "severity": "BLOCKING", "origin": "parser_agreement",
            "slide": d.get("page"), "what": d.get("what"),
            "expected": d.get("first"), "actual": d.get("second"),
            "only_in_xml": d.get("only_in_first"), "only_in_objectmodel": d.get("only_in_second"),
            "detail": f"两个视角对「{d.get('what')}」的结论不一致；"
                      "对象集合都没有对齐时，任何保真核验的分母都不可信。",
        })
    return findings


def check_census_consistency(pptx_path):
    """T1 独立解析一致性：独立普查器（原始字节级 XML 事件普查）vs 业务提取器
    （python-pptx 对象模型），读取**同一份原始输入**。

    两条核对不能相互替代：
      - 来源→候选保真：同一结构视角下原稿与实际候选的比较（check_group_structure 等）；
      - 本检查：同一输入在两个独立解析路径下是否看到同一批对象、字段与换行事件。

    诚实边界：两个解析器读的都是同一批 XML 字节，共享"XML 本身损坏"的盲区；
    共享依赖与共有盲区在此记录，不声称完全独立实现。
    """
    from .census import census_xml_content
    from .extractors import extract_any
    findings = []
    cc = census_xml_content(pptx_path)
    doc = extract_any(pptx_path)
    slides_by_no = {p.get("page_no"): p for p in doc.pages}

    # 1) 字段：普查看到的每个字段显示值，业务提取器必须在该页单元里看到
    for f in cc.get("fields", []):
        page = slides_by_no.get(f["slide"], {})
        texts = " ".join(u.text if hasattr(u, "text") else str(u.get("text", ""))
                         for u in page.get("units", []))
        if f["display"] and f["display"] not in texts:
            findings.append({
                "code": PARSE_CODES["divergence"], "reason_code": "PARSE.OBJECT_SET_DIVERGENCE",
                "severity": "BLOCKING", "origin": "census_consistency", "slide": f["slide"],
                "expected": f"字段显示值 {f['display']!r}（{f['object_path']}）",
                "actual": "业务提取器在该页未见此字段显示值",
                "detail": "普查器与业务提取器对字段的可见性不一致；"
                          "字段不能被某一解析器静默漏读。",
            })

    # 2) 软换行：普查事件的每页数量与业务提取文本中的 \x0b 数量必须一致
    br_by_slide = Counter(b["slide"] for b in cc.get("soft_breaks", []))
    for no, page in slides_by_no.items():
        extractor_br = sum(
            (u.text if hasattr(u, "text") else str(u.get("text", ""))).count("\x0b")
            for u in page.get("units", []))
        if br_by_slide.get(no, 0) != extractor_br:
            findings.append({
                "code": PARSE_CODES["divergence"], "reason_code": "PARSE.OBJECT_SET_DIVERGENCE",
                "severity": "BLOCKING", "origin": "census_consistency", "slide": no,
                "expected": f"软换行事件 {br_by_slide.get(no, 0)} 个",
                "actual": f"业务提取文本中 {extractor_br} 个",
                "detail": "软换行事件在两解析路径间数量不一致；"
                          "原始事件台账与比较用文本必须分开且能对上。",
            })

    # 3) 对象身份：普查的对象 id 多重集合（含组内）与提取器对象台账必须一致。
    #    组合（grpSp）是容器，两侧都不把它当内容对象，故不参与身份比对；
    #    组合结构本身由组路径（para_events 的 object_path）与组结构检查覆盖。
    for s in cc.get("slides", []):
        census_ids = sorted(o["id"] for o in s["objects"] if not o.get("is_group"))
        page_no = s["index"]
        ext_ids = sorted(
            str(o.get("object_id", "")).rsplit("[", 1)[-1].rstrip("]")
            for o in doc.objects if o.get("page") == page_no)
        if census_ids != ext_ids:
            findings.append({
                "code": PARSE_CODES["divergence"], "reason_code": "PARSE.OBJECT_SET_DIVERGENCE",
                "severity": "BLOCKING", "origin": "census_consistency", "slide": page_no,
                "expected": census_ids[:8], "actual": ext_ids[:8],
                "only_in_census": sorted(set(census_ids) - set(ext_ids)),
                "only_in_extractor": sorted(set(ext_ids) - set(census_ids)),
                "detail": "对象身份集合在两解析路径间不一致（含组内对象）；"
                          "漏读对象会让派生台账与预期检查集合同时失真。",
            })
    return findings


# ------------------------------------------------- T1：字段/软换行 来源→候选保真（事件级）

_FIELD_REASON = {
    "br_missing": "BREAK.EVENT_SET_DIVERGENCE",
    "br_extra": "BREAK.EVENT_SET_DIVERGENCE",
    "br_moved": "BREAK.EVENT_POSITION_DIVERGENCE",
    "para_missing": "PARA.EVENT_SET_DIVERGENCE",
    "text": "TEXT.STATIC_CHANGED",
    "fld_display": "FIELD.DISPLAY_CHANGED",
    "fld_type": "FIELD.TYPE_CHANGED",
    "fld_missing": "FIELD.EVENT_SET_DIVERGENCE",
    "fld_extra": "FIELD.EVENT_SET_DIVERGENCE",
    "fld_unsupported": "FIELD.UNSUPPORTED_DISPLAY_CHANGE",
    "omml_missing": "OMML.EVENT_SET_DIVERGENCE",
    "omml_extra": "OMML.EVENT_SET_DIVERGENCE",
    "omml_changed": "OMML.STRUCTURE_DIVERGENCE",
}


def mask_dynamic_text(text: str, displays) -> str:
    """把动态字段（页码/日期/时间）的显示值替换为占位符后再比较。

    冻结规则：动态字段的显示值按渲染时刻变化是允许的（页码随页号变、日期随当天变），
    因此逐字比较时必须先把它们遮掉，否则每一次合法重排都会被误报为丢字。

    已知边界（如实记录，不声称完美）：遮罩按**字符串**替换，原文中恰好与动态显示值
    同形的片段会被一并遮掉；但它对来源文本与输出文本施加的是同一套遮罩，
    因此只能隐藏完全落在动态片段内的差异，不能隐藏静态正文的差异。
    """
    t = text
    for d in displays or []:
        if d:
            t = t.replace(d, "\x00")
    return t


def dynamic_displays_by_slide(cc: dict) -> dict:
    """从原始普查台账取每页的动态字段显示值（按事件顺序）。"""
    out = {}
    for f in cc.get("fields", []):
        if f.get("class") == "dynamic":
            out.setdefault(f["slide"], []).append(f.get("display") or "")
    return out


def _event_sig(events, approved=frozenset()) -> tuple:
    """把一段事件序列折叠成可比较的签名。

    规则（冻结）：
      - br 事件保留为 \\v 标记，数量与相对顺序都必须一致；
      - text 事件先应用已声明的批准规范化（字符级/排版级），不得增删；
      - 动态字段显示值遮为占位符（允许按渲染时刻变化）；
      - 非动态字段（未知类型）显示值**不遮**，因为无法证明其变化是合法的。
    """
    parts = []
    for e in events:
        if e["kind"] == "br":
            parts.append("\v")
        elif e["kind"] == "text":
            parts.append(apply_approved(e.get("text", ""), approved))
        elif e["kind"] == "omml":
            # OMML 必须在签名里可见，否则候选丢公式时签名不变 → 漏检。
            # 用控制字符包裹的标记，区分 form 与内含 oMath 数量。
            parts.append(f"\x0fomml:{e.get('form','?')}:{e.get('omath_count',0)}\x0f")
        else:
            if e.get("class") == "dynamic":
                parts.append("\x00")
            else:
                parts.append(apply_approved(e.get("display", ""), approved))
    return tuple(parts)


def check_field_break_fidelity(src_path, cand_path, approved_normalizations=None,
                              src_cc=None, cand_cc=None):
    """T1：字段与软换行的**来源→候选**保真比较，比较对象是从原始输入建的台账。

    与 check_census_consistency 的区别（两条不能互相替代）：
      - check_census_consistency：两个解析器读**同一份**输入，查账是否一致；
      - 本检查：同一解析路径下**原稿 vs 候选**，查内容是否被改动。

    记账口径：本检查只记「候选内容错误」；「不支持输入被阻断」由原始普查阶段的
    OMML.UNSUPPORTED / FIELD.UNKNOWN_TYPE 记，两者分开，不合并成一条结论。
    """
    from .census import census_xml_content

    approved = frozenset(approved_normalizations or [])
    src_cc = src_cc if src_cc is not None else census_xml_content(src_path)
    cand_cc = cand_cc if cand_cc is not None else census_xml_content(cand_path)
    findings = []

    src_map = src_cc.get("para_events", {}) or {}
    cand_map = cand_cc.get("para_events", {}) or {}
    for key, evs in sorted(src_map.items()):
        slide = int(key.split("|", 1)[0])
        obj_path = key.split("|", 2)[1] if key.count("|") >= 2 else key
        para = key.rsplit("|", 1)[-1]
        if key not in cand_map:
            findings.append({
                "code": CODES["required_text_missing"],
                "reason_code": _FIELD_REASON["para_missing"],
                "severity": "BLOCKING", "origin": "field_break_fidelity",
                "slide": slide, "object_path": obj_path, "para_idx": para,
                "expected": [_e_brief(e) for e in evs],
                "actual": "候选中不存在该段落事件（对象路径或段落丢失）",
                "detail": "原始输入的段落事件在候选中找不到对应结构；"
                          "段落被合并/删除必须被检出，不能靠其余段落仍在而放行。",
            })
            continue
        got = cand_map[key]
        a = _event_sig(evs, approved)
        b = _event_sig(got, approved)
        if a == b:
            continue
        # 逐类定位差异原因，便于复核"到底变了什么"
        reason = _classify_event_diff(evs, got, approved)
        findings.append({
            "code": CODES["required_text_missing"],
            "reason_code": reason,
            "severity": "BLOCKING", "origin": "field_break_fidelity",
            "slide": slide, "object_path": obj_path, "para_idx": para,
            "expected": [_e_brief(e) for e in evs],
            "actual": [_e_brief(e) for e in got],
            "detail": f"段落事件序列与原始输入不等价（{reason}）；"
                      "软换行数量/位置、静态文本、非动态字段显示值都必须与原始一致，"
                      "仅动态字段显示值允许按冻结规则变化。",
        })
    # 候选里多出来的段落：同样要报（无关项补位不能被忽略）
    for key in sorted(set(cand_map) - set(src_map)):
        slide = int(key.split("|", 1)[0])
        findings.append({
            "code": CODES["required_text_missing"],
            "reason_code": _FIELD_REASON["para_missing"],
            "severity": "BLOCKING", "origin": "field_break_fidelity",
            "slide": slide, "object_path": key.split("|", 2)[1],
            "para_idx": key.rsplit("|", 1)[-1],
            "expected": "原始输入无此段落事件",
            "actual": [_e_brief(e) for e in cand_map[key]],
            "detail": "候选出现原始输入没有的段落事件；无关项补位不得顶替保留项。",
        })
    # 未知类型字段：存在即进入复核（不支持转换，不能按等价文本放行）
    for f in cand_cc.get("fields", []):
        if f.get("class") != "dynamic":
            findings.append({
                "code": "REVIEW.SEMANTIC_AMBIGUITY",
                "reason_code": _FIELD_REASON["fld_unsupported"],
                "severity": "REVIEW", "origin": "field_break_fidelity",
                "slide": f["slide"], "object_path": f.get("object_path"),
                "expected": "字段类型已分类且可证明等价", "actual": f,
                "detail": f"候选含非动态字段（type={f.get('type')!r}）；"
                          "无法证明其显示值变化合法，进入人工复核，不静默放行。",
            })
    return findings


def _e_brief(e: dict):
    if e["kind"] == "br":
        return {"kind": "br"}
    if e["kind"] == "text":
        return {"kind": "text", "text": e.get("text", "")}
    if e["kind"] == "omml":
        return {"kind": "omml", "form": e.get("form"),
                "omath_count": e.get("omath_count")}
    return {"kind": "fld", "type": e.get("type"), "class": e.get("class"),
            "display": e.get("display")}


def _classify_event_diff(src_evs, cand_evs, approved) -> str:
    a = _event_sig(src_evs, approved)
    b = _event_sig(cand_evs, approved)
    # OMML：数量/形态变化必须先于文本判定（公式不是文本）
    a_om = [x for x in a if x.startswith("\x0fomml:")]
    b_om = [x for x in b if x.startswith("\x0fomml:")]
    if len(a_om) != len(b_om):
        return (_FIELD_REASON["omml_missing"] if len(a_om) > len(b_om)
                else _FIELD_REASON["omml_extra"])
    if a_om != b_om:
        return _FIELD_REASON["omml_changed"]
    # 字段结构：字段事件数量变化（字段被展平成文本、或文本被改成字段）
    # 必须先于纯文本判定——这是"显示值没变但结构变了"的关键。
    src_flds = sum(1 for e in src_evs if e["kind"] == "fld")
    cand_flds = sum(1 for e in cand_evs if e["kind"] == "fld")
    if src_flds != cand_flds:
        return (_FIELD_REASON["fld_missing"] if src_flds > cand_flds
                else _FIELD_REASON["fld_extra"])
    if a.count("\v") != b.count("\v"):
        return (_FIELD_REASON["br_missing"] if a.count("\v") > b.count("\v")
                else _FIELD_REASON["br_extra"])
    # 数量相同但序列不同：先判是否只是"位置移动"（静态文本前缀变了）
    a_static = "".join(x for x in a if x != "\v" and not x.startswith("\x0fomml:")).replace("\x00", "")
    b_static = "".join(x for x in b if x != "\v" and not x.startswith("\x0fomml:")).replace("\x00", "")
    if a_static != b_static:
        return _FIELD_REASON["text"]
    if [i for i, x in enumerate(a) if x == "\v"] != \
       [i for i, x in enumerate(b) if x == "\v"]:
        return _FIELD_REASON["br_moved"]
    return _FIELD_REASON["fld_display"]
