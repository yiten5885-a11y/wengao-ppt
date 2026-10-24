"""原始普查（raw census）：在任何 normalize / 清洗 / 转换**之前**登记包内事实。

源码审查 A02：内容可能在形成基线之前已经丢失。四类触发要分开验证，不能用一个
"读取失败"总用例替代：

1. 包成员台账（zip 成员名、大小、CRC、哈希）——后续任何"少了一页/少了一个对象"
   都要能回到这里比对，而不是事后猜。
2. 名称规范化后冲突（反斜杠/大小写/重复）——歧义包必须报 REVIEW，不能自动择一。
3. 成员文本严格 UTF-8 解码——声明为 UTF-8 的 XML 含非法字节必须明确失败，
   不能 `errors="ignore"` 静默丢字节。
4. 原始对象身份——在读模型之前先按 XML 记一版对象/表/图/图片台账。

本模块只做**只读**登记，不修改输入，不做修复。
"""
from __future__ import annotations

import hashlib
import unicodedata
import zipfile
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
M = "http://schemas.openxmlformats.org/officeDocument/2006/math"

# 动态字段（自动页码/日期/时间）：显示值允许按冻结规则变化
FIELD_DYNAMIC_PREFIXES = ("slidenum", "datetime", "date", "time", "fo", "pagenum")

# 审查 A02 明确点出的歧义维度：分隔符规范化 + 大小写 + 重复项
_SEP_VARIANTS = ("/", "\\")


def _norm_name(name: str) -> str:
    """斜杠规范化（不折叠大小写：POSIX 与 Windows ZIP 的大小写语义不同）。

    审查原文只要求"经斜杠规范化后相同"，这里保持保守：只归一分隔符，
    大小写差异单独登记为 warning，不自动判定为同一成员。
    """
    n = unicodedata.normalize("NFC", name)
    for sep in _SEP_VARIANTS[1:]:
        n = n.replace(sep, "/")
    while "//" in n:
        n = n.replace("//", "/")
    return n.strip("/")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def census_package(path) -> dict:
    """对 ZIP 容器（PPTX/DOCX 同为 OPC 包）做只读原始普查。"""
    path = Path(path)
    out = {
        "schema_version": "1.0.0",
        "input": str(path),
        "input_sha256": _sha256(path.read_bytes()) if path.exists() else None,
        "is_zip": False,
        "members": [],
        "member_count": 0,
        "conflicts": [],          # 规范化后同名但内容不同 → 歧义，REVIEW
        "decode_errors": [],      # 严格 UTF-8 解码失败 → BLOCKING
        "warnings": [],
        "kind_counts": Counter(),
    }
    if not zipfile.is_zipfile(str(path)):
        out["warnings"].append({"what": "not_a_zip",
                                "detail": "输入不是 ZIP/OPC 容器；本普查只覆盖包级事实。"})
        return out
    out["is_zip"] = True
    zf = zipfile.ZipFile(str(path))
    by_norm = {}
    for info in zf.infolist():
        try:
            data = zf.read(info.filename)
        except Exception as e:  # noqa: BLE001
            out["warnings"].append({"what": "member_unreadable", "member": info.filename,
                                    "detail": f"{type(e).__name__}: {e}"})
            continue
        rec = {"name": info.filename, "norm_name": _norm_name(info.filename),
               "size": info.file_size, "compress_size": info.compress_size,
               "crc": info.CRC, "sha256": _sha256(data)}
        out["members"].append(rec)
        n = rec["norm_name"]
        if n in by_norm:
            prev = by_norm[n]
            if prev["sha256"] != rec["sha256"]:
                out["conflicts"].append({
                    "norm_name": n,
                    "members": [prev["name"], rec["name"]],
                    "sha256": [prev["sha256"], rec["sha256"]],
                    "sizes": [prev["size"], rec["size"]],
                    "detail": "两个成员名经分隔符规范化后相同但内容不同；"
                              "属于包边界歧义，不得自动择一。",
                })
            else:
                out["warnings"].append({"what": "duplicate_identical_member",
                                        "members": [prev["name"], rec["name"]],
                                        "norm_name": n})
        else:
            by_norm[n] = rec
        # 声明为 XML 的成员必须能严格 UTF-8 解码
        if n.lower().endswith((".xml", ".rels")):
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as e:
                out["decode_errors"].append({
                    "member": info.filename, "offset": e.start,
                    "reason": e.reason, "byte": repr(data[e.start:e.end]),
                    "detail": "声明为 XML 的成员含无法按 UTF-8 解码的字节；"
                              "严格解码失败即失败，不得静默丢弃坏字节。",
                })
    out["member_count"] = len(out["members"])
    for m in out["members"]:
        n = m["norm_name"]
        if n.startswith("ppt/slides/slide") and n.endswith(".xml"):
            out["kind_counts"]["slides"] += 1
        elif n.startswith("ppt/charts/chart") and n.endswith(".xml"):
            out["kind_counts"]["charts"] += 1
        elif n.startswith("ppt/embeddings/"):
            out["kind_counts"]["embeddings"] += 1
        elif n.startswith("ppt/media/"):
            out["kind_counts"]["media"] += 1
        elif n.startswith("ppt/notesSlides/") and n.endswith(".xml"):
            out["kind_counts"]["notes"] += 1
    out["kind_counts"] = dict(out["kind_counts"])
    return out


def census_findings(c: dict) -> list:
    """把普查结果转成 findings：歧义进 REVIEW，解码失败进 BLOCKING。"""
    findings = []
    for cf in c.get("conflicts", []):
        findings.append({
            "code": "REVIEW.SEMANTIC_AMBIGUITY", "severity": "REVIEW",
            "origin": "raw_census", "slide": None,
            "reason_code": "INPUT.AMBIGUOUS_PACKAGE",
            "expected": "包成员名一一对应", "actual": cf["members"],
            "detail": cf["detail"],
        })
    for de in c.get("decode_errors", []):
        findings.append({
            "code": "INPUT.INVALID_ENCODING", "severity": "BLOCKING",
            "origin": "raw_census", "slide": None,
            "expected": "声明的 UTF-8 文本可严格解码",
            "actual": f"{de['member']} 偏移 {de['offset']}：{de['reason']}",
            "detail": de["detail"],
        })
    return findings


def census_required_checks(c: dict) -> list:
    """由**原始对象台账**推导到期必需检查（W01）。

    profile 只是"声明的支持范围"；实际输入里有图表/表格/图片/备注时，
    对应检查必须到期，否则"把带图表的输入降成纯文字范围"就能绕过门禁。
    """
    req = []
    k = c.get("kind_counts", {}) or {}
    if k.get("charts"):
        req += ["chart.native_present", "chart.relation_binding", "chart.source_consistency"]
    if k.get("embeddings"):
        # 有内嵌工作簿却没被识别成图表，本身就要靠图表检查去比对
        req += ["chart.source_consistency"]
    # 表格与图片在包层面无法可靠判定（表格无独立 part），由提取后的 doc 台账补充
    return sorted(set(req))


# ---------------------------------------------------------------- 内容级普查（T1）


def _slide_part_order(zf):
    """按 presentation.xml 的真实页序返回 [(1, 'ppt/slides/slideN.xml'), ...]。"""
    pres = ET.fromstring(zf.read("ppt/presentation.xml"))
    rels = ET.fromstring(zf.read("ppt/_rels/presentation.xml.rels"))
    rid_to_target = {}
    for rel in rels:
        rid_to_target[rel.get("Id")] = rel.get("Target")
    order = []
    sld_ids = pres.find(f"{{{P}}}sldIdLst")
    idx = 0
    if sld_ids is not None:
        for sld in sld_ids:
            rid = sld.get(f"{{{R}}}id")
            target = rid_to_target.get(rid)
            if target:
                idx += 1
                part = target.lstrip("/")
                if not part.startswith("ppt/"):
                    part = "ppt/" + part
                order.append((idx, part))
    return order


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _field_class(fld_type: str) -> str:
    t = (fld_type or "").lower()
    if any(t.startswith(p) for p in FIELD_DYNAMIC_PREFIXES):
        return "dynamic"
    if t:
        return "unknown"
    return "unknown"


def census_xml_content(path) -> dict:
    """内容级普查（T1）：a:fld 字段、a:br 软换行、OMML 公式，事件级记录。

    与 census_package（包成员级）互补：ZIP 成员普查看不到成员内部的字段、
    公式和软换行——它们都在同一个合法成员里，成员数量不会变化。
    只读登记；每个事件保留足以回到原始结构的定位信息。
    """
    path = Path(path)
    out = {
        "schema_version": "1.0.0",
        "input": str(path),
        "input_sha256": _sha256(path.read_bytes()) if path.exists() else None,
        "slides": [],          # [{index, member, hidden, objects:[{id,name,kind,group_path}]}]
        "fields": [],          # [{slide, member, object_path, para_idx, event_idx,
                              #   fld_id, type, class, display}]
        "soft_breaks": [],     # [{slide, member, object_path, para_idx, event_idx,
                              #   before_text, after_text}]
        "omml": [],            # [{slide, member, kind, count, parent_object}]
        # 段落事件级台账：key = f"{slide}|{object_path}|{para_idx}"，
        # value = 按原始顺序的 [{"kind": text|br|fld, ...}]，用于来源→候选保真比较。
        "para_events": {},
        "counts": {"fields": 0, "soft_breaks": 0, "omml": 0},
    }
    if not zipfile.is_zipfile(str(path)) or path.suffix.lower() != ".pptx":
        out["note"] = "内容级普查当前只覆盖 PPTX；其他格式按各自原始结构另行登记，不能据此宣称已覆盖。"
        return out
    zf = zipfile.ZipFile(str(path))
    order = _slide_part_order(zf)
    for idx, part in order:
        root = ET.fromstring(zf.read(part))
        show = root.get("show")
        slide_rec = {"index": idx, "member": part, "hidden": (show == "0"), "objects": []}
        sp_tree = root.find(f".//{{{P}}}cSld/{{{P}}}spTree")
        if sp_tree is None:
            out["slides"].append(slide_rec)
            continue

        def walk(el, group_path, order_state):
            """递归遍历 spTree；order_state 持有跨形状的段落/事件计数。"""
            for child in el:
                kind = _local(child.tag)
                if kind not in ("sp", "pic", "graphicFrame", "grpSp", "cxnSp"):
                    continue
                nv = child.find(f".//{{{P}}}cNvPr")
                name = nv.get("name") if nv is not None else ""
                oid = nv.get("id") if nv is not None else "?"
                cur_path = f"{group_path}/{kind}[{oid}]" if group_path else f"{kind}[{oid}]"
                slide_rec["objects"].append({
                    "id": oid, "name": name, "kind": kind, "group_path": group_path,
                    # 组合是容器而非内容对象：身份台账比对只比内容对象，
                    # 组合本身的结构由组路径与组结构检查负责，避免"容器被算成对象"
                    "is_group": kind == "grpSp",
                    "hidden": bool(nv is not None and nv.get("hidden") == "1")})
                if kind == "grpSp":
                    walk(child, cur_path, order_state)
                    continue
                body = child.find(f".//{{{P}}}txBody")
                if body is None:
                    continue
                for para_idx, para in enumerate(body.findall(f"{{{A}}}p")):
                    # 先把段落内事件全部收集，再按顺序算每个 br 的前后文本
                    events = []
                    ev_key = f"{idx}|{cur_path}|{para_idx}"
                    out["para_events"].setdefault(ev_key, [])
                    for ev in para:
                        ev_kind = _local(ev.tag)
                        ev_ns = ev.tag.split("}", 1)[0][1:] if "}" in ev.tag else ""
                        if ev_kind == "r":
                            t = ev.find(f"{{{A}}}t")
                            events.append(("text", t.text or "" if t is not None else ""))
                        elif ev_kind == "br":
                            events.append(("br", None))
                        elif ev_kind == "fld":
                            t = ev.find(f"{{{A}}}t")
                            disp = t.text or "" if t is not None else ""
                            events.append(("fld", (ev.get("id"), ev.get("type"), disp)))
                        elif ev_ns == M and ev_kind in ("oMathPara", "oMath"):
                            # OMML 是 a:p 的直接子元素（math 命名空间）。
                            # 必须按事件登记对象身份/段落/位置，否则候选丢公式时
                            # para_events 比较查不出来（text_frame.text 根本不含公式）。
                            inner = ev.findall(f"{{{M}}}oMath")
                            events.append(("omml", {
                                "form": ev_kind,
                                "omath_count": len(inner) if ev_kind == "oMathPara" else 1,
                            }))
                    full = []
                    static = []
                    for k2, v2 in events:
                        if k2 == "text":
                            full.append(v2)
                            static.append(v2)
                        elif k2 == "fld":
                            full.append(v2[2])
                            # 动态字段（页码/日期等）的显示值允许按冻结规则变化，
                            # 因此另记一份"静态文本"用于判断换行事件位置是否等价。
                            if _field_class(v2[1]) == "dynamic":
                                static.append("")
                            else:
                                static.append(v2[2])
                    full_text = "".join(full)
                    static_text = "".join(static)
                    # 静态文本的前缀长度：与 full_text 的前缀位置一一对应，
                    # 用于给出"不含动态显示值"的换行位置描述。
                    prefix = 0
                    static_prefix = 0
                    for ei, (k2, v2) in enumerate(events):
                        if k2 == "br":
                            out["soft_breaks"].append({
                                "slide": idx, "member": part, "object_path": cur_path,
                                "para_idx": para_idx, "event_idx": ei,
                                "before_text": full_text[:prefix],
                                "after_text": full_text[prefix:],
                                "before_text_static": static_text[:static_prefix],
                                "after_text_static": static_text[static_prefix:],
                            })
                        elif k2 == "text":
                            prefix += len(v2)
                            static_prefix += len(v2)
                        elif k2 == "fld":
                            prefix += len(v2[2])
                            if _field_class(v2[1]) != "dynamic":
                                static_prefix += len(v2[2])
                            out["fields"].append({
                                "slide": idx, "member": part, "object_path": cur_path,
                                "para_idx": para_idx, "event_idx": ei,
                                "fld_id": v2[0], "type": v2[1],
                                "class": _field_class(v2[1]),
                                "display": v2[2],
                            })
                        # omml 不贡献文本前缀（它不是文本），仅登记身份。
                    # 段落事件台账：原始顺序、原始类型，不做任何合并或规范化
                    for ei, (k2, v2) in enumerate(events):
                        if k2 == "text":
                            out["para_events"][ev_key].append(
                                {"kind": "text", "event_idx": ei, "text": v2})
                        elif k2 == "br":
                            out["para_events"][ev_key].append(
                                {"kind": "br", "event_idx": ei})
                        elif k2 == "fld":
                            cls = _field_class(v2[1])
                            out["para_events"][ev_key].append(
                                {"kind": "fld", "event_idx": ei, "fld_id": v2[0],
                                 "type": v2[1], "class": cls, "display": v2[2]})
                        elif k2 == "omml":
                            # OMML 进事件台账：候选丢公式时 para_events 能查出来。
                            out["para_events"][ev_key].append(
                                {"kind": "omml", "event_idx": ei,
                                 "form": v2["form"],
                                 "omath_count": v2["omath_count"],
                                 "object_path": cur_path, "para_idx": para_idx})
                            out["omml"].append({
                                "slide": idx, "member": part,
                                "object_path": cur_path, "para_idx": para_idx,
                                "event_idx": ei, "form": v2["form"],
                                "omath_count": v2["omath_count"],
                                "support_status": "unsupported",
                                "detail": "识别到 OMML 公式；当前不支持转换，"
                                          "原始字节保留，不得以普通文本等价宣称通过。",
                            })
        walk(sp_tree, "", {})
        # OMML 交叉核对：整页 findall 应与 walk 内登记一致。
        # 若不一致，说明 OMML 落在了 txBody 之外（walk 只进 txBody），
        # 这类 OMML 仍要登记，并标注"对象外"以防保真比较漏掉。
        omath_total = len(root.findall(f".//{{{M}}}oMath"))
        omath_para_total = len(root.findall(f".//{{{M}}}oMathPara"))
        walked = [o for o in out["omml"] if o.get("member") == part]
        walked_omath = sum(o["omath_count"] for o in walked
                          if o["form"] == "oMathPara") + sum(
            o["omath_count"] for o in walked if o["form"] == "oMath")
        if omath_total != walked_omath or omath_para_total != sum(
                1 for o in walked if o["form"] == "oMathPara"):
            out["omml"].append({
                "slide": idx, "member": part, "object_path": None,
                "para_idx": None, "event_idx": None,
                "form": "oMathPara" if omath_para_total else "oMath",
                "omath_count": omath_total,
                "support_status": "unsupported",
                "out_of_txbody": True,
                "detail": "整页 OMML 计数与 txBody 内登记不一致；"
                          "部分公式不在任何文本正文的段落事件序列里，"
                          "保真比较无法定位，单独标注。",
            })
        out["slides"].append(slide_rec)
    out["counts"] = {"fields": len(out["fields"]),
                     "soft_breaks": len(out["soft_breaks"]),
                     "omml": len(out["omml"])}
    return out


def content_findings(cc: dict) -> list:
    """内容级普查 → findings：OMML 与未知字段按输入边界策略进 REVIEW（不静默遗漏）。

    动态字段（页码/日期）按冻结规则允许变化，不产生 finding；
    业务显示内容由内容保真检查负责。
    """
    findings = []
    for o in cc.get("omml", []):
        findings.append({
            "code": "REVIEW.SEMANTIC_AMBIGUITY", "severity": "REVIEW",
            "origin": "raw_census_content", "slide": o["slide"],
            "reason_code": "OMML.UNSUPPORTED",
            "expected": "公式被识别并明确登记", "actual": o,
            "detail": f"第 {o['slide']} 页存在 OMML 公式；当前不支持公式转换，"
                      "原始字节保留。不能保证保真的变换不得按原承诺范围放行。",
        })
    for f in cc.get("fields", []):
        if f["class"] == "unknown":
            findings.append({
                "code": "REVIEW.SEMANTIC_AMBIGUITY", "severity": "REVIEW",
                "origin": "raw_census_content", "slide": f["slide"],
                "reason_code": "FIELD.UNKNOWN_TYPE",
                "expected": "字段类型已分类（业务显示/动态/未知）", "actual": f,
                "detail": f"第 {f['slide']} 页存在未知类型字段（type={f['type']!r}）；"
                          "实际允许哪些变化由冻结任务规则决定，先进入复核。",
            })
    return findings
