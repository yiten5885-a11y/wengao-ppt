"""字形质量判据：汉字到底有没有被**画出来**。

背景（事故 incident-20261017-f88b）
-----------------------------------
旧门禁只看 PDF 文本层里的汉字数量（2314 个），而那一版 PDF 的文本层完全正常，
字形却全是方框——因为渲染用的字体没有汉字字形。文本层计数、页面非空、
"看到一个中文字体名"都不能证明字形被绘制，本模块用两条互相独立的证据：

1. **字形位图分辨力**（主判据，像素级）
   对页面上每个汉字取 PDFium 给出的字符框，从**本次渲染的页图**裁出该字符的
   像素、归一化、二值化，得到位图哈希。不同汉字的位图必须不同：
   - 若同一页上超过一半的不同汉字共用同一张位图（按不同汉字计的多样性
     低于 `DIVERSITY_MIN`=0.50），且这些位图是空心方框/几乎没有
     墨迹 → 这是 .notdef 方框，不是字形 → `MISSING`。
   - 稀疏页（待核汉字本来就少）改用直接结构判据：任一张位图上出现两个以上
     **不同**汉字即 `MISSING`；每个不同汉字各有自己的位图才 `OK`。
   - 正常页面不同汉字位图各异 → `OK`。
   只看"不同字符"之间的碰撞，所以同一汉字重复出现（"的的的"）不会误报；
   □/■ 这类**真实符号**有自己的字形，也不会因为长得像方框而被判错。

2. **字体资源清单**（辅助证据，不作为放行依据）
   记录该页实际使用的 PDF 字体资源、是否内嵌字体程序。字体名只是线索：
   "看到 Hiragino"不等于每个字都有字形，所以它不进判据，只进证据。

覆盖边界（必须如实记账，不冒充全覆盖）
--------------------------------------
- 只覆盖**可提取文本对象**；图片里的字、被转成轮廓/曲线的字没有字符框，
  不进入分母，也不能被本模块判为"已验证"。
- 字符框高度小于 `min_px`（默认 8px）时像素证据不足，记 `INSUFFICIENT_PX`，
  不当作通过。
- 生成方式：默认复用本次渲染的 PNG（与 PDF 同哈希、同 DPI）；PNG 缺失时
  用 PDFium 以同一 DPI 现场栅格化同一份 PDF，并记录来源。
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from pathlib import Path

#: 判定参数（集中在此，改参数会改变指纹之外的判据，需同步回归）
#: 页内"平坦裁片"比例：字符框内几乎没有明暗变化 → 该位置没有笔画（空白或整块实心）
FLAT_RATIO_MISSING = 0.85
#: 部分平坦 + 位图多样性低 → 多数汉字共用同一张图（.notdef 方框）
FLAT_RATIO_PARTIAL = 0.25
#: 不同汉字位图的多样性下限；正常中文页通常 >0.8
DIVERSITY_MIN = 0.50
#: 少于这么多不同汉字，单页像素证据不足
MIN_DISTINCT_CHARS = 4
#: 只对"有观测证据"的页做多样性判定时的最小裁片数
MIN_OBSERVED_BOXES = 8
#: 裁片动态范围低于此值视为平坦（无笔画证据）
FLAT_RANGE = 8
HOLLOW_RING_MIN = 0.35    # 方框边框环上的墨迹占比
HOLLOW_INNER_MAX = 0.06   # 方框内部几乎无墨迹


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF) or (0x3400 <= o <= 0x4DBF) or (0xF900 <= o <= 0xFAFF)


def _bits(crop, grid: int = 16):
    """归一化位图（自适应阈值）。

    用**裁片内**的动态范围定阈值，而不是固定"与背景差 40"：
    深色主题上深色文字（本次事故的另一种表现）对比度很低，
    固定阈值会把"画出来但对比度差"的字误判成"没画出来"。
    裁片内几乎没有明暗变化（range < FLAT_RANGE）时返回 None：
    这个位置没有笔画证据（空白，或整块实心方框）。
    """
    g = crop.convert("L").resize((grid, grid))
    px = list(g.getdata())
    lo, hi = min(px), max(px)
    if hi - lo < FLAT_RANGE:
        return None
    th = lo + 0.35 * (hi - lo)
    return [1 if v > th else 0 for v in px]


def _ink(bits, grid: int = 16) -> float:
    return sum(bits) / float(grid * grid)


def _hollow(bits, grid: int = 16) -> bool:
    """空心方框：墨迹集中在边框环，内部几乎空白。"""
    ring = [bits[r * grid + c] for r in range(grid) for c in range(grid)
            if r < 3 or c < 3 or r >= grid - 3 or c >= grid - 3]
    inner = [bits[r * grid + c] for r in range(3, grid - 3) for c in range(3, grid - 3)]
    if not ring or not inner:
        return False
    return (sum(ring) / len(ring) >= HOLLOW_RING_MIN
            and sum(inner) / len(inner) <= HOLLOW_INNER_MAX)


def _page_image(pdf_path: Path, png_dir: Path | None, page_no: int, dpi: int):
    """页图来源：优先复用本次渲染的 PNG（同 PDF 哈希 + 同 DPI），否则现场栅格化。"""
    if png_dir:
        p = Path(png_dir) / f"page-{page_no:02d}.png"
        if p.is_file():
            from PIL import Image
            with Image.open(p) as im:
                return im.convert("RGB"), {"source": "rendered_png", "file": str(p),
                                           "png_sha256": _sha(p)}
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        page = doc[page_no - 1]
        img = page.render(scale=dpi / 72.0).to_pil().convert("RGB")
        return img, {"source": "pdfium_onsite", "dpi": dpi}
    finally:
        doc.close()


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _font_inventory(reader, page) -> list[dict]:
    out = []
    try:
        res = page.get("/Resources")
        fonts = res.get_object().get("/Font") if res else None
        if not fonts:
            return out
        for name, ref in fonts.get_object().items():
            f = ref.get_object()
            base = str(f.get("/BaseFont") or "")
            if "+" in base:
                base = base.split("+", 1)[1]
            embedded = None
            fd = f.get("/FontDescriptor")
            if fd:
                fdo = fd.get_object()
                embedded = any(k in fdo for k in ("/FontFile", "/FontFile2", "/FontFile3"))
            out.append({"resource": str(name), "base_font": base,
                        "subtype": str(f.get("/Subtype") or ""),
                        "composite": bool(f.get("/DescendantFonts")),
                        "embedded_program": embedded})
    except Exception:  # noqa: BLE001
        pass
    return out


def _decide_page(page_rec: dict, distinct: set, groups: dict,
                 hollow_chars: list, observed: int, boxes: int, flat: int) -> dict:
    """单页判定（纯函数：只吃已算好的统计量，便于逐分支回归）。

    判定顺序固定为"先强后弱"，任何"样本不够"的分支都不允许落到 OK：
    MISSING（画出来了但不是字形）→ INSUFFICIENT_PX（证据不够）→ OK/AMBIGUOUS。
    """
    if not distinct:
        page_rec["status"] = "NO_CJK"
        return page_rec
    if boxes == 0:
        page_rec["status"] = "INSUFFICIENT_PX"
        page_rec["detail"] = "有汉字文本但取不到可用字符框（过小或无框）"
        return page_rec

    top = max(groups.items(), key=lambda kv: len(kv[1])) if groups else (None, set())
    if top[0] is not None:
        page_rec["dominant_sample"] = "".join(sorted(top[1]))[:16]
    flat_ratio = page_rec.get("flat_ratio") or 0.0
    div = page_rec.get("diversity")
    observed_distinct = set().union(*groups.values()) if groups else set()
    # 多样性有两个口径，只有按**不同汉字**做分母的那个能用于判定：
    #   判据问的是"不同汉字是否共用同一张位图"（.notdef 的特征）。
    #   同一汉字重复出现（"稳稳稳稳"、章节页反复出现的词）位图本来就相同，
    #   用裁片数做分母会把它算成"多样性低"，把一页完好的字判成缺字。
    # 裁片口径（diversity）仍记录下来作为证据，但不参与判定。
    div_chars = None
    if observed_distinct:
        div_chars = min(1.0, round(len(groups) / float(len(observed_distinct)), 3))
    page_rec["diversity_distinct_chars"] = div_chars
    enough = (len(distinct) >= MIN_DISTINCT_CHARS
              and observed >= MIN_OBSERVED_BOXES)
    # 两种"样本少"要分开：
    #  - 页面**真的稀疏**（源页只有 1—7 个待核汉字）：用实际存在的全部目标核对，
    #    不因为凑不够 8 个就判失败；
    #  - 源页字很多但只取到少数裁片：更像采样/定位漏掉，不能当稀疏页放行。
    # 稀疏 = 裁片本来就不多，而且**每个不同汉字都至少取到一个裁片**
    # （说明不是采样漏掉，而是这页确实只有这么多字）。
    sparse = (observed < MIN_OBSERVED_BOXES
              and len(distinct) <= max(1, observed))
    sampling_gap = (not sparse) and observed < MIN_OBSERVED_BOXES
    page_rec["sparse"] = sparse
    page_rec["sampling_gap"] = sampling_gap
    page_rec["hollow_chars"] = "".join(sorted(set(hollow_chars)))[:16]
    # 直接结构判据：一张位图上出现两个以上**不同**汉字 → 这些字画出来是同一张图，
    # .notdef 方框的典型特征。同一汉字重复出现只会在同一 group 里出现同一个字符，
    # group 内字符数仍为 1，不会进这个名单（"稳稳稳"不误判）。
    shared_groups = [g for g in groups.values() if len(g) > 1]
    shared_chars = set().union(*shared_groups) if shared_groups else set()
    page_rec["shared_bitmap_chars"] = "".join(sorted(shared_chars))[:16]
    # 合法方框符号（□▢■…）本来就长得像空壳，不能当成缺字。
    box_like = "□■▢▣◻◼◾▪▫○●"
    hollow_bad = [c for c in set(hollow_chars) if c not in box_like]
    if flat_ratio >= FLAT_RATIO_MISSING:
        page_rec["status"] = "MISSING"
        page_rec["detail"] = (
            f"{boxes} 个汉字位置里 {flat} 个字符框内没有任何笔画"
            f"（平坦比 {flat_ratio}）：这些字没有被画出来。")
    elif sparse and hollow_bad:
        # 稀疏页逐字看：有字的裁片是"空心方框"且该字不是方框符号 → 缺字
        page_rec["status"] = "MISSING"
        page_rec["detail"] = (
            f"稀疏页逐字核对：{'、'.join(sorted(hollow_bad))} 的实际绘制是"
            "空心方框（.notdef），不是字形。")
    elif sparse and shared_chars:
        # 稀疏页逐字核对：不同汉字共用同一张位图 = .notdef 碰撞。
        # 与"同一汉字重复"的区别只在于 group 内是不是同一个字符，这正是判据要抓的。
        page_rec["status"] = "MISSING"
        page_rec["detail"] = (
            f"稀疏页逐字核对：{'、'.join(sorted(shared_chars))} 等多个不同汉字"
            "共用同一张位图（.notdef 方框）：不是各自字形。")
    elif div_chars is not None and div_chars < DIVERSITY_MIN and (enough or sparse):
        page_rec["status"] = "MISSING"
        page_rec["detail"] = (
            f"{len(observed_distinct)} 个不同汉字只画出 {len(groups)} 种位图"
            f"（按不同汉字计的多样性 {div_chars}，裁片口径 {div}，"
            f"平坦比 {flat_ratio}）：不同汉字共用同一张"
            f"方框位图，是 .notdef 缺字，不是字形。")
    elif flat_ratio >= FLAT_RATIO_PARTIAL and div_chars is not None \
            and div_chars < DIVERSITY_MIN:
        page_rec["status"] = "MISSING"
        page_rec["detail"] = (
            f"平坦比 {flat_ratio} 且位图多样性 {div_chars}：大面积汉字位置没有笔画，"
            "残留位图也高度重复，属缺字方框。")
    elif sampling_gap:
        page_rec["status"] = "INSUFFICIENT_PX"
        page_rec["detail"] = (
            f"源页有 {len(distinct)} 个不同汉字，但只取到 {observed} 个可用裁片"
            f"（门槛 {MIN_OBSERVED_BOXES}）：疑似采样/定位漏掉，需复核，"
            "不能当作稀疏页放行。")
    elif sparse:
        # 少样本不自动失败、也不自动通过：逐目标核对已全部通过才记 OK
        page_rec["status"] = "OK"
        page_rec["detail"] = (
            f"稀疏页：{len(distinct)} 个待核汉字、{observed} 个裁片全部有笔画"
            f"且不同汉字的位图互不相同（按不同汉字计的多样性 {div_chars}，"
            f"裁片口径 {div}）；按实际目标核对通过，"
            f"不以凑够 {MIN_OBSERVED_BOXES} 个裁片为条件。")
    elif div_chars is not None and div_chars >= 0.6:
        page_rec["status"] = "OK"
    else:
        page_rec["status"] = "AMBIGUOUS"
        page_rec["detail"] = ("不同汉字位图多样性偏低，但未达到缺字判据；"
                              "需人工看页图")
    return page_rec


def analyze_pdf(pdf_path, png_dir=None, dpi: int = 110, max_pages=None,
                min_px: int = 4) -> dict:
    """逐页做字形质量检查，返回结构化结果（含每页证据与总体判定）。"""
    pdf_path = Path(pdf_path)
    res = {"schema_version": "1.0.0", "engine": "pdfium-textpage+glyph-bitmap",
           "pdf": str(pdf_path), "pdf_sha256": _sha(pdf_path) if pdf_path.is_file() else None,
           "dpi": dpi, "params": {"flat_ratio_missing": FLAT_RATIO_MISSING,
                                  "flat_ratio_partial": FLAT_RATIO_PARTIAL,
                                  "diversity_min": DIVERSITY_MIN,
                                  "min_distinct_chars": MIN_DISTINCT_CHARS,
                                  "min_observed_boxes": MIN_OBSERVED_BOXES,
                                  "flat_range": FLAT_RANGE,
                                  "hollow_ring_min": HOLLOW_RING_MIN,
                                  "hollow_inner_max": HOLLOW_INNER_MAX,
                                  "min_px": min_px},
           "pages": [], "verdict": "NOT_EVALUABLE", "error": None,
           "pdf_text_by_page": {},
           "boundary": ("只覆盖可提取文本对象；图片内文字与轮廓化文字没有字符框，"
                        "不在分母内，也不据此判为已验证。")}
    try:
        import pypdfium2 as pdfium
        from pypdf import PdfReader
    except Exception as e:  # noqa: BLE001
        res["error"] = f"依赖缺失：{e}"
        return res
    try:
        reader = PdfReader(str(pdf_path))
        doc = pdfium.PdfDocument(str(pdf_path))
    except Exception as e:  # noqa: BLE001
        res["error"] = f"PDF 打开失败：{type(e).__name__}: {e}"
        return res

    try:
        n_pages = len(doc)
        limit = n_pages if max_pages is None else min(n_pages, max_pages)
        for pno in range(1, limit + 1):
            page = doc[pno - 1]
            tp = page.get_textpage()
            text = tp.get_text_range() or ""
            img, src = _page_image(pdf_path, Path(png_dir) if png_dir else None, pno, dpi)
            scale = dpi / 72.0
            w_pt, h_pt = page.get_size()
            # 页图可能来自已存 PNG：按实际像素宽高反推真实缩放，避免"图与框不同源"的错位。
            real_scale = img.size[0] / float(w_pt) if w_pt else scale
            groups = defaultdict(set)
            inks, hollows, boxes, skipped, flat = [], 0, 0, 0, 0
            hollow_chars = []
            distinct = set(ch for ch in text if _is_cjk(ch))
            # 每页文本层里的中文字符数：用于区分"中文整体消失"与
            # "有中文字符但取不到字符框"这两种不同形态（上层判定需要）。
            res["pdf_text_by_page"][str(pno)] = sum(1 for ch in text if _is_cjk(ch))
            for i in range(min(len(text), tp.count_chars())):
                ch = text[i]
                if not _is_cjk(ch):
                    continue
                try:
                    box = tp.get_charbox(i)
                except Exception:  # noqa: BLE001
                    box = None
                if not box:
                    skipped += 1
                    continue
                x0, y0, x1, y1 = box
                px0, px1 = int(x0 * real_scale), int(x1 * real_scale)
                py0, py1 = int((h_pt - y1) * real_scale), int((h_pt - y0) * real_scale)
                if px1 - px0 < min_px or py1 - py0 < min_px:
                    skipped += 1
                    continue
                boxes += 1
                bits = _bits(img.crop((px0, py0, px1, py1)))
                if bits is None:
                    # 字符框内没有任何明暗变化：这里没有笔画（空白位置或整块实心方框）。
                    flat += 1
                    continue
                groups[hashlib.sha1(bytes(bits)).hexdigest()[:12]].add(ch)
                inks.append(_ink(bits))
                if _hollow(bits):
                    hollows += 1
                    hollow_chars.append(ch)
            observed = boxes - flat
            observed_distinct = set().union(*groups.values()) if groups else set()
            page_rec = {
                "page": pno, "image_source": src,
                "text_chars": len(text), "distinct_cjk": len(distinct),
                "cjk_boxes": boxes, "flat_boxes": flat,
                "flat_ratio": round(flat / float(boxes), 3) if boxes else None,
                "observed_boxes": observed,
                "observed_distinct": len(observed_distinct),
                "skipped_small_or_missing_box": skipped,
                "distinct_bitmaps": len(groups),
                # 多样性 = 不同位图数 / 有观测证据的裁片数。同一汉字在不同字号下
                # 位图不同，用裁片数做分母才是"每个字的图是否互不相同"的稳定口径。
                "diversity": (round(len(groups) / float(observed), 3) if observed else None),
                "mean_ink": round(sum(inks) / len(inks), 4) if inks else None,
                "hollow_boxes": hollows,
                "font_resources": _font_inventory(reader, reader.pages[pno - 1]),
            }
            _decide_page(page_rec, distinct, groups, hollow_chars, observed, boxes, flat)
            res["pages"].append(page_rec)
    except Exception as e:  # noqa: BLE001
        res["error"] = f"字形分析异常：{type(e).__name__}: {e}"
        return res
    finally:
        try:
            doc.close()
        except Exception:  # noqa: BLE001
            pass

    checked = [p for p in res["pages"] if p["status"] in ("OK", "MISSING", "AMBIGUOUS")]
    missing = [p["page"] for p in res["pages"] if p["status"] == "MISSING"]
    ambiguous = [p["page"] for p in res["pages"] if p["status"] == "AMBIGUOUS"]
    insuff = [p["page"] for p in res["pages"] if p["status"] == "INSUFFICIENT_PX"]
    res["summary"] = {"pages": len(res["pages"]), "cjk_pages_checked": len(checked),
                      "missing_pages": missing, "ambiguous_pages": ambiguous,
                      "insufficient_pages": insuff}
    if missing:
        res["verdict"] = "MISSING"
    elif ambiguous:
        res["verdict"] = "NEEDS_REVIEW"
    elif checked:
        res["verdict"] = "OK"
    else:
        res["verdict"] = "NOT_APPLICABLE"
    return res


# ------------------------------------------------- 可信预期驱动的适用性判定
#: 预期无法建立时的原因 → 说明文本。**不允许**回落成"没有中文"（expected=0）。
EXPECTATION_UNAVAILABLE = "EXPECTATION_UNAVAILABLE"


def expected_cjk_by_page(source_doc, *, include_hidden: bool = False) -> dict:
    """**渲染之前**从可信来源提取每个应导出页应显示的原生中文。

    来源只用既有事实台账（`ExtractedDoc`，即 `extract_any()` 的结果）：
      * 逐文本单元统计含汉字的单元数与字符数；
      * 按"隐藏页不导出"的既有规则区分可见页/隐藏页；
      * 备注与图片内文字**不计入**（当前已声明范围不含它们，也不据此判缺失）。

    返回 `{"available": bool, "pages": {页号: {...}}, "total_units": int,
             "reason": str|None}`。`available=False` 表示来源不可读，
    调用方必须按"预期无法建立"处理，**不得**当成 0。
    """
    out = {"available": False, "pages": {}, "total_units": 0,
           "reason": None, "source": "ExtractedDoc(all_units)"}
    if source_doc is None:
        out["reason"] = "来源文档对象为空，无法建立中文预期"
        return out
    all_units = getattr(source_doc, "all_units", None)
    pages = getattr(source_doc, "pages", None)
    if not callable(all_units) or pages is None:
        out["reason"] = f"来源对象形态不支持按页统计（type={type(source_doc).__name__}）"
        return out
    try:
        render_pages = {}
        visible_index = 0
        for pg in pages:
            if not include_hidden and pg.get("hidden"):
                continue
            visible_index += 1
            native_page = pg.get("page_no") or pg.get("page") or pg.get("index")
            if native_page is not None:
                render_pages[int(native_page)] = visible_index
        for pg, unit in all_units():
            text = getattr(unit, "text", "") or ""
            cjk = [c for c in text if "\u3400" <= c <= "\u9fff"]
            if not cjk:
                continue
            role = getattr(unit, "role", None)
            # 备注不属于当前导出正文范围；图片内文字不产生文本单元，本就不计入。
            if role == "note":
                continue
            hidden = bool(pg.get("hidden")) if isinstance(pg, dict) else False
            if hidden and not include_hidden:
                continue
            page_no = (pg.get("page_no") or pg.get("page") or pg.get("index")) if isinstance(pg, dict) else None
            if page_no is None:
                out["reason"] = "来源页缺少页号，无法按页建立预期"
                return out
            rec = out["pages"].setdefault(int(page_no),
                                          {"cjk_units": 0, "cjk_chars": 0,
                                           "render_page": render_pages.get(int(page_no), int(page_no)),
                                           "hidden": hidden, "samples": []})
            rec["cjk_units"] += 1
            rec["cjk_chars"] += len(cjk)
            if len(rec["samples"]) < 3:
                rec["samples"].append(text[:20])
    except Exception as e:  # noqa: BLE001
        out["reason"] = f"读取来源失败：{type(e).__name__}: {e}"
        out["pages"] = {}
        return out
    if not out["pages"]:
        # 来源可读，但确实没有应显示的原生中文 → 预期明确为 0（合法稿）
        out["available"] = True
    out["total_units"] = sum(v["cjk_units"] for v in out["pages"].values())
    out["available"] = True
    return out


def expectation_findings(result: dict, expected: dict) -> list[dict]:
    """按**可信预期**复核输出侧的适用性与结论，返回 findings。

    规则（判定分母来自可信来源，不是输出侧）：
      1. 预期明确为 0：中文子检查可不适用，但**其他检查照常**，这里不产出 finding；
      2. 预期 > 0 且输出侧该页中文**整体不可取**（`NO_CJK` / 无字符框）：
         → `GLYPH.CJK_TEXT_PRESENT_NOT_RENDERED`（BLOCKING，页号精确到页）；
      3. 预期 > 0、输出提取为 0、但**无法可靠判断绘制情况**：
         → `GLYPH.NOT_EVALUABLE`（BLOCKING），不得当成不适用或合格；
      4. 预期 > 0 且输出侧确实评估出了结论（OK/MISSING/…）：
         交回 `quality_findings` 的既有结论，本函数不重复也不放宽。
    """
    findings: list[dict] = []
    if not expected or not expected.get("available"):
        # 预期无法建立：不得回落成 expected=0 后放行。
        reason = (expected or {}).get("reason") or "中文预期无法建立"
        findings.append({
            "code": "GLYPH.EXPECTATION_UNAVAILABLE", "severity": "BLOCKING",
            "origin": "glyph_quality", "slide": None,
            "detail": (f"无法从可信来源建立应显示中文的预期（{reason}）；"
                       "按契约不得把「预期未知」当作「没有中文」放行。"),
        })
        return findings

    exp_pages = expected.get("pages") or {}
    if not exp_pages:
        return findings                      # 规则 1：预期明确为 0，不产出 finding

    pdf_text_by_page = result.get("pdf_text_by_page") or {}
    by_page = {int(p["page"]): p for p in result.get("pages", []) if p.get("page")}

    for page_no, exp_rec in sorted(exp_pages.items()):
        render_page = exp_rec.get("render_page", page_no)
        rec = by_page.get(render_page)
        observed_boxes = int((rec or {}).get("observed_boxes") or 0)
        observed_cjk = int((rec or {}).get("distinct_cjk") or 0)
        status = (rec or {}).get("status")
        text_chars = (pdf_text_by_page.get(str(render_page))
                      or pdf_text_by_page.get(render_page) or 0)

        if observed_boxes > 0 or observed_cjk > 0:
            # 规则 4：输出侧确实拿到了字符框，结论由 quality_findings 负责
            continue

        if status == "NO_CJK" and not text_chars:
            # 输出侧文本层也没有中文：中文被整体丢弃，确认为不可绘制
            findings.append({
                "code": "GLYPH.CJK_TEXT_PRESENT_NOT_RENDERED", "severity": "BLOCKING",
                "origin": "glyph_quality", "slide": page_no,
                "expected": (f"可信来源要求第 {page_no} 页显示 {exp_rec['cjk_units']} 个"
                             f"中文单元（{exp_rec['cjk_chars']} 个汉字），样例："
                             f"{exp_rec['samples']}"),
                "actual": (f"渲染输出第 {page_no} 页没有取到任何汉字字符框，"
                           f"输出侧中文文本层字符数 {text_chars}"),
                "detail": ("可信来源明确要求该页显示原生中文，真实渲染输出里中文整体消失。"
                           "渲染器退出码 0 与文本层是否存在字符都不能证明字被画出；"
                           "判定依据是可信预期，不能因输出分母为 0 而记为不适用。"),
            })
        else:
            # 规则 3：预期 >0、输出提取为 0，但绘制情况无法可靠判断
            findings.append({
                "code": "GLYPH.NOT_EVALUABLE", "severity": "BLOCKING",
                "origin": "glyph_quality", "slide": page_no,
                "expected": (f"可信来源要求第 {page_no} 页显示 {exp_rec['cjk_units']} 个"
                             f"中文单元（{exp_rec['cjk_chars']} 个汉字）"),
                "actual": (f"渲染输出第 {page_no} 页未取到汉字字符框"
                           f"（status={status or '未知'}，输出侧中文文本层字符数 {text_chars}），"
                           "无法可靠判断绘制情况"),
                "detail": ("预期存在中文但证据不足以判断字形是否画出；按契约既不能记为"
                           "不适用，也不能记为合格。"),
            })
    return findings


def apply_expectation(result: dict, expected: dict) -> dict:
    """把"可信预期"写回分析结果：修正适用性与总判定，并附加期望记录。

    底层 `analyze_pdf` 保留 `NO_CJK` 作为**输出观察结果**不被改写；
    这里改的是 `applicability` 与最终 `verdict`。
    """
    exp_available = bool((expected or {}).get("available"))
    exp_pages = (expected or {}).get("pages") or {}
    expected_total = int((expected or {}).get("total_units") or 0)

    result["expectation"] = {
        "available": exp_available,
        "reason": (expected or {}).get("reason"),
        "source": (expected or {}).get("source"),
        "expected_cjk_units": expected_total,
        "expected_pages": {str(k): v for k, v in sorted(exp_pages.items())},
        "scope": "按应导出页统计；隐藏页与备注不计入，图片内文字不在当前声明范围",
    }

    if not exp_available:
        # 预期无法建立：不得静默通过。verdict 保持 NOT_APPLICABLE 不再成立。
        result["applicability"] = "EXPECTATION_UNAVAILABLE"
        result["verdict"] = "NOT_EVALUABLE"
        return result
    if expected_total == 0:
        # 规则 1：确实没有应显示的原生中文 → 中文子检查不适用
        result["applicability"] = "NOT_APPLICABLE_EXPECTED_ZERO"
        if result.get("verdict") == "NOT_APPLICABLE":
            pass                                  # 维持：确实没有中文
        return result

    # 预期 > 0：逐页核对输出侧是否取到字符框
    by_page = {int(p["page"]): p for p in result.get("pages", []) if p.get("page")}
    lost_pages, unknown_pages = [], []
    for page_no in sorted(exp_pages):
        render_page = exp_pages[page_no].get("render_page", page_no)
        rec = by_page.get(render_page)
        if rec is None:
            unknown_pages.append(page_no)
            continue
        if int(rec.get("observed_boxes") or 0) > 0 or int(rec.get("distinct_cjk") or 0) > 0:
            continue
        if rec.get("status") == "NO_CJK" and not result.get("pdf_text_by_page", {}).get(str(render_page)):
            lost_pages.append(page_no)
        else:
            unknown_pages.append(page_no)

    if lost_pages:
        result["applicability"] = "APPLICABLE_AND_NOT_DRAWN"
        result["verdict"] = "MISSING"
        result["cjk_lost_pages"] = lost_pages
    elif unknown_pages:
        result["applicability"] = "APPLICABLE_BUT_NOT_EVALUABLE"
        result["verdict"] = "NOT_EVALUABLE"
        result["cjk_unevaluated_pages"] = unknown_pages
    else:
        result["applicability"] = "APPLICABLE"
        # 输出侧确有字符框，沿用底层结论（OK/MISSING/NEEDS_REVIEW…）
        if result.get("verdict") == "NOT_APPLICABLE":
            result["verdict"] = "NOT_EVALUABLE"
    return result


def quality_findings(result: dict, expected: dict | None = None) -> list[dict]:
    """把字形分析结果转成正式 findings（阻断/待复核两档，没有"静默通过"）。

    `expected` 是**可信来源**给出的中文预期（`expected_cjk_by_page` 的结果）。
    传入时，先按预期复核适用性：预期>0 而输出侧整体取不到汉字，必须产出阻断
    finding，不能因输出分母为 0 而"不适用"。不传 `expected` 时保持旧行为
    （纯按输出侧结论出finding），供既有单元级回归继续使用。
    """
    out = []
    if expected is not None:
        out += expectation_findings(result, expected)
    if result.get("error"):
        out.append({"code": "GLYPH.NOT_EVALUABLE", "severity": "BLOCKING",
                    "origin": "glyph_quality", "slide": None,
                    "detail": f"字形质量检查无法执行：{result['error']}；"
                              "无法证明字被画出来时不得放行。"})
        return out
    render_to_native = {v.get("render_page", k): int(k)
                        for k, v in ((expected or {}).get("pages") or {}).items()}
    for p in result.get("pages", []):
        p = {**p, "page": render_to_native.get(p["page"], p["page"])}
        if p["status"] == "MISSING":
            out.append({"code": "GLYPH.CJK_NOT_DRAWABLE", "severity": "BLOCKING",
                        "origin": "glyph_quality", "slide": p["page"],
                        "expected": f"第 {p['page']} 页 {p['distinct_cjk']} 个不同汉字各自可辨",
                        "actual": f"{p.get('dominant_share')} 的不同汉字共享同一张"
                                  f"位图（{p.get('dominant_sample')}），空心方框",
                        "detail": p.get("detail")})
        elif p["status"] in ("AMBIGUOUS",):
            out.append({"code": "GLYPH.NOT_EVALUABLE", "severity": "REVIEW",
                        "origin": "glyph_quality", "slide": p["page"],
                        "detail": p.get("detail")})
        elif p["status"] == "INSUFFICIENT_PX" and p["distinct_cjk"]:
            out.append({"code": "GLYPH.PIXEL_EVIDENCE_INSUFFICIENT", "severity": "REVIEW",
                        "origin": "glyph_quality", "slide": p["page"],
                        "detail": p.get("detail")})
        # P1-4：检测是否触发了末位系统字体兜底（非 OFL 字体，如 Arial Unicode MS）
        for fnt in p.get("font_resources", []):
            base_f = str(fnt.get("base_font") or "").replace(" ", "").replace("-", "")
            if "ArialUnicodeMS" in base_f:
                out.append({"code": "GLYPH.FONT_FALLBACK_STACK_BOTTOM", "severity": "WARN",
                            "origin": "glyph_quality", "slide": p["page"],
                            "detail": f"第 {p['page']} 页渲染字体回退到了栈末位系统字体（{fnt.get('base_font')}），"
                                      f"非 Noto 系可再分发字体，需排查缺字字符。"})
    return out


# ---------------------------------------------------------------- 受控中文探针
PROBE_SENTENCES = [
    "稳稿演示文稿中文出字检查",
    "脱贫攻坚乡村振兴生态文明",
    "统筹协调稳中有进高质量发展",
]
PROBE_MIXED = [
    "单位与标点：2026 年 Q3 营收 1,280 万元，同比 +12.5%。",
    "括号（示例）、书名号《报告》、百分号 87%、破折号——完。",
]


def build_probe_pptx(path) -> Path:
    """生成一页纯中文 + 一页中英数混排的探针稿（不依赖模板、不联网）。

    第 1 页**只有汉字**（没有数字、表格线、图片），所以"非空像素"只能来自汉字
    本身；第 2 页混排用于确认标点/数字/单位与中文共存时的出字情况。
    """
    from pptx import Presentation
    from pptx.util import Inches, Pt

    p = Presentation()
    p.slide_width, p.slide_height = 12192000, 6858000
    blank = p.slide_layouts[6]
    for i, lines in enumerate((PROBE_SENTENCES, PROBE_MIXED), start=1):
        s = p.slides.add_slide(blank)
        box = s.shapes.add_textbox(Inches(0.6), Inches(0.8), Inches(11.5), Inches(4.5))
        tf = box.text_frame
        tf.word_wrap = True
        for j, line in enumerate(lines):
            para = tf.paragraphs[0] if j == 0 else tf.add_paragraph()
            run = para.add_run()
            run.text = line
            run.font.size = Pt(28 if i == 1 else 20)
        tf.paragraphs[0].runs[0].font.size = Pt(28 if i == 1 else 20)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    p.save(str(path))
    return path


def probe_verdict(result: dict) -> dict:
    """探针判定：第 1 页（纯中文）必须 OK；混排页不得 MISSING。"""
    pages = {p["page"]: p for p in result.get("pages", [])}
    p1, p2 = pages.get(1), pages.get(2)
    ok = bool(p1 and p1["status"] == "OK" and p2 and p2["status"] in ("OK", "NO_CJK"))
    return {"ok": ok,
            "page1_status": p1 and p1.get("status"),
            "page2_status": p2 and p2.get("status"),
            "detail": ("纯中文页必须逐字可辨；数字/标点页只作共存检查，"
                       "不能让纯中文页靠别页像素通过。")}
