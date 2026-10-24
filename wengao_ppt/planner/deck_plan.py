"""W2-E2　DeckPlan / PageSpec（契约 C5）与 plan_deck（契约 C6）。

规划是**确定性规则**：材料 → 事实台账 → 分节 → 按容量分页 →
插入 ResearchBundle.views 的对比/趋势/洞察页 → 图片槽位。

事实保护语义：
- 页面上的每个数字都来自台账（`facts[].value` / 原句）或 bundle.facts；
  规划器不生成、不估算、不改写数字；
- `conflict_decisions`（W4 写盘）：`open` 时采用网络值并标"待确认"，
  不覆盖用户值；`use_mine` 用用户值；`use_web` 用网络值；
- 对比/趋势/洞察页放在提到该指标的页面之后；找不到锚点进"补充对比"小节；
- 图片槽位 `scene` 由现有 `semantic_scenes`（W1 域）生成；规划期不可用时
  scene=None 并登记 LAYOUT.SCENE_REQUEST_UNAVAILABLE，不重造。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .. import page_catalog
from ..layout import engine, tokens
from . import fact_ledger as FL

SCHEMA = "rs.deck_plan.v1"

IMAGE_ROLES = {"content", "compare", "insight", "image"}
ROLE_TYPE_KEY = {  # role → page_catalog key（容量计算用）
    "cover": "cover", "section": "toc_section", "content": "single_column",
    "compare": "double_column", "trend": "chart", "insight": "metrics",
    "image": "image_caption", "process": "flow", "timeline": "timeline",
    "table": "table", "quote": "conclusion", "closing": "summary_action",
}


def _emit(emit, stage, status, summary, **kw):
    if emit is None:
        return
    try:
        emit({"stage": stage, "status": status, "summary": summary, **kw})
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- 数字保护
_NUM_TOKEN = re.compile(FL.NUM_RE)


def collect_grounded_numbers(facts: list[dict], bundle: dict | None) -> set[str]:
    """所有允许出现在稿件里的数字 token（台账原值 + 主体/原句 + bundle facts 值）。

    主体字符串（如"第 1 段"）与日期不是数据，不参与溯源，也不被扫描
    （见 numbers_in_plan_ungrounded 的预处理）。
    """
    allowed: set[str] = set()
    for f in facts or []:
        for key in ("value", "raw_value_text", "subject"):
            v = str(f.get(key) or "").strip()
            if v:
                allowed.add(v)
                for m in _NUM_TOKEN.finditer(v):
                    allowed.add(m.group(0).strip())
        sent = str((f.get("source_ref") or {}).get("sentence") or "")
        for m in _NUM_TOKEN.finditer(sent):
            allowed.add(m.group(0).strip())
    b = bundle or {}
    for f in b.get("facts") or []:
        v = f.get("value")
        if v is not None:
            allowed.add(str(v))
            allowed.add(f"{v:g}")
        if f.get("period"):
            allowed.add(str(f["period"]).strip())
    # views 的数值由 W3 产生并经 verify_bundle 重算；引用其值与周期视为已溯源
    for v in b.get("views") or []:
        if v.get("period"):
            allowed.add(str(v["period"]).strip())
        for s in v.get("series") or []:
            for pt in s.get("points") or []:
                if pt.get("value") is not None:
                    allowed.add(str(pt["value"]))
                    allowed.add(f"{pt['value']:g}")
                if pt.get("x") is not None:
                    allowed.add(str(pt["x"]).strip())
        # view title 与 footnote 中的数字同样可信（来自 research bundle）
        if v.get("title"):
            for m in re.finditer(r"\d+(?:\.\d+)?", v["title"]):
                allowed.add(m.group(0).strip())
        if v.get("footnote", {}).get("text"):
            for m in re.finditer(r"\d+(?:\.\d+)?", v["footnote"]["text"]):
                allowed.add(m.group(0).strip())
    return allowed


#: 非数据数字：ISO 日期、独立年份/带年份/季度、分页序号"（1/3）"——结构性文本，不溯源
_NON_DATA_PATTERNS = (
    re.compile(r"\d{4}-\d{2}-\d{2}"),           # ISO 日期
    re.compile(r"（\d+/\d+）"),                  # 同节分页序号
    re.compile(r"(19|20)\d{2}(?:Q[1-4])?年?"),  # 年份（含季度/中文"年"）
    re.compile(r"\bQ[1-4]\b"),                  # 季度标记 Q1-Q4
)


def _strip_non_data(text: str) -> str:
    t = str(text or "")
    for p in _NON_DATA_PATTERNS:
        t = p.sub(" ", t)
    return t


def numbers_in_plan_ungrounded(plan: dict, allowed: set[str]) -> list[dict]:
    """扫描 plan 里所有文本，找出不在允许集合里的数字 token（模型补写 = 缺陷）。"""
    bad = []

    def scan(text, where):
        for m in _NUM_TOKEN.finditer(_strip_non_data(text)):
            tok = m.group(0).strip()
            if not m.group("num"):
                continue
            if tok not in allowed and str(float(tok.replace(",", ""))) not in allowed:
                bad.append({"where": where, "number": tok})

    for page in plan.get("pages", []):
        scan(page.get("title") or "", f"{page.get('page_id')}.title")
        for b in page.get("blocks") or []:
            scan(b.get("text") or "", f"{page.get('page_id')}.block")
            for it in b.get("items") or []:
                scan(it, f"{page.get('page_id')}.bullet")
            for row in b.get("rows") or []:
                scan(" ".join(str(c) for c in row), f"{page.get('page_id')}.table")
            for s in b.get("series") or {}:
                pass
            payload = b
            for v in (payload.get("series") or {}).values() if isinstance(payload, dict) else []:
                for vv in v or []:
                    scan(str(vv), f"{page.get('page_id')}.chart")
            if b.get("value") is not None:
                scan(str(b.get("value")), f"{page.get('page_id')}.bignumber")
        fn = page.get("footnote") or {}
        scan(fn.get("text") or "", f"{page.get('page_id')}.footnote")
    return bad


# ---------------------------------------------------------------- 分节与分页
def _sections_from_facts(facts: list[dict]) -> list[dict]:
    """按 subject 聚节；保持首次出现顺序。"""
    order: list[str] = []
    buckets: dict[str, list] = {}
    for f in facts:
        s = f.get("subject") or "unknown"
        if s not in buckets:
            buckets[s] = []
            order.append(s)
        buckets[s].append(f)
    return [{"subject": s, "facts": buckets[s]} for s in order]


def _units_per_page(key: str) -> int:
    pt = page_catalog.PAGE_TYPES[key]
    return pt.capacity["mid"]


#: 双栏版式的栏间距（与 engine.flow_blocks 的 two_col 保持一致）
_COL_GUTTER_EMU = int(0.35 * tokens.EMU_PER_IN)


def _content_region():
    """content 页（带页脚）的正文区域——切页容量按真实区域实测。"""
    return engine.page_regions(has_footnote=True).content


def _bullets_height_emu(items: list[str], width_emu: int) -> int:
    """要点列表在给定宽度下的实测高度（含 bullet 前缀与段间距）。"""
    role = tokens.TYPE_ROLES["body"]
    total = 0
    for it in items:
        total += engine.measure.block_height_emu(
            f"· {it}", width_emu, role.pt, role.line_height, role.space_after_pt)
    return total


def _greedy_capacity(units: list[dict], width_emu: int, height_emu: int) -> int:
    """单栏能放下的最多单元数（贪心实测，不拍脑袋）。"""
    used, n = 0, 0
    for u in units:
        h = _bullets_height_emu([u["sentence"]], width_emu)
        if n and used + h > height_emu:
            break
        used += h
        n += 1
    return max(1, n)


def _content_pages_for_section(section: dict, gtitle2facts: dict) -> list[dict]:
    """把一节的事实切成 content 页（确定性，无"（续）"标题）。

    容量按**真实版面实测**：先试单栏，放不下再试双栏，都放不下才按双栏
    容量切页。每页标题 = 节标题（同节多页时加序号"节标题 (2/3)"）。
    """
    facts = section["facts"]
    subject = section["subject"]
    region = _content_region()
    # 数值事实优先按句聚合，定性事实逐句；每"句"算 1 单元
    units: list[dict] = []
    seen_sent = set()
    for f in facts:
        sent = str((f.get("source_ref") or {}).get("sentence") or "").strip()
        if not sent or sent in seen_sent:
            continue
        # 合成样例/演示用途等元信息句不进正文（台账口径不变，只是不渲染）
        if FL.is_meta_sentence(sent):
            continue
        seen_sent.add(sent)
        units.append({"sentence": sent, "fact_ids": [f["fact_id"]]})
    # 同句多数值事实合并 fact_ids
    merged: list[dict] = []
    for u in units:
        if merged and merged[-1]["sentence"] == u["sentence"]:
            merged[-1]["fact_ids"] += u["fact_ids"]
        else:
            merged.append(u)
    if not merged:
        return []

    col_w = region.w
    two_col_w = (region.w - _COL_GUTTER_EMU) // 2
    cap_single = _greedy_capacity(merged, col_w, region.h)
    cap_double = _greedy_capacity(merged, two_col_w, region.h)

    if len(merged) <= cap_single:
        chunks, variant = [merged], "a"
    elif len(merged) <= cap_double * 2:
        chunks, variant = [merged], "b"
    else:
        per = max(1, cap_double * 2)
        chunks = [merged[i:i + per] for i in range(0, len(merged), per)]
        variant = "b"
    pages = []
    total = len(chunks)
    for pi, chunk in enumerate(chunks):
        title = subject if total == 1 else f"{subject}（{pi + 1}/{total}）"
        pages.append({"title": title, "units": chunk, "variant": variant})
    return pages


# ---------------------------------------------------------------- 视图锚定
def _find_anchor_page(pages: list[dict], metric: str) -> int | None:
    """返回提到该指标的最后一个内容页的 plan 页号（插入点之后）。"""
    if not metric:
        return None
    key = str(metric).strip().lower()
    last = None
    for i, p in enumerate(pages):
        hay = " ".join([str(p.get("title") or "")] +
                       [str(u.get("sentence") or "") for u in p.get("units") or []]
                       ).lower()
        if key in hay:
            last = i
    return last


def _view_page_spec(view: dict, decisions: dict | None) -> dict | None:
    """把 ResearchBundle.view 转成页 spec（compare/trend/insight）。

    数字只能来自 view.series[].points[].value（它们本身来自 facts）。
    """
    kind = view.get("kind")
    series = view.get("series") or []
    fact_refs: list[str] = []
    for s in series:
        for pt in s.get("points") or []:
            if pt.get("fact_ref"):
                fact_refs.append(pt["fact_ref"])
    foot = view.get("footnote") or {}
    footnote = {"text": foot.get("text") or "", "fact_refs": foot.get("fact_refs") or fact_refs}
    title = str(view.get("title") or view.get("metric") or "")
    pending = False
    # 冲突标记：view 有 conflicts 且 decision 为 open → 相关系列标"待确认"
    conflicts = view.get("conflicts") or []
    dec = decisions or {}
    labels = []
    for s in series:
        label = str(s.get("label") or "")
        if label:
            labels.append(label)
    if kind == "comparison":
        if not series or not series[0].get("points"):
            return None
        pts = series[0]["points"]
        cats = [str(p.get("x")) for p in pts]
        sdict = {}
        for s in series:
            lab = str(s.get("label") or "值")
            pts_s = s.get("points") or []
            sdict[lab] = [p.get("value") for p in pts_s]
        block = {"kind": "chart", "chart_kind": "bar",
                 "categories": cats,
                 "series": sdict,
                 "legend": len(sdict) > 1,
                 "highlight_series": None, "fact_refs": fact_refs,
                 "h_emu": int(3.0 * tokens.EMU_PER_IN)}
        return {"role": "compare", "variant": "b", "title": title,
                "blocks": [block], "footnote": footnote,
                "conflicts": conflicts,
                "research_view_ref": view.get("view_id")}
    if kind == "trend":
        pts_all = [(s.get("label") or "series", s.get("points") or []) for s in series]
        if not pts_all or all(not p for _, p in pts_all):
            return None
        cats = [str(p.get("x")) for p in pts_all[0][1]]
        sdict = {lab: [p.get("value") for p in pts] for lab, pts in pts_all}
        block = {"kind": "chart", "chart_kind": "line", "categories": cats,
                 "series": sdict, "legend": len(sdict) > 1,
                 "fact_refs": fact_refs,
                 "h_emu": int(3.2 * tokens.EMU_PER_IN)}
        return {"role": "trend", "variant": "a", "title": title,
                "blocks": [block], "footnote": footnote,
                "research_view_ref": view.get("view_id")}
    if kind == "insight":
        hl = view.get("highlight") or {}
        pts = series[0]["points"] if series else []
        value = None
        if pts:
            value = pts[-1].get("value")
        if value is None:
            return None
        big = {"kind": "bignumber", "value": str(value),
               "label": str(view.get("metric") or ""),
               "note": str(hl.get("text") or ""),
               "fact_refs": hl.get("fact_refs") or fact_refs}
        if conflicts:
            for c in conflicts:
                if str(dec.get(c.get("conflict_id"), "")) == "open":
                    pending = True
        big["pending"] = pending
        return {"role": "insight", "variant": "a", "title": title,
                "blocks": [big], "footnote": footnote,
                "research_view_ref": view.get("view_id")}
    return None


def _supplement_section(views: list[dict], decisions: dict | None) -> list[dict]:
    specs = []
    for v in views:
        spec = _view_page_spec(v, decisions)
        if spec:
            specs.append(spec)
    return specs


# ---------------------------------------------------------------- 图片槽位
def _scene_request_skeleton(page_id: str, slot_id: str, language: str,
                            title: str, sentences: list[str]) -> dict:
    """SceneRequest 骨架（C3）。visual_query/anchors 由现有 semantic_scenes
    （W1 域，模型驱动）填充；规划器**不重造**锚点，只带话题上下文。"""
    return {
        "schema": "rs.scene_request.v1",
        "scene_id": slot_id, "page_id": page_id, "slot_id": slot_id,
        "visual_query": "",          # 由 semantic_scenes 填充（中性英文词）
        "anchors": {"subject": "", "action": "", "setting": ""},
        "language": language or "zh",
        "aspect": "16:9", "orientation": "landscape",
        "requires_real_photo": False,
        "user_image": {"path": None, "sha256": None},
        "exclude_sha256": [],
        "context_topic": str(title or "")[:60],
        "context_sentences": [s[:80] for s in sentences[:3]],
    }


def _image_slot(page: dict, page_id: str, language: str, images_enabled: bool,
                warnings: list) -> dict | None:
    if not images_enabled or page["role"] not in IMAGE_ROLES:
        return None
    slot_id = f"{page_id}-img1"
    sentences = [str(u.get("sentence") or "") for u in page.get("units") or []]
    scene = _scene_request_skeleton(page_id, slot_id, language,
                                    page.get("title") or "", sentences)
    warnings.append({"code": "LAYOUT.SCENE_REQUEST_UNAVAILABLE",
                     "detail": f"{slot_id} 的 visual_query/anchors 待 semantic_scenes 填充"})
    return {"slot_id": slot_id, "aspect": "16:9", "scene": scene}


# ---------------------------------------------------------------- plan_deck
def _read_material_text(material) -> str:
    """str 既可能是文件路径也可能是材料正文。

    只有"像路径且确实存在"才按文件读；多行长文本绝不当路径（否则在部分
    平台会直接抛 File name too long）。
    """
    p = Path(material)
    looks_like_path = ("\n" not in str(material) and len(str(material)) < 4096)
    if looks_like_path and p.is_file():
        return p.read_text(encoding="utf-8")
    return str(material)


def plan_deck(material, bundle, decisions, options, job_dir, emit=None) -> dict:
    """契约 C6：material → DeckPlan。bundle 可为 None。

    options：JobOptions dict（C1）。job_dir：任务目录（证据落盘）。
    """
    opts = options or {}
    language = opts.get("language") or "zh"
    images_enabled = bool((opts.get("images") or {}).get("enabled", True))
    target_pages = opts.get("target_pages") or None
    warnings: list[dict] = []

    # 1) 材料与台账
    if isinstance(material, (str, Path)):
        text = _read_material_text(material)
        facts = FL.ledger_from_plain_text(text)
    else:
        facts = FL.build_fact_ledger(material)
    if not facts:
        raise ValueError("材料为空或未抽出任何必讲事实")

    b = bundle or {}
    views = b.get("views") or []
    decisions = decisions or {}

    # 2) 分节 → content 页
    sections = _sections_from_facts(facts)
    content_pages: list[dict] = []
    used_subjects: list[str] = []
    for sec in sections:
        pgs = _content_pages_for_section(sec, {})
        if not pgs:
            # 该节没有可渲染单元（如整节都是合成样例元信息）：不建节，
            # 也不让它把封面标题带成"第 N 段"这种无意义标题。
            continue
        used_subjects.append(sec["subject"])
        content_pages += pgs

    # 3) 视图锚定：插到提到该指标的最后一个内容页之后；无锚点进"补充对比"
    supplement: list[dict] = []
    view_specs: list[tuple[int | None, dict]] = []
    for v in views:
        spec = _view_page_spec(v, decisions)
        if spec is None:
            warnings.append({"code": "RESEARCH.VIEW_NOT_RENDERABLE",
                             "detail": f"view {v.get('view_id')} 缺数据，按宁缺勿编省略"})
            continue
        anchor = _find_anchor_page(content_pages, v.get("metric") or v.get("title"))
        view_specs.append((anchor, spec))

    # 4) 组装页序：cover → [content (+views)] → [补充对比] → closing
    pages: list[dict] = []
    pages.append({"role": "cover", "variant": "a",
                  "title": used_subjects[0] if used_subjects else "",
                  "blocks": [{"kind": "text", "text": "稳稿 PPT"}],
                  "units": []})
    supplement_used = False
    for idx, cp in enumerate(content_pages):
        pid = f"p{len(pages) + 1:02d}"
        block = {"kind": "bullets", "items": [u["sentence"] for u in cp["units"]],
                 "fact_refs": [fid for u in cp["units"] for fid in u["fact_ids"]]}
        page = {"role": "content", "variant": cp.get("variant") or "a",
                "page_type": "double_column" if (cp.get("variant") == "b") else "single_column",
                "title": cp["title"], "blocks": [block], "units": cp["units"],
                "page_id": pid, "index": len(pages) + 1}
        pages.append(page)
        # 该页之后要插的视图
        for anchor, spec in view_specs:
            if anchor == idx:
                spec = dict(spec)
                spec["page_id"] = f"p{len(pages) + 1:02d}"
                spec["index"] = len(pages) + 1
                spec["units"] = []
                pages.append(spec)
    trailing = [spec for anchor, spec in view_specs if anchor is None]
    if trailing:
        supplement_used = True
        pages.append({"role": "section", "variant": "a", "title": "补充对比",
                      "blocks": [], "units": [],
                      "page_id": f"p{len(pages) + 1:02d}",
                      "index": len(pages) + 1})
        for spec in trailing:
            spec = dict(spec)
            spec["page_id"] = f"p{len(pages) + 1:02d}"
            spec["index"] = len(pages) + 1
            spec["units"] = []
            pages.append(spec)
    pages.append({"role": "closing", "variant": "b", "title": "小结与行动",
                  "blocks": [{"kind": "bullets",
                              "items": used_subjects[:4]}],
                  "units": [], "page_id": f"p{len(pages) + 1:02d}",
                  "index": len(pages) + 1})

    # 统一补齐 page_id / index（覆盖/插页分支之外的页也必须有）
    for i, page in enumerate(pages, start=1):
        page.setdefault("page_id", f"p{i:02d}")
        page.setdefault("index", i)

    # 5) 图片槽位
    if images_enabled:
        for page in pages:
            slot = _image_slot(page, page["page_id"], language, True, warnings)
            if slot:
                page["image_slots"] = [slot]

    plan = {
        "schema": SCHEMA, "deck_id": f"dp-{abs(hash(tuple(p['page_id'] for p in pages)))}"[:16],
        "language": language, "mode": opts.get("mode") or "generate",
        "job_dir": str(job_dir) if job_dir else None,
        "pages": pages,
        "warnings": warnings,
        "stats": {
            "facts_total": len(facts),
            "facts_numeric": sum(1 for f in facts if not f.get("qualitative")),
            "facts_qualitative": sum(1 for f in facts if f.get("qualitative")),
            "pages_total": len(pages),
            "views_total": len(views),
            "views_rendered": sum(1 for _, s in view_specs),
            "supplement_section": supplement_used,
        },
    }

    # 6) 事实保护自检：规划里的数字必须都能溯源
    allowed = collect_grounded_numbers(facts, b)
    bad = numbers_in_plan_ungrounded(plan, allowed)
    if bad:
        plan["warnings"].append({"code": "LAYOUT.NUMBER_NOT_GROUNDED",
                                 "detail": f"{len(bad)} 个数字无法溯源"})
        raise ValueError(f"规划出现无法溯源的数字（模型补写视为缺陷）：{bad[:5]}")

    # 7) 证据落盘
    if job_dir:
        ev = Path(job_dir) / "evidence" / "layout"
        ev.mkdir(parents=True, exist_ok=True)
        (ev / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
        (ev / "fact_ledger.json").write_text(
            json.dumps(facts, ensure_ascii=False, indent=1), encoding="utf-8")
    _emit(emit, "layout", "done",
          f"规划完成：{len(pages)} 页，{plan['stats']['facts_total']} 条必讲事实",
          pages=len(pages))
    return plan
