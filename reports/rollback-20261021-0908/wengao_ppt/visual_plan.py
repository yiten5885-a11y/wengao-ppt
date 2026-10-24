#!/usr/bin/env python3
"""关键词 → 配图 计划器（视觉升级阶段的产品化入口）。

设计对应工作包第五节：
- **页面语义驱动**：逐页从标题+正文提取关键词，与素材库语义标签匹配选图，
  不用页码/文件名/哈希强行绑定；
- 数据页/表格页/引用密集页不配图，理由如实记录；
- 放置模板按页面结构启发式选择（右半幅 / 右窄列 / 底部横带 / 整页背景），
  cover 裁切焦点随模板给定，横纵缩放恒=1；
- 计划落盘 material 化：images/particles/每页关键词与理由 → visual_plan.json，
  供媒体绑定校验按**计划**放行（计划外新增仍然阻断）。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from pptx import Presentation
from pptx.util import Emu

from .visual_fixes import RESERVED_RECTS as RESERVED

ASSETS = Path(__file__).resolve().parents[1] / "assets" / "visual_img"

#: 素材库：语义标签 + 来源记录（全部为图像生成的**示意素材**，非现场照片）
LIBRARY = {
    "promo_ecom": {
        "file": str(ASSETS / "promo_ecom.png"),
        "tags": ["618", "大促", "促销", "降价", "半价", "秒杀", "电商", "购物节", "优惠"],
        "source": "ImageGen 2026-10-19T17:38 示意素材：电商大促手机屏特写与降价标签（冷蓝光）",
    },
    "factory_night": {
        "file": str(ASSETS / "factory_night.png"),
        "tags": ["工厂", "产线", "车间", "外卖", "骑手", "配送", "劳动者", "夜班", "生产", "制造业"],
        "source": "ImageGen 2026-10-19T17:38 示意素材：夜晚工厂产线与路边外卖电动车（冷色调）",
    },
    "aisle_supermarket": {
        "file": str(ASSETS / "aisle_supermarket.png"),
        "tags": ["货架", "商超", "超市", "商品", "过剩", "库存", "零售", "消费", "供给"],
        "source": "ImageGen 2026-10-19T17:37 示意素材：空荡的大型超市货架通道（冷白灯光）",
    },
    "desk_planning": {
        "file": str(ASSETS / "desk_planning.png"),
        "tags": ["家庭", "规划", "笔记", "账本", "现金流", "体检", "桌面", "个人", "预算"],
        "source": "ImageGen 2026-10-19T17:47 示意素材：暗色桌面笔记本写分镜提纲与手机",
    },
    "skyline_city": {
        "file": str(ASSETS / "skyline_city.png"),
        "tags": ["城市", "天际线", "宏观", "结论", "收束", "未来", "景气", "港口"],
        "source": "ImageGen 2026-10-19T17:38 示意素材：清晨冷蓝沿海城市天际线与港口吊机",
    },
}

#: 标签域（全局唯一一份）：按素材库/标签的**声明顺序**去重固定。
#: 不能用 set 直接遍历——CPython 字符串哈希随 PYTHONHASHSEED 加盐，
#: set 迭代顺序跨进程不同，会让同分标签的首位、进而配图随进程漂移。
#: 声明顺序同时充当同分时的策展优先级（靠前者优先）。
_ALL_TAGS = tuple(dict.fromkeys(
    tg for lib in LIBRARY.values() for tg in lib["tags"]))

#: 放置模板（16:9，13.333×7.5in）：rect + cover 焦点 + 遮罩强度
_TEMPLATES = {
    "right_half":   {"rect": (6.67, 0.0, 6.66, 7.5), "focus": (0.50, 0.50), "overlay": 26},
    "right_column": {"rect": (10.02, 1.72, 2.55, 4.55), "focus": (0.30, 0.55), "overlay": 22},
    "bottom_band":  {"rect": (0.72, 5.66, 11.95, 1.22), "focus": (0.50, 0.45), "overlay": 42},
    "full_bleed":   {"rect": (0.0, 0.0, 13.33, 7.5), "focus": (0.50, 0.60), "overlay": 62},
}

#: 底带右侧变体：页级修复（fix_p8 把引用条移到左下 (0.9,5.55,6.3,1.3)）占用
#: 左半底带时，配图让位右半（与 full6 已验证的 P8 图位一致），互不叠压。
_BAND_RIGHT = {"rect": (7.45, 5.52, 4.62, 1.86), "focus": (0.50, 0.55), "overlay": 28}

_STOP = set("的了吗呢吧啊这那我们你们他们什么如何为什么以及但是因此所以如果虽然而且并且"
            "一个一些就是不是没有可以可能因为不过还是已经关于通过对于其中之一等等"
            "普通人工资收入消费经济增长产能补贴出口工业 macro")

_TITLE_BAND = 1.6   # in：标题带高度
_IN = 914400.0


def _page_text(slide):
    """正文文本（排除页脚/页码/来源等微元素：top>6.8in 或高<0.3in）。"""
    parts = []
    for sh in slide.shapes:
        if sh.has_text_frame and sh.text_frame.text.strip():
            top_in = (sh.top or 0) / _IN
            h_in = (sh.height or 0) / _IN
            if top_in > 6.8 or h_in < 0.3:
                continue
            parts.append((top_in <= _TITLE_BAND, sh.text_frame.text.strip()))
    return parts


def _keywords(parts, k=3):
    """标题 2 倍权取词：命中素材标签域的最长 CJK 串。

    同分优先级（显式、跨进程稳定）：分数降序 → 词长降序（更具体优先）
    → 标签在素材库中的声明顺序（策展优先级，靠前者优先）。
    不再依赖 set 的迭代顺序。
    """
    score = {}
    for is_title, t in parts:
        w = 2 if is_title else 1
        for tag in _ALL_TAGS:
            if tag in t:
                score[tag] = score.get(tag, 0) + w * len(tag)
    order = {tag: i for i, tag in enumerate(_ALL_TAGS)}
    ranked = sorted(score.items(),
                    key=lambda kv: (-kv[1], -len(kv[0]), order[kv[0]]))
    return [w for w, _ in ranked[:k]]


def _structure(slide, prs_w, prs_h):
    """结构启发式（排除页脚微元素）：文本块右半/底部占用 → 放置模板。"""
    texts = [s for s in slide.shapes
             if s.has_text_frame and s.text_frame.text.strip()
             and (s.top or 0) / _IN <= 6.8 and (s.height or 0) / _IN >= 0.3]
    n = len(texts)
    right_used = sum(1 for s in texts if (s.left or 0) / _IN > prs_w / 2)
    lowest = max((((s.top or 0) + (s.height or 0)) / _IN) for s in texts) if texts else 0
    has_table = any(getattr(s, "has_table", False) for s in slide.shapes)
    has_chart = any(getattr(s, "has_chart", False) for s in slide.shapes)
    if n <= 3 and right_used == 0:
        return "right_half", has_table, has_chart
    if lowest < prs_h - 1.4:
        return "bottom_band", has_table, has_chart
    if right_used <= 1 and n >= 4 and not has_table:
        return "right_column", has_table, has_chart
    return "full_bleed", has_table, has_chart


def _rect_text_overlap(rect, texts):
    """模板矩形与正文文本框的相交面积占矩形面积比例（放置安全检查）。"""
    rl, rt, rw, rh = rect
    ra = rw * rh
    if ra <= 0:
        return 1.0
    hit = 0.0
    for s in texts:
        l, t = (s.left or 0) / _IN, (s.top or 0) / _IN
        w, h = (s.width or 0) / _IN, (s.height or 0) / _IN
        ow = max(0.0, min(rl + rw, l + w) - max(rl, l))
        oh = max(0.0, min(rt + rh, t + h) - max(rt, t))
        hit += ow * oh
    return hit / ra


def _rects_intersect(a, b) -> bool:
    al, at, aw, ah = a
    bl, bt, bw, bh = b
    return (min(al + aw, bl + bw) - max(al, bl)) > 0.05 and \
           (min(at + ah, bt + bh) - max(at, bt)) > 0.05


def build_plan(pptx_path: str) -> dict:
    """逐页关键词 → 选图/模板/粒子/转场计划（含每页理由，可审计）。

    泛词治理：在 >35% 页面出现的标签视为文档级泛词，不参与该篇选图打分
    （仍记录在关键词列表里），避免"消费/宏观"这类词页页命中。
    """
    prs = Presentation(pptx_path)
    pw, ph = Emu(prs.slide_width).inches, Emu(prs.slide_height).inches
    slides = list(prs.slides)
    all_tags = sorted({tg for lib in LIBRARY.values() for tg in lib["tags"]})
    page_texts = []
    for slide in slides:
        parts = _page_text(slide)
        page_texts.append((parts, " ".join(t for _, t in parts)))
    doc_freq = {tg: sum(1 for _, txt in page_texts if tg in txt) for tg in all_tags}
    generic = {tg for tg, c in doc_freq.items()
               if c > max(1, int(0.35 * len(slides)))}

    pages, images = [], {}
    for i, (parts, text_all) in enumerate(page_texts, 1):
        slide = slides[i - 1]
        kws_all = _keywords(parts)
        kws = [k for k in kws_all if k not in generic] or kws_all[:1]
        reason = ""
        chosen = None
        tpl_name, has_table, has_chart = _structure(slide, pw, ph)
        if has_table or has_chart:
            reason = "表格/图表已承担主要信息，不配图"
        elif not kws:
            reason = "有效关键词为空（其余为文档级泛词），不强行配图"
        else:
            best, best_score = None, 0.0
            # 同分只认严格大于：保留 LIBRARY 声明顺序中靠前的素材，
            # 与 _keywords 的策展优先级一致（字典字面量顺序是确定的）。
            for key, lib in LIBRARY.items():
                s = float(sum(len(t) for t in kws if t in lib["tags"]))
                if s > best_score:
                    best, best_score = key, s
            if best:
                tpl = _TEMPLATES[tpl_name]
                rect_variant = ""
                # 页级修复保留区：配图不得进入修复后文字将占用的矩形
                reserved = RESERVED.get(i, [])
                if any(_rects_intersect(tuple(tpl["rect"]), r) for r in reserved) \
                        and tpl_name == "bottom_band" \
                        and not any(_rects_intersect(_BAND_RIGHT["rect"], r)
                                    for r in reserved):
                    tpl = _BAND_RIGHT
                    rect_variant = "band_right"
                body_texts = [s for s in slide.shapes
                              if s.has_text_frame and s.text_frame.text.strip()]
                ov = _rect_text_overlap(tpl["rect"], body_texts)
                if tpl_name != "full_bleed" and ov > 0.30:
                    reason = (f"关键词 {kws} 匹配 {best}，但 {tpl_name} 模板与正文"
                              f"相交 {ov:.0%}，为不遮内容放弃配图")
                    best = None
                else:
                    chosen = best
                    reason = (f"关键词 {kws} 匹配素材 {best}（模板 {tpl_name}"
                              f"{'/' + rect_variant if rect_variant else ''}，"
                              f"与正文相交 {ov:.0%}）")
            else:
                reason = f"关键词 {kws} 未匹配到有效素材"
        pages.append({"page": i, "keywords": kws_all, "generic_excluded": sorted(generic),
                      "template": tpl_name, "image": chosen, "reason": reason})
        if chosen:
            images[str(i)] = {"file": LIBRARY[chosen]["file"], "rect": list(tpl["rect"]),
                              "overlay": tpl["overlay"], "fit": "cover",
                              "focus": list(tpl["focus"]),
                              "sha256": hashlib.sha256(
                                  Path(LIBRARY[chosen]["file"]).read_bytes()).hexdigest(),
                              "source": LIBRARY[chosen]["source"], "tags": kws}
    # 粒子页：封面、结论收束页（skyline 命中或末页）、以及中部转折（图像页序中间）
    key_pages = [p["page"] for p in pages if p["image"] in (None, "skyline_city")]
    particle_pages = {}
    if pages:
        first, last = 1, len(pages)
        seq = [first]
        if len(pages) >= 6:
            seq.append(len(pages) // 2 + 1)
        seq.append(last)
        modes = ["converge", "drift", "disperse"]
        for j, pg in enumerate(dict.fromkeys(seq)):
            particle_pages[str(pg)] = {"rect": [8.1, 0.3, 4.8, 2.0] if pg == first
                                       else [7.55, 5.6, 4.4, 1.7] if pg != last
                                       else [8.95, 0.4, 3.55, 1.25],
                                       "count": [20, 14, 22][j % 3],
                                       "color": "E8C877", "mode": modes[j % 3],
                                       "dur": 2200, "alpha": 30}
    plan = {
        "schema_version": "1.0.0",
        "kind": "visual_upgrade_plan",
        "pages": pages, "images": images, "particles": particle_pages,
        "library": {k: {"tags": v["tags"], "source": v["source"]}
                    for k, v in LIBRARY.items()},
        "note": "关键词语义选图 + 结构启发式放置模板；示意图素材非现场照片；"
                "数据页/表格页不配图并记录理由。",
    }
    plan["plan_sha256"] = hashlib.sha256(
        json.dumps({k: plan[k] for k in ("pages", "images", "particles")},
                   ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return plan
