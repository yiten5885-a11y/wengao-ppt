"""W2-B　约束式版式引擎：网格布局 · 溢出检测 · 两栏再平衡。

原则（W2 指令 §4-B）：
- 在 12 栏网格里按约束布局；文字用 `measure` 判断放不放得下；
- 溢出时**重新平衡**（单栏→两栏）或如实上报（由规划器分页），不缩字、不删内容；
- 修掉已知缺陷：不产生"（续）"标题、不在多页重复条目（分页由规划器按台账切分，
  引擎只对单页负责）；整套不再白底小字（字号来自硬门禁档，正文 ≥26pt）。

区域模型：title_band（标题）/ content（内容）/ footnote_zone（脚注带）。
硬门禁 geometry.region_gap_min_in：title_lead 0.12、lead_body 0.16、
body_conclusion 0.18、content_footer_zone 0.12 —— 引擎取等号或更大。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import measure, tokens
from .tokens import EMU_PER_IN, CANVAS_H_EMU, CANVAS_W_EMU, MARGINS_EMU

#: 硬门禁 region_gap_min_in
GAP_TITLE_LEAD_EMU = _t = int(0.12 * EMU_PER_IN)
GAP_LEAD_BODY_EMU = int(0.16 * EMU_PER_IN)
GAP_BODY_CONCLUSION_EMU = int(0.18 * EMU_PER_IN)
GAP_CONTENT_FOOTER_EMU = int(0.12 * EMU_PER_IN)
#: 硬门禁 footer_zone_from_bottom_in {top: 0.5, bottom: 0.15}
FOOTER_ZONE_TOP_EMU = CANVAS_H_EMU - int(0.50 * EMU_PER_IN)
FOOTER_ZONE_BOTTOM_EMU = CANVAS_H_EMU - int(0.15 * EMU_PER_IN)


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    def inset(self, dx: int, dy: int) -> "Rect":
        return Rect(self.x + dx, self.y + dy, max(0, self.w - 2 * dx),
                    max(0, self.h - 2 * dy))

    def intersects(self, other: "Rect", epsilon_emu: int = 0) -> bool:
        return not (self.x + self.w <= other.x + epsilon_emu
                    or other.x + other.w <= self.x + epsilon_emu
                    or self.y + self.h <= other.y + epsilon_emu
                    or other.y + other.h <= self.y + epsilon_emu)

    def as_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}


class Grid:
    """12 栏网格：栏起点/跨栏矩形。"""

    def __init__(self, margins: dict | None = None, cols: int = tokens.GRID_COLS):
        m = margins or MARGINS_EMU
        self.cols = cols
        self.left = m["left"]
        self.top = m["top"]
        self.content_w = CANVAS_W_EMU - m["left"] - m["right"]
        self.content_h = CANVAS_H_EMU - m["top"] - m["bottom"]
        self.col_w = (self.content_w - tokens.GUTTER_EMU * (cols - 1)) // cols

    def col_x(self, index: int) -> int:
        return self.left + index * (self.col_w + tokens.GUTTER_EMU)

    def rect(self, col_start: int, col_span: int, y: int, h: int) -> Rect:
        """col_start 0 基；col_span 1..cols。"""
        if not 0 <= col_start < self.cols or not 1 <= col_span <= self.cols - col_start:
            raise ValueError(f"非法栏位：start={col_start} span={col_span}")
        x = self.col_x(col_start)
        w = self.col_w * col_span + tokens.GUTTER_EMU * (col_span - 1)
        return Rect(x, y, w, h)


@dataclass
class Block:
    """待布局的内容块（来自 PageSpec.blocks 的规范化形态）。"""
    kind: str                 # text | bullets | bignumber | quote | chart | table | process | timeline
    text: str = ""
    payload: dict = field(default_factory=dict)
    role: str = "body"        # TYPE_ROLES key
    fact_refs: list = field(default_factory=list)


@dataclass
class Placed:
    block: Block
    rect: Rect
    col_start: int
    col_span: int
    est_lines: int
    overflow: bool = False

    def as_dict(self) -> dict:
        return {"kind": self.block.kind, "role": self.block.role,
                "rect": self.rect.as_dict(), "col_start": self.col_start,
                "col_span": self.col_span, "est_lines": self.est_lines,
                "overflow": self.overflow}


@dataclass
class PageRegions:
    """一页的三段区域（引擎计算，模板消费）。"""
    title: Rect
    lead: Rect | None
    content: Rect
    footnote: Rect
    grid: Grid

    def as_dict(self) -> dict:
        return {"title": self.title.as_dict(),
                "lead": self.lead.as_dict() if self.lead else None,
                "content": self.content.as_dict(),
                "footnote": self.footnote.as_dict()}


def page_regions(has_lead: bool = False, has_footnote: bool = True,
                 title_h_emu: int | None = None,
                 title_role: str = "title") -> PageRegions:
    """按 tokens 计算三段区域。标题带高度按**该页实际用的字号角色**实测，
    不拍脑袋——封面用 cover_title(44pt) 时标题带必须按 44pt 算高。"""
    g = Grid()
    m = MARGINS_EMU
    tr = tokens.TYPE_ROLES[title_role]
    th = title_h_emu or int(tr.pt * tr.line_height * 12700)
    y = m["top"]
    title = g.rect(0, tokens.GRID_COLS, y, th)
    y += th + (GAP_TITLE_LEAD_EMU if has_lead else GAP_LEAD_BODY_EMU)
    lead = None
    if has_lead:
        lr = tokens.TYPE_ROLES["lead"]
        lh = int(lr.pt * lr.line_height * 12700)
        lead = g.rect(0, tokens.GRID_COLS, y, lh)
        y += lh + GAP_LEAD_BODY_EMU
    foot_h = int(tokens.TYPE_ROLES["footnote"].pt * 1.3 * 12700)
    content_bottom = (FOOTER_ZONE_TOP_EMU - GAP_CONTENT_FOOTER_EMU
                      if has_footnote else CANVAS_H_EMU - m["bottom"])
    content = g.rect(0, tokens.GRID_COLS, y, max(0, content_bottom - y))
    footnote = g.rect(0, tokens.GRID_COLS,
                      FOOTER_ZONE_TOP_EMU + int(0.06 * EMU_PER_IN), foot_h)
    return PageRegions(title=title, lead=lead, content=content,
                       footnote=footnote if has_footnote else None, grid=g)


def _block_height(b: Block, w: int) -> int:
    role = tokens.TYPE_ROLES.get(b.role, tokens.TYPE_ROLES["body"])
    if b.kind == "bullets":
        items = b.payload.get("items") or [b.text]
        total = 0
        for it in items:
            total += measure.block_height_emu(str(it), w, role.pt, role.line_height,
                                              role.space_after_pt)
        return total
    if b.kind == "bignumber":
        kn = tokens.TYPE_ROLES["key_number"]
        cap = measure.block_height_emu(b.payload.get("caption") or "", w,
                                       tokens.TYPE_ROLES["body"].pt)
        return int(kn.pt * kn.line_height * 12700) + cap + int(0.08 * EMU_PER_IN)
    if b.kind in ("chart", "table", "process", "timeline"):
        return int(b.payload.get("h_emu") or _default_block_h(b))
    return measure.block_height_emu(b.text, w, role.pt, role.line_height,
                                    role.space_after_pt)


#: 图元块的默认高度（in）。调用方可用 payload.h_emu 覆盖。
DEFAULT_BLOCK_H_IN = {"chart": 3.0, "process": 2.4, "timeline": 3.2, "table": 0.45}


def _default_block_h(b: Block) -> int:
    if b.kind == "table":
        rows = len(b.payload.get("rows") or [])
        header = 1 if b.payload.get("headers") else 0
        n = rows + header
        return int(max(1, n) * DEFAULT_BLOCK_H_IN["table"] * tokens.EMU_PER_IN)
    return int(DEFAULT_BLOCK_H_IN.get(b.kind, 2.4) * tokens.EMU_PER_IN)


def flow_blocks(blocks: list[Block], content: Rect, g: Grid,
                allow_two_col: bool = True) -> dict:
    """把块顺序布进 content 区域。

    **content 矩形是权威的**：放置位置从 content.x / content.w 算，不从网格
    重算——模板经常传子区（如"文字 7 栏 + 图 5 栏"里的 7 栏），按网格重算
    会把文字布满 12 栏、压到图上。网格只用于整幅内容区的列对齐参考。

    返回 {"placements", "mode", "overflow", "detail"}。
    - 单栏放得下 → mode="single"；
    - 放不下且允许两栏 → 按"第 0 栏高度上限尽量装"切分成两栏 → mode="two_col"；
    - 仍放不下 → overflow=True（块级明细），**不缩字、不删块**。
    """
    def _col_w(n: int) -> int:
        return (content.w - tokens.GUTTER_EMU * (n - 1)) // n

    def _run(cols: int) -> dict:
        n = max(1, cols)
        col_w = _col_w(n)
        placements, overflow = [], []
        if n == 1:
            y = content.y
            for b in blocks:
                h = _block_height(b, col_w)
                avail = content.y + content.h - y
                fits_h = h <= avail
                placements.append(Placed(b, Rect(content.x, y, content.w,
                                                 min(h, max(0, avail))),
                                         0, 1, est_lines=0, overflow=not fits_h))
                if not fits_h:
                    overflow.append({"kind": b.kind, "head": (b.text or b.payload.get("title") or "")[:20],
                                     "need_h": h, "avail_h": avail})
                    return {"ok": False, "placements": placements, "overflow": overflow}
                y += h + GAP_LEAD_BODY_EMU // 2
            return {"ok": True, "placements": placements, "overflow": []}
        # 两栏：先把多条目块拆成单条目单元，再按累计高度一半切。
        # 只按"块"切是错的——content 页往往是单个 bullets 块装 12 条，
        # 整块切分会把 12 条全塞进第 0 栏然后报溢出。
        def _explode(blocks: list[Block]) -> list[Block]:
            units: list[Block] = []
            for b in blocks:
                if b.kind == "bullets":
                    items = [str(x) for x in (b.payload.get("items") or [b.text])]
                    for it in items:
                        if not it.strip():
                            continue
                        units.append(Block(kind="bullets", text=it,
                                           payload={**b.payload, "items": [it]},
                                           role=b.role, fact_refs=b.fact_refs))
                else:
                    units.append(b)
            return units

        units = _explode(blocks)
        heights = [_block_height(b, col_w) for b in units]
        gap = GAP_LEAD_BODY_EMU // 2
        # 切分点必须同时满足两个约束：第 0 栏不超过栏高上限，且尽量装满。
        # 用 total/2 当目标是错的——密料时两栏各 total/2 仍可能超过栏高，
        # 结果第 0 栏溢出、第 1 栏空白。
        limit = content.h
        acc, split = 0, 0
        for i, h in enumerate(heights):
            need = h + (gap if i else 0)
            if i and acc + need > limit:
                break
            acc += need
            split = i + 1
        groups = [units[:split], units[split:]]
        max_h = content.h
        for ci, grp in enumerate(groups):
            x = content.x + ci * (col_w + tokens.GUTTER_EMU)
            y = content.y
            for b in grp:
                h = _block_height(b, col_w)
                avail = max_h - (y - content.y)
                fits_h = h <= avail
                placements.append(Placed(b, Rect(x, y, col_w,
                                                 min(h, max(0, avail))),
                                         ci, 1, est_lines=0,
                                         overflow=not fits_h))
                if not fits_h:
                    overflow.append({"kind": b.kind, "col": ci,
                                     "head": (b.text or "")[:20], "need_h": h})
                    return {"ok": False, "placements": placements, "overflow": overflow}
                y += h + gap
        return {"ok": True, "placements": placements, "overflow": []}

    single = _run(1)
    if single["ok"]:
        return {"mode": "single", **single}
    if allow_two_col:
        two = _run(2)
        if two["ok"]:
            return {"mode": "two_col", **two}
        return {"mode": "two_col", **two, "overflow": two.get("overflow")
                or single.get("overflow") or []}
    return {"mode": "single", **single}


def check_regions_invariants(regions: PageRegions) -> list[str]:
    """区域自身与硬门禁几何约束的一致性自检（布局前）。"""
    errs = []
    m = MARGINS_EMU
    for name, r in (("title", regions.title), ("content", regions.content)):
        if r.x < m["left"] or r.y < m["top"] or r.x + r.w > CANVAS_W_EMU - m["right"]:
            errs.append(f"{name} 超出安全边距")
    if regions.footnote is not None:
        fz = regions.footnote
        if fz.y < FOOTER_ZONE_TOP_EMU or fz.y + fz.h > FOOTER_ZONE_BOTTOM_EMU + int(0.02 * EMU_PER_IN):
            errs.append("脚注不在硬门禁 footer_zone 内")
    if regions.lead is not None and regions.lead.y < regions.title.y + regions.title.h:
        errs.append("lead 与标题重叠")
    if regions.content.h <= 0:
        errs.append("content 区域高度为 0")
    return errs
