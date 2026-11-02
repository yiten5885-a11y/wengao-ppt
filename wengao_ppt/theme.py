"""真实 PPT 主题：注册表 + 设计令牌 + 单一应用器（D10-B）。

设计要点
--------
- **一个应用器**处理全部主题，不复制三套生成器。
- 主题身份 = `theme_id` + `theme_version` + **有效配置摘要**（`config_digest`）。
  摘要只由实际影响输出的参数（背景/颜色/字体/字号）确定性序列化得到；
  展示名、说明文字等不影响渲染的字段不参与摘要。
- 角色判定不再用“字数 ≤24 就当标题”这类长度启发式，改用**来源结构**：
  占位符角色 → 来源字号层次（相对本稿最大字号）→ 几何位置（是否在顶部标题带）。
  这样同级正文不会因长短不同而变成不同等级，长标题也不会因超过 24 字降为正文。
- 只改样式：不改文字、不重排、不清空重建；保留 runs、字段、超链接、粗体/斜体等强调，
  保留数字/单位/负号、表格主体对应、图表数据与轴语义、图片与备注。
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from .type_scale import ROLE_RANGE_PT

# 本机实测可用字体（见 reports/d01-ub/.../RESOURCE_INVENTORY.md）
#: 2026-10-21 00:25 变更：东亚字体槽（a:ea）由 Hiragino Sans GB 改为
#: **Arial Unicode MS**。依据：监督独立对照 S6（仅改 a:ea，其余字节/ZIP 顺序/
#: 文字/备注/图片/转场/粒子全部保持）真实原生逐页放映 13 页未见中文缺字方框，
#: 关闭并从磁盘重开后 P6 仍字形完整；CoreText 对照显示日文 HiraginoSans-W3/
#: HiraKakuProN-W3 对 46 字样本缺 19 个简体字，而 Arial Unicode MS 全覆盖。
#: 声明：这是**字体选择**，不是渲染根因定论；LibreOffice 内部实际选字库仍未证实。
#: 平台范围：本机 macOS（/System/Library/Fonts/Arial Unicode.ttf 实测存在），
#: 不在系统安装/复制/分发字体文件，只改本产品写出的 OOXML 字体引用与包内
#: fontconfig 映射。其他平台需各自实测后再声明适用。
FALLBACK_FONTS = ["Songti SC", "STHeiti", "Arial Unicode MS"]

# 注册表/令牌结构版本（与单个主题的 theme_version 分开记录）
REGISTRY_VERSION = "1.1.0"

# 不带 theme 的旧调用使用的默认主题（保持 D10-A 合法默认行为）
DEFAULT_THEME_ID = "wengao-basic"

THEMES: dict[str, dict] = {
    # ------------------------------------------------------------------ 旧默认
    "wengao-basic": {
        "name": "稳稿基础主题（旧默认）",
        "version": "1.0.0",
        "legacy_default": True,
        "slide_bg": "FFFFFF",
        "colors": {
            "primary": "1F3864", "accent": "C00000", "text": "1A1A1A", "muted": "5A5A5A",
            "heading": "1F3864", "subtitle": "5A5A5A",
            "surface": "FFFFFF", "border": "C9CDD4",
            "table_header_bg": "1F3864", "table_header_text": "FFFFFF",
            "table_row_bg": "FFFFFF", "table_row_alt_bg": "F2F4F7",
            "chart_bg": "FFFFFF", "chart_text": "1A1A1A", "chart_grid": "D9DCE1",
            "chart_series": ["1F3864", "C00000", "2E75B6", "7F7F7F"],
        },
        "fonts": {"heading_ea": "Songti", "body_ea": "Songti",
                  "heading_latin": "Arial", "body_latin": "Arial"},
        "sizes": {"title": 30, "subtitle": 18, "heading": 22, "body": 15, "note": 11,
                  "chart_text": 12},
        "font_fallback_note": "上游默认字体 Microsoft YaHei 本机缺失，已显式替换为 Songti；"
                             "该替换是计划动作，不得静默进行。",
    },
    # ------------------------------------------------------------------ 商务清晰
    "wengao-business": {
        "name": "商务清晰",
        # 1.0.0 的 primary/accent（1F3864/C00000）跟 wengao-basic 逐字相同，只有
        # muted/border/chart_grid 等次要字段有细微差别——两个主题在卡片预览和
        # 实际生成的 PPT 里几乎看不出区别（user 2026-11-01："6个主题都要分开"）。
        # 1.1.0 换成独立的钢蓝+琥珀配色；每个颜色都按 WCAG 相对亮度公式核验过
        # 对白底的对比度（最低 4.87:1，高于本项目 HG09 的 4.5:1 门槛，留出渲染
        # 抗锯齿软化的余量）。
        # 1.1.1：真实渲染暴露出 4.87:1 的余量不够——job-mt-56ebac59/
        # job-51d167cddbc1（2026-11-02）实测 muted/subtitle（5B6B7A，名义
        # 5.48:1）在 11pt「来源与限定」说明文字上，窄体全角标点（（）等）实测
        # 对比度低至 3.17:1，远低于 4.5 门槛——抗锯齿在小字号细笔画上的损耗比
        # 名义公式预估的大得多。muted/subtitle 换成 3A4855（名义 9.38:1，约
        # 1.7 倍原余量），不是猜测：按实测 5.48→3.17（约 58%）的衰减比例外推，
        # 9.38:1 名义值衰减后仍有约 5.4:1，留出安全冗余；真正的确认仍是重新
        # 生成后 layout_gate 复核零 HG09 命中。
        "version": "1.1.1",
        "slide_bg": "FFFFFF",
        "colors": {
            "primary": "2F5B7C", "accent": "9C5A0A", "text": "1A1A1A", "muted": "3A4855",
            "heading": "2F5B7C", "subtitle": "3A4855",
            "surface": "FFFFFF", "border": "C7D2DC",
            "table_header_bg": "2F5B7C", "table_header_text": "FFFFFF",
            "table_row_bg": "FFFFFF", "table_row_alt_bg": "EAF0F5",
            "chart_bg": "FFFFFF", "chart_text": "1A1A1A", "chart_grid": "D7E1EA",
            "chart_series": ["2F5B7C", "9C5A0A", "3F7A5A", "8C8C8C"],
        },
        "fonts": {"heading_ea": "Songti", "body_ea": "Songti",
                  "heading_latin": "Arial", "body_latin": "Arial"},
        "sizes": {"title": 30, "subtitle": 18, "heading": 22, "body": 16, "note": 11,
                  "chart_text": 12},
        "font_fallback_note": "Microsoft YaHei 本机缺失，按已核验可用字体显式替换为 Songti（标题/正文）；"
                             "替换是计划动作并写入主题配置与证据。",
    },
    # ------------------------------------------------------------------ 深色科技
    "wengao-dark": {
        "name": "深色科技",
        "version": "1.0.0",
        "slide_bg": "0A192F",
        "colors": {
            "primary": "00F2FF", "accent": "00F2FF", "text": "E6F1FF", "muted": "9FB3C8",
            "heading": "00F2FF", "subtitle": "9FB3C8",
            # 深色下不用纯白绘图区（避免突兀白块），用略亮于背景的深蓝
            "surface": "112240", "border": "1F3A5F",
            "table_header_bg": "00F2FF", "table_header_text": "0A192F",
            "table_row_bg": "112240", "table_row_alt_bg": "172C4A",
            "chart_bg": "112240", "chart_text": "E6F1FF", "chart_grid": "24405F",
            "chart_series": ["00F2FF", "FFB86C", "7BD88F", "C792EA"],
        },
        "fonts": {"heading_ea": "STHeiti", "body_ea": "STHeiti",
                  "heading_latin": "Arial", "body_latin": "Arial"},
        "sizes": {"title": 30, "subtitle": 20, "heading": 22, "body": 18, "note": 11,
                  "chart_text": 12},
        "sizes_note": ("2026-10-20 可读性基线（用户要求“字不要太小”）：主标题 32–40、"
                       "页标题 28–32、正文 22–24（下限 20）、表格 18–20、"
                       "图表与辅助注 ≥16。主题值是**上限闸**（_capped 只缩不放），"
                       "生成端显式字号 ≤ 上限时按生成端值渲染。"),
        "font_fallback_note": "深色主题采用 STHeiti（本机已核验可显示中文）替代缺失的 Microsoft YaHei；"
                             "文字与绘图区均按深底可读性配置。",
    },
    # ------------------------------------------------------------------ 极简阅读
    "wengao-minimal": {
        "name": "极简阅读",
        "version": "1.0.0",
        "slide_bg": "F5F5F5",
        "colors": {
            "primary": "333333", "accent": "888888", "text": "333333", "muted": "333333",
            "heading": "333333", "subtitle": "333333",
            "surface": "FFFFFF", "border": "DDDDDD",
            "table_header_bg": "EFEFEF", "table_header_text": "333333",
            "table_row_bg": "FFFFFF", "table_row_alt_bg": "FAFAFA",
            "chart_bg": "FFFFFF", "chart_text": "333333", "chart_grid": "E4E4E4",
            "chart_series": ["5A5A5A", "9AB0C4", "C9A227", "7F9A6E"],
        },
        "fonts": {"heading_ea": "Songti", "body_ea": "Songti",
                  "heading_latin": "Arial", "body_latin": "Arial"},
        "sizes": {"title": 30, "subtitle": 17, "heading": 22, "body": 15, "note": 11,
                  "chart_text": 12},
        "font_fallback_note": "极简主题使用 Songti 与低饱和配色；Microsoft YaHei 缺失的替换为计划动作。",
    },
    # ------------------------------------------------------- 原稿适配精修（自适应）
    # 与上面四个"换肤"主题不同：这个主题不把整套稿染成固定调色板，而是
    # **识别并保留原稿自身的设计语言**（底色/文字色/强调色/字体），只按
    # 结构规则做布局、位置、尺寸、字号、间距与对齐的精修
    # （实现见 wengao_ppt/refine.py，规则版本与参数进入同一身份摘要）。
    "wengao-adaptive": {
        "name": "原稿适配精修",
        "version": "1.0.0",
        "adaptive": True,
        "refine_rules_version": "1.0.0",
        "slide_bg": None,          # 自适应：沿用原稿每页自己的背景
        "colors": None,            # 自适应：沿用原稿文字/强调色
        "fonts": None,             # 自适应：沿用原稿字体
        "sizes": None,             # 自适应：只按度量下限提升过小文字
        "font_fallback_note": "原稿适配主题不替换字体：原稿声明的字体按原样保留，"
                             "本机缺失时由渲染环境自行替换（与 preserve 路径一致）。",
    },
    # ------------------------------------------------------------------ 视觉升级
    # 本轮新增：深色令牌 + 视觉升级阶段（关键词配图 cover 裁切、统一转场、
    # 粒子动画）。visual_upgrade 进入身份摘要；选图计划在 run 阶段按页面
    # 语义生成并冻结（visual_plan.json），媒体校验按计划放行。
    "wengao-visual-dark": {
        "name": "深色视觉升级（关键词配图+动效）",
        "version": "1.0.0",
        "slide_bg": "101014",
        "visual_upgrade": True,
        "colors": {
            "primary": "E8C877", "accent": "E06C5A", "text": "EDEDEA", "muted": "9A9A9E",
            "heading": "FFFFFF", "subtitle": "C9C9CE",
            "surface": "1A1A20", "border": "2A2A32",
            "table_header_bg": "1F1F26", "table_header_text": "EDEDEA",
            "table_row_bg": "16161B", "table_row_alt_bg": "1C1C22",
            "chart_bg": "16161B", "chart_text": "EDEDEA", "chart_grid": "2E2E36",
            "chart_series": ["E8C877", "E06C5A", "6CA0E0", "8C8C92"],
        },
        "fonts": {"heading_ea": "Arial Unicode MS", "body_ea": "Arial Unicode MS",
                  "heading_latin": "Avenir Next", "body_latin": "Avenir Next"},
        # 2026-10-21 00:25 新可读基线（用户"字体还是太小/不协调"）：
        # 页标题 36–40、导语与主正文 26–28（常规正文下限 24）、小标题/关键结论
        # 28–32、表格 20–22、图表与辅助说明 18–20、页脚/来源 16–18。
        # 主题值仍是**上限闸**；真正把原稿小字抬起来的是 READABILITY_SIZE_FLOOR
        # （见下）配合精修重排，不是靠全局放大画布。
        "sizes": {"title": 30, "subtitle": 22, "heading": 22, "body": 18, "note": 11,
                  "chart_text": 12},
        "font_fallback_note": ("东亚字体槽改 Arial Unicode MS（S6 原生对照 13 页无缺字，"
                               "依据与平台范围见 FALLBACK_FONTS 注释）；拉丁槽仍 Avenir Next。"),
    },
}

# 参与配置摘要的字段（只含影响输出的参数；不含展示名与说明）
_DIGEST_FIELDS = ("slide_bg", "colors", "fonts", "sizes", "visual_upgrade")

# 字号规则：主题字号只作为**上限**，不放大来源字号。
# 依据（2026-10-14 D10-B 实测）：高密度原稿正文 11pt，若按主题 body 16pt 放大，
# 固定行距的文本框会互相重叠/截断；而缩小不增加溢出风险。写成配置项以便进入身份摘要。
#: 2026-10-21 00:25：旧规则 "min(theme_size, source_size)"（只缩不放）会让原稿
#: 9.5–16pt 的小字原样留在导出件里，正是用户"字太小"的直接原因。新规则是**双向钳制**：
#: clamp(source, READABILITY_SIZE_FLOOR[role], theme_size[role])——过小抬到下限，
#: 过大压到上限；抬字必须同时由精修/重排承担几何后果（不越界、不遮挡、不缩字）。
SIZE_RULE = "clamp(source, readability_floor, theme_size)"

#: 材料生成路径的可读性下限配置：材料生成端产出的正文天生偏大（2026-10-20
#: 监督实测约 22pt），所以这个下限要明显高于默认 READABILITY_SIZE_FLOOR，
#: 不能把生成端有意放大的字号缩回去。但 2026-10-27 原稿路径把 ROLE_RANGE_PT
#: 整体收紧到 10–30pt 分级体系后，这里仍留着收紧前的旧数值（title 36/
#: body 24/table 20 等）未跟着改——于是每一档下限都反过来**超过**了
#: ROLE_RANGE_PT 同角色的上限（table 20 > 14、body 24 > 18……），导致
#: _capped() 里 `cap = max(theme_size, floor)` 恒等于这个过大的下限，
#: 材料生成任务只要出现该角色就必然撞上 HG07 ROLE_SIZE（2026-10-31
#: job-ed0b23aa8f45 第 5 页表格 20pt vs 允许 11–14pt，10 条阻断发现）。
#: 改为从 ROLE_RANGE_PT 的**上限**派生（与 READABILITY_SIZE_FLOOR 用同一套
#: 角色映射，但取区间上沿而非下沿）：材料生成内容仍落在本角色允许区间的
#: 最宽松一端，同时和 HG07 用的同一份单一事实来源天然保持同步，不会再因为
#: 两边各自硬编码数值而重新drift。
MATERIAL_SIZE_FLOOR = {
    "title": int(ROLE_RANGE_PT["title"][1]),
    "subtitle": int(ROLE_RANGE_PT["lead"][1]),
    "heading": int(ROLE_RANGE_PT["lead"][1]),
    "body": int(ROLE_RANGE_PT["body"][1]),
    "note": int(ROLE_RANGE_PT["footer"][1]),
    "chart_text": int(ROLE_RANGE_PT["chart_aux"][1]),
    "table": int(ROLE_RANGE_PT["table"][1]),
}

#: 2026-10-27 10–30pt 字号分级体系下的角色下限，派生自 type_scale.ROLE_RANGE_PT。
#: _classify() 只有 title/subtitle/heading/body/note 五档粗分类，映射关系为：
#:   title -> title (24)
#:   subtitle -> lead (16)
#:   heading -> lead (16)
#:   body -> body (13)
#:   note -> footer (10)
#:   table -> table (11)
#:   chart_text -> chart_aux (10)
READABILITY_SIZE_FLOOR = {
    "title": int(ROLE_RANGE_PT["title"][0]),
    "subtitle": int(ROLE_RANGE_PT["lead"][0]),
    "heading": int(ROLE_RANGE_PT["lead"][0]),
    "body": int(ROLE_RANGE_PT["body"][0]),
    "note": int(ROLE_RANGE_PT["footer"][0]),
    "table": int(ROLE_RANGE_PT["table"][0]),
    "chart_text": int(ROLE_RANGE_PT["chart_aux"][0]),
}

#: 原稿导入/主题路径是否默认启用可读下限（True = 落实用户新要求；
#: 想退回旧"只缩不放"行为显式传 size_floor={} 并置该开关 False）。
READABILITY_FLOOR_DEFAULT_ENABLED = True

def fit_type_scale_in_place(prs) -> tuple[list, list]:
    """Clamp every page's text into the gate's role ranges (refine.fit_page_type_scale).

    Used at the end of apply_theme, and again after any later step that writes
    its own font sizes (the visual upgrade sets eyebrow/chapter text to its own
    16 pt "body" size, which the gate classifies as a label limited to 11-14 pt).
    Returns (per-page actions, warnings).
    """
    from . import refine as refine_mod
    sw_in = prs.slide_width / 914400.0
    sh_in = prs.slide_height / 914400.0
    pages, warnings = [], []
    for idx, slide in enumerate(prs.slides, start=1):
        shs = [refine_mod.Sh(slide, sp, j, sw_in, sh_in) for j, sp in enumerate(slide.shapes)]
        acts = []
        try:
            refine_mod.fit_page_type_scale(shs, sw_in, sh_in, acts)
        except Exception as e:  # noqa: BLE001
            warnings.append(f"slide{idx} fit_page_type_scale: {type(e).__name__}")
        if acts:
            pages.append({"slide": idx, "actions": acts})
    return pages, warnings


def clamp_oversized_to_role_max(prs) -> list[dict]:
    """Shrink runs above their gate role's maximum to that maximum. Nothing else.

    For use after a step that writes its own font sizes onto an already laid
    out page (the visual upgrade puts eyebrow/chapter text at 16 pt; the gate
    calls those labels, 11-14 pt). Re-running the full fit there also moved and
    resized boxes and broke the visual layout (2026-10-29: layout findings went
    28 -> 70). Shrinking needs no extra room, so geometry is left untouched and
    text below its range is not raised here.
    """
    from pptx.util import Pt

    from . import refine as refine_mod
    from .type_scale import ROLE_RANGE_PT
    sw_in = prs.slide_width / 914400.0
    sh_in = prs.slide_height / 914400.0
    changes = []
    for idx, slide in enumerate(prs.slides, start=1):
        shs = [refine_mod.Sh(slide, sp, j, sw_in, sh_in) for j, sp in enumerate(slide.shapes)]
        for shape_idx, role in refine_mod.infer_text_roles(shs, sw_in, sh_in).items():
            rng = ROLE_RANGE_PT.get(role)
            s = shs[shape_idx]
            if rng is None or not s.is_text or s.has_table or s.has_chart:
                continue
            hi = rng[1]
            for pi, ri, _t, pt, _c, _b in s.runs:
                if pt is not None and pt > hi + 1e-9:
                    s.shape.text_frame.paragraphs[pi].runs[ri].font.size = Pt(hi)
                    changes.append({"slide": idx, "shape": s.name, "role": role,
                                    "from": pt, "to": hi})
        changes.extend(_shrink_for_hierarchy_and_row_fit(slide, idx, sw_in, sh_in))
    return changes


#: Rendered CJK/Latin runs came out ~15% wider than visual.text_width_in
#: estimates (2026-10-29, "01 战略转变" at 14 pt: 1.05 in estimated, 1.20 in drawn).
_ROW_FIT_SAFETY = 1.2
_ROW_FIT_GAP_IN = 0.06


def _shrink_for_hierarchy_and_row_fit(slide, idx, sw_in, sh_in) -> list[dict]:
    """Two shrink-only fixes on one page, never moving or resizing a box.

    1. Labels must be smaller than body text (gate HG07 hierarchy): when the
       page's label median is not below the body median, labels go to
       body median - 2 pt, but never under the label minimum.
    2. A single-line (no-wrap) text that would run into the next text box on
       the same row is shrunk, within its role range, until it fits the gap
       (gate HG03 collisions in the header row, 2026-10-29).
    """
    import statistics

    from pptx.util import Pt

    from . import refine as refine_mod
    from .type_scale import ROLE_RANGE_PT
    from .visual import text_frame_no_wrap, text_width_in

    shs = [refine_mod.Sh(slide, sp, j, sw_in, sh_in) for j, sp in enumerate(slide.shapes)]
    roles = refine_mod.infer_text_roles(shs, sw_in, sh_in)
    changes = []

    def _shrink(s, role, target):
        target = round(target * 2) / 2.0
        for pi, ri, _t, pt, _c, _b in s.runs:
            if pt is not None and pt > target + 1e-9:
                s.shape.text_frame.paragraphs[pi].runs[ri].font.size = Pt(target)
                changes.append({"slide": idx, "shape": s.name, "role": role,
                                "from": pt, "to": target})
        s.runs = [(pi, ri, t, (min(pt, target) if pt else pt), c, b)
                  for pi, ri, t, pt, c, b in s.runs]

    def _sizes(role):
        return [pt for i, r in roles.items() if r == role
                for *_x, pt, _c, _b in shs[i].runs if pt]

    body, labels = _sizes("body"), _sizes("label")
    if body and labels and statistics.median(labels) >= statistics.median(body):
        target = max(ROLE_RANGE_PT["label"][0], statistics.median(body) - 2)
        for i, r in roles.items():
            if r == "label":
                _shrink(shs[i], r, target)

    for i, role in roles.items():
        s = shs[i]
        rng = ROLE_RANGE_PT.get(role)
        if rng is None or not s.is_text or not text_frame_no_wrap(s.shape):
            continue
        right = [o for o in shs if o is not s and o.is_text and o.L > s.L + 0.01
                 and min(s.T + s.H, o.T + o.H) > max(s.T, o.T)]
        if not right:
            continue
        avail = min(o.L for o in right) - s.L - _ROW_FIT_GAP_IN
        line = max((ln for ln in s.text.split("\n")), key=len, default="")
        cur = max((pt for *_x, pt, _c, _b in s.runs if pt), default=None)
        if not line or cur is None or avail <= 0:
            continue
        if text_width_in(line, cur) * _ROW_FIT_SAFETY <= avail:
            continue
        fit = avail * 72.0 / (max(text_width_in(line, 1.0), 1e-6) * 72.0 * _ROW_FIT_SAFETY)
        _shrink(s, role, max(rng[0], min(cur, fit)))
    return changes


#: Contrast row statuses for which the theme foreground color is applied
#: (see backdrop.plan_backdrop classify): OK, or IMPROVED_BELOW — the theme
#: color is still below the bar but strictly better than the original.
_THEME_COLOR_STATUSES = frozenset({"OK", "IMPROVED_BELOW", "AUTO_ADJUSTED"})

#: Margin above the required ratio when auto-adjusting, so rendering and
#: anti-aliasing do not land the pixel-level check (HG09) just under the bar.
_AUTO_CONTRAST_MARGIN = 0.25


def _auto_contrast_adjust(row: dict, theme_hex: str) -> None:
    """Fix text that would stay under its contrast bar, in place on the plan row.

    Before 2026-10-29 such text kept its color and became a REVIEW item ("you
    decide whether to change the design"), i.e. work handed to the user. The
    agent now does it: starting from the theme color, blend toward white or
    black (whichever contrasts more with the measured backdrop) in 5% steps and
    take the first shade that clears the bar. The row becomes AUTO_ADJUSTED with
    the new ratio and both colors recorded. Rows without a measured backdrop
    (gradients/images) are left alone; they are not claimed as passing.
    """
    from . import backdrop as bd   # 模块级没有 bd（见 apply_theme）
    if row.get("status") not in ("PREEXISTING_LOW", "IMPROVED_BELOW"):
        return
    bg_hex, need = row.get("backdrop_rgb"), row.get("required")
    if not bg_hex or not need:
        return
    bg = bd.hex_to_rgb(bg_hex)
    start = bd.hex_to_rgb(theme_hex)
    targets = sorted(((255, 255, 255), (0, 0, 0)),
                     key=lambda c: bd.contrast_ratio(c, bg), reverse=True)
    for target in targets:
        if bd.contrast_ratio(target, bg) < need + _AUTO_CONTRAST_MARGIN:
            continue
        for step in range(1, 21):
            cand = bd.mix(target, start, step / 20.0)
            ratio = bd.contrast_ratio(cand, bg)
            if ratio >= need + _AUTO_CONTRAST_MARGIN:
                row["auto_from"] = {"status": row["status"], "theme_rgb": theme_hex,
                                    "theme_ratio": row.get("ratio"),
                                    "orig_ratio": row.get("orig_ratio")}
                row["auto_color"] = "%02X%02X%02X" % cand
                row["fg_rgb"] = "#" + row["auto_color"]
                row["ratio"] = round(ratio, 2)
                row["status"] = "AUTO_ADJUSTED"
                return


class ThemeError(Exception):
    """主题注册/配置错误（由 runner 转成 GateError，使用正式错误码）。"""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _params(theme_id: str) -> dict:
    t = THEMES.get(theme_id)
    if t is None:
        raise ThemeError(
            "THEME.UNKNOWN",
            f"未注册的主题 {theme_id!r}；仅接受注册表内的主题 ID（{'、'.join(sorted(THEMES))}），"
            "不接受任意路径/URL/自定义配置。")
    return t


def effective_config(theme_id: str) -> dict:
    """实际影响生成结果的有效配置（确定性、可用于摘要）。"""
    t = _params(theme_id)
    if t.get("adaptive"):
        # 自适应主题：身份 = 精修规则版本 + 规则参数（换参数 = 换身份）。
        from . import refine as refine_mod
        return {"theme_id": theme_id, "theme_version": t["version"],
                "adaptive": True,
                "refine_rules_version": t.get("refine_rules_version"),
                "refine_rules_digest": refine_mod.rules_digest(),
                "refine_scope": "layout_position_size_spacing_alignment_only"}
    cfg = {"theme_id": theme_id, "theme_version": t["version"]}
    for f in _DIGEST_FIELDS:
        if f in t:  # 旧主题无该字段时不写入，保持其历史摘要逐位不变
            cfg[f] = copy.deepcopy(t[f])
    cfg["size_rule"] = SIZE_RULE
    return cfg


def config_digest(theme_id: str) -> str:
    blob = json.dumps(effective_config(theme_id), ensure_ascii=False,
                      sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def resolve_theme(theme_id: str | None) -> dict:
    """把 theme_id 解析成一个**冻结**的身份+参数包。

    - 未知主题 → ThemeError("THEME.UNKNOWN")，在入口即拒绝（不会拖到运行中 KeyError）。
    - 返回对象是深拷贝；运行中的线程读不到被其他请求改写的共享可变对象。
    - 自适应主题：params 只含精修规则身份（无固定调色板），apply 时走 refine 路径。
    """
    tid = theme_id or DEFAULT_THEME_ID
    t = _params(tid)
    if t.get("adaptive"):
        from . import refine as refine_mod
        return {
            "theme_id": tid,
            "name": t["name"],
            "theme_version": t["version"],
            "registry_version": REGISTRY_VERSION,
            "config_digest": config_digest(tid),
            "legacy_default": bool(t.get("legacy_default")),
            "adaptive": True,
            "params": {
                "refine_rules_version": t.get("refine_rules_version"),
                "refine_rules_digest": refine_mod.rules_digest(),
                "font_fallback_note": t["font_fallback_note"],
            },
        }
    return {
        "theme_id": tid,
        "name": t["name"],
        "theme_version": t["version"],
        "registry_version": REGISTRY_VERSION,
        "config_digest": config_digest(tid),
        "legacy_default": bool(t.get("legacy_default")),
        "params": {
            "slide_bg": t["slide_bg"],
            "colors": copy.deepcopy(t["colors"]),
            "fonts": copy.deepcopy(t["fonts"]),
            "sizes": copy.deepcopy(t["sizes"]),
            "font_fallback_note": t["font_fallback_note"],
            "visual_upgrade": bool(t.get("visual_upgrade")),
        },
    }


def list_themes() -> list[dict]:
    """给界面/CLI 的主题清单（含版本与配置摘要，用于身份展示）。"""
    out = []
    for tid, t in THEMES.items():
        if t.get("adaptive"):
            out.append({
                "id": tid, "name": t["name"], "version": t["version"],
                "registry_version": REGISTRY_VERSION,
                "config_digest": config_digest(tid),
                "legacy_default": bool(t.get("legacy_default")),
                "adaptive": True,
                "slide_bg": None,
                "colors": None,
                "fonts": None,
                "font_fallback_note": t["font_fallback_note"],
                "refine_rules_version": t.get("refine_rules_version"),
                "note": "识别并保留原稿设计语言，只做结构/度量精修；不换肤。",
                "visual_upgrade": bool(t.get("visual_upgrade")),
            })
            continue
        out.append({
            "id": tid, "name": t["name"], "version": t["version"],
            "registry_version": REGISTRY_VERSION,
            "config_digest": config_digest(tid),
            "legacy_default": bool(t.get("legacy_default")),
            "slide_bg": t["slide_bg"],
            "colors": {k: t["colors"][k] for k in ("primary", "accent", "text", "muted")},
            "fonts": {"heading_ea": t["fonts"]["heading_ea"], "body_ea": t["fonts"]["body_ea"],
                      "latin": t["fonts"]["heading_latin"]},
            "font_fallback_note": t["font_fallback_note"],
            # Surfaced to the workbench UI so it can present this theme as a
            # distinct kind of choice, not a peer color swatch: enabling it
            # runs the visual_upgrade generation branch (keyword imagery +
            # transition/particle animation), not just a different palette
            # (user, 2026-11-01: wengao-visual-dark must not share the same
            # picker entry as the 5 plain-color themes).
            "visual_upgrade": bool(t.get("visual_upgrade")),
        })
    out.sort(key=lambda x: (not x["legacy_default"], x["id"]))
    return out


# --------------------------------------------------------------------------- 应用器

# 角色 → (字号键, 颜色键, 字体键)
_ROLE_STYLE = {
    "title": ("title", "primary", "heading"),
    "subtitle": ("subtitle", "subtitle", "body"),
    "heading": ("heading", "heading", "heading"),
    "body": ("body", "text", "body"),
    "note": ("note", "muted", "body"),
}


def apply_theme(pptx_path, out_path, theme_name: str | None = None,
                projection: bool = False, frozen: dict | None = None,
                preserve: bool = False, size_floor: dict | None = None):
    """把主题应用到真实 PPTX，返回变更统计（含主题身份与对比度账本）。

    `preserve=True` 是"保留原设计"路径：**不改任何字节**，只复制文件，
    用于"原设计检查与预览"。它不得被当成"主题已应用"——统计里
    `theme_application="PRESERVE_ORIGINAL"`，调用方据此如实展示。

    `size_floor`：可读性下限字典。
    - **2026-10-21 00:25 起默认启用** `READABILITY_SIZE_FLOOR`（见上）：
      用户明确要求"字体还是太小"，且要求不能只修材料生成配置——原稿真实
      导入/主题/精修/导出路径同样要落实。`_capped(raise_to_floor=True)` 会把
      过小来源字号抬到下限，再由主题上限压下（clamp）。
    - 材料任务仍可显式传 `MATERIAL_SIZE_FLOOR` 覆盖；要退回旧"只缩不放"
      行为，显式传 `size_floor={}` 并把 `READABILITY_FLOOR_DEFAULT_ENABLED`
      置 False（仅作回归对照，不得用于交付）。

    自适应主题（wengao-adaptive）走 `wengao_ppt.refine`：识别并保留原稿
    设计语言，只做结构/度量精修；它同样**不是** preserve（ preserve 一字节不改），
    统计里 `theme_application="REFINED_ORIGINAL"`。

    其余主题路径按**文字实际底色**成对处理（见 `wengao_ppt.backdrop`）：
    - 只重染不透明纯色、无文字、够大、中性的背景面板；
    - 图片/渐变/半透明/高饱和装饰一律不动；
    - 每个文字形状按它真正压着的颜色算对比度，达不到就保留原色并记账；
    - 证明不了（UNKNOWN）的形状不算通过，交给上层复核。
    """
    from pptx import Presentation
    from pptx.util import Pt
    from pptx.dml.color import RGBColor

    from . import backdrop as bd

    ident = frozen or resolve_theme(theme_name or DEFAULT_THEME_ID)

    # 2026-10-21 00:25：可读下限默认生效（原稿导入路径也要落实"字不能太小"）。
    if size_floor is None and READABILITY_FLOOR_DEFAULT_ENABLED:
        size_floor = dict(READABILITY_SIZE_FLOOR)

    # 自适应主题：原稿适配精修路径（保留原稿设计语言，只做结构/度量精修）。
    if ident.get("adaptive"):
        from . import refine as refine_mod
        stats = refine_mod.refine_deck(pptx_path, out_path, ident=ident)
        stats["theme"] = ident["theme_id"]
        stats["theme_version"] = ident["theme_version"]
        stats["theme_registry_version"] = ident["registry_version"]
        stats["theme_config_digest"] = ident["config_digest"]
        stats["preserve_design"] = False
        return stats

    p = ident["params"]
    colors, fonts, sizes = p["colors"], p["fonts"], p["sizes"]

    def rgb(hexstr):
        return RGBColor.from_string(hexstr)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    if preserve:
        import shutil
        shutil.copyfile(pptx_path, out)
        prs = Presentation(str(pptx_path))
        limits = _preserve_limits(prs, ident)
        stats = {
            "theme": ident["theme_id"], "theme_id": ident["theme_id"],
            "theme_version": ident["theme_version"],
            "theme_registry_version": ident["registry_version"],
            "theme_config_digest": ident["config_digest"],
            "theme_application": "PRESERVE_ORIGINAL",
            "preserve_design": True,
            "note": "保留原设计：未改背景、形状、文字颜色与字号，输出字节与输入一致；"
                    "这是检查/预览用途，不代表主题已应用。",
            "input_sha256": __import__("wengao_ppt.core", fromlist=["sha256_file"]).sha256_file(pptx_path),
            "output_sha256": __import__("wengao_ppt.core", fromlist=["sha256_file"]).sha256_file(out),
            "runs_reformatted": 0, "fills_set": 0, "backgrounds_set": 0,
            "slides": len(prs.slides), "role_counts": {k: 0 for k in _ROLE_STYLE},
            # 保留原设计不做任何改动，因此**不声称对比度达标**：
            # 只把原稿本来就低于门槛的对象如实列出，供"这是原始限制"的复核。
            "contrast_claim": "NOT_ASSERTED",
            "preserve_limits": limits,
            "protected_business_colors": [],
            "below_threshold": [],
            "contrast": {"checked": 0, "violations": [], "kept_original": [],
                         "unknown_backdrop": [], "preexisting_low": [],
                         "unsafe": [], "min_ratio": None,
                         "preserve_limits": limits},
        }
        return stats

    prs = Presentation(str(pptx_path))
    slide_w = int(prs.slide_width or 0)
    slide_h = int(prs.slide_height or 0)
    theme_bg_rgb = bd.hex_to_rgb(p["slide_bg"])
    role_rgb = {k: bd.hex_to_rgb(colors[v[1]]) for k, v in _ROLE_STYLE.items()}
    role_rgb["note"] = bd.hex_to_rgb(colors["muted"])

    changed = {
        "theme": ident["theme_id"], "theme_id": ident["theme_id"],
        "theme_version": ident["theme_version"],
        "theme_registry_version": ident["registry_version"],
        "theme_config_digest": ident["config_digest"],
        "theme_application": "APPLIED",
        "preserve_design": False,
        "runs_reformatted": 0, "fills_set": 0, "backgrounds_set": 0,
        "tables_styled": 0, "table_cells_styled": 0, "charts_styled": 0,
        "notes_styled": 0, "slides": len(prs.slides),
        "role_counts": {k: 0 for k in _ROLE_STYLE},
        "emphasis_preserved": True,
        "warnings": [],
        "contrast": {"checked": 0, "violations": [], "kept_original": [],
                     "unknown_backdrop": [], "preexisting_low": [],
                     "unsafe": [], "min_ratio": None},
        "background_plan": [],
        # 业务色与"未达门槛"账本：这两项必须在报告里逐条可查，
        # 不能只给一个 theme.contrast=EXECUTED 就算通过。
        "protected_business_colors": [],
        "below_threshold": [],
        "contrast_claim": "ASSERTED",
    }
    deck_max = _deck_max_size(prs)

    from . import refine as refine_mod
    sw_in = slide_w / 914400.0
    sh_in = slide_h / 914400.0

    for idx, slide in enumerate(prs.slides, start=1):
        info = bd.scan_slide(slide, slide_w, slide_h, idx)
        shs_theme = [refine_mod.Sh(slide, sp, j, sw_in, sh_in) for j, sp in enumerate(slide.shapes)]
        page_roles = refine_mod.infer_text_roles(shs_theme, sw_in, sh_in)

        def role_of(item, _idx=idx):
            shape = getattr(item, "shape", item)
            for j, sp in enumerate(slide.shapes):
                if sp == shape:
                    r = page_roles.get(j)
                    if r == "title":
                        return "title"
                    elif r in ("subtitle", "lead", "heading"):
                        return "heading"
                    elif r in ("label", "ordinal", "quote", "footer", "source", "note"):
                        return "note"
                    return "body"
            return _classify(shape, _idx, deck_max, slide_w, slide_h)

        def orig_color_of(item):
            shape = getattr(item, "shape", item)
            try:
                for para in shape.text_frame.paragraphs:
                    for run in para.runs:
                        c = run.font.color
                        if c is not None and c.type is not None:
                            return bd.hex_to_rgb(str(c.rgb))
            except Exception:  # noqa: BLE001
                return None
            return None

        # 业务色：值型文字上的高饱和原色（红/绿/金/蓝对应负/正/横盘/资金）。
        # 它们不进主题调色板——改色就是改含义。
        protected = {sh.z: bd.protected_runs(sh)
                     for sh in info["shapes"]
                     if sh.has_text and bd.protected_runs(sh)}
        plan = bd.plan_slide(info, (theme_bg_rgb, "theme"), role_rgb, role_of,
                             orig_color_of=orig_color_of, protected=protected)
        recolor = plan["recolor"] or {}
        # 1) 成对重染背景面板（只动计划里列出的中性大面板）
        for sh in info["shapes"]:
            if sh.z in recolor:
                try:
                    sh.shape.fill.solid()
                    sh.shape.fill.fore_color.rgb = rgb(p["slide_bg"])
                    changed["fills_set"] += 1
                except Exception as e:  # noqa: BLE001
                    changed["warnings"].append(f"slide{idx} recolor {sh.name}: {type(e).__name__}")
        # 2) 页面背景
        try:
            bg = slide.background
            bg.fill.solid()
            bg.fill.fore_color.rgb = rgb(p["slide_bg"])
            changed["backgrounds_set"] += 1
        except Exception as e:  # noqa: BLE001
            changed["warnings"].append(f"slide{idx} background: {type(e).__name__}")
        changed["background_plan"].append({
            "slide": idx, "mode": plan["mode"],
            "panels_considered": [{k: v for k, v in x.items()} for x in plan["decisions"][0]["panels_considered"]],
            "recolored": [info["shapes"][z].name for z in sorted(recolor)],
            # 因业务色而保持原底色的面板：局部恢复原设计，不是漏改
            "locked_for_business_color": [info["shapes"][z].name
                                          for z in plan.get("locked_panels") or []],
        })

        # 3) 文字：按行决定"用主题色"还是"保留原色"
        rows = {r.get("z", r["shape"]): r for r in plan["rows"]}
        for sh in info["shapes"]:
            obj = sh.shape
            if getattr(obj, "has_table", False) and obj.has_table:
                _style_table(obj.table, colors, fonts, sizes, changed,
                             floor=size_floor)
                continue
            if getattr(obj, "has_chart", False) and obj.has_chart:
                _style_chart(obj.chart, colors, fonts, sizes, changed,
                             floor=size_floor)
                continue
            if not sh.has_text:
                continue
            role = role_of(obj)
            changed["role_counts"][role] += 1
            size_key, color_key, font_key = _ROLE_STYLE[role]
            row = rows.get(sh.z) or rows.get(sh.name) or {}
            prot = protected.get(sh.z) or protected.get(sh.name) or []
            keep = {(r["para"], r["run"]) for r in prot}
            # Text that would stay below its contrast bar gets a corrected shade
            # of the theme color instead of a "please review" item for the user.
            _auto_contrast_adjust(row, colors[color_key])
            # IMPROVED_BELOW: theme color beats the original but is still below
            # the bar — apply it (the better of the two); the below-threshold
            # ledger below records it as not worsened.
            if row.get("status") in _THEME_COLOR_STATUSES:
                _style_text_frame(obj.text_frame, size=Pt(sizes[size_key]),
                                  color=rgb(row.get("auto_color") or colors[color_key]),
                                  latin=fonts[f"{font_key}_latin"],
                                  ea=fonts[f"{font_key}_ea"], changed=changed,
                                  keep_runs=keep,
                                  floor=(size_floor or {}).get(size_key))
            else:
                # 达不到对比度 / 底色无法证明：保留原样式，不制造"深字压深底"。
                orig = _capture_colors(obj.text_frame)
                status = row.get("status")
                kept = {"slide": idx, "shape": sh.name, "role": role, "status": status,
                        "reason": {
                            "UNKNOWN": "backdrop_unknown（渐变/图片/半透明底色，无法证明对比度）",
                            "KEEP_ORIGINAL_OK": f"主题色 {row.get('ratio')} 不足，"
                                                f"原色 {row.get('orig_ratio')} 达标 → 保留原色",
                            "PREEXISTING_LOW": f"原稿本来就只有 {row.get('orig_ratio')}，"
                                               f"主题色更低（{row.get('ratio')}）→ 改色无益，保留原样待复核",
                            "UNSAFE": f"改色后 {row.get('ratio')} 低于原稿 {row.get('orig_ratio')}："
                                      "本次转换会变差，按不安全处理",
                        }.get(status, str(status)),
                        "backdrop": row.get("backdrop"),
                        "theme_ratio": row.get("ratio"),
                        "orig_ratio": row.get("orig_ratio"),
                        "original_colors": orig}
                if status == "UNKNOWN":
                    changed["contrast"]["unknown_backdrop"].append(kept)
                elif status == "PREEXISTING_LOW":
                    changed["contrast"]["preexisting_low"].append(kept)
                elif status == "UNSAFE":
                    changed["contrast"]["unsafe"].append(kept)
                else:
                    changed["contrast"]["kept_original"].append(kept)
        # 3.5) 抬字号后可能把原来一行的文字顶成好几行，自己的框装不下
        # （甚至直接出画布），或者压住下一个文字框——这里按真实折行高度把
        # 框适当加高（不超出画布底边），并把被压的后续形状顺势下移；
        # 不改字号、不改文字。
        from . import visual as vis_mod
        overlap_fixes = vis_mod.fix_stacked_text_overlap(
            slide, acts=None, page=idx, slide_h_in=slide_h / 914400,
            slide_w_in=slide_w / 914400)
        if overlap_fixes:
            changed.setdefault("stacked_text_overlap_fixes", []).extend(overlap_fixes)

        # 4) 业务色账本：原色、最终底色、保留后的实测对比度、是否达标
        for sh in info["shapes"]:
            prot = protected.get(sh.name) or []
            if not prot:
                continue
            row = rows.get(sh.name) or {}
            bg_hex = row.get("backdrop_rgb")
            bg = bd.hex_to_rgb(bg_hex) if bg_hex else None
            need = row.get("required")
            for r in prot:
                ratio = bd.contrast_ratio(r["rgb"], bg) if (bg and need) else None
                rec = {"slide": idx, "shape": sh.name, "text": r["text"],
                       "original_rgb": r["hex"],
                       "applied_rgb": r["hex"], "kept": True,
                       "backdrop": row.get("backdrop"), "backdrop_rgb": bg_hex,
                       "required": need, "ratio": ratio,
                       "font_pt": row.get("font_pt"), "bold": row.get("bold"),
                       "meets_required": (ratio is not None and need is not None
                                          and ratio >= need),
                       "role": row.get("role"),
                       "reason": "值型文字的业务色（状态/正负/资金含义）；"
                                 "不套用主题前景色，避免静默丢掉映射"}
                changed["protected_business_colors"].append(rec)
                if ratio is not None and need is not None and ratio < need:
                    changed["below_threshold"].append({
                        "slide": idx, "shape": sh.name, "kind": "protected_business_color",
                        "text": r["text"], "required": need, "ratio": ratio,
                        "font_pt": row.get("font_pt"), "bold": row.get("bold"),
                        "source": "original", "backdrop": row.get("backdrop"),
                        "backdrop_rgb": bg_hex,
                        # 保留业务色后仍不达标：不是本次改色造成，但结论仍是"未达门槛"
                        "preexisting": True, "worsened": False,
                    })

        # 5) 对比度账本：把每行结果落盘（含通过项），便于复核"谁判定通过"
        for r in plan["rows"]:
            r2 = dict(r)
            r2["slide"] = idx
            changed["contrast"]["checked"] += 1
            ratio = r.get("ratio")
            if ratio is not None:
                changed["contrast"]["min_ratio"] = (
                    ratio if changed["contrast"]["min_ratio"] is None
                    else min(changed["contrast"]["min_ratio"], ratio))
            if r.get("status") not in ("OK",):
                changed["contrast"]["violations"].append(r2)
            # 实际呈现的对比度：套主题色看 ratio，保留原色看 orig_ratio。
            # 低于门槛就要落进 below_threshold——PREEXISTING_LOW 不是"通过"。
            applied_theme = r.get("status") in _THEME_COLOR_STATUSES
            eff = r.get("ratio") if applied_theme else r.get("orig_ratio")
            src = "theme" if applied_theme else "original"
            if eff is not None and r.get("required") is not None and eff < r["required"]:
                changed["below_threshold"].append({
                    "slide": idx, "shape": r.get("shape"), "kind": "text",
                    "required": r["required"], "ratio": eff, "source": src,
                    "font_pt": r.get("font_pt"), "bold": r.get("bold"),
                    "backdrop": r.get("backdrop"), "backdrop_rgb": r.get("backdrop_rgb"),
                    "role": r.get("role"),
                    "preexisting": eff == r.get("orig_ratio"),
                    "worsened": (r.get("orig_ratio") is not None
                                 and eff < r["orig_ratio"]),
                })

        # 备注（口播稿）——只调字号/字体/颜色，不动文字
        try:
            if slide.has_notes_slide:
                ntf = slide.notes_slide.notes_text_frame
                if ntf is not None and ntf.text.strip():
                    _style_text_frame(ntf, size=Pt(sizes["note"]), color=rgb(colors["muted"]),
                                      latin=fonts["body_latin"], ea=fonts["body_ea"],
                                      changed=changed,
                                      floor=(size_floor or {}).get("note"))
                    changed["notes_styled"] += 1
        except Exception as e:  # noqa: BLE001
            changed["warnings"].append(f"slide{idx} notes: {type(e).__name__}")

    # 换肤主题同样靠 _capped(raise_to_floor=True) 把过小来源字号抬到可读下限，
    # 但那一步只改字号、不动框——24pt 正文塞进为更小字号设计的框里就是
    # OWNER_BOX_OVERFLOW/CANVAS_MARGIN 的直接成因；而 _classify() 只有
    # title/subtitle/heading/body/note 五档粗分类，和门禁 HG07 实际检查的
    # title/lead/body/conclusion/key_number/table/chart_aux/ordinal/footer/
    # source/label 十一档角色区间对不上，字号钳的是错的区间。
    # wengao_ppt.refine 已经实现了同口径的角色判定 + 字号钳 + 几何吸收
    # （抬字后撑高框、级联下推同列块，放不下就如实放弃不强塞），原本只接给
    # wengao-adaptive 主题用；这里对所有换肤主题的最终结果复用同一遍，
    # 让字号与门禁用的是同一套角色区间，几何后果也一并吸收。
    readability_pages, fit_warnings = fit_type_scale_in_place(prs)
    changed["warnings"].extend(fit_warnings)
    changed["readability_floor_actions"] = readability_pages

    prs.save(str(out))
    changed["font_substitution"] = p["font_fallback_note"]
    # 主题整体结论：有无法证明的底色或保留原色 → 不是"完整套用"
    if changed["contrast"]["unsafe"]:
        changed["theme_application"] = "UNSAFE_REJECTED"
    elif changed["contrast"]["unknown_backdrop"]:
        changed["theme_application"] = "APPLIED_WITH_UNVERIFIED"
    elif changed["below_threshold"]:
        # 有对象最终仍低于它适用的门槛：不能叫"已套用"，也不能靠主题色低就记为通过
        changed["theme_application"] = "APPLIED_WITH_UNMET_CONTRAST"
    elif changed["contrast"]["kept_original"]:
        changed["theme_application"] = "APPLIED_WITH_PROTECTED"
    return changed


def _first_run_color(shape):
    from . import backdrop as bd   # 模块级没有 bd：apply_theme 里是局部导入
    try:
        for para in shape.text_frame.paragraphs:
            for run in para.runs:
                c = run.font.color
                if c is not None and c.type is not None:
                    return bd.hex_to_rgb(str(c.rgb))
    except Exception:  # noqa: BLE001
        return None
    return None


def _preserve_limits(prs, ident) -> list:
    """原设计里**本来就**低于适用门槛的对象（只读扫描，不改文件）。

    "保留原设计"不承诺对比度达标：这里只把原始限制如实列出来。它是披露，
    不是通过；也不与主题转换的结果混在一起计数。
    """
    from . import backdrop as bd
    slide_w = int(prs.slide_width or 0)
    slide_h = int(prs.slide_height or 0)
    out = []
    for idx, slide in enumerate(prs.slides, start=1):
        info = bd.scan_slide(slide, slide_w, slide_h, idx)
        # 用**原稿自己的**页面背景，不能拿目标主题的浅色底去算原稿对比度：
        # 那会把深底浅字算成 1.1:1 这种假结果。
        page_bg, bg_kind = bd.bg_of_slide(slide, fallback=None)
        for t in [x for x in info["shapes"] if x.has_text]:
            b = bd.backdrop_for(info, t, {}, page_bg)
            if not b.get("analyzable") or b.get("rgb") is None:
                continue
            fg = _first_run_color(t.shape)
            if fg is None:
                continue
            need = bd.threshold_for(t)
            ratio = bd.contrast_ratio(fg, b["rgb"])
            if ratio < need:
                sizes = [s for s in t.font_sizes if s]
                out.append({"slide": idx, "shape": t.name,
                            "text": (t.shape.text_frame.text or "").strip()[:24],
                            "font_pt": min(sizes) if sizes else None,
                            "required": need, "ratio": ratio,
                            "fg_rgb": "#%02X%02X%02X" % fg,
                            "backdrop_rgb": "#%02X%02X%02X" % b["rgb"]})
    return out


def _capture_colors(tf):
    out = []
    for para in tf.paragraphs:
        for run in para.runs:
            try:
                c = run.font.color
                out.append(str(c.rgb) if c and c.type is not None else None)
            except Exception:  # noqa: BLE001
                out.append(None)
    return out


# --------------------------------------------------------------------------- 角色判定

def _deck_max_size(prs) -> float:
    """本稿最大字号（用于按**来源层次**判定角色，而不是按字数）。"""
    mx = 0.0
    for slide in prs.slides:
        for shp in slide.shapes:
            if not (getattr(shp, "has_text_frame", False) and shp.has_text_frame):
                continue
            for para in shp.text_frame.paragraphs:
                for run in para.runs:
                    if run.font.size is not None:
                        mx = max(mx, float(run.font.size.pt))
    return mx or 18.0


def _max_run_size(tf) -> float | None:
    sizes = [float(r.font.size.pt) for para in tf.paragraphs for r in para.runs
             if r.font.size is not None]
    return max(sizes) if sizes else None


def _classify(shape, slide_idx: int, deck_max: float, slide_w: int, slide_h: int) -> str:
    """按来源结构判定角色：来源注释 → 占位符 → 来源字号层次 → 顶部标题带几何。"""
    from pptx.enum.shapes import PP_PLACEHOLDER

    # Source attribution/limitations are notes, even when authored as ordinary text
    # boxes. Treating them as body raised 16pt source text to 24pt and clipped it.
    try:
        source_text = shape.text_frame.text.lstrip()
    except Exception:  # noqa: BLE001
        source_text = ""
    if source_text.startswith(("来源与限定（原句）", "来源：", "来源 ",
                               "数据来源：", "资料来源：")):
        return "note"

    ph = None
    if getattr(shape, "is_placeholder", False):
        try:
            ph = shape.placeholder_format.type
        except Exception:  # noqa: BLE001
            ph = None
    if ph is not None:
        if ph in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE):
            return "title"
        if ph == PP_PLACEHOLDER.SUBTITLE:
            return "subtitle"
        return "body"

    src = _max_run_size(shape.text_frame)
    top = int(shape.top or 0)
    width = int(shape.width or 0)
    in_top_band = bool(slide_h) and top < slide_h * 0.30
    wide = bool(slide_w) and width > slide_w * 0.55

    if src is None:
        return "heading" if (in_top_band and wide) else "body"
    if slide_idx == 1 and src >= deck_max * 0.9:
        return "title"
    if src >= deck_max * 0.75:
        return "heading"
    if slide_idx == 1 and src >= deck_max * 0.5:
        return "subtitle"
    if in_top_band and wide and src >= deck_max * 0.55:
        return "heading"
    return "body"


# --------------------------------------------------------------------------- 样式写入

def _set_ea(run, typeface: str) -> bool:
    """安全写入 a:ea（python-pptx 没有 ea 属性；按 schema 顺序插入）。"""
    from pptx.oxml.ns import qn
    rPr = run._r.get_or_add_rPr()
    ea = rPr.find(qn("a:ea"))
    if ea is None:
        ea = rPr.makeelement(qn("a:ea"), {})
        try:
            rPr.insert_element_before(ea, "a:cs", "a:sym", "a:hlinkClick",
                                      "a:hlinkMouseOver", "a:rtl", "a:extLst")
        except Exception:  # noqa: BLE001
            rPr.append(ea)
    ea.set("typeface", typeface)
    return True


def _style_text_frame(tf, *, size, color, latin, ea, changed, keep_runs=None,
                      floor: float | None = None):
    """逐 run 设样式。字号规则：

    - 不传 floor（原稿导入/保留路径）：`min(主题字号, 来源字号)`，只缩不放；
    - 传 floor（材料生成任务）：`min(max(主题字号, floor), 来源字号)`——
      主题上限低于可读性下限时按下限放行，生成端 22pt 不再被缩成 15pt。

    `keep_runs` 里的 (段号, run 号) **不改颜色**：这些是业务色（红=负/绿=正/金=横盘），
    改色等于改含义。字号与字体仍按主题处理。
    装饰性大字（≥60pt 且 ≤3 字符）归为 decor，保留原字号与原样式。
    """
    from .type_scale import is_decor
    keep = keep_runs or set()
    for pi, para in enumerate(tf.paragraphs):
        for ri, run in enumerate(para.runs):
            f = run.font
            if f.size is not None and is_decor(float(f.size.pt), run.text):
                continue
            f.name = latin
            f.size = _capped(size, run, floor)
            if (pi, ri) not in keep:
                f.color.rgb = color
            _set_ea(run, ea)
            # 不改 bold/italic/underline：保留原稿的有意义强调
            changed["runs_reformatted"] += 1


def _capped(theme_size, run, floor: float | None = None, raise_to_floor: bool = True):
    """字号归一：先按可读下限**抬起**过小来源字号，再按主题上限**压下**过大值。"""
    from pptx.util import Pt
    from .type_scale import is_decor
    src = run.font.size
    if src is not None and is_decor(float(src.pt), run.text):
        return src
    cap = float(theme_size.pt)
    if floor is not None:
        cap = max(cap, float(floor))
    if src is None:
        return Pt(cap)
    pt = float(src.pt)
    if raise_to_floor and floor is not None:
        pt = max(pt, float(floor))
    return Pt(pt) if pt < cap else Pt(cap)


def _style_table(tbl, colors, fonts, sizes, changed, floor: dict | None = None):
    from pptx.util import Pt

    # 表格字号下限：材料任务用 floor["table"]（缺省退回 body 键）；
    # 原稿路径不传 floor，行为不变（min(主题, 来源)）。
    tbl_floor = None
    if floor:
        tbl_floor = floor.get("table", floor.get("body"))
    # _capped() 的 cap 是 max(theme_size, floor)：仅仅把 floor 收紧到
    # ROLE_RANGE_PT["table"] 的上限还不够——只要主题自身的 body 字号
    # （wengao-basic 是 15pt）超过这个上限（11-14pt），cap 恒等于主题
    # body 字号，表格字号就永远卡在 15pt，过不了 HG07（2026-10-31
    # job-f04e1e2368a6 第 5 页：15pt vs 允许 11-14pt）。表格有自己独立
    # 的角色区间，不该被通用 body 字号顶住；这里直接把喂给 _capped 的
    # “主题字号”本身也收紧到表格角色的上限。
    table_hi = ROLE_RANGE_PT["table"][1]
    table_theme_size = Pt(min(float(sizes["body"]), table_hi))
    if tbl_floor is not None:
        tbl_floor = min(float(tbl_floor), table_hi)

    for ri, row in enumerate(tbl.rows):
        header = (ri == 0)
        bg = colors["table_header_bg"] if header else (
            colors["table_row_alt_bg"] if (ri % 2 == 0) else colors["table_row_bg"])
        # Body rows: pure black, not the theme's near-black colors["text"]
        # (1A1A1A for wengao-basic). HG09 already special-cases thin single-
        # stroke glyphs ("一" etc) with a relaxed 4.0:1 floor because raster
        # anti-aliasing softens their core-pixel sample toward the
        # background — but at 1A1A1A on a white cell that still measured
        # 3.69:1 (2026-10-31 job-8227a4c941e4 page 5). Maximizing the ink/
        # background delta gives the anti-aliased edge sample the most
        # margin this renderer can offer without touching that gate floor.
        fg = colors["table_header_text"] if header else "000000"
        for cell in row.cells:
            try:
                cell.fill.solid()
                cell.fill.fore_color.rgb = _RGB(bg)
                changed["fills_set"] += 1
            except Exception as e:  # noqa: BLE001
                changed["warnings"].append(f"table fill: {type(e).__name__}")
            _set_cell_borders(cell, colors["border"], changed)
            for para in cell.text_frame.paragraphs:
                for run in para.runs:
                    run.font.size = _capped(table_theme_size, run, tbl_floor)
                    run.font.color.rgb = _RGB(fg)
                    run.font.name = fonts["body_latin"]
                    _set_ea(run, fonts["body_ea"])
                    changed["runs_reformatted"] += 1
            changed["table_cells_styled"] += 1
    changed["tables_styled"] += 1


def _RGB(hexstr):
    from pptx.dml.color import RGBColor
    return RGBColor.from_string(hexstr)


def _set_cell_borders(cell, hex_color, changed, width_emu: int = 9525):
    """给单元格四边加细边框（a:tcPr 里按 lnL/lnR/lnT/lnB 顺序插入）。"""
    from pptx.oxml.ns import qn
    from lxml import etree

    try:
        tcPr = cell._tc.get_or_add_tcPr()
    except Exception as e:  # noqa: BLE001
        changed["warnings"].append(f"tcPr: {type(e).__name__}")
        return
    order = ["a:lnL", "a:lnR", "a:lnT", "a:lnB", "a:lnTlToBr", "a:lnBlToTr", "a:cell3D",
             "a:noFill", "a:solidFill", "a:gradFill", "a:blipFill", "a:pattFill", "a:grpFill",
             "a:headers", "a:extLst"]
    for tag in ("a:lnL", "a:lnR", "a:lnT", "a:lnB"):
        ln = tcPr.find(qn(tag))
        if ln is None:
            ln = etree.SubElement(tcPr, qn(tag))
            # 重新排到 schema 位置
            idx = order.index(tag)
            after = set(qn(t) for t in order[idx + 1:])
            for child in list(tcPr):
                if child is ln:
                    continue
                if child.tag in after:
                    child.addprevious(ln)
                    break
        ln.set("w", str(width_emu))
        ln.set("cap", "flat")
        ln.set("cmpd", "sng")
        ln.set("algn", "ctr")
        for c in list(ln):
            ln.remove(c)
        fill = etree.SubElement(ln, qn("a:solidFill"))
        clr = etree.SubElement(fill, qn("a:srgbClr"))
        clr.set("val", hex_color.upper())
        etree.SubElement(ln, qn("a:prstDash")).set("val", "solid")


def _style_chart(chart, colors, fonts, sizes, changed, floor: dict | None = None):
    """图表：文字/图例/绘图区/序列配色；**不改数据、类别、系列对应与轴范围**。

    `floor`（材料任务）：chart_text 下限，默认主题 chart_text=13/14 时
    不低于 16pt（可读性基线）。
    """
    from pptx.util import Pt
    from pptx.oxml.ns import qn
    from lxml import etree

    chart_text_pt = float(sizes["chart_text"])
    if floor and floor.get("chart_text"):
        chart_text_pt = max(chart_text_pt, float(floor["chart_text"]))

    ok = []
    try:
        f = chart.font
        f.name = fonts["body_latin"]
        f.size = Pt(chart_text_pt)
        f.color.rgb = _RGB(colors["chart_text"])
        ok.append("font")
    except Exception as e:  # noqa: BLE001
        changed["warnings"].append(f"chart font: {type(e).__name__}")

    # Chart-level defaults do not override explicit axis tick-label formatting.
    # Apply the verified floor to both visible axes so a native chart cannot retain
    # the old 12–16pt labels while data labels alone happen to be 18pt.
    for axis_name in ("category_axis", "value_axis"):
        try:
            axis = getattr(chart, axis_name)
            tick_font = axis.tick_labels.font
            tick_font.name = fonts["body_latin"]
            tick_font.size = Pt(chart_text_pt)
            tick_font.color.rgb = _RGB(colors["chart_text"])
            ok.append(axis_name + "_tick_labels")
        except Exception as e:  # noqa: BLE001
            changed["warnings"].append(f"chart {axis_name} labels: {type(e).__name__}")

    if chart.has_legend:
        try:
            legend_font = chart.legend.font
            legend_font.name = fonts["body_latin"]
            legend_font.size = Pt(chart_text_pt)
            legend_font.color.rgb = _RGB(colors["chart_text"])
            ok.append("legend_font")
        except Exception as e:  # noqa: BLE001
            changed["warnings"].append(f"chart legend font: {type(e).__name__}")

    cs = chart._chartSpace
    # 图表区（c:chartSpace/c:spPr）：深色主题避免白块
    try:
        spPr = cs.find(qn("c:spPr"))
        if spPr is None:
            spPr = etree.Element(qn("c:spPr"))
            cs.insert_element_before(spPr, "c:txPr", "c:externalData", "c:printSettings",
                                     "c:userShapes", "c:extLst")
        _fill_spPr(spPr, colors["chart_bg"], colors["border"])
        ok.append("chart_bg")
    except Exception as e:  # noqa: BLE001
        changed["warnings"].append(f"chart bg: {type(e).__name__}")

    # 绘图区（c:plotArea/c:spPr，schema 中位于末尾）
    try:
        pa = cs.chart.plotArea
        pspPr = pa.find(qn("c:spPr"))
        if pspPr is None:
            pspPr = etree.SubElement(pa, qn("c:spPr"))
        _fill_spPr(pspPr, colors["surface"], colors["border"])
        ok.append("plot_bg")
    except Exception as e:  # noqa: BLE001
        changed["warnings"].append(f"plot bg: {type(e).__name__}")

    # 序列配色
    series_colors = colors["chart_series"]
    is_line = "LINE" in str(chart.chart_type).upper()
    for i, ser in enumerate(chart.series):
        col = _RGB(series_colors[i % len(series_colors)])
        try:
            if is_line:
                ser.format.line.color.rgb = col
                ser.format.line.width = Pt(2.25)
            else:
                ser.format.fill.solid()
                ser.format.fill.fore_color.rgb = col
            ok.append(f"series{i}")
        except Exception as e:  # noqa: BLE001
            changed["warnings"].append(f"series{i}: {type(e).__name__}")

    # 网格线
    try:
        chart.value_axis.major_gridlines.format.line.color.rgb = _RGB(colors["chart_grid"])
        ok.append("grid")
    except Exception as e:  # noqa: BLE001
        changed["warnings"].append(f"grid: {type(e).__name__}")

    changed["charts_styled"] += 1
    changed.setdefault("chart_ops", []).append(ok)


def _fill_spPr(spPr, bg_hex: str, border_hex: str):
    from pptx.oxml.ns import qn
    from lxml import etree

    for c in list(spPr):
        spPr.remove(c)
    fill = etree.SubElement(spPr, qn("a:solidFill"))
    etree.SubElement(fill, qn("a:srgbClr")).set("val", bg_hex.upper())
    ln = etree.SubElement(spPr, qn("a:ln"))
    ln.set("w", "9525")
    lf = etree.SubElement(ln, qn("a:solidFill"))
    etree.SubElement(lf, qn("a:srgbClr")).set("val", border_hex.upper())
