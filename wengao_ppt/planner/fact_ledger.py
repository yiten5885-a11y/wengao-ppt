"""W2-E1　事实台账（自 `samples/r5_material_to_ppt.py` 拆出，语义保持不变）。

保留的事实保护语义（W2 指令 §3）：
- 数值事实：句中含数字 token（NUM_RE 口径不变）；
- 定性事实：无数字的完整句子（监督 2026-10-20：不能只保数字）；
- 主体取页面标题，纯文本回退"第 N 段"；
- source_ref 保留 文件 / 行号区间 / 标题层级，满足 source_ref_spec 最小定位要求。

本模块是**确定性规则**，不经任何模型。
"""
from __future__ import annotations

import re
import unicodedata

# ---- 常量自样例脚本原样搬运（不改口径） ------------------------------
NUM_RE = re.compile(
    r"(?P<num>-?\d[\d,]*\.?\d*)\s*"
    r"(?P<unit>%|个百分点|亿元|万元|亿|万|元|人|家|天|小时|分钟|倍|台|次|分)?")
NEG_RE = re.compile(r"(下降|减少|降低|下滑|亏损|负增长|减少额)")
_SENT_END = "。！？"
_META_SENTENCE_CUES = ("合成样例", "均为虚构", "不对应任何真实",
                       "不得作为真实业务结论", "本示例", "演示用途")
_QUAL_MIN_LEN = 10


def _as_dict(o):
    import dataclasses
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return dataclasses.asdict(o)
    if isinstance(o, dict):
        return o
    return {k: getattr(o, k) for k in dir(o)
            if not k.startswith("_") and not callable(getattr(o, k))}


def _page_segments(pd) -> list[dict]:
    """连续 body 单元拼接成段（跨行不断句），heading 切段。与样例脚本一致。"""
    segs: list[dict] = []
    for raw_unit in (pd.get("units") or []):
        unit = _as_dict(raw_unit)
        text = (unit.get("text") or "").strip()
        role = unit.get("role")
        if not text:
            continue
        if role == "heading":
            segs.append({"kind": "heading", "text": text})
            continue
        ref = _as_dict(unit.get("source_ref") or {})
        if segs and segs[-1]["kind"] == "body":
            segs[-1]["text"] += "\n" + text
        else:
            segs.append({"kind": "body", "text": text, "ref": ref, "unit": unit})
    return segs


def split_sentences(text: str) -> list[str]:
    """按句末标点切完整句；换行是软分隔。与样例脚本一致。"""
    text = text or ""
    out, start = [], 0
    for i, ch in enumerate(text):
        if ch in _SENT_END:
            seg = text[start:i + 1]
            if seg.strip():
                out.append(seg)
            start = i + 1
    tail = text[start:]
    if tail.strip():
        out.append(tail)
    return [re.sub(r"\s*\n\s*", "", s).strip() for s in out if s.strip()]


def is_meta_sentence(sent: str) -> bool:
    """合成样例/演示用途等元信息句：可进台账，但不该作为正文渲染。"""
    return any(c in sent for c in _META_SENTENCE_CUES)


#: 兼容旧名（模块内原为私有）
_is_meta_sentence = is_meta_sentence


def build_fact_ledger(doc) -> list[dict]:
    """从既有提取结果构建必讲事实候选台账（确定性规则，不经模型）。

    doc：`extractors.extract_any` 的返回对象（pages[].units[] + source_ref）；
    也接受 {"pages": [...]} 形状的 dict。
    """
    pages = getattr(doc, "pages", None)
    if pages is None and isinstance(doc, dict):
        pages = doc.get("pages")
    facts: list[dict] = []
    for page in (pages or []):
        pd = _as_dict(page)
        page_title = (pd.get("title") or "").strip()
        page_no = pd.get("page_no")
        subject = page_title or f"第 {page_no} 段"
        for seg in _page_segments(pd):
            if seg["kind"] != "body":
                continue
            ref = seg["ref"]
            locator = (f"line {ref.get('line_start')}-{ref.get('line_end')}"
                       + (f" / {ref.get('heading')}" if ref.get("heading") else ""))
            text = seg["text"]
            sentences = split_sentences(text)
            covered_sentences = set()
            for sent in sentences:
                if not NUM_RE.search(sent):
                    continue
                for m in NUM_RE.finditer(sent):
                    unit_txt = m.group("unit") or ""
                    val = m.group("num")
                    if not unit_txt and not val:
                        continue
                    facts.append({
                        "fact_id": f"F{len(facts)+1:03d}",
                        "subject": subject[:40],
                        "raw_value_text": m.group(0).strip(),
                        "value": val,
                        "unit_class": unit_txt or "unknown",
                        "time": "unknown",
                        "scope": "unknown",
                        "qualifier": "unknown",
                        "negation_or_comparison": ("neg" if NEG_RE.search(sent)
                                                   else "unknown"),
                        "source_ref": {
                            "file": ref.get("file"),
                            "line_start": ref.get("line_start"),
                            "line_end": ref.get("line_end"),
                            "heading": ref.get("heading"),
                            "locator": locator,
                            "unit_id": (seg.get("unit") or {}).get("id"),
                            "sentence": sent[:200],
                        },
                    })
                covered_sentences.add(sent)
            for sent in sentences:
                core = sent.strip("。！？")
                if NUM_RE.search(sent) or len(core) < _QUAL_MIN_LEN:
                    continue
                if _is_meta_sentence(sent) or sent in covered_sentences:
                    continue
                facts.append({
                    "fact_id": f"F{len(facts)+1:03d}",
                    "subject": subject[:40],
                    "raw_value_text": "",
                    "value": "",
                    "unit_class": "qualitative",
                    "qualitative": True,
                    "time": "unknown",
                    "scope": "unknown",
                    "qualifier": "unknown",
                    "negation_or_comparison": ("neg" if NEG_RE.search(sent)
                                               else "unknown"),
                    "source_ref": {
                        "file": ref.get("file"),
                        "line_start": ref.get("line_start"),
                        "line_end": ref.get("line_end"),
                        "heading": ref.get("heading"),
                        "locator": locator,
                        "unit_id": (seg.get("unit") or {}).get("id"),
                        "sentence": sent[:200],
                    },
                })
    return facts


# ---- 纯文本输入的轻量分页（txt 没有 pages 结构时） --------------------
#: 标题行判定：短、无任何句读、后面还有正文行。宁可漏判也不把正文当标题。
_HEADING_MAX_LEN = 30
_HEADING_FORBIDDEN_TAIL = "。！？；，、！？;.,：:）)】」”\"'"
#: markdown 标题/引用前缀（回放材料多为 .md，标题常与正文之间空一行）
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+")
_MD_QUOTE_RE = re.compile(r"^>\s?")
#: 编号标题（"一、xxx" / "1. xxx" / "1、xxx"）是强信号，直接算标题——
#: 否则中文枚举逗号"、"会被上面的句读黑名单误杀。
_NUMBERED_HEADING_RE = re.compile(r"^(?:[一二三四五六七八九十百]+、|\d+\s*[\.、．])\s*\S")


def _strip_md_markers(line: str) -> str:
    """去掉 markdown 标题/引用前缀，仅用于标题判定与标题文本。"""
    t = (line or "").strip()
    t = _MD_HEADING_RE.sub("", t)
    t = _MD_QUOTE_RE.sub("", t)
    return t.strip()


def _looks_like_heading(line: str, has_next_body: bool) -> bool:
    t = _strip_md_markers(line)
    if not t or not has_next_body:
        return False
    if _NUMBERED_HEADING_RE.match(t):
        return True
    if len(t) > _HEADING_MAX_LEN:
        return False
    if any(ch in t for ch in "。！？；，、!?;,:"):
        return False
    if t[-1] in _HEADING_FORBIDDEN_TAIL:
        return False
    # 形如 "1. xxx" / "一、xxx" 的编号行也算标题
    return True


def ledger_from_plain_text(text: str, *, source_name: str = "material.txt") -> list[dict]:
    """把纯文本按段落切块，构造与 extract_any 兼容的 doc 后建账。

    分段规则：**独立成行的短行视为小标题**，从它开始新起一页并以该行作为
    subject——否则整篇材料只会得到"第 1 段"这种无意义标题，封面与节页都会
    跟着退化。标题判定是**全文级**的：markdown 材料的 `## 标题` 与正文之间
    常空一行，若只在空行块内找标题会全部漏判，因此这里对后续任意非空行
    做前瞻；同时剥掉 `#` / `>` 前缀再判定。
    """
    lines = str(text or "").splitlines()
    # (行号, 去 markdown 前缀后的文本, 原文本是否非空)
    norm: list[tuple[int, str, bool]] = []
    for idx, line in enumerate(lines, start=1):
        norm.append((idx, _strip_md_markers(line), bool(line.strip())))
    # 后续是否还有非空行（前瞻与原文一致，但不再受空行分块限制）
    nxt_nonempty = [False] * len(norm)
    seen = False
    for i in range(len(norm) - 1, -1, -1):
        nxt_nonempty[i] = seen
        if norm[i][2]:
            seen = True

    groups: list[dict] = []   # {"title": str|None, "lines": [(ln, t), ...]}
    for i, (ln, t, nonempty) in enumerate(norm):
        if not nonempty:
            continue
        if _looks_like_heading(t, nxt_nonempty[i]):
            groups.append({"title": t, "lines": []})
        elif groups:
            groups[-1]["lines"].append((ln, t))
        else:
            groups.append({"title": None, "lines": [(ln, t)]})

    pages: list[dict] = []
    for grp in groups:
        body = grp["lines"]
        if not body and grp["title"] is None:
            continue
        units = [{"id": f"u{len(pages)+1}-{k}", "role": "body", "text": t,
                  "source_ref": {"file": source_name, "line_start": ln,
                                 "line_end": ln,
                                 "heading": grp["title"] or ""}}
                 for k, (ln, t) in enumerate(body)]
        pages.append({
            "page_no": len(pages) + 1,
            "title": grp["title"] or f"第 {len(pages) + 1} 段",
            "units": units,
            "source_text": "\n".join(t for _, t in body),
        })
    if not pages:
        pages = [{"page_no": 1, "title": "第 1 段", "units": [], "source_text": ""}]

    class _Doc:
        pass

    d = _Doc()
    d.pages = pages
    return build_fact_ledger(d)


def title_visible_units(value: str) -> int:
    """可见标题字符数：组合音标（Mn/Mc）不计数。与样例脚本一致。"""
    return sum(1 for ch in str(value or "")
               if not unicodedata.category(ch).startswith(("M", "C")))
