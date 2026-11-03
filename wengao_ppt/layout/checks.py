"""W2-H　`check_layout(pptx_or_job_dir)`：版式硬检查，全部从 PPTX 重算。

检查项（W2 指令 §4-H）：溢出、重叠、最小字号、对比度、边距、
孤行/续页规则、图片槽位填充状态。

诚实边界：文本溢出用 `measure` 的近似估算（±10%）复核已放置文本框的
"内容是否可能超出框高"，作为**廉价前置**；硬门禁的最终判定以渲染证据
（glyphcheck / 渲染复核）为准。对比度按形状填充色与文字色的 WCAG 比值。

错误码（登记于 contracts/errors/layout.json）：
- LAYOUT.OVERFLOW           文本估算超出其文本框
- LAYOUT.OVERLAP             对象矩形重叠（白底大画布矩形除外）
- LAYOUT.FONT_TOO_SMALL      有效字号低于硬门禁下限
- LAYOUT.CONTRAST_TOO_LOW    文字对底色对比度不足
- LAYOUT.MARGIN_VIOLATION    对象越出安全边距/画布
- LAYOUT.ORPHAN_CONTINUATION 标题含"（续）"续页标记
- LAYOUT.IMAGE_SLOT_EMPTY    计划有图槽位但页面上没有图片
- LAYOUT.IMAGE_NO_ALT        图片缺 alt/署名（descr 为空）
- LAYOUT.IMAGE_SLOT_DEGENERATE 图片槽位退化（任一边 < 1in）
- LAYOUT.EMPTY_TEXT          空文本框
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import engine, measure, tokens

EMU_IN = 914400
#: 矩形重叠判定的 epsilon（硬门禁 float_coordinate_epsilon_pt_max = 0.25pt）
EPSILON_EMU = int(0.25 * 12700)
#: 正文硬性地（body_hard_min），用于未打角色标记的形状兜底
MIN_FONT_PT = tokens.BODY_HARD_MIN_PT
#: 近似测量允许的高度余量（10%），只用于"疑似溢出"降级为 warn 的判定
MEASURE_SLACK = 1.10

#: 模板角色 → 冻结策略 font_roles_pt 的键；第二项是附加地板键
_ROLE_TO_POLICY = {
    "title": ("title", None),
    "section_title": ("title", None),
    "cover_title": ("cover_title", None),
    "lead": ("body_preferred", "body_hard_min"),
    "body": ("body_preferred", "body_hard_min"),
    "conclusion": ("conclusion", None),
    "key_number": ("key_number", None),
    "table": ("table", None),
    "chart_aux": ("chart_auxiliary", None),
    "ordinal": ("content_ordinal_allowed_ranges", None),
    "caption": ("chart_auxiliary", None),
    "footnote": ("source_footer", None),
}
#: 策略文件缺失时的代码级镜像（与 contracts/layout_hard_gate_policy.json 同步）
_SIZE_RULES_MIRROR = {
    "title": (36, 40), "body_preferred": (26, 28), "body_hard_min": 24,
    "conclusion": (28, 32), "key_number": (44, 56), "table": (20, 22),
    "chart_auxiliary": (18, 20), "source_footer": (16, 18),
    "cover_title": (36, None), "content_ordinal_allowed_ranges": (18, 22),
}


def _policy_path() -> Path:
    return Path(__file__).resolve().parents[2] / "contracts" / "layout_hard_gate_policy.json"


def size_rules() -> tuple[dict, str]:
    """(字号区间表, 来源)。优先读冻结策略文件；缺失时用镜像并在报告里标注。"""
    p = _policy_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        fp = raw.get("material_font_roles_pt") or raw.get("font_roles_pt") or {}
        rules = {
            "title": tuple(fp["title"]),
            "body_preferred": tuple(fp["body_preferred"]),
            "body_hard_min": fp["body_hard_min"],
            "conclusion": tuple(fp["conclusion"]),
            "key_number": tuple(fp["key_number"]),
            "table": tuple(fp["table"]),
            "chart_auxiliary": tuple(fp["chart_auxiliary"]),
            "source_footer": tuple(fp["source_footer"]),
            "content_ordinal_allowed_ranges": (
                min(r[0] for r in fp["content_ordinal_allowed_ranges"]),
                max(r[1] for r in fp["content_ordinal_allowed_ranges"])),
            "cover_title": (fp["cover_title"]["min"], None),
        }
        return rules, f"policy:{p.name}"
    except Exception:  # noqa: BLE001
        return dict(_SIZE_RULES_MIRROR), "mirror:checks.py"


def _shape_roles(shp) -> list[str]:
    """从形状名解析角色：`w2:body+ordinal` → ["body", "ordinal"]。"""
    name = str(getattr(shp, "name", "") or "")
    if not name.startswith("w2:"):
        return []
    return [r for r in name[3:].split("+") if r]


def _allowed_sizes(roles: list[str], rules: dict) -> list[tuple[float, float | None]]:
    """该形状允许的字号区间列表（多角色取并集）。"""
    out: list[tuple[float, float | None]] = []
    for role in roles:
        spec = _ROLE_TO_POLICY.get(role)
        if spec is None:
            continue
        key, floor_key = spec
        rng = rules.get(key)
        if rng is None:
            continue
        lo = float(rng[0])
        if floor_key and rules.get(floor_key) is not None:
            lo = max(lo, float(rules[floor_key]))
        out.append((lo, None if rng[1] is None else float(rng[1])))
    return out


def _font_findings(shp, pi, add, rules):
    """逐 run 按角色核对字号；角色不明时按正文硬性地兜底。"""
    roles = _shape_roles(shp)
    allowed = _allowed_sizes(roles, rules) if roles else [(float(MIN_FONT_PT), None)]
    for p in shp.text_frame.paragraphs:
        for r in p.runs:
            if not r.text.strip() or r.font.size is None:
                continue
            pt = float(r.font.size.pt)
            ok = any(pt >= lo - 1e-6 and (hi is None or pt <= hi + 1e-6)
                     for lo, hi in allowed)
            if not ok:
                exp = " / ".join(
                    f"{lo:g}–{hi:g}pt" if hi is not None else f"≥{lo:g}pt"
                    for lo, hi in allowed)
                add("LAYOUT.FONT_TOO_SMALL", "FAIL", pi,
                    f"形状 {shp.shape_id} 角色 {'+'.join(roles) or 'body(兜底)'} "
                    f"字号 {pt:g}pt 不在允许区间 [{exp}]",
                    shape_id=shp.shape_id, font_pt=pt, roles=roles or ["body"])


def _table_font_findings(shp, pi, add, rules):
    """原生表格：逐格 run 核对 table 档区间。"""
    lo, hi = rules.get("table", (20, 22))
    try:
        tbl = shp.table
    except Exception:  # noqa: BLE001
        return
    for row in tbl.rows:
        for cell in row.cells:
            for p in cell.text_frame.paragraphs:
                for r in p.runs:
                    if not r.text.strip() or r.font.size is None:
                        continue
                    pt = float(r.font.size.pt)
                    if not (lo - 1e-6 <= pt <= (hi if hi is not None else pt) + 1e-6):
                        add("LAYOUT.FONT_TOO_SMALL", "FAIL", pi,
                            f"表格 {shp.shape_id} 单元格字号 {pt:g}pt 不在 "
                            f"[{lo:g}–{hi:g}pt]", shape_id=shp.shape_id,
                            font_pt=pt, roles=["table"])


def _chart_font_findings(shp, pi, add, rules):
    """原生图表：轴刻度 / 图例 / 数据标签 / 标题核对 chart_auxiliary 档。"""
    lo, hi = rules.get("chart_auxiliary", (18, 20))
    try:
        chart = shp.chart
    except Exception:  # noqa: BLE001
        return
    probes = []

    def _probe(font, what):
        try:
            if font is not None and font.size is not None:
                probes.append((float(font.size.pt), what))
        except Exception:  # noqa: BLE001
            pass

    for ax_name in ("category_axis", "value_axis"):
        try:
            ax = getattr(chart, ax_name)
            _probe(ax.tick_labels.font, f"{ax_name} 刻度")
        except Exception:  # noqa: BLE001
            pass
    try:
        if chart.has_legend:
            _probe(chart.legend.font, "图例")
    except Exception:  # noqa: BLE001
        pass
    try:
        for plot in chart.plots:
            if plot.has_data_labels:
                _probe(plot.data_labels.font, "数据标签")
    except Exception:  # noqa: BLE001
        pass
    try:
        if chart.has_title:
            for p in chart.chart_title.text_frame.paragraphs:
                for r in p.runs:
                    if r.font.size is not None:
                        probes.append((float(r.font.size.pt), "图表标题"))
    except Exception:  # noqa: BLE001
        pass
    for pt, what in probes:
        if not (lo - 1e-6 <= pt <= (hi if hi is not None else pt) + 1e-6):
            add("LAYOUT.FONT_TOO_SMALL", "FAIL", pi,
                f"图表 {shp.shape_id} {what}字号 {pt:g}pt 不在 "
                f"[{lo:g}–{hi:g}pt]", shape_id=shp.shape_id,
                font_pt=pt, roles=["chart_aux"])


def _shape_rect(shp):
    return engine.Rect(int(shp.left or 0), int(shp.top or 0),
                       int(shp.width or 0), int(shp.height or 0))


def _is_picture(shp) -> bool:
    return shp.shape_type is not None and "PICTURE" in str(shp.shape_type)


#: descr 只是图片文件名时不算署名（python-pptx add_picture 的默认行为）
_IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff")


def _picture_alt(shp) -> str:
    """图片的 alt/署名（descr）。缺失返回空串。

    python-pptx 的 `add_picture` 会把**文件名**写进 descr——那不是署名，
    与"没有 descr"同等对待（否则任何裸插入的图都会被算作已署名）。
    """
    try:
        descr = str(shp._element._nvXxPr.cNvPr.get("descr") or "")
    except Exception:  # noqa: BLE001
        return ""
    if descr.lower().endswith(_IMG_EXTS):
        return ""
    return descr


def _picture_findings(shp, pi, add):
    """图片槽位检查（W2 指令 §4-H）：退化尺寸 + 署名缺失。"""
    r = _shape_rect(shp)
    if r.w < EMU_IN or r.h < EMU_IN:
        add("LAYOUT.IMAGE_SLOT_DEGENERATE", "WARN", pi,
            f"图片 {shp.shape_id} 槽位退化（{r.w / EMU_IN:.2f}in × "
            f"{r.h / EMU_IN:.2f}in）", shape_id=shp.shape_id)
    if not _picture_alt(shp).strip():
        add("LAYOUT.IMAGE_NO_ALT", "WARN", pi,
            f"图片 {shp.shape_id} 缺 alt/署名（descr 为空）", shape_id=shp.shape_id)


def _effective_font_pt(shp) -> float | None:
    """形状内最小的显式字号（None = 无文字）。autofit 缩字会被后续渲染复核。"""
    sizes = []
    if not shp.has_text_frame:
        return None
    for p in shp.text_frame.paragraphs:
        for r in p.runs:
            if r.text.strip() and r.font.size is not None:
                sizes.append(r.font.size.pt)
    return min(sizes) if sizes else None


def _est_text_height_emu(shp, width_emu: int) -> int:
    """逐段估算文本高度：用该段最小字号 + 该段真实行距/段后距。

    混排文本框（如"序号+正文"）按最保守的那一段算，避免低估。
    """
    total = 0
    for p in shp.text_frame.paragraphs:
        sizes = [r.font.size.pt for r in p.runs if r.text.strip() and r.font.size]
        if not sizes:
            continue
        pt = min(sizes)
        lh = p.line_spacing if isinstance(p.line_spacing, (int, float)) else 1.4
        sa = p.space_after.pt if p.space_after is not None else 6.0
        text = "".join(r.text for r in p.runs)
        total += measure.block_height_emu(text, width_emu, pt, float(lh), float(sa),
                                          para_spacing=False)
    return total


def _text_color(shp) -> str | None:
    if not shp.has_text_frame:
        return None
    for p in shp.text_frame.paragraphs:
        for r in p.runs:
            if r.text.strip() and r.font.color is not None and \
                    r.font.color.type is not None and r.font.color.rgb is not None:
                return "#" + str(r.font.color.rgb)
    return None


def _fill_color(shp) -> str | None:
    try:
        if shp.fill.type is not None and str(shp.fill.type) == "MSO_FILL_TYPE.SOLID":
            return "#" + str(shp.fill.fore_color.rgb)
    except Exception:  # noqa: BLE001
        pass
    return None


def _is_background(shp) -> bool:
    """全画布无文字矩形视作页面底色，不参与重叠/边距检查。"""
    w, h = shp.width or 0, shp.height or 0
    return (w >= tokens.CANVAS_W_EMU * 0.95 and h >= tokens.CANVAS_H_EMU * 0.95
            and not (shp.has_text_frame and shp.text_frame.text.strip()))


def check_layout(target) -> dict:
    """target：PPTX 路径或 Presentation。返回 CheckReport（可 JSON 落盘）。"""
    from pptx import Presentation
    if isinstance(target, (str, Path)):
        prs = Presentation(str(target))
        pptx_path = str(target)
        pptx_sha256 = hashlib.sha256(Path(target).read_bytes()).hexdigest()
    else:
        prs = target
        pptx_path = None
        pptx_sha256 = None

    m = tokens.MARGINS_EMU
    rules, rules_source = size_rules()
    findings: list[dict] = []

    def add(code, severity, page, detail, **fields):
        findings.append({"code": code, "severity": severity, "page": page,
                         "detail": detail, **fields})

    for pi, slide in enumerate(prs.slides, start=1):
        page_findings_before = len(findings)
        rects = []
        has_picture = False
        for shp in slide.shapes:
            if _is_picture(shp):
                has_picture = True
            if _is_background(shp):
                continue
            r = _shape_rect(shp)
            rects.append((shp, r))
            # 边距 / 画布
            if r.x < m["left"] - EPSILON_EMU or r.y < m["top"] - EPSILON_EMU \
                    or r.x + r.w > tokens.CANVAS_W_EMU - m["right"] + EPSILON_EMU:
                if not (r.y + r.h >= tokens.CANVAS_H_EMU - int(0.30 * EMU_IN)
                        and r.h <= int(0.35 * EMU_IN) + EPSILON_EMU):
                    add("LAYOUT.MARGIN_VIOLATION", "FAIL", pi,
                        f"形状 {shp.shape_id} 越出安全边距", shape_id=shp.shape_id,
                        rect=r.as_dict())
            if r.y + r.h > tokens.CANVAS_H_EMU or r.x + r.w > tokens.CANVAS_W_EMU:
                add("LAYOUT.MARGIN_VIOLATION", "FAIL", pi,
                    f"形状 {shp.shape_id} 越出画布", shape_id=shp.shape_id,
                    rect=r.as_dict())
            # 字号（按形状登记的角色套冻结策略区间）
            if shp.has_text_frame:
                _font_findings(shp, pi, add, rules)
            # 原生表格单元格字号（graphicFrame 没有 text_frame，单独走表格）
            if getattr(shp, "has_table", False):
                _table_font_findings(shp, pi, add, rules)
            # 原生图表内部字号（轴/图例/数据标签在 chart XML 里）
            if getattr(shp, "has_chart", False):
                _chart_font_findings(shp, pi, add, rules)
            # 图片槽位（退化尺寸 / alt 署名）
            if _is_picture(shp):
                _picture_findings(shp, pi, add)
            # 空文本
            if shp.has_text_frame and not shp.text_frame.text.strip() \
                    and "TEXT_BOX" in str(shp.shape_type):
                add("LAYOUT.EMPTY_TEXT", "WARN", pi,
                    f"空文本框 {shp.shape_id}", shape_id=shp.shape_id)
            # 对比度
            fg = _text_color(shp)
            bg = _fill_color(shp) or "#FFFFFF"
            if fg:
                try:
                    ratio = tokens.contrast_ratio(fg, bg)
                    role_is_title = any(
                        (r.font.size or 0) >= 32 and r.font.bold
                        for p in shp.text_frame.paragraphs for r in p.runs)
                    floor = (tokens.MIN_CONTRAST_BOLD_TITLE if role_is_title
                             else tokens.MIN_CONTRAST_NORMAL)
                    if ratio < floor:
                        add("LAYOUT.CONTRAST_TOO_LOW", "FAIL", pi,
                            f"形状 {shp.shape_id} 对比度 {ratio:.2f} < {floor}",
                            shape_id=shp.shape_id, ratio=round(ratio, 2))
                except Exception:  # noqa: BLE001
                    pass
            # 溢出（估算）：逐段用该段最小字号 + 真实行距/段距实测
            if shp.has_text_frame and shp.text_frame.text.strip():
                est = _est_text_height_emu(shp, r.w)
                if est > r.h * MEASURE_SLACK:
                    add("LAYOUT.OVERFLOW", "WARN", pi,
                        f"形状 {shp.shape_id} 文本估算高度可能超出框（估算 {est} EMU / 框 {r.h} EMU）",
                        shape_id=shp.shape_id, est_h_emu=est, box_h_emu=r.h)
        # 重叠（有文字/图片的形状之间；epsilon 不是重叠豁免）
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                s1, r1 = rects[i]
                s2, r2 = rects[j]
                t1 = s1.has_text_frame and s1.text_frame.text.strip()
                t2 = s2.has_text_frame and s2.text_frame.text.strip()
                pic1 = "PICTURE" in str(s1.shape_type)
                pic2 = "PICTURE" in str(s2.shape_type)
                if not (t1 or pic1) or not (t2 or pic2):
                    continue
                if r1.intersects(r2, epsilon_emu=EPSILON_EMU):
                    # 底色卡片与其内部文字是包含关系，不算冲突；**图片不适用**
                    # 包含豁免——图片不透明且后绘制，套内文字会被遮住（HG04）。
                    if not (pic1 or pic2):
                        if r1.x <= r2.x and r1.y <= r2.y and \
                                r1.x + r1.w >= r2.x + r2.w and \
                                r1.y + r1.h >= r2.y + r2.h:
                            continue
                        if r2.x <= r1.x and r2.y <= r1.y and \
                                r2.x + r2.w >= r1.x + r1.w and \
                                r2.y + r2.h >= r1.y + r1.h:
                            continue
                    # 图与图的相邻判定不适用；文字对文字才算重叠冲突
                    if pic1 and pic2:
                        continue
                    add("LAYOUT.OVERLAP", "FAIL", pi,
                        f"形状 {s1.shape_id} 与 {s2.shape_id} 文字/图片区域重叠",
                        shape_ids=[s1.shape_id, s2.shape_id],
                        rects=[r1.as_dict(), r2.as_dict()])
        # 续页标记
        for shp in slide.shapes:
            if shp.has_text_frame and "（续）" in shp.text_frame.text:
                add("LAYOUT.ORPHAN_CONTINUATION", "FAIL", pi,
                    "标题含“（续）”续页标记", shape_id=shp.shape_id)
        # 图片槽位降级留痕：模板在页脚写"本页未配图（T4 降级）"，
        # 这里核对"留了痕就该真的没图"——有痕有图 = 备注造假，无痕无图不管。
        if not has_picture:
            for shp in slide.shapes:
                if shp.has_text_frame and "本页未配图" in shp.text_frame.text:
                    add("LAYOUT.IMAGE_SLOT_EMPTY", "INFO", pi,
                        "页脚声明未配图（T4 降级），页面确实无图片：留痕一致",
                        shape_id=shp.shape_id)
                    break
        if len(findings) == page_findings_before and not slide.shapes:
            add("LAYOUT.EMPTY_PAGE", "FAIL", pi, "空页")
    report = {
        "schema": "rs.check_report.v1",
        "check": "check_layout",
        "pptx_path": pptx_path,
        "pptx_sha256": pptx_sha256,
        "page_count": len(prs.slides._sldIdLst),
        "size_rules_source": rules_source,
        "size_rules": {k: list(v) if isinstance(v, tuple) else v
                       for k, v in rules.items()},
        "findings": findings,
        "blocking_codes": ["LAYOUT.OVERLAP", "LAYOUT.FONT_TOO_SMALL",
                           "LAYOUT.CONTRAST_TOO_LOW", "LAYOUT.MARGIN_VIOLATION",
                           "LAYOUT.ORPHAN_CONTINUATION", "LAYOUT.EMPTY_PAGE"],
        "pass": not any(f["code"] in ("LAYOUT.OVERLAP", "LAYOUT.FONT_TOO_SMALL",
                                     "LAYOUT.CONTRAST_TOO_LOW",
                                     "LAYOUT.MARGIN_VIOLATION",
                                     "LAYOUT.ORPHAN_CONTINUATION",
                                     "LAYOUT.EMPTY_PAGE") for f in findings),
    }
    return report


def write_check_evidence(job_dir: str, report: dict) -> str:
    ev = Path(job_dir) / "evidence" / "layout"
    ev.mkdir(parents=True, exist_ok=True)
    out = ev / "layout_check.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return str(out)
