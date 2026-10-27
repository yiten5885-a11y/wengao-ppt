"""视觉重构引擎（wengao-visual）：每页重点、字体阶梯、关键词配图、统一转场、粒子动画。

设计原则
--------
1. 只改视觉表达：文字、数字、单位、引用、备注一律不改写；不删页、不加页、不改比例。
2. 只改「可安全改」的三类属性：字号/字重、图片与遮罩、转场与对象动画。
3. 一切尺寸变更先做**容量检查**：放不下就退回原字号并记账（`fit_blocked`），
   绝不靠缩字或溢出取得"看起来变好"。
4. 配图插入在**文字层之下**（z 序），保证文字始终在图片之上、可编辑。
"""
from __future__ import annotations

import math
import random
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR
from pptx.oxml.ns import qn
from pptx.util import Emu, Inches, Pt

A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
NSMAP = {"a": A_NS, "p": P_NS}

VERSION = "1.0.0"

#: 角色 → 目标字号（16:9，13.333×7.5in）
TARGET_PT = {
    "title": 30.0,
    "subtitle": 15.0,
    "quote": 17.0,
    "kpi_num": None,      # 数字保持原字号（已足够大）
    "label": 14.0,
    "sub": 12.0,
    "body": 16.0,
    "source": 10.5,
    "footer": 9.5,
    "pageno": 9.5,
}

#: 任何文字不得低于此值（能放下就抬到 10.5）
MIN_PT = 10.5

#: 页码/来源/页脚判定
MICRO_TOP = 6.95


def _in(v) -> float:
    return (v or 0) / 914400.0


def _char_w(ch: str) -> float:
    """相对字号的宽度权重（CJK≈1，拉丁≈0.55）。"""
    o = ord(ch)
    if o > 0x2E80 or ch in "，。、；：（）“”‘’！？—…《》【】":
        return 1.0
    if ch == " ":
        return 0.30
    if ch.isdigit():
        return 0.55
    if ch.isupper():
        return 0.62
    return 0.52


def text_width_in(text: str, pt: float) -> float:
    return sum(_char_w(c) for c in text) * pt / 72.0


def n_lines(text: str, pt: float, box_w: float) -> int:
    box_w = max(box_w, 0.2)
    lines = 0
    for para in (text.split("\n") or [""]):
        if not para:
            lines += 1
            continue
        cur = 0.0
        n = 1
        for ch in para:
            w = _char_w(ch) * pt / 72.0
            if cur + w > box_w and cur > 0:
                n += 1
                cur = w
            else:
                cur += w
        lines += n
    return max(1, lines)


def need_h_in(text: str, pt: float, box_w: float, line_gap: float = 1.32) -> float:
    return n_lines(text, pt, box_w) * pt * line_gap / 72.0


class T:
    """文本形状包装（只读几何 + 可改字号/字重）。"""

    def __init__(self, shape, idx: int):
        self.shape = shape
        self.idx = idx
        self.name = shape.name
        self.T = _in(shape.top)
        self.L = _in(shape.left)
        self.W = _in(shape.width)
        self.H = _in(shape.height)
        self.text = shape.text_frame.text if shape.has_text_frame else ""
        self.paras = shape.text_frame.paragraphs if shape.has_text_frame else []

    # -- 读取 -----------------------------------------------------------------
    def sizes(self) -> list[float]:
        out = []
        for p in self.paras:
            for r in p.runs:
                if r.font.size is not None:
                    out.append(float(r.font.size.pt))
        return out

    def size(self) -> float:
        s = self.sizes()
        return max(s) if s else 0.0

    def min_size(self) -> float:
        s = self.sizes()
        return min(s) if s else 0.0

    # -- 写入 -----------------------------------------------------------------
    def set_pt(self, pt: float) -> int:
        n = 0
        for p in self.paras:
            for r in p.runs:
                if r.font.size is None:
                    continue
                if abs(float(r.font.size.pt) - pt) > 1e-6:
                    r.font.size = Pt(pt)
                    n += 1
        return n

    def set_bold(self, on: bool = True) -> int:
        n = 0
        for p in self.paras:
            for r in p.runs:
                if r.font.bold != on:
                    r.font.bold = on
                    n += 1
        return n

    def move_to(self, top: float | None = None, left: float | None = None,
                height: float | None = None) -> None:
        if top is not None:
            self.shape.top = Inches(round(top, 3))
            self.T = top
        if left is not None:
            self.shape.left = Inches(round(left, 3))
            self.L = left
        if height is not None:
            self.shape.height = Inches(round(height, 3))
            self.H = height


class Box:
    def __init__(self, shape, idx: int):
        self.shape = shape
        self.idx = idx
        self.name = shape.name
        self.T = _in(shape.top)
        self.L = _in(shape.left)
        self.W = _in(shape.width)
        self.H = _in(shape.height)

    @property
    def area(self) -> float:
        return self.W * self.H

    def contains(self, t: T, pad: float = 0.05) -> bool:
        cx = t.L + t.W / 2
        cy = t.T + max(t.H, 0.2) / 2
        return (self.L - pad <= cx <= self.L + self.W + pad
                and self.T - pad <= cy <= self.T + self.H + pad)


def is_text_shape(sh) -> bool:
    return bool(getattr(sh, "has_text_frame", False) and sh.has_text_frame
                and (sh.text_frame.text or "").strip())


# --------------------------------------------------------------------------- 分类

def classify(slide, texts: list[T], boxes: list[Box], sw: float = 13.333,
             sh: float = 7.5) -> dict[str, str]:
    """按几何与字号给每个文本定角色（不做任何写入）。

    顺序很重要：**卡片内文字先判**，否则高位的大数字会被当成页面标题。
    """
    roles: dict[str, str] = {}
    slide_area = sw * sh
    real_boxes = [b for b in boxes if b.area < 0.45 * slide_area]
    cards: dict[str, list[T]] = {}

    for t in texts:
        name, sz, top, left = t.name, t.size(), t.T, t.L
        txt = t.text.strip()
        if top >= MICRO_TOP - 0.02:
            roles[name] = "pageno" if left >= 11.0 else ("source" if left >= 6.4 else "footer")
            continue
        if top >= 6.0 and t.W >= 8.0 and sz <= 10.5:
            roles[name] = "source"
            continue
        if txt in ("\u201c", "\u201d", '"'):
            roles[name] = "mark"
            continue
        host = None
        for b in real_boxes:
            if b.area > 0.6 and b.W < 9.5 and b.H < 3.4 and b.contains(t):
                if host is None or b.area < host.area:
                    host = b
        if host is not None:
            cards.setdefault(host.name, []).append(t)
            continue
        # 占位符按**类型**定性：正文类占位符不是页面标题（生成稿正文占位符
        # top=1.9、22pt，曾被下面那条几何规则误判为 title → visual-dark 抬到
        # 30pt → 目录 8 条中第 7 条被页面下沿切断、第 8 条画出画布；
        # 监督 2026-10-20 18:07 独立复核实证，page-02-detail.png）。
        ph_kind = None
        shp = getattr(t, "shape", None)
        if shp is not None and getattr(shp, "is_placeholder", False):
            try:
                ph_kind = str(shp.placeholder_format.type)
            except Exception:  # noqa: BLE001
                ph_kind = None
        if ph_kind is not None:
            # "SUBTITLE" contains the substring "TITLE". Test it first so a
            # subtitle placeholder is not enlarged as a page title (26pt -> 30pt).
            if ph_kind.startswith("SUBTITLE"):
                roles[name] = "subtitle"
                continue
            if "TITLE" in ph_kind:
                roles[name] = "title"
                continue
            if "BODY" in ph_kind:
                roles[name] = "body"
                continue
        # 非占位符（自由文本框）：只有**真正位于页首带**的大字才是标题。
        # 生成稿标题占位符在 top≈0.55、正文区自 1.9 起；阈值 1.2 把两者分开
        # （旧阈值 2.0 会把正文区文字抬成 30pt 标题，目录/正文页同根因）。
        if top < 1.2 and sz >= 22:
            roles[name] = "title"
            continue
        if top < 1.6 and 10 <= sz <= 19:
            roles[name] = "subtitle"
            continue
        band = None
        for b in real_boxes:
            if b.area > 4.0 and b.H < 2.2 and b.contains(t):
                band = b
                break
        if band is not None and sz >= 14 and t.W >= band.W * 0.45:
            roles[name] = "quote"
            continue
        roles[name] = "body"

    # 卡片内按原字号排序定层级：>=20 → 数字；最大 → 标签；较小 → 说明
    for _card, items in cards.items():
        nums = [t for t in items if t.size() >= 20]
        rest = [t for t in items if t.size() < 20]
        for t in nums:
            roles[t.name] = "kpi_num"
        if rest:
            mx = max(t.size() for t in rest)
            for t in rest:
                roles[t.name] = "label" if t.size() >= mx - 0.01 else "sub"
    return roles


# --------------------------------------------------------------------------- 排版

def apply_type_scale(slide, texts, boxes, roles, acts) -> None:
    """按角色字号阶梯写入，带容量检查与卡片内垂直重排。"""
    for t in texts:
        role = roles.get(t.name)
        if role in (None, "mark", "kpi_num"):
            continue
        target = TARGET_PT.get(role)
        cur = t.size()
        if not target or not cur:
            continue
        if role in ("source", "footer", "pageno"):
            target = max(target, min(cur, MIN_PT)) if cur < MIN_PT else target
        # 只放大或保持；不缩小正文字号（避免把可读性又压回去）
        want = max(cur, target) if role in ("title", "subtitle", "quote", "label", "sub", "body") else target
        if role in ("source", "footer", "pageno"):
            want = max(cur, target)
        # 可长高的空间 = 自身高度 + 到"下方最近文字"的空隙（不许压到别人）。
        # label 类文字通常坐在固定尺寸的色块/门牌上（背景不会跟着文字长高），
        # 只允许在自身框高内变化，否则真实渲染必折行裁字（P1 门牌缺陷）。
        gap = 0.30
        for o in texts:
            if o is t:
                continue
            if o.T >= t.T + t.H - 0.03 and abs(o.L - t.L) < max(t.W, o.W) * 0.8:
                gap = min(gap, max(0.0, o.T - (t.T + t.H)))
        allowed = t.H + max(0.0, gap - 0.03)
        if role in ("label", "source", "footer", "pageno"):
            allowed = min(allowed, t.H + 0.06)
        # 容量检查用**真实字体度量**：估算模型会低估中西文混排宽度，
        # 放行实际会折行的字号（P2 压字 / P1 门牌裁字同根因）。
        pt = want
        while pt > cur and real_text_h_in(t.text, pt, t.W) > allowed:
            pt = round(pt - 0.5, 1)
        if pt < cur:
            pt = cur
        if abs(pt - cur) > 1e-6:
            n = t.set_pt(pt)
            new_h = round(min(real_text_h_in(t.text, pt, t.W) + 0.02, allowed), 3)
            if new_h > t.H + 0.01:
                t.move_to(height=new_h)
            acts.append({"rule": f"{role}_size", "shape": t.name,
                         "from": cur, "to": pt, "runs": n,
                         "h": [round(t.H, 2)]})
        if role in ("title", "quote"):
            if t.set_bold(True):
                acts.append({"rule": f"{role}_bold", "shape": t.name})
        # 来源/页脚：下方留白不足时允许向上长高（不压正文）
        if role == "source" and real_text_h_in(t.text, t.size(), t.W) > t.H + 0.04:
            need = real_text_h_in(t.text, t.size(), t.W)
            new_h = round(min(need + 0.06, 0.7), 2)
            new_t = round(max(0.0, t.T + t.H - new_h), 2)
            t.move_to(top=new_t, height=new_h)
            acts.append({"rule": "source_refit", "shape": t.name,
                         "T": new_t, "H": new_h})
    # 卡片内重排：先按 x 重叠聚簇，只有**上下叠放**的一簇才做垂直堆叠；
    # 左右并排（如"标签 + 说明"）各自垂直居中，互不干扰。
    for b in boxes:
        if b.area <= 0.6 or b.area >= 0.45 * 13.333 * 7.5 or b.H > 3.4:
            continue
        inside = [t for t in texts if b.contains(t) and roles.get(t.name) in
                  ("kpi_num", "label", "sub", "body")]
        if not inside:
            continue
        clusters: list[list[T]] = []
        for t in sorted(inside, key=lambda x: x.L):
            placed = False
            for cl in clusters:
                ref = cl[0]
                lo = max(t.L, ref.L)
                hi = min(t.L + t.W, ref.L + ref.W)
                if hi - lo > 0.5 * min(t.W, ref.W):
                    cl.append(t)
                    placed = True
                    break
            if not placed:
                clusters.append([t])
        for cl in clusters:
            if len(cl) == 1:
                continue                      # 单元素不做垂直居中：会与同排文字重叠
            cl.sort(key=lambda x: x.T)
            # 原本就重叠（左右并排被误聚）→ 跳过
            overlap = any(cl[i].T + cl[i].H - 0.03 > cl[i + 1].T for i in range(len(cl) - 1))
            if overlap:
                continue
            heights = [real_text_h_in(t.text, t.size() or 12, t.W) for t in cl]
            gap = 0.03
            total = sum(heights) + gap * (len(cl) - 1)
            if total > b.H - 0.04:
                continue
            cur = b.T + (b.H - total) / 2
            moved = False
            for t, h in zip(cl, heights):
                if abs(t.T - cur) > 0.02:
                    t.move_to(top=round(cur, 3))
                    moved = True
                cur += h + gap
            if moved:
                acts.append({"rule": "card_stack_center", "card": b.name,
                             "items": len(cl)})


# --------------------------------------------------------------------------- 配图

def _set_alpha(shape, alpha_pct: int) -> None:
    """给纯色填充加透明度（0=不透明，100=全透明）。"""
    spPr = shape._element.spPr
    solid = spPr.find(qn("a:solidFill"))
    if solid is None:
        return
    srgb = solid.find(qn("a:srgbClr"))
    if srgb is None:
        return
    for old in srgb.findall(qn("a:alpha")):
        srgb.remove(old)
    el = etree.SubElement(srgb, qn("a:alpha"))
    el.set("val", str(int((100 - alpha_pct) * 1000)))


def first_text_el(slide):
    for sh in list(slide.shapes):
        if is_text_shape(sh):
            return sh._element
    return None


def insert_below_text(slide, new_el) -> None:
    """把形状放到「所有文字之下」：插到第一个文字形状前面。"""
    spTree = slide.shapes._spTree
    spTree.remove(new_el)
    target = first_text_el(slide)
    if target is None:
        spTree.append(new_el)
    else:
        target.addprevious(new_el)


def _cover_srcrect(img: Path, w: float, h: float,
                   fx: float = 0.5, fy: float = 0.5) -> dict:
    """按 cover 原则计算 a:srcRect 裁切（单位 1/100000）。

    可见区宽高比 = 容器宽高比 → 横纵缩放相等（=1），不拉伸。
    fx/fy 为内容焦点（0=左/上，1=右/下，默认居中）；裁掉的一侧
    按焦点比例分配，主体留在可见区内。
    """
    from PIL import Image as _PILImage
    with _PILImage.open(img) as im:
        nw, nh = im.size
    dr, nr = w / h, nw / nh
    l = t = r = b = 0.0
    if dr > nr:                       # 容器更宽：裁上下，保留全宽
        cut = 1.0 - (nw / dr) / nh
        t, b = cut * (1.0 - fy), cut * fy
    else:                             # 容器更窄：裁左右，保留全高
        cut = 1.0 - (nh * dr) / nw
        l, r = cut * (1.0 - fx), cut * fx
    return {k: int(round(v * 100000))
            for k, v in (("l", l), ("t", t), ("r", r), ("b", b))}


def _apply_srcrect(pic, bounds: dict) -> None:
    """把 srcRect 写进图片的 p:blipFill（位于 a:blip 之后）。"""
    blipFill = pic._element.find(qn("p:blipFill"))
    if blipFill is None:
        return
    for old in blipFill.findall(qn("a:srcRect")):
        blipFill.remove(old)
    blip = blipFill.find(qn("a:blip"))
    sr = etree.Element(qn("a:srcRect"))
    for k in ("l", "t", "r", "b"):
        sr.set(k, str(int(bounds.get(k, 0))))
    if blip is not None:
        blip.addnext(sr)
    else:
        blipFill.insert(0, sr)


def add_image(slide, img: Path, rect, dark_pct: int, acts, page: int,
              rounded: bool = False, focus: tuple = (0.5, 0.5),
              fit: str = "cover") -> None:
    """在文字层之下插入图片 + 遮罩。

    fit="cover"：按 focus 焦点做等比例裁切（srcRect），横纵缩放相等，
    不拉伸；fit="contain"：整图等比缩放装入，不足处留背景（letterbox）。
    """
    left, top, w, h = rect
    pic = slide.shapes.add_picture(str(img), Inches(left), Inches(top),
                                   Inches(w), Inches(h))
    if fit == "cover":
        _apply_srcrect(pic, _cover_srcrect(img, w, h, *focus))
    acts.append({"rule": "image", "page": page, "file": img.name,
                 "rect": [round(x, 2) for x in rect], "overlay": dark_pct,
                 "fit": fit, "focus": list(focus)})
    insert_below_text(slide, pic._element)
    # 遮罩
    ov = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(left), Inches(top),
                                Inches(w), Inches(h))
    ov.fill.solid()
    ov.fill.fore_color.rgb = RGBColor(0x10, 0x10, 0x14)
    ov.line.fill.background()
    _set_alpha(ov, dark_pct)
    insert_below_text(slide, ov._element)
    acts.append({"rule": "image", "page": page, "file": img.name,
                 "rect": [round(x, 2) for x in rect], "overlay": dark_pct})


# --------------------------------------------------------------------------- 转场

P14_NS = "http://schemas.microsoft.com/office/powerpoint/2010/main"

#: 统一转场语言：全稿淡入（fade），用速度分档建立节奏。
#: 普通页 0.8s（0.6—1.2s 区间）；关键页 1.6s（1.2—2.4s 区间）。
TRANS_PLAN = {
    "key_pages": {1, 8, 13},          # 封面 / 中段转折 / 收束结论
    "normal": {"kind": "fade", "spd": "med", "dur_ms": 800},
    "key": {"kind": "fade", "spd": "slow", "dur_ms": 1600},
}


def transition_for(page: int) -> dict:
    plan = TRANS_PLAN["key"] if page in TRANS_PLAN["key_pages"] else TRANS_PLAN["normal"]
    return dict(plan)


def add_transition(slide, kind: str = "fade", spd: str = "med",
                   dur_ms: int | None = None, advance_ms: int | None = None) -> None:
    """统一转场语言（默认淡入）。

    spd 分档建立节奏；dur_ms 写入 p14:dur 精确时长（PowerPoint 2010+）。
    advance_ms **仅供录像专用副本**启用自动换页；交付稿一律不传，
    保持点击翻页，不改变讲解控制方式。
    """
    sld = slide._element
    for old in sld.findall(qn("p:transition")):
        sld.remove(old)
    attrs = {"spd": spd}
    if advance_ms is not None:
        attrs["advTm"] = str(int(advance_ms))
    if dur_ms is not None:
        attrs["{%s}dur" % P14_NS] = str(int(dur_ms))
    tr = etree.SubElement(sld, qn("p:transition"), nsmap={"p": P_NS, "p14": P14_NS},
                          **attrs)
    etree.SubElement(tr, qn("p:" + kind))
    # 必须位于 clrMapOvr 之后、timing 之前
    clr = sld.find(qn("p:clrMapOvr"))
    sld.remove(tr)
    pos = list(sld).index(clr) + 1 if clr is not None else len(list(sld))
    sld.insert(pos, tr)


def set_auto_advance(prs, ms_map: dict) -> int:
    """给指定页写 advTm（毫秒自动换页）——**只用于录像副本**，不改交付稿。"""
    n = 0
    for i, slide in enumerate(prs.slides, 1):
        if i not in ms_map:
            continue
        tr = slide._element.find(qn("p:transition"))
        if tr is None:
            continue
        tr.set("advTm", str(int(ms_map[i])))
        n += 1
    return n


# --------------------------------------------------------------------------- 粒子动画

#: 单粒子 = 入场(set 可见 + fade) + 位移(animMotion，pathEditMode=relative)，
#: 位置与透明度都真实变化；auto-start 结构让粒子随页面出现自动播放，
#: 用户正常翻页即可看完，不需要逐个点击粒子。
EFFECT_TMPL = """
<p:par><p:cTn id="{i0}" presetID="10" presetClass="entr" presetSubtype="0" fill="hold" grpId="0" nodeType="{node}">
  <p:stCondLst><p:cond delay="{delay}"/></p:stCondLst>
  <p:childTnLst>
    <p:set><p:cBhvr>
      <p:cTn id="{i1}" dur="1" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst></p:cTn>
      <p:tgtEl><p:spTgt spid="{spid}"/></p:tgtEl>
      <p:attrNameLst><p:attrName>style.visibility</p:attrName></p:attrNameLst>
    </p:cBhvr><p:to><p:strVal val="visible"/></p:to></p:set>
    <p:animEffect transition="in" filter="fade">
      <p:cBhvr><p:cTn id="{i2}" dur="{dur}" fill="hold"/><p:tgtEl><p:spTgt spid="{spid}"/></p:tgtEl></p:cBhvr>
    </p:animEffect>
    <p:animMotion origin="layout" path="{path}" pathEditMode="relative">
      <p:cBhvr>
        <p:cTn id="{i3}" dur="{dur}" fill="hold"/>
        <p:tgtEl><p:spTgt spid="{spid}"/></p:tgtEl>
        <p:attrNameLst><p:attrName>ppt_x</p:attrName><p:attrName>ppt_y</p:attrName></p:attrNameLst>
      </p:cBhvr>
    </p:animMotion>
  </p:childTnLst>
</p:cTn></p:par>
"""

#: 主序列首节点 delay=0（非 indefinite）→ 动画随页面出现自动开始，
#: 不是点击触发；nextAc="seek" 保留正常翻页语义。
TIMING_TMPL = """<p:timing xmlns:p="{p}" xmlns:a="{a}">
<p:tnLst><p:par><p:cTn id="1" dur="indefinite" restart="never" nodeType="tmRoot"><p:childTnLst>
<p:seq concurrent="1" nextAc="seek"><p:cTn id="2" dur="indefinite" nodeType="mainSeq"><p:childTnLst>
<p:par><p:cTn id="3" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>
<p:par><p:cTn id="4" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst><p:childTnLst>
{effects}
</p:childTnLst></p:cTn></p:par>
</p:childTnLst></p:cTn></p:par>
</p:childTnLst></p:cTn>
<p:prevCondLst><p:cond evt="onPrev" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:prevCondLst>
<p:nextCondLst><p:cond evt="onNext" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:nextCondLst>
</p:seq></p:childTnLst></p:cTn></p:par></p:tnLst>
</p:timing>"""


def _motion_path(mode: str, rnd: random.Random, x: float, y: float,
                 left: float, top: float, w: float, h: float) -> str:
    """按模式生成相对位移路径（单位=页宽/页高比例，正值右/下）。

    converge：从周围偏移向最终位置聚合（封面"聚"）。
    drift   ：小幅缓慢漂移（中段"漂"）。
    disperse：以区域中心为原点向外散开（收束页"散"）。
    """
    if mode == "converge":
        ang = rnd.uniform(0, 2 * math.pi)
        mag = rnd.uniform(0.022, 0.048)
        return "M %.4f %.4f L 0 0" % (mag * math.cos(ang), mag * math.sin(ang))
    if mode == "disperse":
        cx, cy = left + w / 2, top + h / 2
        dx = (x - cx) / max(w, 0.1)
        dy = (y - cy) / max(h, 0.1)
        mag = rnd.uniform(0.06, 0.11)
        n = max(0.25, math.hypot(dx, dy))
        return "M 0 0 L %.4f %.4f" % (dx / n * mag, dy / n * mag)
    # drift
    return "M 0 0 L %.4f %.4f" % (rnd.uniform(-0.028, 0.028),
                                   rnd.uniform(-0.05, -0.02))


def add_particles(slide, rect, count: int, color: str, seed: int,
                  mode: str = "drift", dur: int = 2200,
                  base_delay: int = 150, stagger: int = 40,
                  alpha_pct: int = 32, acts=None, page: int = 0) -> int:
    """在 rect 内撒粒子，做成"依次淡入 + 相对位移"的原生对象动画（自动播放）。"""
    left, top, w, h = rect
    rnd = random.Random(seed)
    spids, paths = [], []
    for i in range(count):
        r = rnd.uniform(0.018, 0.05)
        x = left + rnd.uniform(0.03, max(0.04, w - 0.08))
        y = top + rnd.uniform(0.03, max(0.04, h - 0.08))
        paths.append(_motion_path(mode, rnd, x, y, left, top, w, h))
        dot = slide.shapes.add_shape(MSO_SHAPE.OVAL, Inches(round(x, 3)),
                                     Inches(round(y, 3)), Inches(round(r, 3)),
                                     Inches(round(r, 3)))
        dot.fill.solid()
        dot.fill.fore_color.rgb = RGBColor.from_string(color)
        dot.line.fill.background()
        _set_alpha(dot, alpha_pct)
        insert_below_text(slide, dot._element)
        spids.append(dot.shape_id)

    effects = []
    i = 5
    # 自动触发（监督实播 0347 证据）：任何 clickEffect 都会成为播放器的点击门。
    # 首粒子 = afterEffect（随页面出现即起播，delay=base_delay 起拍）；
    # 其余 = withEffect（与前一动画并行，绝对偏移 150/190/230...ms 错峰）。
    # 整链构成一个自动单元：进页即自动运行，翻页键恢复纯翻页语义。
    for k, spid in enumerate(spids):
        node = "afterEffect" if k == 0 else "withEffect"
        delay = base_delay + k * stagger
        effects.append(EFFECT_TMPL.format(i0=i, i1=i + 1, i2=i + 2, i3=i + 3,
                                          node=node, delay=delay, dur=dur,
                                          spid=spid, path=paths[k]))
        i += 4
    xml = TIMING_TMPL.format(p=P_NS, a=A_NS, effects="".join(effects))
    timing = etree.fromstring(xml.encode("utf-8"))
    sld = slide._element
    for old in sld.findall(qn("p:timing")):
        sld.remove(old)
    sld.append(timing)
    if acts is not None:
        acts.append({"rule": "particles", "page": page, "count": count,
                     "mode": mode, "stagger_ms": stagger, "duration_ms": dur,
                     "trigger": "auto"})
    return count




# --------------------------------------------------------------------------- 真实字体度量

#: 渲染链实际字体：Microsoft YaHei/微软雅黑 → Hiragino Sans GB（见包内 fonts.conf）。
#: 用它做**真实**换行预测，替代估算模型——估算模型曾低估宽度导致 P2 指标卡压字。
HIragino = "/System/Library/Fonts/Hiragino Sans GB.ttc"
_FONT_CACHE: dict = {}


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return o > 0x2E80 and o < 0xFFE6


def _auto_spaced(text: str) -> str:
    """模拟 LibreOffice/PowerPoint 的中西文自动间距（CJK↔拉丁/数字边界插空格）。

    实测：Hiragino Sans GB 下该间距接近一个空格宽度；不模拟就会低估宽度、
    漏判折行（P2 指标卡缺陷的直接原因）。
    """
    out, prev = [], None
    for ch in text:
        if prev is not None and ch not in " \t" and prev not in " \t":
            if _is_cjk(prev) != _is_cjk(ch):
                out.append(" ")
        out.append(ch)
        prev = ch
    return "".join(out)


def _pil_font(pt: float):
    key = round(pt, 1)
    if key not in _FONT_CACHE:
        from PIL import ImageFont
        _FONT_CACHE[key] = ImageFont.truetype(HIragino, max(8, int(round(pt * 8))))
    return _FONT_CACHE[key]


def real_text_w_in(text: str, pt: float) -> float:
    """真实字体度量宽度（英寸，含中西文自动间距模拟）。"""
    if not text:
        return 0.0
    try:
        return _pil_font(pt).getlength(_auto_spaced(text)) / 8.0 / 72.0
    except Exception:
        return text_width_in(_auto_spaced(text), pt)


def real_wrapped_lines(text: str, pt: float, box_w: float) -> list[str]:
    """真实度量下的逐行文本（与 real_lines 完全同一断行规则）。

    供「标题孤字」检测使用：需要知道最后一行到底剩几个字，而不是只数行数。
    """
    box_w = max(box_w, 0.2)
    out: list[str] = []
    for para in (text.split("\n") or [""]):
        if not para:
            out.append("")
            continue
        units: list[str] = []
        buf = ""
        for ch in _auto_spaced(para):
            if ch == " ":
                if buf:
                    units.append(buf)
                    buf = ""
                units.append(" ")
            elif _is_cjk(ch):
                if buf:
                    units.append(buf)
                    buf = ""
                units.append(ch)
            else:
                buf += ch
        if buf:
            units.append(buf)
        line, cur = "", 0.0
        for u in units:
            w = real_text_w_in(u, pt)
            if u == " ":
                if cur > 0:
                    line += " "
                    cur += w       # 空格随行；是否折行由下一个单元判定
                continue
            if cur > 0 and cur + w > box_w + 1e-9:
                out.append(line)
                line, cur = u, w
            else:
                line += u
                cur += w
        out.append(line)
    return out


def real_lines(text: str, pt: float, box_w: float) -> int:
    """真实度量下的折行数。

    折行单元规则（与 LibreOffice/PowerPoint 的 CJK 断行一致）：
    - 拉丁/数字连续串是一个不可断单元（词内不断行）；
    - CJK 与全角标点逐字可断（此前按空格分词，无空格 CJK 长串被当成
      一个不可断词，超宽也不折行 → P1 门牌 12pt 实宽 3.44 > 框宽 3.34
      仍按 1 行放行，真实渲染折行裁字）；
    - 超过框宽的单元强制计入新行。
    """
    return max(1, len(real_wrapped_lines(text, pt, box_w)))


def _container_right_in(slide, sh, slide_w: float) -> float:
    """文本框所在卡片（最小的、能完整包住它的无字形状）的右内边界。

    找不到容器时退回版面右边距；用于判断"卡内还有多少空白可借"。
    """
    l, t = _in(sh.left), _in(sh.top)
    w, h = _in(sh.width), _in(sh.height)
    best = None
    for c in slide.shapes:
        if c is sh or is_text_shape(c):
            continue
        cl, ct, cw, chh = _in(c.left), _in(c.top), _in(c.width), _in(c.height)
        if cw < w or chh < h or cw < 1.0:
            continue
        if (cl - 0.03 <= l and ct - 0.03 <= t
                and l + w <= cl + cw + 0.03 and t + h <= ct + chh + 0.03):
            if best is None or cw < best[0]:
                best = (cw, cl)
    if best is not None:
        return best[1] + best[0] - 0.02
    return slide_w - 0.4


def fix_orphan_title(slide, acts=None, page: int = 0, slide_w: float = 13.333) -> list[dict]:
    """标题孤字治理（结构规则，不绑页码/形状名/输入哈希）。

    检测：单段、统一字号（≥12.5pt）、框高≤0.6in、自动换行的短文本框，
    当前宽度下恰好折成两行，且**末行只剩一个 CJK 字**（孤字）。
    处理：若"整句真实宽度"能放进卡内可用宽度（容器右内边界 − 框左边），
    就把框宽加到恰好单行（上限=可用宽度），不改文字、字号、位置与动画。
    放不进则不动（留给人工/后续平衡两行方案），并记录 skipped。
    """
    changed, skipped = [], []
    for sh in list(slide.shapes):
        if not is_text_shape(sh):
            continue
        tf = sh.text_frame
        if not tf.word_wrap:
            continue
        text = tf.text
        if not text.strip() or "\n" in text or "\v" in text:
            continue
        paras = [p for p in tf.paragraphs if p.text.strip()]
        if len(paras) != 1:
            continue
        runs = [r for r in paras[0].runs if r.text]
        if not runs:
            continue
        sizes = {r.font.size.pt if r.font.size else None for r in runs}
        if len(sizes) != 1:
            continue
        pt = sizes.pop()
        if pt is None or pt < 12.5:
            continue
        w, h = _in(sh.width), _in(sh.height)
        if h > 0.6 or w < 0.8:
            continue
        lines = real_wrapped_lines(text, pt, w)
        if len(lines) != 2:
            continue
        last = lines[-1].strip()
        if len(last) != 1 or not _is_cjk(last):
            continue
        need = real_text_w_in(text, pt)
        avail = _container_right_in(slide, sh, slide_w) - _in(sh.left)
        if need + 0.02 > avail:
            skipped.append({"shape": sh.name, "need": round(need, 3),
                            "avail": round(avail, 3), "why": "card_space_insufficient"})
            continue
        new_w = round(min(avail, need + 0.04), 3)
        if new_w <= w + 0.01:
            continue
        if len(real_wrapped_lines(text, pt, new_w)) != 1:
            skipped.append({"shape": sh.name, "need": round(need, 3),
                            "avail": round(avail, 3), "why": "still_wraps"})
            continue
        sh.width = Inches(new_w)
        changed.append({"shape": sh.name, "from_w": round(w, 3), "to_w": new_w,
                        "need": round(need, 3), "avail": round(avail, 3),
                        "text": text})
    if acts is not None and (changed or skipped):
        acts.append({"rule": "fix_orphan_title", "page": page,
                     "widened": changed, "skipped": skipped})
    return changed


def fix_autofit_capacity(slide, acts=None, page: int = 0) -> list[dict]:
    """Restore room for a single line when a thin native box forces renderer shrinkage."""
    changed = []
    texts = [T(s, i) for i, s in enumerate(slide.shapes) if is_text_shape(s)]
    boxes = [Box(s, i) for i, s in enumerate(slide.shapes)
             if not is_text_shape(s) and _in(s.width) > 0.5 and _in(s.height) > 0.3]
    for t in texts:
        pt = t.size()
        if pt < 12 or t.H >= pt / 72 or len(t.text.strip()) < 2:
            continue
        if real_lines(t.text, pt, t.W) != 1:
            continue
        needed = real_text_h_in(t.text, pt, t.W) + 0.02
        top = t.T + (t.H - needed) / 2
        hosts = [b for b in boxes if b.contains(t) and b.W >= t.W
                 and top >= b.T + 0.02 and top + needed <= b.T + b.H - 0.02]
        if not hosts:
            continue
        overlaps = any(o is not t and min(t.L + t.W, o.L + o.W) > max(t.L, o.L) + 0.02
                       and min(top + needed, o.T + o.H) > max(top, o.T) + 0.02
                       for o in texts)
        if overlaps:
            continue
        old = [t.T, t.H]
        t.move_to(top=top, height=needed)
        changed.append({"shape": t.name, "before": old, "after": [t.T, t.H], "pt": pt})
    if changed and acts is not None:
        acts.append({"rule": "fix_autofit_capacity", "page": page, "changed": changed})
    return changed


def real_text_h_in(text: str, pt: float, box_w: float,
                   line_factor: float = 1.32) -> float:
    return real_lines(text, pt, box_w) * pt * line_factor / 72.0


def _shape_has_opaque_fill(shape) -> bool:
    try:
        fill = shape.fill
        if fill.type is None:
            return False
        transparency = fill.transparency
        return transparency is None or float(transparency) < 0.97
    except Exception:
        # The installed python-pptx here has no FillFormat.transparency at
        # all (AttributeError on every call, not just an unreadable value),
        # so this is the ordinary path for any solid-filled shape, not a
        # rare fallback: check the XML directly for a real <a:solidFill>.
        try:
            return shape._element.spPr.find(
                "{http://schemas.openxmlformats.org/drawingml/2006/main}solidFill") is not None
        except Exception:
            return False


def _backdrop_shapes_for(slide, rect, *, exclude) -> list:
    """Plain, textless, opaquely-filled shapes mostly behind `rect`.

    A quote/highlight card's solid-fill background is a separate shape from
    the text it frames, with no text of its own — is_text_shape excludes it,
    so the push-down loop below never sees it as something to move. A
    background that is mostly behind this one text box (not a full-page
    backdrop: its own area is comparable to the text box's, not many times
    larger, so the overlap ratio against *its own* area stays high) is this
    text's own card and must move with it, or it is left stranded exactly
    where the text used to be instead of where the text now is.
    """
    left, top, width, height = rect
    out = []
    for other in slide.shapes:
        # `in exclude`/`is` never matches shape wrappers reliably: python-pptx
        # hands back a fresh wrapper object on each iteration, even for the
        # same underlying XML element, so identity/equality checks against a
        # previously-captured wrapper silently fail — shape_id is the
        # element's stable identity, not the wrapper's.
        if other.shape_id in exclude or is_text_shape(other):
            continue
        o_l, o_t = _in(other.left), _in(other.top)
        o_w, o_h = _in(other.width), _in(other.height)
        other_area = o_w * o_h
        if other_area <= 0:
            continue
        ix = max(0.0, min(left + width, o_l + o_w) - max(left, o_l))
        iy = max(0.0, min(top + height, o_t + o_h) - max(top, o_t))
        if (ix * iy) / other_area < 0.5:
            continue
        if not _shape_has_opaque_fill(other):
            continue
        out.append(other)
    return out


def fix_stacked_text_overlap(slide, acts=None, page: int = 0,
                             gap: float = 0.04, slide_h_in: float | None = None,
                             bottom_margin: float = 0.15,
                             slide_w_in: float | None = None) -> list[dict]:
    """Keep a text shape's own content on-slide, and push a later text shape
    down when the real (wrapped) height of the text shape above it bleeds
    past its nominal box into the next one.

    Font size is often raised after this geometry is laid out (e.g. the
    readability floor), which makes text that used to fit wrap onto more
    lines than its box was sized for. Two symptoms follow, both handled here
    from the same real-height measurement: the shape's own text can run past
    its own box (and even off the bottom of the slide, with no other shape
    to detect against), and/or it can visually bleed into whatever text box
    comes next. Neither font size nor text content is ever touched — only a
    shape's own `height` (grown just enough to hold its real content, capped
    at the slide's bottom edge so it is never pushed off-canvas) and a later
    shape's `top` (moved down only when the shape above it genuinely needs
    more room than its box has). Only shapes whose horizontal extents
    actually overlap are considered, so side-by-side columns are left alone.
    """
    changed = []
    texts = sorted([T(s, i) for i, s in enumerate(slide.shapes) if is_text_shape(s)],
                   key=lambda t: t.T)
    for i, a in enumerate(texts):
        pt = a.size()
        if pt <= 0 or not a.text.strip():
            continue
        # A shape can be positioned past the canvas bottom margin on its
        # own declared geometry, independent of whether its content wraps
        # to fit that box or not (seen on a real deck: a textbox's own
        # `top` already left only its very last sliver above the slide
        # edge). Growing height never helps here — there is no room left
        # to grow into — so pull the box itself back up first, before any
        # content-driven check below, as long as that does not run it
        # into something genuinely above it.
        if slide_h_in is not None and a.T + a.H > slide_h_in - bottom_margin + 0.01:
            min_top = 0.0
            for other in slide.shapes:
                if other is a.shape:
                    continue
                o_l, o_w = _in(other.left), _in(other.width)
                if min(a.L + a.W, o_l + o_w) <= max(a.L, o_l) + 0.02:
                    continue  # different column
                o_b = _in(other.top) + _in(other.height)
                if o_b <= a.T + 0.02 and o_b > min_top:
                    min_top = o_b + gap
            new_top = max(min_top, slide_h_in - bottom_margin - a.H)
            if new_top < a.T - 0.01:
                old_top = a.T
                a.move_to(top=new_top)
                changed.append({"shape": a.name, "before": [old_top, a.H],
                                "after": [a.T, a.H], "pulled_up_into_canvas": True})
        real_h = real_text_h_in(a.text, pt, a.W)
        # real_text_h_in's line-wrap model is passed the shape's full width;
        # it does not know about a bullet's indent eating into the usable
        # width, nor about inter-paragraph spacing — both make it under-
        # count wrapped lines for bulleted, multi-paragraph body text (the
        # common case this whole function exists for), badly enough that it
        # can miss a real overflow entirely, not just under-size the fix.
        # Use this more cautious estimate to decide whether there is a
        # problem at all, not only how much to grow by: a generous estimate
        # here only costs a slightly taller box or a slightly smaller font,
        # while an optimistic one leaves text cut off past the slide edge,
        # which is the defect being fixed.
        margin_w = max(0.2, a.W - 0.5)
        spacing_in = sum(
            (float(p.space_before.pt) if p.space_before is not None else 0.0)
            + (float(p.space_after.pt) if p.space_after is not None else 0.0)
            for p in a.paras) / 72.0
        # +0.1in flat buffer on top of the formula: the real layout/export
        # gate that actually decides pass/fail checks true PDF glyph bounds
        # against a tight bottom safety margin, and this estimate — however
        # it is refined — can never perfectly reproduce that measurement. A
        # fixed buffer catches the narrow, easy-to-miss violations (a few
        # hundredths to tenths of an inch past the margin) that a purely
        # formula-driven estimate keeps landing just short of.
        needed_h = max(real_h, real_text_h_in(a.text, pt, margin_w) * 1.15 + spacing_in + 0.1)
        if needed_h > a.H + 0.01:
            cap = (slide_h_in - bottom_margin - a.T) if slide_h_in else None
            # A shape's box can have been deliberately shrunk by an earlier
            # reflow to make room for something placed right below it (a
            # photo in a reflowed row, a caption, ...). Growing past that
            # neighbor's top edge would grow this text right on top of it,
            # which is worse than the overflow being fixed — so any shape
            # that genuinely sits below (not beside) this one caps how far
            # it may grow, the same way the canvas edge does.
            for other in slide.shapes:
                if other is a.shape:
                    continue
                o_l, o_w = _in(other.left), _in(other.width)
                if min(a.L + a.W, o_l + o_w) <= max(a.L, o_l) + 0.02:
                    continue  # different column — no horizontal overlap
                o_t = _in(other.top)
                if o_t <= a.T + 0.02:
                    continue  # not below this shape's own top
                neighbor_cap = o_t - a.T - gap
                cap = neighbor_cap if cap is None else min(cap, neighbor_cap)
            # Growing a shape's own `height` never moves where its (default
            # top-anchored) text actually renders — PowerPoint/LibreOffice do
            # not clip text to a shape's box, so the glyphs land wherever the
            # content naturally wraps to regardless of the declared box size.
            # Growing only makes the box's declared bounds honestly describe
            # that content (fixes an "owner box too small" finding); it can
            # never pull the real content's bottom edge up off the canvas
            # edge. Shrinking the font is NOT a safe second lever here: a
            # placeholder's role (e.g. "body") has an enforced pt range
            # (scripts/layout_gate.py ROLE_PT), and body text is laid out at
            # that range's floor already, so shrinking it trades a canvas-
            # margin violation for a role-size violation instead of fixing
            # anything. Reducing inter-paragraph spacing does move the real
            # bottom edge up (every paragraph after the first renders higher)
            # without touching font size, so try that instead.
            new_h = min(needed_h, max(a.H, cap)) if cap else needed_h
            spacing_change = None
            content_bottom = a.T + needed_h
            # Compare against whichever boundary actually constrains this
            # shape — a neighbor sitting right below it, if that is closer
            # than the canvas margin — not always the canvas margin itself.
            boundary = (a.T + cap) if cap is not None else None
            if boundary is not None and content_bottom > boundary + 0.01:
                spacing_floor_pt = 4.0
                cur_spacing_pt = spacing_in * 72.0
                floor_total_pt = spacing_floor_pt * 2 * len(a.paras) if a.paras else 0.0
                if cur_spacing_pt > floor_total_pt + 1e-6:
                    overflow_pt = (content_bottom - boundary) * 72.0
                    target_pt = max(floor_total_pt, cur_spacing_pt - overflow_pt - 2.0)
                    scale = target_pt / cur_spacing_pt
                    for p in a.paras:
                        for attr in ("space_before", "space_after"):
                            cur = getattr(p, attr)
                            if cur is None or float(cur.pt) <= spacing_floor_pt:
                                continue
                            setattr(p, attr, Pt(round(max(spacing_floor_pt, float(cur.pt) * scale), 1)))
                    new_spacing_in = sum(
                        (float(p.space_before.pt) if p.space_before is not None else 0.0)
                        + (float(p.space_after.pt) if p.space_after is not None else 0.0)
                        for p in a.paras) / 72.0
                    if new_spacing_in < spacing_in - 0.001:
                        cand_h = real_text_h_in(a.text, pt, margin_w) * 1.15 + new_spacing_in + 0.1
                        spacing_change = [round(spacing_in, 3), round(new_spacing_in, 3)]
                        new_h = min(cand_h, cap)
            if new_h > a.H + 0.01 or spacing_change is not None:
                old_self = [a.T, a.H]
                a.move_to(height=new_h)
                entry = {"shape": a.name, "before": old_self, "after": [a.T, a.H],
                         "self_grow": True, "real_h": round(real_h, 3)}
                if spacing_change is not None:
                    entry["paragraph_spacing_in"] = spacing_change
                changed.append(entry)
        # A long CJK line that wraps can leave a single trailing character
        # alone on its own line (a typographic "widow") whenever the real
        # usable width — the box width minus the bullet marker's indent,
        # which real_wrapped_lines is not told about — falls just short of
        # fitting it. Widening the box (only into space nothing else needs:
        # never past the canvas margin or a neighboring shape) pulls that
        # character back onto the line above without touching font size or
        # text content, so try it whenever such a widow is detected.
        if slide_w_in is not None:
            # A lone trailing punctuation mark (a line ending in "。" etc.)
            # is not a widow — the gate itself excludes these — so testing
            # against that same exclusion set here keeps this check from
            # "fixing" lines that were never actually flagged.
            _punct = "，。、；：！？“”‘’（）()·—…"
            orphaned = any(
                len(lns := real_wrapped_lines("".join(r.text for r in p.runs), a.size() or pt, margin_w)) >= 2
                and len(lns[-1].strip()) == 1 and lns[-1].strip() not in _punct
                for p in a.paras if "".join(r.text for r in p.runs).strip())
            if orphaned:
                # 0.15in here cost a real fix: job-c0af57f36099 P10 needed
                # the box to reach within 0.10in of the photo beside it to
                # pull "元" back onto the line above, and the authoritative
                # gate (collisions/occlusions, both pixel-measured) is
                # clean at that gap — 0.15 was just an untested guess.
                neighbor_gap = 0.10
                left_limit, right_limit = 0.40, slide_w_in - 0.40
                for other in slide.shapes:
                    if other is a.shape:
                        continue
                    o_t, o_h = _in(other.top), _in(other.height)
                    if o_t + o_h <= a.T or a.T + a.H <= o_t:
                        continue  # no vertical overlap with this shape
                    o_l, o_w = _in(other.left), _in(other.width)
                    if o_l >= a.L + a.W:
                        right_limit = min(right_limit, o_l - neighbor_gap)
                    elif o_l + o_w <= a.L:
                        left_limit = max(left_limit, o_l + o_w + neighbor_gap)
                max_w = right_limit - left_limit

                def _any_orphan(width: float) -> bool:
                    test_margin_w = max(0.2, width - 0.5)
                    return any(
                        len(lns := real_wrapped_lines("".join(r.text for r in p.runs), a.size() or pt, test_margin_w)) >= 2
                        and len(lns[-1].strip()) == 1 and lns[-1].strip() not in _punct
                        for p in a.paras if "".join(r.text for r in p.runs).strip())

                if max_w > a.W + 0.01:
                    # Jumping straight to the widest available width can
                    # land in a *different* bad wrap point than the one
                    # that triggered this fix — this heuristic word-wrap
                    # model doesn't track LibreOffice's text shaping
                    # exactly, especially for mixed digit+CJK runs, so a
                    # width that looks clear in this model's own estimate
                    # can still wrap badly in the real render. Searching
                    # outward in small steps and stopping at the first
                    # width this model itself sees as clear — rather than
                    # the widest one — stays closer to the original wrap
                    # points elsewhere in the same text, so it disturbs
                    # fewer of them.
                    widest = max_w
                    step_w = a.W
                    while step_w < widest - 0.01:
                        step_w = min(widest, step_w + 0.1)
                        if not _any_orphan(step_w):
                            max_w = step_w
                            break
                    else:
                        max_w = widest
                    resolved = not _any_orphan(max_w)
                    if resolved:
                        old_lw = [a.L, a.W]
                        a.shape.left = Inches(round(left_limit, 3))
                        a.L = left_limit
                        a.shape.width = Inches(round(max_w, 3))
                        a.W = max_w
                        changed.append({"shape": a.name, "before": old_lw,
                                        "after": [a.L, a.W], "widen_for_orphan_char": True})
        real_bottom = a.T + max(real_h, a.H)
        for b in texts[i + 1:]:
            if min(a.L + a.W, b.L + b.W) <= max(a.L, b.L) + 0.02:
                continue  # different column — leave side-by-side shapes alone
            if b.T >= real_bottom - 0.02:
                continue  # already clear
            old = [b.T, b.H]
            delta = (real_bottom + gap) - b.T
            backdrops = _backdrop_shapes_for(slide, (b.L, b.T, b.W, b.H),
                                             exclude={a.shape.shape_id, b.shape.shape_id})
            b.move_to(top=real_bottom + gap)
            changed.append({"shape": b.name, "before": old, "after": [b.T, b.H],
                            "pushed_by": a.name, "real_h": round(real_h, 3)})
            for card in backdrops:
                card_old_top = _in(card.top)
                card.top = Inches(round(card_old_top + delta, 3))
                changed.append({"shape": card.name, "before": [card_old_top],
                                "after": [_in(card.top)], "moved_with": b.name,
                                "backdrop_follows_text": True})
    if changed and acts is not None:
        acts.append({"rule": "fix_stacked_text_overlap", "page": page, "changed": changed})
    return changed


# --------------------------------------------------------------------------- 指标卡定向修复

def reflow_cards(slide, card_names, left: float = 0.72, right: float = 12.67,
                 top: float | None = None, gap: float = 0.25, pad: float = 0.14,
                 inner_gap: float = 0.05, set_pt: dict | None = None,
                 max_bottom: float | None = None, acts=None, page: int = 0) -> dict:
    """指标卡定向修复（P2 压字缺陷）：按区域等宽重排卡片，卡内文字按**真实
    字体度量**重排；空间不足时加高卡片（受 max_bottom 约束，不压下方元素）。

    - 不改文字内容；可选 set_pt 统一上调某类小字（如标签 10.5→12pt）；
    - 卡片加宽可显著减少折行；加高保证每行都有真实高度，不再相互重叠。
    """
    shapes = {sh.name: sh for sh in list(slide.shapes)}
    cards = [shapes[n] for n in card_names if n in shapes]
    if len(cards) < 2:
        return {}
    n = len(cards)
    cw = (right - left - gap * (n - 1)) / n

    # 第一遍：只按原始几何采集，不做任何移动
    # （若边采边移，先移走的文字会落进下一张卡的采集范围，造成子元素累积错位）
    plan = []
    for i, c in enumerate(cards):
        cl, ct, cw0, ch0 = _in(c.left), _in(c.top), _in(c.width), _in(c.height)
        kids = [sh for sh in list(slide.shapes)
                if is_text_shape(sh)
                and cl - 0.06 <= _in(sh.left) <= cl + cw0 + 0.06
                and ct - 0.06 <= _in(sh.top) <= ct + ch0 + 0.06]
        plan.append((i, c, cl, ct, kids))

    # 第二遍：移动卡片与卡内文字（保持卡内相对偏移）
    for i, c, cl, ct, kids in plan:
        nl = round(left + i * (cw + gap), 3)
        dx = nl - cl
        c.left = Inches(nl)
        if top is not None:
            c.top = Inches(round(top, 3))
        c.width = Inches(round(cw, 3))
        for sh in kids:
            sh.left = Inches(round(_in(sh.left) + dx, 3))
            sh.width = Inches(round(min(_in(sh.width), cw - 0.36), 3))

    # 第三遍：卡内文字按真实度量重排；空间不足时加高卡片
    new_h = 0.0
    for _i, c, _cl, ct, kids in plan:
        if not kids:
            continue
        if set_pt:
            for sh in kids:
                cur = T(sh, 0).size()
                if cur in set_pt:
                    T(sh, 0).set_pt(set_pt[cur])
        items = []
        for sh in kids:
            t = T(sh, 0)
            pt = t.size() or 12.0
            h = real_text_h_in(t.text, pt, max(0.3, t.W - 0.06))
            items.append((t, h))
        items.sort(key=lambda x: x[0].T)
        total = sum(h for _, h in items) + inner_gap * (len(items) - 1)
        need = total + 2 * pad
        ch = _in(c.height)
        limit = (max_bottom - 0.14 - ct) if max_bottom else ch + 3.0
        new_h = round(min(max(need, ch), max(ch, limit)), 3)
        c.height = Inches(new_h)
        y = ct + (new_h - total) / 2
        for t, h in items:
            t.move_to(top=round(y, 3), height=round(h, 3))
            y += h + inner_gap
    if acts is not None:
        acts.append({"rule": "reflow_cards", "page": page, "cards": n,
                     "cw": round(cw, 2), "set_pt": set_pt or {}})
    return {"cw": cw, "height": new_h}


# --------------------------------------------------------------------------- 版面重排

def relayout_grid(slide, card_names: list[str], rows: int, cols: int,
                  area: tuple, gap: float = 0.22, acts=None, page: int = 0) -> dict:
    """把一组卡片连同卡内文字重排到 rows×cols 网格（比例映射，不改文字）。

    area = (left, top, width, height)；卡内文字按其在旧卡内的相对位置映射到新卡。
    """
    shapes = {sh.name: sh for sh in list(slide.shapes)}
    cards = [shapes[n] for n in card_names if n in shapes]
    if len(cards) < 2:
        return {}
    left, top, width, height = area
    cw = (width - gap * (cols - 1)) / cols
    ch = (height - gap * (rows - 1)) / rows

    def rel(sh, card_l, card_t):
        return (_in(sh.left) - card_l, _in(sh.top) - card_t,
                _in(sh.width), _in(sh.height))

    old = []
    for c in cards:
        cl, ct, cwid, chei = _in(c.left), _in(c.top), _in(c.width), _in(c.height)
        kids = []
        for sh in list(slide.shapes):
            if is_text_shape(sh) and cl - 0.05 <= _in(sh.left) <= cl + cwid + 0.05 \
                    and ct - 0.05 <= _in(sh.top) <= ct + chei + 0.05:
                kids.append((sh, rel(sh, cl, ct)))
        old.append((c, kids, cwid, chei))

    order = sorted(range(len(old)), key=lambda i: (_in(old[i][0].top), _in(old[i][0].left)))
    for pos, i in enumerate(order):
        c, kids, cwid, chei = old[i]
        r, col = divmod(pos, cols)
        nx = round(left + col * (cw + gap), 3)
        ny = round(top + r * (ch + gap), 3)
        dx, dy = nx - _in(c.left), ny - _in(c.top)
        c.left, c.top = Inches(nx), Inches(ny)
        c.width, c.height = Inches(round(cw, 3)), Inches(round(ch, 3))
        for sh, (ox, oy, ow, oh) in kids:
            sx = cw / cwid if cwid else 1.0
            sy = ch / chei if chei else 1.0
            sh.left = Inches(round(nx + ox * sx, 3))
            sh.top = Inches(round(ny + oy * sy, 3))
            sh.width = Inches(round(min(ow * sx, cw - 0.1), 3))
            if sy < 1.0:
                sh.height = Inches(round(max(oh * sy, 0.18), 3))
    if acts is not None:
        acts.append({"rule": "relayout_grid", "page": page,
                     "cards": card_names, "rows": rows, "cols": cols,
                     "cell": [round(cw, 2), round(ch, 2)]})
    return {"cw": cw, "ch": ch}


# --------------------------------------------------------------------------- 主入口

def visual_deck(src_path, out_path, images: dict | None = None,
                particle_pages: dict | None = None,
                layouts: dict | None = None,
                fixes: dict | None = None) -> dict:
    """对整份 PPTX 做视觉重构；返回统计（含动作明细）。

    - images: {页码: {"file", "rect", "overlay"}}
    - particle_pages: {页码: {"rect", "count", "color", "mode", "dur", "alpha"}}
    - layouts: {页码: {"cards", "rows", "cols", "area", "gap"}}
    - fixes: {页码: callable(slide, acts)} 页级定向修复（如 P2 指标卡重排）
    转场由 transition_for() 统一规划（普通 0.8s / 关键 1.6s 淡入）。
    """
    src, out = Path(src_path), Path(out_path)
    prs = Presentation(str(src))
    sw, sh = _in(prs.slide_width), _in(prs.slide_height)
    acts: list[dict] = []
    pages_stats = []

    images = images or {}
    particle_pages = particle_pages or {}
    layouts = layouts or {}

    for page, slide in enumerate(prs.slides, 1):
        page_acts: list[dict] = []

        # 0) 版面重排（若该页有布局计划）——必须在采集几何之前
        spec = layouts.get(page)
        if spec:
            relayout_grid(slide, spec["cards"], spec["rows"], spec["cols"],
                          spec["area"], spec.get("gap", 0.22),
                          acts=page_acts, page=page)

        # 采集（重排之后的真实几何）
        texts = [T(s, i) for i, s in enumerate(list(slide.shapes)) if is_text_shape(s)]
        boxes = [Box(s, i) for i, s in enumerate(list(slide.shapes))
                 if not is_text_shape(s) and _in(s.width) > 0.5 and _in(s.height) > 0.3]
        roles = classify(slide, texts, boxes, sw, sh)

        # 1) 配图（先插，确保在文字层之下）
        for img_page, spec in images.items():
            if img_page == page:
                add_image(slide, Path(spec["file"]), spec["rect"],
                          spec.get("overlay", 40), page_acts, page,
                          focus=tuple(spec.get("focus", (0.5, 0.5))),
                          fit=spec.get("fit", "cover"))

        # 2) 字体阶梯
        apply_type_scale(slide, texts, boxes, roles, page_acts)

        # 3) 转场（统一淡入语言，速度分档；交付稿不启用自动换页）
        tp = transition_for(page)
        add_transition(slide, tp["kind"], tp["spd"], dur_ms=tp["dur_ms"])
        page_acts.append({"rule": "transition", "kind": tp["kind"],
                          "spd": tp["spd"], "dur_ms": tp["dur_ms"]})

        # 4) 粒子（自动播放；聚/漂/散 三式，位置 + 透明度真实变化）
        if page in particle_pages:
            spec = particle_pages[page]
            add_particles(slide, spec["rect"], spec.get("count", 18),
                          spec.get("color", "C9A45B"), seed=1000 + page,
                          mode=spec.get("mode", "drift"),
                          dur=spec.get("dur", 2200),
                          acts=page_acts, page=page,
                          alpha_pct=spec.get("alpha", 32))

        # 5) 页级定向修复（如 P2 指标卡压字：等宽重排 + 真实度量重排 + 加高）
        fix = (fixes or {}).get(page)
        if fix is not None:
            fix(slide, page_acts)

        # 6) 标题孤字治理（结构规则：按真实度量把标题框加宽到卡内可用宽度）
        fix_orphan_title(slide, page_acts, page=page, slide_w=sw)
        fix_autofit_capacity(slide, page_acts, page=page)

        pages_stats.append({"slide": page, "type": roles.get(
            next((t.name for t in texts if roles.get(t.name) == "title"), ""), ""),
            "roles": {k: v for k, v in roles.items()},
            "actions": page_acts})
        acts.extend(page_acts)

    out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out))
    return {
        "engine": "wengao-visual",
        "version": VERSION,
        "slide_size_in": [round(sw, 2), round(sh, 2)],
        "slides": len(prs.slides),
        "action_count": len(acts),
        "images": len(images),
        "particle_pages": sorted(particle_pages.keys()),
        "pages": pages_stats,
        "actions": acts,
    }
