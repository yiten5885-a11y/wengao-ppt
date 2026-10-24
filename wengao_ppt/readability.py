# -*- coding: utf-8 -*-
"""可读性与重排引擎（2026-10-21 00:25 用户纠正：排版能力要提高、字体还是太小）。

背景与依据（只写已发生的事实，不写根因定论）
--------------------------------------------
- 用户原话："排版能力要大大提高，字体还是太小，看起来不协调"（三张真实截图
  P6/P2/P8 由监督目视，本模块不读图）。
- 监督实测 exact_v12/e43dad9e：P2 显式字号 9.5/10/10.5/11/12/15/17/20，
  P6 多为 16、标题 20，P8 多为 12/13/14、标题 20 —— 13 页处理链没落实字号要求。
- 原生缺字：监督独立对照 S6（**仅**把 a:ea typeface 由 Hiragino Sans GB 改成
  Arial Unicode MS，其余字节/ZIP 成员顺序/文字/备注/图片/转场/粒子全部保持）
  真实原生逐页放映 13 页未见中文缺字方框；S7（改 PingFang SC）虽无方框但出现
  毛笔/楷体感回退，不采用。本模块因此把东亚槽定为 Arial Unicode MS。

本模块做什么
------------
1. `set_native_ea_font`：只改东亚字体槽（a:ea）的 typeface，拉丁/复杂文种/
   lang/charset/pitchFamily 一律不动；
2. `SIZE_POLICY`：按内容角色给出字号（页标题 36–40、导语与主正文 26–28、
   常规正文下限 24、小标题与关键结论 28–32、关键数字 44–56、表格 20–22、
   图表与辅助 18–20、页脚与来源 16–18）；
3. `layout_p2/p6/p8`：P2 四组数据改 2×2 宽卡并建立数字/说明层级；
   P6 两侧条目统一顶线行距与 26–28 磅正文、中间"同一个人"留呼吸空间、底部结论放大；
   P8 五项影响改统一基线有序行、减少分散小框。

不做什么（硬约束）
------------------
- 不改文字、不删事实、不裁切、不把正文搬进备注；
- 不用 normAutofit 缩字塞内容（统一 noAutofit + 显式行距）；
- 不改页数/页序、不改转场、不删配图与粒子（只按内容需要移位/缩放）；
- 不在系统安装或分发字体文件；平台适用范围仅本机 macOS 实测（Arial Unicode.ttf
  存在于 /System/Library/Fonts），其他平台需各自实测后再声明。
"""
from __future__ import annotations

from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE
from pptx.dml.color import RGBColor
from pptx.util import Pt

IN = 914400.0
CANVAS_W_IN = 13.3333
CANVAS_H_IN = 7.5

#: 左右页边距与内容宽度（按 13.333in 宽稿标定）
MARGIN_IN = 0.72
CONTENT_W_IN = CANVAS_W_IN - 2 * MARGIN_IN      # 11.893in
RIGHT_IN = CANVAS_W_IN - MARGIN_IN              # 12.613in

#: 东亚字体槽（原生放映已验证的选择，见模块文档）
NATIVE_EA_FONT = "Arial Unicode MS"

#: 新可读基线（单位：磅）。角色 → 目标值；区间是允许范围，目标值取区间内确定值。
SIZE_POLICY = {
    "page_title": 36,      # 36–40
    "lead": 26,            # 26–28（导语/副标题，下限 24）
    "body": 26,            # 26–28，常规正文不得低于 24
    "body_min": 24,
    "section_head": 28,    # 28–32（小标题/关键结论）
    "conclusion": 30,      # 28–32
    "key_number": 44,      # 44–56
    "table": 21,           # 20–22
    "chart_label": 19,     # 18–20
    "aux": 18,             # 18–20（来源/序号等辅助说明）
    "footer": 16,          # 16–18
    "index": 20,           # 18–20（有序序号）
}

LAYOUT_VERSION = "readability-2026-10-22-13p-dark-gold-r17-p7-semantic-heading-break"


class ReadabilityError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# --------------------------------------------------------------------------- 度量
#: 度量安全系数：LibreOffice 实际排版比 PIL 度量宽约 4–6%（P2 说明行实测），
#: 估算折行时按 pt*1.06 量，避免"估算 1 行、实渲 2 行"压到相邻角色。
MEASURE_SAFETY = 1.06

#: 文本框高度经验式（来自 LibreOffice + Arial Unicode MS 实测字形框，非拍脑袋）：
#:   框高 = 顶部偏移 + (行数-1)×行距 + 单行字形纵向范围 + 底部内边距
#: 以下四个常数由本候选真实渲染（LibreOffice + Arial Unicode MS）实测标定，
#: 不是估算：spcPct=132% 是"字体自然行高"的百分比，Arial Unicode 自然行高约
#: 1.2em，故实际行距 ≈1.58em（实测 26pt 行间 0.57in、30pt 行间 0.655in）。
ASC_OFFSET_EM = 0.55    # 框顶 → 首行字形顶（实测 0.48–0.55em）
LINE_PITCH_EM = 1.58    # 实测行距（= 1.32 spcPct × 1.2em 自然行高）
GLYPH_EM_CJK = 1.10     # 单行字形纵向范围（实测 CJK 0.96–1.09em，取上限）
GLYPH_EM_DIGIT = 0.80   # 纯数字/符号（实测 44pt 数字 0.77em）
BOTTOM_PAD_IN = 0.06    # 字形底 → 框底


def measure_lines(text: str, pt: float, box_w_in: float, bold: bool = False) -> int:
    """按框宽估算折行数（复用 refine 的 4x 超采样度量，带 1.06 安全系数）。"""
    from .refine import wrapped_lines
    hard = (text or "").count("\n") + 1
    try:
        return max(hard, wrapped_lines(text, pt * MEASURE_SAFETY, box_w_in, bold))
    except Exception:  # noqa: BLE001  度量不可用时不阻塞排版，退化为按字宽估算
        if not text:
            return 1
        w_in = len(text) * pt * MEASURE_SAFETY / 72.0
        return max(1, int(w_in / max(box_w_in, 0.01)) + (1 if w_in % box_w_in else 0))


def box_h(pt: float, lines: int = 1, digit_only: bool = False) -> float:
    """容纳 n 行 pt 磅文字所需的**真实**框高（英寸）。"""
    em = GLYPH_EM_DIGIT if digit_only else GLYPH_EM_CJK
    return (ASC_OFFSET_EM + (lines - 1) * LINE_PITCH_EM + em) * pt / 72.0 + BOTTOM_PAD_IN


def _digit_only(text: str) -> bool:
    import re
    return re.fullmatch(r"[0-9+\-.%/,\s]*", text or "") is not None


def line_h_in(pt: float, factor: float = 1.32) -> float:
    return pt * factor / 72.0


# --------------------------------------------------------------------------- 字体槽
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _qn(tag: str) -> str:
    return "{%s}%s" % (_A, tag)


def _ensure_ea(rpr_el, font: str) -> bool:
    """在 rPr/defRPr 上把 a:ea 的 typeface 设为 font（缺失则按 schema 顺序插入）。"""
    ea = rpr_el.find(_qn("ea"))
    if ea is None:
        ea = rpr_el.makeelement(_qn("ea"), {})
        latin = rpr_el.find(_qn("latin"))
        cs = rpr_el.find(_qn("cs"))
        if latin is not None:
            latin.addnext(ea)
        elif cs is not None:
            cs.addprevious(ea)
        else:
            rpr_el.append(ea)
    if ea.get("typeface") == font:
        return False
    ea.set("typeface", font)
    return True


def set_native_ea_font(prs, font: str = NATIVE_EA_FONT) -> int:
    """只改东亚字体槽：slides + layouts + masters + notes。

    返回被修改的 a:ea 元素数量。拉丁槽/复杂文种/lang/charset/pitchFamily 不动。
    """
    n = 0
    parts = []
    for slide in prs.slides:
        parts.append(slide._element)
        try:
            if slide.has_notes_slide:
                parts.append(slide.notes_slide._element)
        except Exception:  # noqa: BLE001
            pass
    for layout in prs.slide_layouts:
        parts.append(layout._element)
        try:
            parts.append(layout.slide_master._element)
        except Exception:  # noqa: BLE001
            pass
    for master in prs.slide_masters:
        parts.append(master._element)
    seen = set()
    for root in parts:
        if id(root) in seen:
            continue
        seen.add(id(root))
        for tag in ("rPr", "defRPr", "endParaRPr"):
            for el in root.iter(_qn(tag)):
                if _ensure_ea(el, font):
                    n += 1
        # 主题字体方案里的 ea（majorFont/minorFont）
        for el in root.iter():
            if el.tag.endswith("}ea") and el.get("typeface") and el.get("typeface") != font:
                el.set("typeface", font)
                n += 1
    return n


# --------------------------------------------------------------------------- 形状
def _by_name(slide, name: str):
    for sh in slide.shapes:
        if sh.name == name:
            return sh
    return None


def set_box(shape, x, y, w, h):
    shape.left, shape.top = int(round(x * IN)), int(round(y * IN))
    shape.width, shape.height = int(round(w * IN)), int(round(h * IN))


def set_pt(shape, pt: float, line_factor: float = 1.32, wrap: bool = True):
    """把一个形状里**全部**字号槽（rPr/defRPr/endParaRPr）设为 pt，并关掉缩字。"""
    if not getattr(shape, "has_text_frame", False):
        return
    tf = shape.text_frame
    tf.word_wrap = wrap
    tf.auto_size = MSO_AUTO_SIZE.NONE          # 禁止 normAutofit 缩字塞内容
    # 顶锚定：原稿不少框是居中锚定，字形会从框中心向上下同时溢出，
    # 于是"对象框不相交"而真实字形相撞（5a6bcb2d P2 的失败形态之一）。
    tf.vertical_anchor = MSO_ANCHOR.TOP
    tf.margin_left = tf.margin_right = 0
    tf.margin_top = tf.margin_bottom = 0
    for para in tf.paragraphs:
        para.line_spacing = line_factor
        para.font.size = Pt(pt)
        for run in para.runs:
            run.font.size = Pt(pt)
    # XML 兜底：把列表样式层级的 defRPr / endParaRPr 也统一，避免原生端回退小字
    for tag in ("rPr", "defRPr", "endParaRPr"):
        for el in shape._element.iter(_qn(tag)):
            el.set("sz", str(int(round(pt * 100))))


def _fit(slide, name, x, y, w, pt, lines=None, digit_only=None):
    """按实际度量安置文本形状：折行数 → 真实框高（含顶部偏移与底部内边距）。

    这是修掉 5a6bcb2d P2 失败件的关键：不再把所有角色塞进固定高度框，
    而是按字体度量算高度，保证真实字形框**被文本框容纳**且彼此不相撞。
    """
    sh = _by_name(slide, name)
    if sh is None:
        return 0.0
    txt = sh.text_frame.text
    if digit_only is None:
        digit_only = _digit_only(txt)
    n = lines if lines else measure_lines(txt, pt, w)
    h = box_h(pt, n, digit_only)
    set_box(sh, x, y, w, h)
    set_pt(sh, pt)
    return h


def _place(slide, name, x, y, w, h, pt, line_factor=1.32, wrap=True):
    """pt=None 表示纯装饰形状（无文字），只重排不改字号。"""
    sh = _by_name(slide, name)
    if sh is None:
        return None
    set_box(sh, x, y, w, h)
    if pt is not None:
        set_pt(sh, pt, line_factor, wrap)
    return sh


def _del(slide, name) -> bool:
    """删除约束容器（如 P11 八张行卡 / P12 五张窄卡——内容改排全宽行）。

    只删无文字的纯容器形状；文字形状一律不删（事实守恒）。
    """
    sh = _by_name(slide, name)
    if sh is None:
        return False
    if getattr(sh, "has_text_frame", False) and sh.text_frame.text.strip():
        return False
    sh._element.getparent().remove(sh._element)
    return True


# --------------------------------------------------------------------------- 语义断行
def _char_w(ch: str, pt: float) -> float:
    from .refine import text_width_in
    return text_width_in(ch, pt)


def insert_break_before(shape, token: str) -> bool:
    """在段内 token 首字符前插入显式换行 a:br（HG01：换行用版式实现，不改事实）。

    字符序列不变（a:t 内容不变，只是拆 run + 插 <a:br/>），基线按形状拼接
    文本比较时完全守恒。仅当 token 存在于同一段落时生效。
    """
    if not getattr(shape, "has_text_frame", False):
        return False
    A = _qn  # noqa: F841  语义记号
    for para in shape.text_frame.paragraphs:
        runs = para.runs
        full = "".join(r.text for r in runs)
        pos = full.find(token)
        if pos < 0:
            continue
        # 定位 token 首字符所在 run 与 run 内偏移
        acc = 0
        for ri, r in enumerate(runs):
            if acc + len(r.text) > pos:
                off = pos - acc
                p_el = para._p
                # 拆分 run：前半保留，后半从 token 首字符起
                head_txt = r.text[:off]
                tail_txt = r.text[off:]
                r_el = r._r
                if off > 0:
                    r.text = head_txt
                    br = r_el.makeelement(_qn("br"), {})
                    # a:br 需要位于两个 run 之间；复制 rPr 保持字体一致
                    import copy
                    rpr = r_el.find(_qn("rPr"))
                    if rpr is not None:
                        br.append(copy.deepcopy(rpr))
                    r_el.addnext(br)
                    if len(tail_txt) > 0:
                        new_r = copy.deepcopy(r_el)
                        # new_r 的 a:t 设为 tail
                        t = new_r.find(_qn("t"))
                        t.text = tail_txt
                        br.addnext(new_r)
                else:
                    br = r_el.makeelement(_qn("br"), {})
                    import copy
                    prev = r_el.getprevious()
                    rpr = r_el.find(_qn("rPr"))
                    if rpr is not None:
                        br.append(copy.deepcopy(rpr))
                    if prev is not None:
                        prev.addnext(br)
                    else:
                        p_el.insert(0, br)
                return True
            acc += len(r.text)
    return False


def protected_line_breaks(shape, pt: float, w_in: float,
                          tokens=None) -> int:
    """按真实度量模拟折行，在会跨行的受保护 token 前插 a:br。

    返回插入的换行数。贪心逐 token 累积（与 LibreOffice 的 CJK 逐字折行一致，
    西文/数字串不拆），若某受保护 token 的字符被折行点切开，则把该行断点
    前移到 token 首字符处（显式 a:br）。
    """
    import re
    from .refine import text_width_in
    if not getattr(shape, "has_text_frame", False) or w_in <= 0:
        return 0
    toks = list(tokens or [])
    inserted = 0
    for para in shape.text_frame.paragraphs:
        text = "".join(r.text for r in para.runs)
        if not text or "\n" in text or "\x0b" in text:
            continue
        # 内容相关 token（数字单位/拉丁词）并集
        local = list(toks)
        for m in re.finditer(r"\d+(?:\.\d+)?(?:%|亿|万|个月)|[A-Za-z]{2,}", text):
            if m.group(0) not in local:
                local.append(m.group(0))
        # 贪心折行模拟（run 级 token：CJK 逐字 + 连续西文数字）
        pieces = re.findall(r"[A-Za-z0-9.%+\-–—/]+|[^\s]", text)
        widths = [text_width_in(p, pt) for p in pieces]
        # 每个受保护 token 覆盖的 piece 下标区间
        spans = []
        for tok in local:
            idx, cur = 0, ""
            start = None
            for i, p in enumerate(pieces):
                if text.find(tok) < 0:
                    break
            # 直接用字符位置映射 piece 区间
            pos = 0
            start = None
            for i, p in enumerate(pieces):
                if text[pos:pos + len(p)] != p:  # 防御
                    break
                if text.find(tok, pos, pos + len(text)) == pos:
                    pass
                pos += len(p)
            # 简化：token 在 pieces 中的连续覆盖
            joined = ""
            spans_i = []
            for i, p in enumerate(pieces):
                if tok.startswith((joined + p)[:len(tok)]) or tok.startswith(joined + p):
                    spans_i.append(i)
                    joined += p
                    if joined == tok or joined.startswith(tok):
                        break
                elif joined:
                    break
            if joined and (joined == tok or joined.startswith(tok)):
                spans.append((spans_i[0], spans_i[-1]))
        # 逐 token 检查跨行并插 br
        for s0, s1 in spans:
            # 计算 s0..s1 是否跨越贪心断行点
            line_w, line_no = 0.0, 1
            breaks = []
            for i, p in enumerate(pieces):
                if line_w + widths[i] > w_in and line_w > 0:
                    breaks.append(i)
                    line_no += 1
                    line_w = widths[i]
                else:
                    line_w += widths[i]
            # token 内部断行点 = breaks 落在 (s0, s1] 的
            inside = [b for b in breaks if s0 < b <= s1]
            if inside:
                insert_break_before(shape, pieces[s0])
                inserted += 1
    return inserted


# --------------------------------------------------------------------------- 门禁协作工具
# 与 layout_gate.py 冻结阈值（HG05/HG06）对齐：字形（非文本框）才受 canvas
# 边距/净空约束；实底容器内字形到边 ≥ max(0.10, 0.35em)；content 字形底 ≤
# 7.0-0.12=6.88；footer 16pt 字形底 ≤ 7.35。以下常数按此推得。
FOOTER_Y = 6.96            # 16pt 墨迹 7.082-7.304 ⊂ [7.0 页脚区, 7.35 画布]
CONCL_BOTTOM_MAX = 6.88    # HG06 content_footer：footer_top(7.0) - 0.12
SP_FOOTER = SIZE_POLICY["footer"]


def card_pad(pt: float) -> float:
    """HG05 CARD_PADDING：字形到实底容器边 ≥ max(0.10, 0.35em)。"""
    return max(0.10, 0.35 * pt / 72.0)


def ink_top(pt: float) -> float:
    """框顶 → 首行字形顶。"""
    return 0.55 * pt / 72.0


def ink_bottom(pt: float, n: int = 1) -> float:
    """框顶 → 第 n 行字形底（相对框顶）。"""
    return (0.55 + (n - 1) * 1.58 + 1.00) * pt / 72.0


def _dot(slide, name: str, x=None, y=None, size: float = 0.08) -> bool:
    """把失去布局功能的实底容器缩成圆点，并可指定圆点位置。

    HG01 要求形状守恒（空 txBody 也不可删），故缩点而非删除；默认 0.08in
    远小于文字，z 序不变（先画），不产生 HG04 遮挡。显式设置 ellipse 几何，
    避免仅缩小矩形后留下方块；P11 可按行传入位置，避免旧卡片 y 坐标漂移。
    """
    sh = _by_name(slide, name)
    if sh is None:
        return False
    left = sh.left / IN + 0.01 if x is None else x
    top = sh.top / IN + 0.01 if y is None else y
    set_box(sh, left, top, size, size)
    sp_pr = getattr(sh._element, "spPr", None)
    geom = sp_pr.find(_qn("prstGeom")) if sp_pr is not None else None
    if geom is not None:
        geom.set("prst", "ellipse")
    return True


def _set_shape_alpha(slide, name: str, pct: int) -> bool:
    """只调整指定实底形状的填充不透明度到 pct%（不改色相/几何/文字）。

    依据（监督 1420："需要换局部底板时以实际低对比证据为依据"）：修复后
    门禁以无字渲染同坐标 crop 为背景真值，实测照片页 101014@38% 纱罩在
    照片亮区不够暗（见各 layout_pN 内的引用值）。加深 alpha 使浅色正文
    （EDEDEA）达标，同时保留照片透出度——仍为深色金色语言的深纱罩。
    """
    sh = _by_name(slide, name)
    if sh is None:
        return False
    spPr = getattr(sh._element, "spPr", None)
    if spPr is None:
        return False
    sf = spPr.find(_qn("solidFill"))
    if sf is None:
        return False
    clr = sf.find(_qn("srgbClr"))
    if clr is None:
        return False
    al = clr.find(_qn("alpha"))
    if al is None:
        al = clr.makeelement(_qn("a:alpha"), {})
        clr.append(al)
    al.set("val", str(int(pct * 1000)))
    return True


# --------------------------------------------------------------------------- 页布局（13 页专用版式）
def layout_p1(slide, acts):
    """P1 封面：kicker 24pt/主标题 38pt 两硬行/副题 26pt 两行/双列 24pt。

    infer_roles（改后）：Text7 顶区最大字号→title(38)；Text6 kicker 24pt
    为 body（在 title 之上，lead=无 → title→lead/lead→body 对跳过）；
    Text9 副题 body；底部无 ≥15 字 body → 无结论。横幅 Picture14/Rect15
    原位 (0.72,5.66) z 序在文字前。
    """
    SP = SIZE_POLICY
    # 原稿的垂直金线原本从页顶贯穿到横幅，原生放映时切穿主标题和导语。
    # 保留金线作为宏观/微观双列分隔，只让它出现在两列正文之间。
    _place(slide, "Shape 4", 6.667, 4.08, 0.0, 1.46, None)
    _fit(slide, "Text 6", 0.92, 0.52, 8.30, SP["body_min"], lines=1)  # kicker 24pt
    _place(slide, "Shape 5", 0.72, 0.58, 8.60, 0.46, None)            # kicker 底条
    _fit(slide, "Text 7", 0.72, 1.30, 11.89, 38, lines=2)             # 主标题两硬行
    _place(slide, "Shape 8", 0.72, 3.02, 1.30, 0.07, None)            # 分隔短线
    _fit(slide, "Text 9", 0.72, 3.10, 11.89, SP["lead"], lines=2)     # 副题 26pt×2
    # 宏观/微观 y4.15（top<4.2 → 不作结论候选；P1 无结论角色）
    _fit(slide, "Text 10", 0.72, 4.15, 5.60, SP["body_min"], lines=1)
    _fit(slide, "Text 11", 6.95, 4.15, 5.66, SP["body_min"], lines=1)
    for nm in ("Picture 14", "Rectangle 15"):
        _place(slide, nm, 0.72, 5.66, 11.95, 1.22, None)              # 横幅原位
    _fit(slide, "Text 12", 0.72, FOOTER_Y, 6.50, SP_FOOTER, lines=1)
    if acts is not None:
        acts.append({"rule": "layout_p1", "page": 1, "layout_version": LAYOUT_VERSION})


def layout_p3(slide, acts):
    """P3：对比区两行（标签+说明同排）+ A/B/C 三条全宽行 + 结论带。

    推导：title 1L(0.425-0.975)、lead 24pt 1L(1.313-1.829)；对比 Text3/4
    同排 y2.30、Text6/7 同排 y2.90（Text7 w4.35 1L）；三行卡 y3.30/4.25/5.20
    （tit w4.20 1L、desc w6.65 2L，末行墨迹底 6.243）；结论 28pt 28 字单行
    y6.10（墨迹 6.314-6.703），带 Shape23 (0.68,6.02,11.97,0.86)；引号带内右。
    原三卡 Shape8/13/18 与左条缩点（HG01 守恒）。
    """
    SP = SIZE_POLICY
    marker_pairs = (("Shape 8", "Shape 9"), ("Shape 13", "Shape 14"),
                    ("Shape 18", "Shape 19"))
    for (outer, rail), ry in zip(marker_pairs, (3.05, 4.00, 4.95)):
        # Each former card/left-rail pair becomes one aligned marker for its row;
        # don't leave the three colored dots in their old card-grid positions.
        _dot(slide, outer, x=0.72, y=ry + 0.25)
        _dot(slide, rail, x=0.72, y=ry + 0.25)
    _fit(slide, "Text 1", 0.72, 0.20, 11.89, SP["page_title"], lines=1)
    _fit(slide, "Text 2", 0.72, 1.13, 11.89, SP["body_min"], lines=1)
    _fit(slide, "Text 3", 0.80, 2.12, 1.50, SP["body_min"], lines=1)
    _fit(slide, "Text 4", 2.45, 2.12, 5.90, SP["body_min"], lines=1)
    _fit(slide, "Text 6", 0.80, 2.68, 1.50, SP["body_min"], lines=1)
    _fit(slide, "Text 7", 2.45, 2.68, 4.35, SP["body_min"], lines=1)
    # 对比箭头(<0.5㎡)：x7.00→7.55 避让 Text 4 末字 '多'（墨迹至 ~7.46，
    # 第三轮 7.00 压字触发 HG04 遮挡 + HG02 不可见）
    _place(slide, "Shape 5", 7.55, 2.18, 0.45, 0.45, None)
    rows = [("Text 10", "Text 11", "Text 12", 3.05),
            ("Text 15", "Text 16", "Text 17", 4.00),
            ("Text 20", "Text 21", "Text 22", 4.95)]
    for o, t, d, ry in rows:
        _fit(slide, o, 0.90, ry + 0.02, 0.55, SP["index"], lines=1)
        _fit(slide, t, 1.55, ry, 4.20, SP["body_min"], lines=1)
        _fit(slide, d, 5.95, ry, 6.65, SP["body_min"], lines=2)
    # 照片纱罩 38%→65%（证据化局部底板调整）：修复后门禁以无字渲染为背景
    # 真值，实测 Text 2 '困' 处照片局部 ≈208 灰、38% 纱罩下背景 [135,138,138]
    # → 对比 2.97 < 4.5；Text 26 '与' 4.46。65% 纱罩 → 背景约 83 → ≈6.0，
    # 照片仍透出 35%，纱罩仍为设计深色 101014（未换浅色主题）。
    _set_shape_alpha(slide, "Rectangle 30", 65)
    _place(slide, "Shape 23", 0.68, 6.02, 11.97, 0.86, None)
    _fit(slide, "Text 25", 0.72, 6.10, 11.89, 28, lines=1)
    _fit(slide, "Text 24", 12.20, 6.22, 0.30, 20, lines=1)           # 引号带内右
    _fit(slide, "Text 26", 0.72, FOOTER_Y, 6.50, SP_FOOTER, lines=1)
    _fit(slide, "Text 27", 11.80, FOOTER_Y, 0.81, SP_FOOTER, lines=1)
    if acts is not None:
        acts.append({"rule": "layout_p3", "page": 3, "layout_version": LAYOUT_VERSION})


def layout_p2(slide, acts):
    """P2：四数据 2×2 宽卡（44pt 数字列 w2.50 + 24pt 标签 2 行 + 16pt 来源行）。

    推导：lead 24pt 1L；卡 (1.76/3.64, h1.82)（数字墨迹 cy+0.386-1.06、
    标签 cy+0.273-1.316、来源 cy+1.422-1.644，pad 达标）；'+13.8%'/'2500亿'
    44pt 实测 ≤2.5in；结论 Text21 34 字 30pt 两行 y5.56（墨迹 5.809-6.864），
    带 Shape19 (0.60,5.48,9.90,1.54)；'越多' 不跨行（a:br 前移断点）；
    来源行含 Syntun/统计局/国务院 → source 角色 16pt 合规；页脚 y6.96。
    """
    SP = SIZE_POLICY
    _fit(slide, "Text 1", MARGIN_IN, 0.22, CONTENT_W_IN, SP["page_title"], lines=1)
    _fit(slide, "Text 2", MARGIN_IN, 1.13, CONTENT_W_IN, SP["body_min"], lines=1)

    gap = 0.30
    cw = (CONTENT_W_IN - gap) / 2
    col_x = [MARGIN_IN, MARGIN_IN + cw + gap]
    row_y = [1.76, 3.64]
    card_h = 1.82
    cards = ["Shape 3", "Shape 7", "Shape 11", "Shape 15"]
    nums = ["Text 4", "Text 8", "Text 12", "Text 16"]
    labels = ["Text 5", "Text 9", "Text 13", "Text 17"]
    srcs = ["Text 6", "Text 10", "Text 14", "Text 18"]
    for i, (cx, cy) in enumerate([(col_x[i % 2], row_y[i // 2]) for i in range(4)]):
        _place(slide, cards[i], cx, cy, cw, card_h, None)
        # 数字列 44pt（'+13.8%' 实测 2.28in ≤ w2.50）；x+0.21 使墨迹左边距
        # ≈0.244 ≥ 0.35em(44pt)=0.214（HG05，第三轮 0.154 不足）。
        # 第六轮监督：**不得**再把 '.' run 抬 baseline 去凑 0.08in 行簇阈值——
        # 44pt 下 baseline=21000 会把小数点抬到数字中线（渲染成中点，+0·9%），
        # 是可见数值语义退化。归簇改由门禁按基线/字号/标点分组（HG08），
        # 产品端恢复原样小数点（字符与 run 结构均不变）。
        _fit(slide, nums[i], cx + 0.21, cy + 0.05, 2.50, SP["key_number"], lines=1)
        # 标签列 24pt 两行：Text5/Text17 加宽到 w3.01（margins=0，'电商'/'GMV'
        # 自然折行点右移保持同簇；Text17 另加显式断行保 '专项资金' 整簇）
        if i in (0, 3):
            _fit(slide, labels[i], cx + 2.78, cy + 0.09, 3.01, SP["body_min"], lines=2)
        else:
            _fit(slide, labels[i], cx + 2.97, cy + 0.09, 2.72, SP["body_min"], lines=2)
        _fit(slide, srcs[i], cx + 0.12, cy + 1.30, 5.50, SP["footer"], lines=1)
    # 语义断行（确定性 a:br，字符序列不变）：protected_line_breaks 的贪心
    # 模拟与 LO 实际折行不一致（第三轮 '越多' 漏判 br=0），全部改显式断点
    insert_break_before(_by_name(slide, "Text 17"), "专项资金")
    # 底部结论带：引号带内，结论 30pt 两行（'越多' 经显式断点保持同簇）
    _place(slide, "Shape 19", 0.60, 5.48, 9.90, 1.54, None)
    _fit(slide, "Text 20", 0.85, 5.52, 0.30, 20, lines=1)
    _fit(slide, "Text 21", 1.25, 5.56, 8.87, SP["conclusion"], lines=2)
    insert_break_before(_by_name(slide, "Text 21"), "越来越多")
    for nm in ("Picture 26", "Rectangle 27"):
        _place(slide, nm, 10.61, 5.48, 2.00, 1.21, None)

    _fit(slide, "Text 22", MARGIN_IN, FOOTER_Y, 6.10, SP["footer"], lines=1)
    _fit(slide, "Text 24", 6.95, FOOTER_Y, 4.60, SP["footer"], lines=1)
    _fit(slide, "Text 23", 11.80, FOOTER_Y, 0.81, SP["footer"], lines=1)
    if acts is not None:
        acts.append({"rule": "layout_p2", "page": 2, "layout_version": LAYOUT_VERSION})


def layout_p4(slide, acts):
    """P4：三卡改三条全宽行 + 公式行带 + 结论（Text20 公式 28pt 单行）。

    推导：title 24 字 36pt 两行(墨迹 0.475-1.765)；lead 24pt 1L y1.90；
    三行 y2.62/3.60/4.58（ord 20pt | tit w5.00 1L | desc w5.85 2L，末行
    墨迹底 5.623）；Text23 y5.53 + Text19(label 20pt) y5.58 同排（body 底
    6.229）；结论 Text20 y6.10 w12.53 x0.40 单行（est 12.37 ≤ 12.53，墨迹
    6.314-6.703），带 Shape18 (0.58,6.00,12.41,0.82)；引号 (0.66,5.55)。
    原三卡/左条/引号带缩点。
    """
    SP = SIZE_POLICY
    marker_pairs = (("Shape 3", "Shape 4"), ("Shape 8", "Shape 9"),
                    ("Shape 13", "Shape 14"))
    for (outer, rail), ry in zip(marker_pairs, (2.40, 3.35, 4.30)):
        _dot(slide, outer, x=0.72, y=ry + 0.25)
        _dot(slide, rail, x=0.72, y=ry + 0.25)
    _dot(slide, "Shape 21", x=0.72, y=5.75)
    _fit(slide, "Text 1", 0.72, 0.20, 11.89, SP["page_title"], lines=2)
    _fit(slide, "Text 2", 0.72, 1.90, 11.89, SP["body_min"], lines=1)
    rows = [("Text 5", "Text 6", "Text 7", 2.40),
            ("Text 10", "Text 11", "Text 12", 3.35),
            ("Text 15", "Text 16", "Text 17", 4.30)]
    for o, t, d, ry in rows:
        _fit(slide, o, 0.92, ry + 0.06, 0.55, SP["index"], lines=1)
        _fit(slide, t, 1.58, ry, 5.00, SP["body_min"], lines=1)
        _fit(slide, d, 6.75, ry, 5.85, SP["body_min"], lines=2)
    # P4 右侧说明压在照片中亮区；提升为白色以恢复至少 4.5:1 实测对比度。
    for nm in ("Text 7", "Text 12", "Text 17"):
        shape = _by_name(slide, nm)
        for para in shape.text_frame.paragraphs:
            for run in para.runs:
                run.font.color.rgb = RGBColor(255, 255, 255)
    _fit(slide, "Text 23", 1.00, 5.53, 7.50, SP["body_min"], lines=1)
    _fit(slide, "Text 19", 8.80, 5.42, 3.60, SP["body_min"], lines=1)  # 标签 24pt body
    _fit(slide, "Text 22", 0.66, 5.55, 0.30, 20, lines=1)             # 引号
    _place(slide, "Shape 18", 0.58, 6.00, 12.41, 0.82, None)          # 公式/结论带
    _fit(slide, "Text 20", 0.40, 6.10, 12.53, 28, lines=1)            # 公式=结论
    _fit(slide, "Text 24", 0.72, FOOTER_Y, 6.50, SP_FOOTER, lines=1)
    _fit(slide, "Text 25", 11.80, FOOTER_Y, 0.81, SP_FOOTER, lines=1)
    if acts is not None:
        acts.append({"rule": "layout_p4", "page": 4, "layout_version": LAYOUT_VERSION})


def layout_p5(slide, acts):
    """P5：四步卡(标签+两硬行说明) + 恶性循环带 + 双底卡(标题+句子同卡) + 结论入卡。

    推导：title 1L、lead 33 字 24pt 1L(1.313-1.829)；四卡 y1.90 h2.20
    （label 24pt y2.00、desc 显式两行 y2.75，墨迹底 3.976，卡 pad 达标）；
    循环带 Shape18 (2.20,4.24,8.70,0.76) Text19 y4.18；
    第六轮监督：原左卡只有标题、判断句 Text23 被放到卡外全宽（为满足全局
    conclusion union）→ 语义容器错配。现改为**两张自洽卡**：
    底卡 y5.02 h1.76（标题 y5.12；句子 y5.55、28pt 两行，墨迹底≈6.67，
    卡 pad ≥0.10）；左卡放判断句 Text23(w5.45)，右卡放传播金句 Text27(w5.45)，
    标题与句子同容器，内容与对象不变（不删事实、不隐藏空框）。
    """
    SP = SIZE_POLICY
    _fit(slide, "Text 1", 0.72, 0.20, 11.89, SP["page_title"], lines=1)
    _fit(slide, "Text 2", 0.72, 1.13, 11.89, SP["body_min"], lines=1)
    cards = ["Shape 3", "Shape 7", "Shape 11", "Shape 15"]
    labels = ["Text 4", "Text 8", "Text 12", "Text 16"]
    descs = ["Text 5", "Text 9", "Text 13", "Text 17"]
    arrows = ["Shape 6", "Shape 10", "Shape 14"]
    cxs = [0.72, 3.55, 6.38, 9.21]
    for i, cx in enumerate(cxs):
        _place(slide, cards[i], cx, 1.90, 2.25, 2.20, None)
        _fit(slide, labels[i], cx + 0.14, 2.00, 1.97, SP["body_min"], lines=1)
        _fit(slide, descs[i], cx + 0.14, 2.75, 1.97, SP["body_min"], lines=2)
    for i, ax in enumerate([2.99, 5.82, 8.65]):
        _place(slide, arrows[i], ax, 2.85, 0.54, 0.42, None)
    _place(slide, "Shape 18", 2.20, 4.24, 8.70, 0.76, None)           # 恶性循环带
    _fit(slide, "Text 19", 5.89, 4.18, 1.45, SP["body_min"], lines=1)
    # 左卡：标题 + 判断句（同容器；不再把句子挪到卡外全宽）
    _place(slide, "Shape 20", 0.72, 5.02, 5.85, 1.76, None)
    _place(slide, "Shape 21", 0.72, 5.02, 0.06, 1.76, None)
    _fit(slide, "Text 22", 0.94, 5.12, 2.50, SP["body_min"], lines=1)
    _fit(slide, "Text 23", 0.94, 5.55, 5.55, 28, lines=2)
    # 右卡：标题 + 传播金句（同字号层级，两卡视觉均衡）
    _place(slide, "Shape 24", 6.94, 5.02, 5.85, 1.76, None)
    _place(slide, "Shape 25", 6.94, 5.02, 0.06, 1.76, None)
    _fit(slide, "Text 26", 7.16, 5.12, 2.50, SP["body_min"], lines=1)
    _fit(slide, "Text 27", 7.16, 5.55, 4.70, 28, lines=2)
    _fit(slide, "Text 28", 0.72, FOOTER_Y, 6.50, SP_FOOTER, lines=1)
    _fit(slide, "Text 29", 11.80, FOOTER_Y, 0.81, SP_FOOTER, lines=1)
    if acts is not None:
        acts.append({"rule": "layout_p5", "page": 5, "layout_version": LAYOUT_VERSION})


def layout_p6(slide, acts):
    """P6：两侧条目同顶线/行距 + 26pt 正文；竖线在标签下方起；结论整幅单行。

    修 5a6bcb2d：① 结论只剩“需求。”的失衡断行 → 改整幅单行（30pt、框宽
    11.36in，实测容纳得下 23 字）；② 中央竖线穿过“同一个人” → 标签移到中央列
    顶部，竖线自标签下方起，不盖字；配图落中央列底部服务“同一个人”语义。
    """
    SP = SIZE_POLICY
    _fit(slide, "Text 1", MARGIN_IN, 0.22, CONTENT_W_IN, SP["page_title"], lines=1)
    _fit(slide, "Text 2", MARGIN_IN, 1.13, CONTENT_W_IN, SP["lead"], lines=1)

    panel_y, panel_h = 1.83, 4.31
    panels = {"left": (MARGIN_IN, 5.05), "right": (7.563, 5.05)}
    groups = {
        "left": {"rect": "Shape 3", "head": "Text 4",
                 "dots": ["Shape 5", "Shape 7", "Shape 9", "Shape 11"],
                 "items": ["Text 6", "Text 8", "Text 10", "Text 12"],
                 "lines": [1, 1, 1, 1]},
        "right": {"rect": "Shape 13", "head": "Text 14",
                  "dots": ["Shape 15", "Shape 17", "Shape 19", "Shape 21"],
                  "items": ["Text 16", "Text 18", "Text 20", "Text 22"],
                  "lines": [1, 1, 2, 1]},
    }
    for side, (px, pw) in panels.items():
        g = groups[side]
        _place(slide, g["rect"], px, panel_y, pw, panel_h, None)
        _fit(slide, g["head"], px + 0.22, 1.91, pw - 0.42, SP["section_head"], lines=1)
        y, item_w = 2.66, pw - 0.65
        for dot, item, n in zip(g["dots"], g["items"], g["lines"]):
            h = box_h(SP["body"], n)
            _place(slide, dot, px + 0.28, y + 0.16, 0.08, 0.08, None)
            _fit(slide, item, px + 0.55, y, item_w, SP["body"], lines=n)
            y += h + 0.06

    _fit(slide, "Text 24", 5.78, 1.91, 1.77, SP["section_head"], lines=1)
    _place(slide, "Shape 23", 6.667, 2.66, 0.0, 1.55, None)
    for nm in ("Picture 31", "Rectangle 32"):
        _place(slide, nm, 5.79, 4.28, 1.75, 1.06, None)

    _place(slide, "Shape 25", MARGIN_IN, 6.20, CONTENT_W_IN, 0.86, None)
    _place(slide, "Text 26", 0.85, 5.94, 0.35, 0.42, 32)
    _fit(slide, "Text 27", 1.25, 6.22, 11.36, SP["conclusion"], lines=1)

    _fit(slide, "Text 28", MARGIN_IN, FOOTER_Y, 6.10, SP["footer"], lines=1)
    _fit(slide, "Text 29", 11.80, FOOTER_Y, 0.81, SP["footer"], lines=1)
    if acts is not None:
        acts.append({"rule": "layout_p6", "page": 6, "layout_version": LAYOUT_VERSION})


def layout_p7(slide, acts):
    """P7：三卡改三条全宽行（横幅 Picture24 原位 z 前置 ✓）+ 结论带两行。

    推导：title 15 字 1L；lead 35 字 24pt 2L y1.15（墨迹至 2.193）；三行
    y2.60/3.60/4.60（ord 20pt | tit w4.60 ≤2L | desc w6.20 2L，末行墨迹底
    5.643）；结论 Text19（36 字，最低 ≥15 字 body）28pt 两行 y5.62
    （est 14.84 → 2L，墨迹 5.834-6.837 ≤6.88），带 Shape18 (0.58,5.50,
    12.41,1.50)；横幅 (0.72,5.66) 原位在文字前；来源 16pt x6.40 w5.30。
    原三卡/左条缩点。
    """
    SP = SIZE_POLICY
    marker_pairs = (("Shape 3", "Shape 4"), ("Shape 8", "Shape 9"),
                    ("Shape 13", "Shape 14"))
    for (outer, rail), ry in zip(marker_pairs, (2.20, 3.40, 4.60)):
        _dot(slide, outer, x=0.72, y=ry + 0.25)
        _dot(slide, rail, x=0.72, y=ry + 0.25)
    _fit(slide, "Text 1", 0.72, 0.20, 11.89, SP["page_title"], lines=1)
    _fit(slide, "Text 2", 0.72, 0.95, 11.89, SP["body_min"], lines=2)
    rows = [("Text 5", "Text 6", "Text 7", 2.20),
            ("Text 10", "Text 11", "Text 12", 3.40),
            ("Text 15", "Text 16", "Text 17", 4.60)]
    for o, t, d, ry in rows:
        _fit(slide, o, 0.92, ry + 0.04, 0.55, SP["index"], lines=1)
        _fit(slide, t, 1.58, ry, 4.50, SP["body_min"], lines=2)
        _fit(slide, d, 6.20, ry, 6.70, SP["body_min"], lines=2)
    # 行2 标题 14 字在 w4.50 会把'来'折成孤字行 → 显式断在'但'前（a:br 不改字符）
    insert_break_before(_by_name(slide, "Text 11"), "但")
    # 行3 标题在自动排版中将末尾“谨慎”孤立；保留词组完整并使两行均衡。
    insert_break_before(_by_name(slide, "Text 16"), "消费自然偏谨慎")
    # 结论带 top 5.50→5.78：Text 17 第 2 行墨迹底 5.64，第三轮带沿切割字脚
    # （HG04 遮挡 9 字 + HG02 '，''。' 不可见）；横幅同步下移收进带内
    _place(slide, "Shape 18", 0.58, 5.78, 12.41, 0.98, None)          # 结论带：一行句子按真实内容收高
    _fit(slide, "Text 19", 0.72, 5.94, 11.89, 28, lines=1)
    for nm in ("Picture 24", "Rectangle 25"):
        _place(slide, nm, 0.72, 5.76, 12.02, 1.00, None)              # 与结论带对齐；右侧补足最小内边距
    _fit(slide, "Text 20", 0.72, FOOTER_Y, 6.50, SP_FOOTER, lines=1)
    _fit(slide, "Text 21", 11.80, FOOTER_Y, 0.81, SP_FOOTER, lines=1)
    _fit(slide, "Text 22", 6.40, FOOTER_Y, 5.30, SP_FOOTER, lines=1)  # 来源
    if acts is not None:
        acts.append({"rule": "layout_p7", "page": 7, "layout_version": LAYOUT_VERSION})


def layout_p9(slide, acts):
    """P9：四政策行（带 h0.85，26pt 标签+说明单行）+ 结论带（Text17 30pt）。

    推导：title 1L；lead 24pt 1L(1.313-1.829)；行 y1.86/2.76/3.66/4.56
    （内容 y+0.10，26pt 墨迹底行4=5.220，带 pad 达标）；结论 Text17（22 字，
    最低 ≥15 字 body；desc 均 <15 字被排除）30pt 单行 y6.006（est 9.72，
    墨迹 6.225-6.644），带 Shape15 (0.58,5.86,12.23,0.94)；引号带内左。
    """
    SP = SIZE_POLICY
    _fit(slide, "Text 1", 0.72, 0.20, 11.89, SP["page_title"], lines=1)
    _fit(slide, "Text 2", 0.72, 1.13, 11.89, SP["body_min"], lines=1)
    bands = ["Shape 3", "Shape 6", "Shape 9", "Shape 12"]
    labels = ["Text 4", "Text 7", "Text 10", "Text 13"]
    descs = ["Text 5", "Text 8", "Text 11", "Text 14"]
    for i, ry in enumerate([1.86, 2.76, 3.66, 4.56]):
        _place(slide, bands[i], 1.15, ry, 10.95, 0.85, None)
        _fit(slide, labels[i], 1.45, ry + 0.10, 1.40, SP["lead"], lines=1)
        _fit(slide, descs[i], 2.95, ry + 0.10, 8.90, SP["lead"], lines=1)
    _place(slide, "Shape 15", 0.58, 5.86, 12.23, 0.94, None)
    _fit(slide, "Text 16", 0.80, 6.08, 0.30, 20, lines=1)             # 引号
    _fit(slide, "Text 17", 1.30, 6.006, 11.30, 30, lines=1)
    _fit(slide, "Text 18", 0.72, FOOTER_Y, 6.50, SP_FOOTER, lines=1)
    _fit(slide, "Text 19", 11.80, FOOTER_Y, 0.81, SP_FOOTER, lines=1)
    if acts is not None:
        acts.append({"rule": "layout_p9", "page": 9, "layout_version": LAYOUT_VERSION})


def layout_p8(slide, acts):
    """P8：五项影响改统一基线有序行；行框按 26pt 真实字形高度给内边距。

    修 5a6bcb2d：行框 0.52in 装不下 26pt 字形（真实需要 0.656in），字形压在框
    底；结论同样缺底部内边距。现在行框 0.656in、结论框按 2 行度量；同行内
    序号/标题/说明按**字形顶**对齐（不是框顶对齐）。导语取 24pt（正文下限）：
    该页要同时容纳 5 行正文 + 30pt 两行结论 + 页脚，26pt 导语会挤掉行间距。
    """
    SP = SIZE_POLICY
    _fit(slide, "Text 1", MARGIN_IN, 0.20, CONTENT_W_IN, SP["page_title"], lines=1)
    _fit(slide, "Text 2", MARGIN_IN, 1.07, CONTENT_W_IN, SP["body_min"], lines=2)

    bands = ["Shape 3", "Shape 8", "Shape 13", "Shape 18", "Shape 23"]
    bars = ["Shape 4", "Shape 9", "Shape 14", "Shape 19", "Shape 24"]
    idxs = ["Text 5", "Text 10", "Text 15", "Text 20", "Text 25"]
    titles = ["Text 6", "Text 11", "Text 16", "Text 21", "Text 26"]
    descs = ["Text 7", "Text 12", "Text 17", "Text 22", "Text 27"]
    # 原生放映发现第五行与结论卡边线相接；整组上移 0.15in，保留 26pt 字号。
    row_h, row_gap, y0 = box_h(SP["body"], 1), 0.02, 1.95
    base_off = ASC_OFFSET_EM * SP["body"] / 72.0
    desc_w = CONTENT_W_IN - 3.75 + MARGIN_IN - 0.13
    for i in range(5):
        ry = y0 + i * (row_h + row_gap)
        _place(slide, bands[i], MARGIN_IN, ry, CONTENT_W_IN, row_h, None)
        _place(slide, bars[i], MARGIN_IN, ry, 0.07, row_h, None)
        for nm, pt, x, w in ((idxs[i], SP["index"], 0.90, 0.55),
                             (titles[i], SP["body"], 1.52, 2.10),
                             (descs[i], SP["body"], 3.75, desc_w)):
            dy = base_off - ASC_OFFSET_EM * pt / 72.0   # 字形顶对齐，而非框顶对齐
            ty = ry + dy
            if nm == "Text 17":
                # 第三轮 HG05 实测：Text 17 唯一被点（card Shape 13，底内边距
                # 0.106 < 0.126，其余行同几何未触发=该行真实墨迹更深）。上移
                # 0.03 → 底 0.136 ≥ 0.126；顶 0.19-0.03=0.16 仍达标；行内视觉
                # 偏移 2.2pt 不破坏序号/标题字形顶对齐。
                ty -= 0.03
            _fit(slide, nm, x, ty, w, pt, lines=1)

    _place(slide, "Shape 28", 0.60, 5.46, 9.90, 1.55, None)
    _fit(slide, "Text 29", 0.85, 5.52, 0.30, 20, lines=1)
    _fit(slide, "Text 30", 1.25, 5.50, 8.87, SP["conclusion"], lines=2)
    for nm in ("Picture 34", "Rectangle 35"):
        _place(slide, nm, 10.61, 5.46, 2.00, 1.21, None)

    _fit(slide, "Text 31", MARGIN_IN, FOOTER_Y, 6.10, SP["footer"], lines=1)
    _fit(slide, "Text 32", 11.80, FOOTER_Y, 0.81, SP["footer"], lines=1)
    if acts is not None:
        acts.append({"rule": "layout_p8", "page": 8, "layout_version": LAYOUT_VERSION})


def layout_p10(slide, acts):
    """P10：双卡（表头+条目 24pt pitch0.44）+ 结论带（Text25 w12.53 单行）。

    推导：title 25 字 36pt 2L(0.475-2.040)；lead 24pt 1L y2.15；双卡
    (0.85/6.85, 2.95, 5.65, 3.17)：表头 y3.10、条目 y3.82 起 pitch0.44
    （条5 墨迹底约 6.09，卡底 6.20，保留约 0.11in 内距）；结论 Text25
    （30 字，最低 ≥15 字 body）28pt 单行 y6.15 x0.40 w12.53（est 12.37，
    墨迹 6.364-6.753），带 Shape23 (0.46,6.34,12.41,0.54)。
    """
    SP = SIZE_POLICY
    _fit(slide, "Text 1", 0.72, 0.20, 11.89, SP["page_title"], lines=2)
    # 第六轮监督：原自然折行 23/2（首行到“防坠落”、次行只剩“方案”）→ 按语义
    # 短语在“结论，/写普通人的…”处显式断行（a:br 不改字符序列），两行 16/10 字，
    # 字号保持 36pt（角色区间 36–40），不缩字、不改事实。
    insert_break_before(_by_name(slide, "Text 1"), "写普通人的防坠落方案")
    _fit(slide, "Text 2", 0.72, 2.15, 11.89, SP["body_min"], lines=1)
    for cx, head, dots, items in (
            (0.85, "Text 4", ["Shape 5", "Shape 7", "Shape 9"],
             ["Text 6", "Text 8", "Text 10"]),
            (6.85, "Text 12", ["Shape 13", "Shape 15", "Shape 17", "Shape 19",
                               "Shape 21"],
             ["Text 14", "Text 16", "Text 18", "Text 20", "Text 22"])):
        _place(slide, "Shape 3" if cx < 5 else "Shape 11", cx, 2.95, 5.65, 3.17, None)
        _fit(slide, head, cx + 0.40, 3.10, 4.60, SP["body_min"], lines=1)
        for k, (dot, it) in enumerate(zip(dots, items)):
            iy = 3.82 + k * 0.415
            _place(slide, dot, cx + 0.40, iy + 0.13, 0.08, 0.08, None)
            _fit(slide, it, cx + 0.58, iy, 4.60, SP["body_min"], lines=1)
    _place(slide, "Shape 23", 0.46, 6.23, 12.41, 0.67, None)
    _fit(slide, "Text 24", 12.40, 6.19, 0.30, 20, lines=1)            # 引号置于结论带右端
    _fit(slide, "Text 25", 0.40, 6.16, 12.53, 28, lines=1)
    _fit(slide, "Text 26", 0.72, FOOTER_Y, 6.50, SP_FOOTER, lines=1)
    _fit(slide, "Text 27", 11.80, FOOTER_Y, 0.81, SP_FOOTER, lines=1)
    if acts is not None:
        acts.append({"rule": "layout_p10", "page": 10, "layout_version": LAYOUT_VERSION})


def layout_p11(slide, acts):
    """P11：八卡缩点，条目改 2 列×4 行（序号同行 + 条目 24pt 两行 w5.05）。

    推导：title 1L；lead 24pt 1L(1.313-1.829)；行 y1.80/2.77/3.74/4.71
    （ord 20pt 同行 x_col、条目 x_col+0.75 w5.05 2L，est/实一致，末行墨迹
    底约 5.843，与结论卡顶 5.95 留约 0.107in；结论 Text29（29 字，最低
    ≥15 字 body）28pt 单行 y6.00 x0.50 w12.43（est 11.96，墨迹 6.214-6.703），
    带 Shape27 (0.36,5.95,12.61,0.91)；引号带内右 (12.55,6.10)。八张原卡缩点（HG01 守恒）。
    """
    SP = SIZE_POLICY
    markers = ("Shape 3", "Shape 6", "Shape 9", "Shape 12",
               "Shape 15", "Shape 18", "Shape 21", "Shape 24")
    for k, nm in enumerate(markers):
        cx = 0.72 if k < 4 else 6.75
        ry = 1.80 + (k % 4) * 0.97
        ox = cx + (0.03 if k >= 4 else 0.0)
        _dot(slide, nm, x=ox - 0.18, y=ry + 0.25)
    _fit(slide, "Text 1", 0.72, 0.20, 11.89, SP["page_title"], lines=1)
    _fit(slide, "Text 2", 0.72, 1.13, 11.89, SP["body_min"], lines=1)
    ords = ["Text 4", "Text 7", "Text 10", "Text 13",
            "Text 16", "Text 19", "Text 22", "Text 25"]
    items = ["Text 5", "Text 8", "Text 11", "Text 14",
             "Text 17", "Text 20", "Text 23", "Text 26"]
    for k in range(8):
        cx = 0.72 if k < 4 else 6.75
        ry = 1.80 + (k % 4) * 0.97
        # 第三轮 HG05 实测：右列 ords（Text 16/19/22/25，card Rectangle 34，
        # 左边 0.36 起）左内边距 0.093 < 0.10；左列同几何未被点=其卡沿更靠左。
        # 右列 +0.03 → 墨迹左边距 0.123 ≥ 0.10；ord 框右 cx+0.58 仍 < 条目 x。
        ox = cx + (0.03 if k >= 4 else 0.0)
        _fit(slide, ords[k], ox, ry, 0.55, SP["index"], lines=1)
        _fit(slide, items[k], cx + 0.75, ry, 5.05, SP["body_min"], lines=2)
    _place(slide, "Shape 27", 0.36, 5.95, 12.61, 0.91, None)
    # 第三轮 HG05 实测：Text 29 左内边距 0.048 < 0.136（card Shape 27 左沿
    # 0.36）。x 0.40→0.50 → 0.148 ≥ 0.136；右沿不变（w 同步 -0.10），
    # est 11.96 ≤ 12.43 仍单行。
    _fit(slide, "Text 29", 0.50, 6.00, 12.43, 28, lines=1)
    _fit(slide, "Text 28", 12.55, 6.10, 0.30, 20, lines=1)            # 引号
    _fit(slide, "Text 30", 0.72, FOOTER_Y, 6.50, SP_FOOTER, lines=1)
    _fit(slide, "Text 31", 11.80, FOOTER_Y, 0.81, SP_FOOTER, lines=1)
    if acts is not None:
        acts.append({"rule": "layout_p11", "page": 11, "layout_version": LAYOUT_VERSION})


def layout_p12(slide, acts):
    """P12：五窄卡缩点，改 5 条全宽行（序号|标题 26pt|说明 24pt）+ 结论两行。

    推导：title 1L；lead 24pt 1L；行 y1.95/2.60/3.25/3.90/4.55（num 20pt
    x0.90、tit 26pt x1.55 w2.10 1L、desc 24pt x3.80 w8.80：行1-4 单行，
    行5 27 字 2 行+protected break 于'36个月'前，墨迹底 5.593）；结论
    Text25（32 字，最低 ≥15 字 body）28pt 两行 y5.60 x0.40 w12.53
    （est 13.19 → 2L，墨迹 5.814-6.817），带 Shape23 (0.32,5.48,12.69,
    1.48)；引号 (12.55,5.70)。五张原窄卡缩点。
    """
    SP = SIZE_POLICY
    marker_names = ("Shape 3", "Shape 7", "Shape 11", "Shape 15", "Shape 19")
    row_tops = (1.88, 2.50, 3.12, 3.74, 4.36)
    for nm, ry in zip(marker_names, row_tops):
        # Former narrow-card markers follow the new full-width row sequence.
        _dot(slide, nm, x=0.72, y=ry + 0.25)
    _fit(slide, "Text 1", 0.72, 0.20, 11.89, SP["page_title"], lines=1)
    _fit(slide, "Text 2", 0.72, 1.13, 11.89, SP["body_min"], lines=1)
    nums = ["Text 4", "Text 8", "Text 12", "Text 16", "Text 20"]
    tits = ["Text 5", "Text 9", "Text 13", "Text 17", "Text 21"]
    descs = ["Text 6", "Text 10", "Text 14", "Text 18", "Text 22"]
    dla = [1, 1, 1, 1, 2]
    for k, ry in enumerate([1.88, 2.50, 3.12, 3.74, 4.36]):
        _fit(slide, nums[k], 0.90, ry, 0.55, SP["index"], lines=1)
        _fit(slide, tits[k], 1.55, ry, 2.10, SP["lead"], lines=1)
        _fit(slide, descs[k], 3.80, ry, 8.80, SP["body_min"], lines=dla[k])
    # 行5 说明：'36个月' 不得跨行 → 显式断点插 a:br（字符序列不变）
    insert_break_before(_by_name(slide, descs[4]), "36个月")
    # 照片纱罩 38%→65%（证据化局部底板调整，与 P3 同构同处理）：实测
    # Text 22 '3' 处无字背景 [93,108,139]（bg_range 130/134/113=照片区）
    # → 对比 4.49 < 4.5；65% → 背景约 59 → ≈9，照片仍透出 35%。
    _set_shape_alpha(slide, "Rectangle 30", 65)
    if acts is not None:
        acts.append({"rule": "layout_p12", "page": 12, "layout_version": LAYOUT_VERSION})
    _place(slide, "Shape 23", 0.32, 5.48, 12.69, 1.48, None)
    _fit(slide, "Text 25", 0.40, 5.60, 12.53, 28, lines=2)
    _fit(slide, "Text 24", 12.55, 5.70, 0.30, 20, lines=1)            # 引号
    _fit(slide, "Text 26", 0.72, FOOTER_Y, 6.50, SP_FOOTER, lines=1)
    _fit(slide, "Text 27", 11.80, FOOTER_Y, 0.81, SP_FOOTER, lines=1)


def layout_p13(slide, acts):
    """P13：结语页——Text3 26pt 两硬行 + 分隔 + 收口 + 结论（Text6 4L）+ 来源。

    推导：title 1L；lead 24pt 1L；Text3（显式两行 26pt，行2 29 字 est 11.10
    ≤11.89）y1.98 墨迹至 3.109；Shape4 分隔 (4.55,3.18)；Text5 y3.15
    （墨迹 3.333-3.849）；结论 Text6（61 字，最低 ≥15 字 body；显式两行各
    再折行 → 4 行）y3.95 28pt（墨迹 4.164-6.396 ≤6.88）；来源 Text7 16pt
    两行 y6.32（墨迹 6.442-7.014，在结论下、页脚上，无 HG06 对约束）。
    """
    SP = SIZE_POLICY
    _fit(slide, "Text 1", 0.72, 0.20, 11.89, SP["page_title"], lines=1)
    _fit(slide, "Text 2", 0.72, 1.13, 11.89, SP["body_min"], lines=1)
    _fit(slide, "Text 3", 1.15, 1.98, 11.89, SP["lead"], lines=2)
    _place(slide, "Shape 4", 4.55, 3.25, 4.20, 0.07, None)
    _fit(slide, "Text 5", 1.35, 3.40, 10.50, SP["body_min"], lines=1)
    # 结论（61 字显式两行，行1 需折行 → 共 3 行；top<4.2 → body 角色 28pt 合规）
    _fit(slide, "Text 6", 0.40, 4.00, 12.73, 28, lines=3)
    _fit(slide, "Text 7", 1.10, 6.00, 11.20, SP_FOOTER, lines=2)      # 来源 16pt
    _fit(slide, "Text 8", 0.72, FOOTER_Y, 6.50, SP_FOOTER, lines=1)
    _fit(slide, "Text 9", 11.80, FOOTER_Y, 0.81, SP_FOOTER, lines=1)
    if acts is not None:
        acts.append({"rule": "layout_p13", "page": 13, "layout_version": LAYOUT_VERSION})


#: 13 页专用版式注册表：每页一条确定性规则（非一次性脚本）。
PAGE_LAYOUTS = {
    1: layout_p1, 2: layout_p2, 3: layout_p3, 4: layout_p4, 5: layout_p5,
    6: layout_p6, 7: layout_p7, 8: layout_p8, 9: layout_p9, 10: layout_p10,
    11: layout_p11, 12: layout_p12, 13: layout_p13,
}


def apply_readable_deck(prs, pages=None, all_pages=False):
    """全稿处理：默认 13 页全走专用版式；pages 限定子集；all_pages=True 时
    未注册页走通用规则（现 13 页已全部注册，generic 仅作兜底）。

    返回的 acts 记录每页走了哪条规则，便于监督核对"不是一次性脚本样稿"。
    """
    if pages is None:
        pages = tuple(PAGE_LAYOUTS)
    acts = []
    ea = set_native_ea_font(prs)
    for i, slide in enumerate(prs.slides, 1):
        if i in PAGE_LAYOUTS and (all_pages or i in pages or pages is None):
            PAGE_LAYOUTS[i](slide, acts)
        elif all_pages:
            layout_generic(slide, i, acts)
    ink_runs = apply_contrast_palette(prs)
    return {"ea_font_changes": ea, "ink_runs": ink_runs, "acts": acts,
            "layout_version": LAYOUT_VERSION,
            "pages_relayout": sorted({a["page"] for a in acts})}


def apply_readable_batch(prs, pages=None):
    """兼容旧调用：只重排指定页（默认全部注册页；其余页仅改东亚字体槽）。"""
    return apply_readable_deck(prs, pages=pages, all_pages=False)


# --------------------------------------------------------------------------- 深色金色设计语言
# 监督 2026-10-21 14:20 有界反馈 + 人类指令（逐字）：
#   "Fix the HG09 sampling error directly and restore the required dark and gold
#    style do not recolor the deck to hide false findings."
#   "禁止简单调低阈值、忽略该门禁或通过换浅色主题掩盖测量问题。保留人类要求的
#    深色金色语言和真实可读布局；需要换局部底板时以实际低对比证据为依据。"
#
# HG09 误报根因在门禁取样（用含字图 crop 中位数当背景；字形覆盖 ~50% 时混合成
# 中灰），不在设计配色——监督已用无字渲染实测证伪两例（P9 '创'/'修' 真实背景
# [16,16,20] 全平坦、前景 [237,237,234]，真实对比 16.18 > 4.5）。修复必须在
# layout_gate.py 的采样（背景真值=无字渲染同坐标 crop + 差分核心前景），
# 本模块恢复基线深色金色原貌：整稿改色全部停用（迭代 3/4 的强制亮面板 +
# 纯黑墨方案撤销）。构建从基线 v12 拷贝起步，不触碰颜色即天然回归原设计。
#
# 基线 v12（exact_v12/e43dad9e）实测设计 token（inspect_baseline_style.py 取证）：
_STYLE_TOKENS = {
    "page_and_card_base": "101014",   # 近黑底（13 页 p:bg 与卡片一致）
    "gold_accent": "C9A45B",          # 金色强调（Shape 0 等）
    "body_ink": "EDEDEA",             # 暖白正文墨色（P9 Text 13/17 实测）
    "photo_scrim": "101014",          # 照片纱罩（Rectangle 22 = 101014@74%）
    "photo_scrim_alpha_pct": 74,
}
# 仅当固定采样器给出**具体对象**的真实低对比证据（页/形状/字符/实测对比度）
# 时，才允许按证据做局部底板调整；每次调整必须在该 layout_pN 内注释实测值。
#
# 已删除 `_raise_decimal`（第五轮用于给 44pt 数字里的 '.' run 设 baseline=21000
# 以凑 HG08 旧行簇阈值）。第六轮监督实测：该做法把小数点抬到数字中线，渲染成
# "+0·9%" 中点，属可见数值语义退化。归簇职责改由门禁承担（按基线/字号/标点
# 分组，见 layout_gate.HG08），产品端不再改任何 run 的 baseline。


def apply_contrast_palette(prs) -> int:
    """（整稿改色已停用，恒返回 0。保留函数签名以兼容调用方。）

    历史：迭代 3/4 曾把每页背景/承载面强制亮面板、正文统一深墨，以绕过
    门禁 HG09 的取样缺陷。监督 14:20 已证该缺陷根因是门禁以**含字图**中位
    数当背景（字形覆盖 ~50% 时混合成中灰），并以无字渲染实测证伪两例
    （P9 '创'/'修'：真实背景 [16,16,20] 全平坦、前景 [237,237,234]，
    真实对比 16.18 > 4.5，而门禁误报 3.98/3.66）。
    修复落在 layout_gate.py：背景真值 = 无字渲染同坐标 crop 的中位数，
    前景 = full 与 notext 差分核心（最远 1/3 像素中位数）。本函数不再
    触碰任何颜色、填充、alpha 与 run 字色——构建从基线拷贝起步，深色金色
    （101014 / C9A45B / EDEDEA / 101014@74% 纱罩）原样保留。
    """
    return 0


# --------------------------------------------------------------------------- 通用页
_FOOTER_HINTS = ("Yiten Huang", "来源", "资料来源")


def _is_footer_text(t: str) -> bool:
    import re as _re
    t = (t or "").strip()
    if any(h in t for h in _FOOTER_HINTS):
        return True
    return bool(_re.fullmatch(r"\d{1,2}", t))


def _is_mark_text(t: str) -> bool:
    return (t or "").strip() in ("“", "”", "「")


def _columns_of(shapes, tol_in=1.5):
    """按 x 中心聚类成列（保留原横向结构的语义）。"""
    cols = []
    for sh in sorted(shapes, key=lambda s: s.left):
        c = (sh.left + sh.width / 2) / IN
        for col in cols:
            if abs(c - col["c"]) <= tol_in:
                col["items"].append(sh)
                col["c"] = sum((x.left + x.width / 2) / IN for x in col["items"]) / len(col["items"])
                break
        else:
            cols.append({"c": c, "items": [sh]})
    for col in cols:
        col["items"].sort(key=lambda s: s.top)
    return cols


def layout_generic(slide, page, acts):
    """其余页的同一套规则：**只改字号与纵向容纳**，不改文字。

    与 P2/P6/P8 同一口径：
    - 角色：最上为页标题(36)、其次导语(26/24)、底部**长句**(≥20字)为结论(30)、
      含 Yiten Huang/来源/纯页码 为页脚(16)、其余为正文；
    - 每个形状按框宽实测折行数（含显式换行）→ 真实框高（顶锚定 + 底部内边距）；
    - 横向按原结构分列并**等宽归一化**，列内纵向重排；依次尝试
      (边距0.72/26pt/行距0.10) → (0.72/24pt/0.08) → (0.60/24pt/0.06) →
      (0.55/24pt/0.05)，取第一个容纳得下的组合，正文不低于 24pt 下限；
    - 页脚多行也钳在画布内；结论与页脚留真实字形净空；配图移到右下槽。
    """
    SP = SIZE_POLICY
    texts = [sh for sh in slide.shapes
             if getattr(sh, "has_text_frame", False) and sh.text_frame.text.strip()]
    if not texts:
        return
    ordered = sorted(texts, key=lambda s: (s.top, s.left))
    footers = [s for s in texts if _is_footer_text(s.text_frame.text)]
    marks = [s for s in texts if _is_mark_text(s.text_frame.text)]
    rest = [s for s in ordered if s not in footers and s not in marks]
    if not rest:
        return
    title = rest[0]
    lead = rest[1] if len(rest) > 1 else None
    tail = rest[2:]
    bottom = [s for s in tail if s.top / IN > 4.5 and len(s.text_frame.text.strip()) >= 20]
    conclusion = max(bottom, key=lambda s: len(s.text_frame.text)) if bottom else None
    content = [s for s in tail if s is not conclusion]

    # 标题 / 导语
    _fit(slide, title.name, MARGIN_IN, 0.22, CONTENT_W_IN, SP["page_title"], lines=1)
    y = title.top / IN + title.height / IN + 0.10
    if lead is not None:
        n = measure_lines(lead.text_frame.text, SP["lead"], CONTENT_W_IN)
        pt = SP["lead"] if n <= 2 else SP["body_min"]
        _fit(slide, lead.name, MARGIN_IN, y, CONTENT_W_IN, pt, lines=min(n, 3))
        y = lead.top / IN + lead.height / IN + 0.14

    # 页脚：多行也钳在画布内
    footer_y = 7.03
    for s in footers:
        w = min(max(s.width / IN, 1.2), CONTENT_W_IN)
        n = min(measure_lines(s.text_frame.text, SP["footer"], w), 3)
        h = box_h(SP["footer"], n)
        fy = min(footer_y, CANVAS_H_IN - 0.06 - h)
        _fit(slide, s.name, min(max(s.left / IN, MARGIN_IN), CANVAS_W_IN - MARGIN_IN - w),
             fy, w, SP["footer"], lines=n)

    # 结论（页脚之上留净空）
    if conclusion is not None:
        cw = 9.60
        n = min(measure_lines(conclusion.text_frame.text, SP["conclusion"], cw), 3)
        h = box_h(SP["conclusion"], n)
        concl_y = min(footer_y, CANVAS_H_IN - 0.06 - h) - 0.14 - (h - BOTTOM_PAD_IN)
        _fit(slide, conclusion.name, MARGIN_IN + 0.53, concl_y, cw, SP["conclusion"], lines=n)
        for m in marks:
            _place(slide, m.name, MARGIN_IN + 0.13, concl_y - 0.26, 0.35, 0.42, 32)
        content_bottom = concl_y - 0.14
    else:
        content_bottom = footer_y - 0.16

    # 正文：分列 → 等宽归一化 → 列内重排，取第一个容纳得下的组合
    if content:
        cols = _columns_of(content)
        avail = max(content_bottom - y, 0.5)
        plan = None
        for margin, pt, gap in ((0.72, SP["body"], 0.10), (0.72, SP["body_min"], 0.08),
                                (0.60, SP["body_min"], 0.06), (0.55, SP["body_min"], 0.05)):
            cw_tot = CANVAS_W_IN - 2 * margin
            col_gap = 0.20
            col_w = (cw_tot - col_gap * (len(cols) - 1)) / len(cols)
            heights = []
            for col in cols:
                hsum = 0.0
                for s in col["items"]:
                    w = 0.55 if len(s.text_frame.text.strip()) <= 2 else col_w
                    n = measure_lines(s.text_frame.text, pt, w)
                    hsum += box_h(pt, n) + gap
                heights.append(hsum - gap)
            if max(heights) <= avail:
                plan = (margin, pt, gap, col_w, col_gap)
                break
        if plan is None:
            plan = (0.55, SP["body_min"], 0.05,
                    (CANVAS_W_IN - 1.10 - 0.20 * (len(cols) - 1)) / len(cols), 0.20)
        margin, pt, gap, col_w, col_gap = plan
        for j, col in enumerate(cols):
            cx = margin + j * (col_w + col_gap)
            cy = y
            for s in col["items"]:
                short = len(s.text_frame.text.strip()) <= 2
                w = 0.55 if short else col_w
                n = measure_lines(s.text_frame.text, pt, w)
                _fit(slide, s.name, cx, cy, w, pt, lines=n)
                cy += box_h(pt, n) + gap

    for sh in slide.shapes:
        if sh.__class__.__name__ == "Picture" and sh.width / IN < 8.0:
            set_box(sh, 10.61, 5.55, 2.00, 1.21)
    if acts is not None:
        acts.append({"rule": "layout_generic", "page": page,
                     "layout_version": LAYOUT_VERSION})
