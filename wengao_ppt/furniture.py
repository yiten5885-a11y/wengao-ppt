"""主题家具（page furniture）：让"6 个主题"在页面上真的是 6 种版式语言。

为什么需要这个模块
------------------
2026-11-04 实测：把同一份中性稿分别套上 6 个主题渲染，六张首页**都是**
"一行标题 + 大片空白"——`apply_theme()` 只改文字颜色/字体/底色，从不画任何
页面构件。于是"藏蓝公文"没有红头，"钢青商务"没有网格，"深海霓虹"没有边框。
用户两次提出"6 个主题 6 个风格"，指的就是这件事：**换色不是换风格**。

做法
----
每个主题在注册表里声明一组 `furniture.parts`，`apply_theme()` 在样式改完后
把它们画到每一页。图元只有三种参数组合（边 / 距边距离 / 沿边区间 / 粗细 /
颜色令牌），足以表达通栏色带、细线、竖轨、角标。

三条硬约束（每条都有实测依据，不是美学偏好）
--------------------------------------------
1. **细长**：`thickness_in < 0.15`。`scripts/layout_gate.py` 的 HG09
   EMPTY_CONTAINER 会把"不承载文字的实底形状"判缺陷，但显式豁免细长装饰条
   （"色带/引条是设计用途"），判据正是 `width < 0.15 or height < 0.15`。
   超过阈值的色块会被判缺陷——所以这里在上限处**失败关闭**，不是截断。
2. **只落留白**：每个图元先与页面上所有既有形状的包围盒做相交检查，相交就
   跳过并记账。这样既不会压住内容，也不会改变 HG09"文字对底色"的取值
   （门禁用 notext 渲染取底色，色带若跑到文字下面就会被当成底色）。
3. **压到最底层**：插入后立即移到 spTree 索引 2（页面形状树的第一个内容位），
   内容永远在上面。

`wengao-adaptive`（原稿本色）**不声明家具**——它的风格就是"不换肤"，给它加
构件与它的定义矛盾。
"""
from __future__ import annotations

from dataclasses import dataclass

#: 图元最大粗细（英寸）。>= 0.15 会被 layout_gate HG09 当作"空容器"判缺陷。
MAX_THICKNESS_IN = 0.15

#: 图元必须完整落在画布内；留 0.001in 的数值容差。
_EPS_IN = 0.001

_EDGES = ("top", "bottom", "left", "right")


class FurnitureError(ValueError):
    """家具声明不合法（在注册表加载/应用时立刻失败，不静默降级）。"""


@dataclass(frozen=True)
class Part:
    """一个图元的解算结果（全部英寸）。"""
    index: int
    edge: str
    color: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0


def spec_of(ident: dict) -> dict:
    """从冻结身份里取家具声明；没有则返回 `{"id": None, "parts": []}`。"""
    params = (ident or {}).get("params") or {}
    spec = params.get("furniture") or {}
    if not isinstance(spec, dict):
        raise FurnitureError(f"furniture 必须是对象，实际 {type(spec).__name__}")
    return {"id": spec.get("id"), "parts": list(spec.get("parts") or [])}


def validate(spec: dict, colors: dict) -> list[str]:
    """校验家具声明，返回问题清单（空表示合法）。

    单独暴露成函数，测试与注册表自检都直接用它——校验规则只有一处。
    """
    problems: list[str] = []
    parts = spec.get("parts") or []
    if not parts:
        return problems
    if not spec.get("id"):
        problems.append("有 parts 但没有 id")
    for i, p in enumerate(parts):
        edge = p.get("edge")
        if edge not in _EDGES:
            problems.append(f"part{i}: edge={edge!r} 不是 {_EDGES}")
            continue
        try:
            thick = float(p.get("thickness_in"))
            off = float(p.get("offset_in", 0.0))
        except (TypeError, ValueError):
            problems.append(f"part{i}: thickness_in/offset_in 必须是数字")
            continue
        if thick <= 0:
            problems.append(f"part{i}: thickness_in 必须 > 0")
        if thick >= MAX_THICKNESS_IN:
            # 不是"太大不好看"，是会触发 HG09 EMPTY_CONTAINER（见模块 docstring）
            problems.append(
                f"part{i}: thickness_in={thick} 必须 < {MAX_THICKNESS_IN}"
                f"（否则 layout_gate HG09 会把色带判成空容器）")
        if off < 0:
            problems.append(f"part{i}: offset_in 不能为负")
        span = p.get("span", (0.0, 1.0))
        try:
            a, b = float(span[0]), float(span[1])
        except (TypeError, ValueError, IndexError):
            problems.append(f"part{i}: span 必须是两个数字")
            continue
        if not (0.0 <= a < b <= 1.0):
            problems.append(f"part{i}: span={span!r} 必须满足 0<=a<b<=1")
        color = p.get("color")
        value = colors.get(color) if isinstance(color, str) else None
        if not isinstance(value, str) or len(value) != 6:
            problems.append(f"part{i}: color={color!r} 不是调色板里的 6 位十六进制令牌")
    return problems


def solve(spec: dict, width_in: float, height_in: float, colors: dict) -> list[Part]:
    """把声明解算成每页要画的矩形；不合法直接抛错。"""
    problems = validate(spec, colors)
    if problems:
        raise FurnitureError("; ".join(problems))
    out: list[Part] = []
    for i, p in enumerate(spec["parts"]):
        edge = p["edge"]
        thick = float(p["thickness_in"])
        off = float(p.get("offset_in", 0.0))
        span = p.get("span", (0.0, 1.0))
        a, b = float(span[0]), float(span[1])
        if edge in ("top", "bottom"):
            x0, x1 = a * width_in, b * width_in
            y0 = off if edge == "top" else height_in - off - thick
            y0, y1 = y0, y0 + thick
        else:
            y0, y1 = a * height_in, b * height_in
            x0 = off if edge == "left" else width_in - off - thick
            x0, x1 = x0, x0 + thick
        if (x0 < -_EPS_IN or y0 < -_EPS_IN
                or x1 > width_in + _EPS_IN or y1 > height_in + _EPS_IN):
            raise FurnitureError(
                f"part{i}({edge}) 超出画布：({x0:.3f},{y0:.3f})-({x1:.3f},{y1:.3f}) "
                f"画布 {width_in:.3f}x{height_in:.3f}")
        out.append(Part(index=i, edge=edge, color=colors[p["color"]],
                        x0=round(x0, 4), y0=round(y0, 4),
                        x1=round(x1, 4), y1=round(y1, 4)))
    return out


def _boxes(slide) -> list[tuple[float, float, float, float]]:
    """页面上所有既有形状的包围盒（英寸）。无几何信息的占位形状跳过。"""
    emu = 914400.0
    out = []
    for sh in slide.shapes:
        try:
            left = sh.left / emu
            top = sh.top / emu
            width = sh.width / emu
            height = sh.height / emu
        except TypeError:
            continue
        if width <= 0 or height <= 0:
            continue
        out.append((left, top, left + width, top + height))
    return out


def _overlaps(a, b, tol: float = 0.01) -> bool:
    return (a[0] < b[2] - tol and b[0] < a[2] - tol
            and a[1] < b[3] - tol and b[1] < a[3] - tol)


def apply_to(prs, ident: dict) -> dict:
    """把主题家具画到每一页（压到最底层），返回记账字典。"""
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Emu

    params = (ident or {}).get("params") or {}
    colors = params.get("colors") or {}
    spec = spec_of(ident)
    stats = {
        "id": spec["id"],
        "parts": len(spec["parts"]),
        "declared": len(spec["parts"]),
        "pages": 0, "added": 0, "pages_with_furniture": 0,
        "skipped": [], "skipped_count": 0,
    }
    if not spec["parts"]:
        return stats

    emu = 914400.0
    w_in = float(prs.slide_width or 0) / emu
    h_in = float(prs.slide_height or 0) / emu
    if w_in <= 0 or h_in <= 0:
        stats["skipped"].append({"page": None, "part": None,
                                 "reason": "slide_size_unknown"})
        stats["skipped_count"] += 1
        return stats

    rects = solve(spec, w_in, h_in, colors)   # 声明不合法在这里就抛

    for pageno, slide in enumerate(prs.slides, start=1):
        stats["pages"] += 1
        # 只避让**内容**（页面上原本就有的形状）。家具之间允许相接/搭接——
        # 角标与竖轨本来就是一套图形，拿"互相不许碰"去挡会把 L 形角标整条删掉
        # （2026-11-04 实测：business 的左上角标在 6 页上全被自己的竖轨判定为
        # "压住内容"而跳过，页面上只剩一根光秃秃的竖条）。
        content = _boxes(slide)
        placed_here = 0
        for part in rects:
            box = (part.x0, part.y0, part.x1, part.y1)
            hit = next((b for b in content if _overlaps(box, b)), None)
            if hit is not None:
                # 压住内容就不画——宁可少一根线，不可挡字
                stats["skipped"].append({
                    "page": pageno, "part": part.index, "edge": part.edge,
                    "reason": "overlaps_existing_shape",
                    "box_in": [round(v, 3) for v in box],
                    "hit_in": [round(v, 3) for v in hit],
                })
                stats["skipped_count"] += 1
                continue
            shp = slide.shapes.add_shape(
                MSO_SHAPE.RECTANGLE, Emu(int(round(part.x0 * emu))),
                Emu(int(round(part.y0 * emu))),
                Emu(int(round(part.width * emu))),
                Emu(int(round(part.height * emu))))
            shp.fill.solid()
            shp.fill.fore_color.rgb = RGBColor.from_string(part.color)
            shp.line.fill.background()
            shp.shadow.inherit = False          # DECOR_RULES: 无阴影
            # 2.0：显式填充/线条已经写全，删掉 p:style 才能真的"零效果"。
            # python-pptx 新建形状默认带 <p:style> 的 effectRef idx=2，LibreOffice
            # 会据此给每根色带套一层灰色投影；1.x 构件少看不出来，2.0 每页
            # 十几条刻度的阴影叠在一起，装帧立刻显脏（2026-11-05 渲染对照实测）。
            from pptx.oxml.ns import qn as _qn
            style_el = shp._element.find(_qn("p:style"))
            if style_el is not None:
                shp._element.remove(style_el)
            try:
                shp.name = f"wg-furniture:{spec['id']}:{part.index}"
            except Exception:  # noqa: BLE001
                pass
            # 压到最底层（索引 2 = 第一个内容位，前两位是 nvGrpSpPr/grpSpPr）
            spTree = slide.shapes._spTree
            spTree.remove(shp._element)
            spTree.insert(2, shp._element)
            placed_here += 1
            stats["added"] += 1
        if placed_here:
            stats["pages_with_furniture"] += 1
    return stats
