"""文字**实际底色**与对比度：主题不能只改 `p:bg`。

事故背景（incident-20261017-f88b）
---------------------------------
主题只把幻灯片背景改成浅色，却把整页/半页的深色形状留在原地；文字颜色又被
按"主题前景色"整批改深，于是深字落在没动的深色面板上（实测 1.09:1）。
另一些字号被放大后仍按原对比度渲染，用户看到的是"主题套用了但不认识"。

本模块做的事（只做判定，不改文件）：
1. 扫描每页的形状：几何、填充类型/颜色/透明度、是否含文字、是否图片/组/图表；
2. 找出**可成对重染的背景面板**：不透明纯色、无文字、非图片、面积够大、
   颜色中性（低饱和）——这是"页面底色"的候选，不是所有装饰形状；
3. 对每个文字形状，按层叠顺序求它真正的相邻底色（覆盖它的最上层不透明形状），
   而不是读它后面的 `p:bg`；
4. 用 WCAG 相对亮度算对比度。普通文字 4.5:1，大字号允许 3:1（需满足字号条件）。

明确不做：不重染图片、渐变、半透明、高饱和品牌色或用途不明对象；
判不了就记 UNKNOWN，交给上层复核，不拿"面积接近整页"当自动授权。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 面板可重染的最小面积占比（相对整页）
PANEL_MIN_AREA = 0.18
#: 中性色判定：RGB 三通道最大差
NEUTRAL_SPREAD = 28
#: 普通文字对比度下限 / 大字下限（WCAG 2.2 AA 口径，产品可读性准则）
CONTRAST_NORMAL = 4.5
CONTRAST_LARGE = 3.0
LARGE_PT = 18.0

#: 业务色判定：值型文字（纯数值/百分比/金额/编号）上的**高饱和**原色。
#: 中性灰/米白不算业务色——它们是排版色，可以随主题替换。
#: 只在"整块文字很短、且是值"时启用，避免把一句被染色的正文误判成业务色。
# 只有"带方向的数值"（+/-）或"带单位的量"（%/亿/万/元/bp）才算业务色载体：
# 裸编号（01/02）属于装饰性序号，换主题色不影响含义，不能拿来锁住整排卡片。
_VALUE_UNIT = re.compile(r"^\d[\d,，\.\s]*\s*(?:%|％|亿|万|元|bp|个百分点)$")
_VALUE_SIGNED = re.compile(r"^[+\-−±]\s*\d[\d,，\.\s]*\s*(?:%|％|亿|万|元|bp|个百分点)?$")
BUSINESS_MIN_CHROMA = 40   # max-min 通道差；金色 110、红 119、绿 77、蓝 65，灰 10
BUSINESS_MAX_LEN = 16      # 整块文字超过这个长度就不当作"值"


def _clamp(v: float) -> float:
    return 0.0 if v < 0 else (1.0 if v > 1 else v)


def hex_to_rgb(h: str):
    h = (h or "").strip().lstrip("#")
    if len(h) != 6:
        return None
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def rel_luminance(rgb) -> float:
    def ch(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast_ratio(fg_rgb, bg_rgb) -> float:
    l1, l2 = rel_luminance(fg_rgb), rel_luminance(bg_rgb)
    hi, lo = max(l1, l2), min(l1, l2)
    return round((hi + 0.05) / (lo + 0.05), 2)


def is_neutral(rgb) -> bool:
    return max(rgb) - min(rgb) <= NEUTRAL_SPREAD


def mix(fg_rgb, bg_rgb, alpha: float):
    """按不透明度合成：文字/形状叠在底色上的实际观感色。"""
    a = _clamp(alpha)
    return tuple(int(round(f * a + b * (1 - a))) for f, b in zip(fg_rgb, bg_rgb))


@dataclass
class ShapeInfo:
    shape: object
    name: str
    z: int
    left: int
    top: int
    width: int
    height: int
    has_text: bool
    kind: str                 # solid / none / gradient / picture / group / table / chart / unknown
    rgb: tuple | None
    alpha: float
    area_ratio: float
    font_sizes: list = field(default_factory=list)

    @property
    def bbox(self):
        return (self.left, self.top, self.left + self.width, self.top + self.height)

    def covers(self, box) -> float:
        """本形状覆盖给定框的面积比例。"""
        x0, y0, x1, y1 = self.bbox
        a0, b0, a1, b1 = box
        iw = max(0, min(x1, a1) - max(x0, a0))
        ih = max(0, min(y1, b1) - max(y0, b0))
        area = max(1, (a1 - a0) * (b1 - b0))
        return (iw * ih) / float(area)


def _fill_of(sh):
    """返回 (kind, rgb, alpha)。判不了就是 unknown，不猜。"""
    try:
        from pptx.enum.dml import MSO_FILL
    except Exception:  # noqa: BLE001
        MSO_FILL = None
    try:
        if getattr(sh, "shape_type", None) is not None and "PICTURE" in str(sh.shape_type):
            return "picture", None, 1.0
        if getattr(sh, "has_chart", False) and sh.has_chart:
            return "chart", None, 1.0
        if getattr(sh, "has_table", False) and sh.has_table:
            return "table", None, 1.0
        if str(getattr(sh, "shape_type", "")) == "GROUP (6)":
            return "group", None, 1.0
        fill = sh.fill
        ftype = fill.type
        if ftype is None:
            return "none", None, 1.0
        if MSO_FILL is not None and ftype == MSO_FILL.SOLID:
            rgb = fill.fore_color.rgb
            hexv = str(rgb)
            return "solid", hex_to_rgb(hexv), _alpha_of(sh)
        # `str(ftype)` 形如 "GRADIENT (3)"：统一归一到固定词汇，
        # 否则"渐变"会被当成未知字符串而不进入"不可计算"分支，最后按幻灯片
        # p:bg 假算出一个通过结果——这正是本次事故要避免的假证明。
        raw = str(ftype).split(" ")[0].split(".")[-1].upper()
        return {"SOLID": "solid", "GRADIENT": "gradient", "PATTERNED": "patterned",
                "PICTURE": "picture", "BACKGROUND": "none",
                "GROUP": "group"}.get(raw, "unknown"), None, 1.0
    except Exception:  # noqa: BLE001
        return "unknown", None, 1.0


def _alpha_of(sh) -> float:
    """读取纯色填充的透明度（`a:srgbClr/a:alpha`）。读不到按不透明处理但记 1.0。"""
    try:
        from lxml import etree
        xml = etree.tostring(sh._element, encoding="unicode")  # noqa: SLF001
        import re
        m = re.search(r'<a:alpha val="(\d+)"', xml)
        if m:
            return max(0.0, min(1.0, int(m.group(1)) / 100000.0))
    except Exception:  # noqa: BLE001
        pass
    return 1.0


def scan_slide(slide, slide_w: int, slide_h: int, idx: int = 0) -> dict:
    """把一页的真实形状扫成可计算的结构（不改文件）。"""
    shapes = []
    page_area = float(max(1, slide_w * slide_h))
    for z, sh in enumerate(slide.shapes):
        kind, rgb, alpha = _fill_of(sh)
        has_text = bool(getattr(sh, "has_text_frame", False) and sh.has_text_frame
                        and sh.text_frame.text.strip())
        sizes = []
        if has_text:
            for para in sh.text_frame.paragraphs:
                for run in para.runs:
                    if run.font.size:
                        sizes.append(run.font.size.pt)
        shapes.append(ShapeInfo(shape=sh, name=getattr(sh, "name", f"shape{z}"), z=z,
                                left=int(sh.left or 0), top=int(sh.top or 0),
                                width=int(sh.width or 0), height=int(sh.height or 0),
                                has_text=has_text, kind=kind, rgb=rgb, alpha=alpha,
                                area_ratio=((sh.width or 0) * (sh.height or 0)) / page_area,
                                font_sizes=sizes))
    return {"idx": idx, "shapes": shapes, "slide_w": slide_w, "slide_h": slide_h,
            "page": (0, 0, slide_w, slide_h)}


def bg_of_slide(slide, fallback=(255, 255, 255)):
    """`p:bg` 的实际颜色；从主题/版式继承或图片背景时返回 None（未知）。"""
    try:
        bg = slide.background
        if bg.fill.type is None:
            return fallback, "none"
        bg.fill.solid()
        return hex_to_rgb(str(bg.fill.fore_color.rgb)), "solid"
    except Exception:  # noqa: BLE001
        return fallback, "unknown"


def candidates_for_recolor(info: dict) -> list[ShapeInfo]:
    """可成对重染的背景面板。

    资格（全部满足）：
    - 不透明纯色、本身不含文字、不是图片/组/图表；
    - 颜色中性（低饱和）——金色标题条、红色装饰条这类**品牌/强调色一律保护**；
    - 并且满足其一：面积够大（整页/半页背景），**或者它确实是某些文字的底色**
      （卡片、引用块这类容器）。

    "面积接近整页"不作为唯一授权：只把文字真正压着的容器与页面底色成对转换，
    其余形状原样保留。
    """
    texts = [s for s in info["shapes"] if s.has_text]
    out = []
    for s in info["shapes"]:
        if s.has_text or s.kind != "solid" or s.rgb is None:
            continue
        if s.alpha < 0.99:
            continue           # 半透明：叠出来的颜色算不准，不擅自改
        if not is_neutral(s.rgb):
            continue           # 高饱和：品牌色/装饰条，保护
        backs_text = any(s.z < t.z and s.covers(t.bbox) >= 0.5 for t in texts)
        if s.area_ratio < PANEL_MIN_AREA and not backs_text:
            continue
        out.append(s)
    return out


def backdrop_for(info: dict, text_shape: ShapeInfo, recolored: dict, slide_bg):
    """文字真正压在上面的颜色。

    返回 dict：`rgb`、`source`、`panel_z`（来自哪个面板）、`analyzable`。
    `analyzable=False` 表示底色是渐变/图片/半透明等无法可靠计算的结构 ——
    上层不得据此判"对比度通过"。
    """
    best = None
    for s in info["shapes"]:
        if s.z >= text_shape.z:
            continue
        if s.shape is text_shape.shape:
            continue
        if s.covers(text_shape.bbox) < 0.5:
            continue
        if s.kind == "solid" and s.rgb is not None and s.alpha >= 0.99:
            if best is None or s.z > best.z:
                best = s
        elif s.kind not in ("none",):
            # 任何非纯色、非透明的覆盖物（渐变/图片/组/图表/未知）都挡住底色；
            # 它们不可可靠计算 → 交给上层记"无法证明"，不拿 p:bg 顶替。
            if best is None or s.z > best.z:
                best = s
    if best is None:
        return {"rgb": slide_bg, "source": "slide_bg", "panel_z": None,
                "analyzable": slide_bg is not None}
    if best.kind != "solid" or best.rgb is None:
        return {"rgb": None, "source": f"shape:{best.name}:{best.kind}",
                "panel_z": best.z, "analyzable": False}
    rgb = recolored.get(best.z, best.rgb) if recolored else best.rgb
    return {"rgb": rgb, "source": f"shape:{best.name}", "panel_z": best.z,
            "analyzable": True}


def _run_rgb(run):
    try:
        c = run.font.color
        if c is None or c.type is None:
            return None
        return hex_to_rgb(str(c.rgb))
    except Exception:  # noqa: BLE001
        return None


def protected_runs(text_shape: ShapeInfo) -> list:
    """值型文字上的高饱和原色 = 业务色（红=负/绿=正/金=横盘/蓝=资金 …）。

    这类颜色表达的不是美观而是**含义**：同一页里四种颜色对应四种状态，
    标签文字本身写着"下降/增长/横盘"。把它统一成主题前景色会静默丢掉映射，
    所以必须保留，并由调用方记账、核对保留后的对比度。

    只覆盖"短值型文字"：整块文字长度 ≤ BUSINESS_MAX_LEN 且每个 run 是纯值，
    颜色饱和度高于 BUSINESS_MIN_CHROMA。正文段落即使被染色也不会进入这里。
    """
    sh = text_shape.shape
    try:
        full = (sh.text_frame.text or "").strip()
    except Exception:  # noqa: BLE001
        return []
    if not full or len(full) > BUSINESS_MAX_LEN:
        return []
    out = []
    try:
        paras = sh.text_frame.paragraphs
    except Exception:  # noqa: BLE001
        return []
    for pi, para in enumerate(paras):
        for ri, run in enumerate(para.runs):
            t = (run.text or "").strip()
            if not t or not (_VALUE_UNIT.match(t) or _VALUE_SIGNED.match(t)):
                continue
            rgb = _run_rgb(run)
            if rgb is None or (max(rgb) - min(rgb)) < BUSINESS_MIN_CHROMA:
                continue
            out.append({"para": pi, "run": ri, "text": t,
                        "rgb": rgb, "hex": "#%02X%02X%02X" % rgb})
    return out


def threshold_for(text_shape: ShapeInfo) -> float:
    """大字号允许 3:1，其余 4.5:1；不通过放大字号去套低门槛。"""
    sizes = [s for s in text_shape.font_sizes if s]
    if sizes and min(sizes) >= LARGE_PT:
        return CONTRAST_LARGE
    return CONTRAST_NORMAL


def plan_slide(info: dict, theme_bg, theme_colors: dict, role_of,
               orig_color_of=None, protected: dict | None = None) -> dict:
    """给一页做"改哪块底色 + 每块文字用什么色"的成对计划。

    `role_of(shape) -> role` 由调用方（theme）提供，颜色取 `theme_colors[role]`；
    `orig_color_of(shape) -> rgb|None` 给出原稿文字色，用于判断
    "保留原色是否本来就够读"——**不能把原稿已有的低对比算成本次转换的成果**。

    返回结构里的每个决定都带依据，便于落盘复核。
    """
    slide_bg = theme_bg[0]
    panels = candidates_for_recolor(info)
    texts = [s for s in info["shapes"] if s.has_text]

    # 业务色保护：某个值型文字的业务色在主题底色上达不到它的门槛时，它脚下的面板
    # 就不能重染（否则"保留业务色"与"换底色"不能同时成立，只能二选一）。
    # 同一底色的面板视为一组（例如一排同色 KPI 卡），一起保留，避免半套深卡半套浅卡。
    locked = set()
    if slide_bg is not None:
        for pnl in panels:
            for t in texts:
                runs = (protected or {}).get(t.name) or []
                if not runs or pnl.z >= t.z or pnl.covers(t.bbox) < 0.5:
                    continue
                need = threshold_for(t)
                if any(contrast_ratio(r["rgb"], slide_bg) < need for r in runs):
                    locked.add(pnl.z)
        if locked:
            fills = {info["shapes"][z].rgb for z in locked}
            locked |= {pnl.z for pnl in panels if pnl.rgb in fills}

    def evaluate(recolor_map, use_theme_color=True):
        rows = []
        for t in texts:
            b = backdrop_for(info, t, recolor_map, slide_bg)
            role = role_of(t)
            need = threshold_for(t)
            fg = theme_colors[role] if use_theme_color else (
                orig_color_of(t) if orig_color_of else None)
            sizes = [x for x in t.font_sizes if x]
            bolds = []
            try:
                bolds = [bool(run.font.bold) for para in t.shape.text_frame.paragraphs
                         for run in para.runs]
            except Exception:  # noqa: BLE001
                bolds = []
            row = {"shape": t.name, "role": role, "backdrop": b["source"],
                   "panel_z": b["panel_z"], "required": need, "ratio": None,
                   "orig_ratio": None, "status": "UNKNOWN",
                   # 门槛依据必须可核对：字号、是否粗体、以及据此选的门槛
                   "font_pt": min(sizes) if sizes else None,
                   "bold": (any(bolds) if bolds else None),
                   "threshold_basis": ("large>=18pt" if need == CONTRAST_LARGE
                                       else "normal<18pt")}
            if b["rgb"] is not None and b["analyzable"] and fg is not None:
                row["backdrop_rgb"] = "#%02X%02X%02X" % b["rgb"]
                row["fg_rgb"] = "#%02X%02X%02X" % fg
                row["ratio"] = contrast_ratio(fg, b["rgb"])
            orig_fg = orig_color_of(t) if orig_color_of else None
            if b["rgb"] is not None and b["analyzable"] and orig_fg is not None:
                # 原稿对照：同一底色、原文字色。用于区分"本次改坏"与"原来就低"。
                base_bg = backdrop_for(info, t, {}, slide_bg)
                if base_bg["rgb"] is not None:
                    row["orig_ratio"] = contrast_ratio(orig_fg, base_bg["rgb"])
            rows.append(row)
        return rows

    def classify(rows):
        for r in rows:
            if r["ratio"] is not None and r["ratio"] >= r["required"]:
                r["status"] = "OK"
            elif r["orig_ratio"] is not None and r["orig_ratio"] >= r["required"]:
                r["status"] = "KEEP_ORIGINAL_OK"
            elif r["orig_ratio"] is not None and r["ratio"] is not None:
                r["status"] = "PREEXISTING_LOW" if r["orig_ratio"] >= r["ratio"] else "UNSAFE"
            else:
                r["status"] = "UNKNOWN"
        return rows

    no_recolor = classify(evaluate({}))
    active = [p for p in panels if p.z not in locked]
    full = {p.z: slide_bg for p in active} if slide_bg else {}
    chosen_map, rows, mode = {}, no_recolor, "keep_original_backdrop"
    if full:
        with_recolor = classify(evaluate(full))
        good_no = sum(1 for r in no_recolor if r["status"] == "OK")
        good_re = sum(1 for r in with_recolor if r["status"] == "OK")
        if good_re >= good_no:
            chosen_map, rows, mode = dict(full), with_recolor, "recolor_background_panels"

    # 冲突消解：某块文字必须保留原色时，它下面的面板就不能改色（否则原浅字落浅底）。
    for _ in range(3):
        conflicts = {r["panel_z"] for r in rows
                     if r["status"] in ("KEEP_ORIGINAL_OK", "PREEXISTING_LOW", "UNSAFE")
                     and r.get("panel_z") in chosen_map}
        if not conflicts:
            break
        chosen_map = {z: v for z, v in chosen_map.items() if z not in conflicts}
        rows = classify(evaluate(chosen_map))
        mode = "recolor_background_panels_partial" if chosen_map else "keep_original_backdrop"

    return {"mode": mode, "recolor": chosen_map, "rows": rows, "panels": panels,
            "locked_panels": sorted(locked), "protected": protected or {},
            "decisions": [{"mode": mode,
                           "panels_considered": [{"shape": p.name,
                                                  "area": round(p.area_ratio, 3),
                                                  "rgb": "#%02X%02X%02X" % p.rgb}
                                                 for p in panels],
                           "recolored": sorted(chosen_map)}]}
