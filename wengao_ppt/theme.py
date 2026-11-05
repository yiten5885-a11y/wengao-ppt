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
# 1.2.0（2026-11-04）：每个主题增加 style 块（风格名/体裁/一句话/标签/缩略图类型），
# 并把 basic 之外的 4 套浅色/深色主题拉开成互不相同的字体配对与字号层级。
# style 不进 config_digest —— 它能改界面的说法，但不能改历史任务冻结的身份。
# 1.3.0（2026-11-04）：增加 furniture（页面构件：色带/细线/角标）。
# 2.0.0（2026-11-05）：主题从「换色」升级为「装帧语言」——成套调色板、
#   天头/地脚/侧轨/眉标/刻度构成，外加 table_style / chart_style 两类样式
#   令牌。后两者真的改输出，所以一并进摘要。
# 因为它真的改输出——"藏蓝公文"从此有红头，不再只是一张白纸。
REGISTRY_VERSION = "2.0.0"

# 不带 theme 的旧调用使用的默认主题（保持 D10-A 合法默认行为）
DEFAULT_THEME_ID = "wengao-basic"

THEMES: dict[str, dict] = {
    # ---------------------------------------------------------------- S1 朱印公文
    # 灵感：正式文告的天头/落款/中轴。宣纸底而非纯白，朱砂只用在双线与印标，
    # 墨蓝承担全部结构，避免"红配蓝"的廉价感。
    "wengao-basic": {
        "name": "稳稿基础主题（旧默认）",
        "style": {
            "style_id": "S1",
            "style_name": "朱印公文",
            "genre": "公文 / 汇报",
            "tagline": "宣纸底、墨蓝天头、朱砂双线与落款印标，端正的中轴式版式",
            "tags": ["宣纸", "墨蓝", "朱砂", "中轴"],
            "preview": "official",
        },
        "furniture": {
            "id": "imprint",
            "parts": [
                {"edge": "top", "offset_in": 0.0, "thickness_in": 0.10, "color": "primary"},
                {"edge": "top", "offset_in": 0.125, "thickness_in": 0.022, "color": "accent"},
                {"edge": "top", "offset_in": 0.158, "thickness_in": 0.010, "color": "accent"},
                {"edge": "top", "offset_in": 0.29, "thickness_in": 0.012,
                 "span": [0.06, 0.94], "color": "border"},
                {"edge": "top", "offset_in": 0.20, "thickness_in": 0.02,
                 "span": [0.475, 0.525], "color": "primary"},
                {"edge": "left", "offset_in": 0.22, "thickness_in": 0.010,
                 "span": [0.08, 0.16], "color": "border"},
                {"edge": "right", "offset_in": 0.22, "thickness_in": 0.010,
                 "span": [0.08, 0.16], "color": "border"},
                {"edge": "left", "offset_in": 0.0, "thickness_in": 0.028,
                 "span": [0.30, 0.36], "color": "primary"},
                {"edge": "right", "offset_in": 0.0, "thickness_in": 0.028,
                 "span": [0.30, 0.36], "color": "primary"},
                {"edge": "bottom", "offset_in": 0.0, "thickness_in": 0.012,
                 "span": [0.06, 0.94], "color": "border"},
                {"edge": "bottom", "offset_in": 0.055, "thickness_in": 0.045,
                 "span": [0.06, 0.115], "color": "accent"},
                {"edge": "bottom", "offset_in": 0.055, "thickness_in": 0.045,
                 "span": [0.885, 0.94], "color": "primary"},
                {"edge": "bottom", "offset_in": 0.11, "thickness_in": 0.02,
                 "span": [0.475, 0.525], "color": "border"},
            ],
        },
        "version": "2.0.0",
        "legacy_default": True,
        "slide_bg": "FBF9F4",
        "colors": {
            "primary": "1B2D4B", "accent": "A32E26", "text": "20252B", "muted": "4A5058",
            "heading": "1B2D4B", "subtitle": "4A5058",
            "surface": "FFFFFF", "border": "D8D2C4",
            "table_header_bg": "1B2D4B", "table_header_text": "FFFFFF",
            "table_row_bg": "FFFFFF", "table_row_alt_bg": "F5F2EA",
            "chart_bg": "FBF9F4", "chart_text": "20252B", "chart_grid": "E2DCCD",
            "chart_series": ["1B2D4B", "A32E26", "5B7A6B", "8A7350"],
        },
        "fonts": {"heading_ea": "Songti", "body_ea": "Songti",
                  "heading_latin": "Palatino", "body_latin": "Palatino"},
        "sizes": {"title": 30, "subtitle": 18, "heading": 22, "body": 15, "note": 11,
                  "chart_text": 12},
        "table_style": "editorial",
        "chart_style": "minimal",
        "font_fallback_note": "上游默认字体 Microsoft YaHei 本机缺失，已显式替换为 Songti；"
                             "拉丁槽换 Palatino（本机系统已注册），与宋体构成人文衬线对。",
    },
    # ---------------------------------------------------------------- S2 深松商务
    # 灵感：瑞士国际主义网格。非对称的左上/右下压边、三段刻度、纵横细线构成
    # 信息板；黄铜只做刻度与强调，不铺大面。
    "wengao-business": {
        "name": "商务清晰",
        "style": {
            "style_id": "S2",
            "style_name": "深松商务",
            "genre": "商务 / 汇报",
            "tagline": "深松绿与黄铜、瑞士网格与三段刻度，信息密度高的现代汇报",
            "tags": ["黑体", "深松绿", "黄铜", "网格"],
            "preview": "grid",
        },
        "furniture": {
            "id": "swiss",
            "parts": [
                {"edge": "left", "offset_in": 0.0, "thickness_in": 0.11,
                 "span": [0.0, 1.0], "color": "primary"},
                {"edge": "top", "offset_in": 0.0, "thickness_in": 0.075,
                 "span": [0.0, 0.34], "color": "primary"},
                {"edge": "bottom", "offset_in": 0.0, "thickness_in": 0.075,
                 "span": [0.66, 1.0], "color": "accent"},
                {"edge": "right", "offset_in": 0.0, "thickness_in": 0.05,
                 "span": [0.32, 0.36], "color": "border"},
                {"edge": "top", "offset_in": 0.155, "thickness_in": 0.012,
                 "span": [0.055, 0.945], "color": "border"},
                {"edge": "bottom", "offset_in": 0.155, "thickness_in": 0.012,
                 "span": [0.055, 0.945], "color": "border"},
                {"edge": "left", "offset_in": 0.24, "thickness_in": 0.012,
                 "span": [0.12, 0.88], "color": "border"},
                {"edge": "right", "offset_in": 0.24, "thickness_in": 0.012,
                 "span": [0.12, 0.88], "color": "border"},
                {"edge": "top", "offset_in": 0.24, "thickness_in": 0.05,
                 "span": [0.055, 0.09], "color": "primary"},
                {"edge": "top", "offset_in": 0.24, "thickness_in": 0.05,
                 "span": [0.125, 0.16], "color": "primary"},
                {"edge": "top", "offset_in": 0.24, "thickness_in": 0.05,
                 "span": [0.195, 0.23], "color": "border"},
                {"edge": "bottom", "offset_in": 0.24, "thickness_in": 0.05,
                 "span": [0.77, 0.805], "color": "border"},
                {"edge": "bottom", "offset_in": 0.24, "thickness_in": 0.05,
                 "span": [0.84, 0.875], "color": "accent"},
                {"edge": "bottom", "offset_in": 0.24, "thickness_in": 0.05,
                 "span": [0.91, 0.945], "color": "accent"},
                {"edge": "top", "offset_in": 0.45, "thickness_in": 0.02,
                 "span": [0.055, 0.22], "color": "accent"},
            ],
        },
        "version": "2.0.0",
        "slide_bg": "FCFCFA",
        "colors": {
            "primary": "14423C", "accent": "A9761F", "text": "1E2624", "muted": "3C4A46",
            "heading": "14423C", "subtitle": "3C4A46",
            "surface": "FFFFFF", "border": "CCD8D3",
            "table_header_bg": "14423C", "table_header_text": "FFFFFF",
            "table_row_bg": "FFFFFF", "table_row_alt_bg": "F1F5F2",
            "chart_bg": "FCFCFA", "chart_text": "1E2624", "chart_grid": "DCE6E1",
            "chart_series": ["14423C", "A9761F", "4C7A70", "8D9490"],
        },
        "fonts": {"heading_ea": "STHeiti", "body_ea": "STHeiti",
                  "heading_latin": "Helvetica Neue", "body_latin": "Helvetica Neue"},
        "sizes": {"title": 30, "subtitle": 19, "heading": 22, "body": 17, "note": 11,
                  "chart_text": 12},
        "table_style": "ruled",
        "chart_style": "grid",
        "font_fallback_note": "Microsoft YaHei 本机缺失，按已核验可用字体显式替换为 STHeiti；"
                             "拉丁槽 Helvetica Neue 与黑体构成无衬线对。",
    },
    # ---------------------------------------------------------------- S3 午夜蓝科技
    # 1.x 的荧光青 + 品红是"电竞感"，不是"技术感"。2.0 改成午夜蓝底、
    # 单一冰蓝结构色 + 一枚暖橙强调，刻度与点阵像仪表盘而不是霓虹灯箱。
    "wengao-dark": {
        "name": "深色科技",
        "style": {
            "style_id": "S3",
            "style_name": "午夜仪表",
            "genre": "科技 / 产品发布",
            "tagline": "午夜蓝底、冰蓝结构色、单点暖橙，仪表式刻度与数据条",
            "tags": ["深底", "冰蓝", "等宽", "刻度"],
            "preview": "neon",
        },
        "furniture": {
            "id": "instrument",
            "parts": [
                {"edge": "top", "offset_in": 0.0, "thickness_in": 0.035, "color": "primary"},
                {"edge": "bottom", "offset_in": 0.0, "thickness_in": 0.035, "color": "primary"},
                {"edge": "left", "offset_in": 0.0, "thickness_in": 0.035,
                 "span": [0.10, 0.90], "color": "accent"},
                {"edge": "right", "offset_in": 0.0, "thickness_in": 0.035,
                 "span": [0.10, 0.90], "color": "accent"},
                {"edge": "top", "offset_in": 0.10, "thickness_in": 0.012,
                 "span": [0.055, 0.945], "color": "border"},
                {"edge": "bottom", "offset_in": 0.10, "thickness_in": 0.012,
                 "span": [0.055, 0.945], "color": "border"},
                {"edge": "bottom", "offset_in": 0.22, "thickness_in": 0.045,
                 "span": [0.055, 0.085], "color": "primary"},
                {"edge": "bottom", "offset_in": 0.22, "thickness_in": 0.045,
                 "span": [0.095, 0.125], "color": "primary"},
                {"edge": "bottom", "offset_in": 0.22, "thickness_in": 0.045,
                 "span": [0.135, 0.165], "color": "border"},
                {"edge": "bottom", "offset_in": 0.22, "thickness_in": 0.045,
                 "span": [0.175, 0.205], "color": "border"},
                {"edge": "bottom", "offset_in": 0.22, "thickness_in": 0.045,
                 "span": [0.215, 0.245], "color": "border"},
                {"edge": "bottom", "offset_in": 0.22, "thickness_in": 0.045,
                 "span": [0.255, 0.285], "color": "border"},
                {"edge": "top", "offset_in": 0.22, "thickness_in": 0.045,
                 "span": [0.86, 0.89], "color": "accent"},
                {"edge": "top", "offset_in": 0.22, "thickness_in": 0.045,
                 "span": [0.895, 0.925], "color": "accent"},
                {"edge": "top", "offset_in": 0.22, "thickness_in": 0.045,
                 "span": [0.93, 0.96], "color": "border"},
                {"edge": "right", "offset_in": 0.14, "thickness_in": 0.012,
                 "span": [0.08, 0.16], "color": "primary"},
            ],
        },
        "version": "2.0.0",
        "slide_bg": "0A1626",
        "colors": {
            "primary": "74A9F5", "accent": "E59463", "text": "E7EEF8", "muted": "A8BDD6",
            "heading": "F2F7FF", "subtitle": "A8BDD6",
            "surface": "101F33", "border": "22354E",
            "table_header_bg": "1B3550", "table_header_text": "E7EEF8",
            "table_row_bg": "101F33", "table_row_alt_bg": "16273D",
            "chart_bg": "101F33", "chart_text": "E7EEF8", "chart_grid": "2B415E",
            "chart_series": ["74A9F5", "E59463", "7FC8B0", "9C86D8"],
        },
        "fonts": {"heading_ea": "STHeiti", "body_ea": "STHeiti",
                  "heading_latin": "Menlo", "body_latin": "Menlo"},
        "sizes": {"title": 30, "subtitle": 20, "heading": 22, "body": 18, "note": 11,
                  "chart_text": 12},
        "table_style": "data",
        "chart_style": "data",
        "sizes_note": ("2026-10-20 可读性基线（用户要求字不要太小）：主标题 32-40、"
                       "页标题 28-32、正文 22-24（下限 20）、表格 18-20、"
                       "图表与辅助注 >=16。主题值是**上限闸**（_capped 只缩不放），"
                       "生成端显式字号 <= 上限时按生成端值渲染。"),
        "font_fallback_note": "深色主题采用 STHeiti（本机已核验可显示中文）替代缺失的 Microsoft YaHei；"
                             "拉丁槽 Menlo 让数字与代码在深底上保持等宽对齐。",
    },
    # ---------------------------------------------------------------- S4 书卷静读
    # 灵感：书籍的版心与书脊。构件极克制（8 条），靠留白与页边比例说话；
    # 唯一装饰是标题上方的短朱线、地脚的居中短线与左右两枚句点。
    "wengao-minimal": {
        "name": "极简阅读",
        "style": {
            "style_id": "S4",
            "style_name": "书卷静读",
            "genre": "阅读 / 长文",
            "tagline": "宣纸暖白、Georgia 配宋体、书脊细线与大留白，长文最省力",
            "tags": ["暖白", "宋体", "留白", "赭石"],
            "preview": "quiet",
        },
        "furniture": {
            "id": "colophon",
            "parts": [
                {"edge": "top", "offset_in": 0.24, "thickness_in": 0.012,
                 "span": [0.075, 0.20], "color": "accent"},
                {"edge": "top", "offset_in": 0.36, "thickness_in": 0.008,
                 "span": [0.075, 0.30], "color": "border"},
                {"edge": "right", "offset_in": 0.16, "thickness_in": 0.010,
                 "span": [0.10, 0.90], "color": "border"},
                {"edge": "left", "offset_in": 0.16, "thickness_in": 0.045,
                 "span": [0.48, 0.52], "color": "primary"},
                {"edge": "bottom", "offset_in": 0.18, "thickness_in": 0.010,
                 "span": [0.46, 0.54], "color": "border"},
                {"edge": "bottom", "offset_in": 0.18, "thickness_in": 0.045,
                 "span": [0.075, 0.082], "color": "accent"},
                {"edge": "bottom", "offset_in": 0.18, "thickness_in": 0.045,
                 "span": [0.918, 0.925], "color": "accent"},
                {"edge": "bottom", "offset_in": 0.30, "thickness_in": 0.008,
                 "span": [0.70, 0.925], "color": "border"},
            ],
        },
        "version": "2.0.0",
        "slide_bg": "F7F3EA",
        "colors": {
            "primary": "2A2721", "accent": "8B5A3C", "text": "2A2721", "muted": "4A4438",
            "heading": "2A2721", "subtitle": "4A4438",
            "surface": "FFFFFF", "border": "E0D8C7",
            "table_header_bg": "EDE6D8", "table_header_text": "2A2721",
            "table_row_bg": "FFFFFF", "table_row_alt_bg": "F5F0E5",
            "chart_bg": "F7F3EA", "chart_text": "2A2721", "chart_grid": "E8E0D0",
            "chart_series": ["4A5E6B", "8B5A3C", "6E7F62", "82612F"],
        },
        "fonts": {"heading_ea": "Songti", "body_ea": "Songti",
                  "heading_latin": "Georgia", "body_latin": "Georgia"},
        "sizes": {"title": 30, "subtitle": 18, "heading": 22, "body": 16, "note": 10,
                  "chart_text": 11},
        "table_style": "quiet",
        "chart_style": "quiet",
        "font_fallback_note": "极简主题使用 Songti 与 Georgia 的衬线对；Microsoft YaHei 缺失的替换为计划动作。",
    },
    # ------------------------------------------------------- S5 原稿本色（不换肤）
    # 与上面四套"装帧"主题不同：它不把整套稿染成固定调色板，而是识别并保留
    # 原稿自身的设计语言，只按结构规则做布局、位置、尺寸、字号、间距与对齐精修。
    "wengao-adaptive": {
        "name": "原稿适配精修",
        "style": {
            "style_id": "S5",
            "style_name": "原稿本色",
            "genre": "原稿 / 精修",
            "tagline": "不换肤：识别并保留原稿配色与字体，只做结构、度量与对齐精修",
            "tags": ["不换肤", "保留原稿", "结构精修"],
            "preview": "source",
        },
        "version": "1.0.0",
        "adaptive": True,
        "refine_rules_version": "1.0.0",
        "slide_bg": None,
        "colors": None,
        "fonts": None,
        "sizes": None,
        "font_fallback_note": "原稿适配主题不替换字体：原稿声明的字体按原样保留，"
                             "本机缺失时由渲染环境自行替换（与 preserve 路径一致）。",
    },
    # ---------------------------------------------------------------- S6 香槟影院
    # 灵感：宽银幕与舞台压边。上下细银幕线 + 底部金铜通栏 + 三点进度，
    # 金铜不铺满、珊瑚只做角标，避免"土豪金"。
    "wengao-visual-dark": {
        "name": "深色视觉升级（关键词配图+动效）",
        "style": {
            "style_id": "S6",
            "style_name": "香槟影院",
            "genre": "视觉 / 发布",
            "tagline": "炭黑舞台、香槟金与珊瑚、银幕细线与三点进度，配图裁切与转场动效",
            "tags": ["炭黑", "香槟金", "配图", "动效"],
            "preview": "cinema",
        },
        "furniture": {
            "id": "stage",
            "parts": [
                {"edge": "bottom", "offset_in": 0.0, "thickness_in": 0.055, "color": "primary"},
                {"edge": "left", "offset_in": 0.0, "thickness_in": 0.09,
                 "span": [0.0, 0.30], "color": "primary"},
                {"edge": "top", "offset_in": 0.0, "thickness_in": 0.05,
                 "span": [0.86, 1.0], "color": "accent"},
                {"edge": "top", "offset_in": 0.10, "thickness_in": 0.012, "color": "border"},
                {"edge": "bottom", "offset_in": 0.10, "thickness_in": 0.012, "color": "border"},
                {"edge": "left", "offset_in": 0.22, "thickness_in": 0.012,
                 "span": [0.10, 0.90], "color": "border"},
                {"edge": "right", "offset_in": 0.22, "thickness_in": 0.012,
                 "span": [0.10, 0.90], "color": "border"},
                {"edge": "bottom", "offset_in": 0.20, "thickness_in": 0.05,
                 "span": [0.055, 0.085], "color": "accent"},
                {"edge": "bottom", "offset_in": 0.20, "thickness_in": 0.05,
                 "span": [0.095, 0.125], "color": "accent"},
                {"edge": "bottom", "offset_in": 0.20, "thickness_in": 0.05,
                 "span": [0.135, 0.165], "color": "accent"},
                {"edge": "top", "offset_in": 0.22, "thickness_in": 0.02,
                 "span": [0.055, 0.14], "color": "primary"},
                {"edge": "right", "offset_in": 0.0, "thickness_in": 0.05,
                 "span": [0.64, 0.70], "color": "primary"},
                {"edge": "bottom", "offset_in": 0.22, "thickness_in": 0.02,
                 "span": [0.86, 0.945], "color": "primary"},
            ],
        },
        "version": "2.0.0",
        "slide_bg": "0D0D11",
        "visual_upgrade": True,
        "colors": {
            "primary": "C6A667", "accent": "C9705B", "text": "EFEBE3", "muted": "A8A39B",
            "heading": "FFFFF7", "subtitle": "C4BFB5",
            "surface": "17171D", "border": "2C2B33",
            "table_header_bg": "1E1E25", "table_header_text": "EFEBE3",
            "table_row_bg": "141419", "table_row_alt_bg": "1B1B22",
            "chart_bg": "141419", "chart_text": "EFEBE3", "chart_grid": "34333C",
            "chart_series": ["C6A667", "C9705B", "7C9CC4", "8A8A92"],
        },
        "fonts": {"heading_ea": "Arial Unicode MS", "body_ea": "Arial Unicode MS",
                  "heading_latin": "Avenir Next", "body_latin": "Avenir Next"},
        "sizes": {"title": 30, "subtitle": 22, "heading": 22, "body": 18, "note": 11,
                  "chart_text": 12},
        "table_style": "cinema",
        "chart_style": "cinema",
        "font_fallback_note": ("东亚字体槽改 Arial Unicode MS（S6 原生对照 13 页无缺字，"
                               "依据与平台范围见 FALLBACK_FONTS 注释）；拉丁槽仍 Avenir Next。"),
    },
}

# 参与配置摘要的字段（只含影响输出的参数；不含展示名与说明）
# furniture 进摘要：它改变导出的字节（每页多出几条色带），不影响输出的字段才有资格
# 待在摘要外面。style 仍然不进（纯展示说法）。
_DIGEST_FIELDS = ("slide_bg", "colors", "fonts", "sizes", "visual_upgrade",
                  "furniture", "table_style", "chart_style")

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


def _clamp_chart_role_fonts(chart, hi) -> list[dict]:
    """Shrink a native chart's own label fonts (data labels, axis tick
    labels, legend) above `hi` points down to `hi`.

    clamp_oversized_to_role_max shrinks text_frame runs; a chart's own
    labels live in separate chart-part XML a text_frame walk never reaches.
    Real failure on job-mt-56ebac59/job-51d167cddbc1 (2026-11-02): a
    COLUMN_CLUSTERED chart's plot-level data_labels.font.size was set to
    18pt against the chart_aux role's 10-12pt max (confirmed directly via
    python-pptx: chart.plots[0].data_labels.font.size == 228600 EMU ==
    18.0pt, while the axis tick-label fonts were already correctly at 12pt)
    — the first HG07 finding this session's material-generation fixes
    actually reached. Only an explicitly-set font.size is touched; an
    inherited/theme-default None is left alone, matching the shrink-only,
    geometry-untouched contract of the rest of this module.

    Called unconditionally for every chart shape, not gated on
    refine_mod.infer_text_roles: that classifier only ever assigns a role to
    is_text shapes (confirmed directly — a real chart shape's index never
    appears in its returned role map at all), while scripts/layout_gate.py's
    independent, render-based role inference assigns "chart_aux" to a
    chart's own rendered label text by geometric position. chart_aux is the
    only role that classifier ever assigns to a chart's own text, so this
    function targets that role's max directly rather than trying to recover
    a per-shape role for charts that was never computed for them.
    """
    from pptx.util import Pt

    changes = []
    targets = []
    for plot in chart.plots:
        try:
            targets.append(("data_labels", plot.data_labels.font))
        except Exception:
            pass
    for attr in ("category_axis", "value_axis"):
        try:
            targets.append((f"{attr}.tick_labels", getattr(chart, attr).tick_labels.font))
        except Exception:
            pass
    try:
        if chart.has_legend:
            targets.append(("legend", chart.legend.font))
    except Exception:
        pass
    for target, font in targets:
        size = font.size
        if size is None:
            continue
        pt = size.pt
        if pt > hi + 1e-9:
            font.size = Pt(hi)
            changes.append({"target": target, "from": pt, "to": hi})
    return changes


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
    chart_aux_hi = ROLE_RANGE_PT.get("chart_aux", (None, None))[1]
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
        if chart_aux_hi is not None:
            for s in shs:
                if not s.has_chart:
                    continue
                for change in _clamp_chart_role_fonts(s.shape.chart, chart_aux_hi):
                    changes.append({"slide": idx, "shape": s.name, "role": "chart_aux",
                                    **change})
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
            # 家具（页面构件）必须进 params：apply_theme 拿到的就是这个包，
            # 它同时进了 config_digest（_DIGEST_FIELDS 含 furniture），
            # 所以"同一主题、不同构件"必然是不同身份。
            "furniture": copy.deepcopy(t.get("furniture") or {}),
            # 2.0 样式令牌：与家具同理由，必须进 params 才会在 apply 时生效，
            # 且进 config_digest 才不会让"同一主题、不同表格语言"共用身份。
            "table_style": t.get("table_style"),
            "chart_style": t.get("chart_style"),
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
                "style": copy.deepcopy(t.get("style") or {}),
                # 自适应主题**不声明**家具：它的风格就是"不换肤"，加构件与定义矛盾。
                "furniture": {"id": None, "parts": 0},
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
            "style": copy.deepcopy(t.get("style") or {}),
            # 2.0：把装帧语言与表格/图表语言一起交给界面，卡片不再只是色卡。
            "table_style": t.get("table_style"),
            "chart_style": t.get("chart_style"),
            # 家具摘要：界面要能说明"这套风格在页面上会画什么"，
            # 而不是只能给一个色卡。parts=0 表示这套主题不加任何构件。
            "furniture": {
                "id": (t.get("furniture") or {}).get("id"),
                "parts": len((t.get("furniture") or {}).get("parts") or []),
            },
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
                             floor=size_floor, style=p.get("table_style"))
                continue
            if getattr(obj, "has_chart", False) and obj.has_chart:
                _style_chart(obj.chart, colors, fonts, sizes, changed,
                             floor=size_floor, style=p.get("chart_style"))
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

    # 页面构件（家具）：换色 vs 换风格的差别就在这里。
    # 必须在所有配色/字体/字号/重排都落定之后、保存之前画——它要与既有形状的
    # 最终包围盒做相交检查，早画会拿旧几何去避让。自适应主题在函数开头就
    # return 了（不换肤），preserve 也在开头就 return（一字节不改）。
    from . import furniture as fur_mod
    try:
        changed["furniture"] = fur_mod.apply_to(prs, ident)
    except fur_mod.FurnitureError as exc:
        # 声明不合法属于代码缺陷，不静默降级成"没画"——宁可整单失败也别悄悄换风格
        raise ThemeError(f"THEME.FURNITURE_INVALID: {exc}") from exc

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


def _table_body_fg(bg_hex: str) -> str:
    """表格数据行文字色：在该行底色上对比度更高的那一端（纯黑 / 纯白）。

    保留"最大化墨/底差"的原意（抗锯齿会让细笔画的核心像素向底色靠拢，余量越大越稳），
    同时不再是"只对浅色主题成立"的硬编码。
    """
    from .backdrop import contrast_ratio, hex_to_rgb
    rgb = hex_to_rgb(bg_hex)
    return "000000" if contrast_ratio((0, 0, 0), rgb) >= contrast_ratio((255, 255, 255), rgb) else "FFFFFF"


def _style_table(tbl, colors, fonts, sizes, changed, floor: dict | None = None,
                 style: str | None = None):
    """2.0 表格：编辑部式横线表。

    1.x 给每个单元格画四边实线 + 实心表头 + 明显斑马纹，六套主题只是"同一张
    Word 表格换颜色"。2.0 按 ``table_style`` 解算成不同的表格语言，但共守三条：
    竖线一律关闭、表头只用一条强调线收口、数据行只用极细横线分隔。
    文字、数据、行列对应、单元格几何全部不动。
    """
    from pptx.util import Pt

    rules = _table_style_rules(style, colors)

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
        if header:
            bg = colors["table_header_bg"]
        elif rules["banded"] and (ri % 2 == 0):
            bg = colors["table_row_alt_bg"]
        else:
            bg = colors["table_row_bg"]
        # Body rows: 取**对该行实际底色**对比度更高的纯黑或纯白，而不是写死黑色。
        #
        # 写死 "000000" 的理由曾是抗锯齿余量：1A1A1A 在白格上实测只到 3.69:1
        # （2026-10-31 job-8227a4c941e4 第 5 页，HG09 对细笔画已放宽到 4.0 仍没够），
        # 于是干脆用纯黑把墨/底差拉到最大。
        #
        # 但深色主题的行底色是 121C2E / 18253A：纯黑文字在上面只有 1.2:1，
        # 整张表等于不可读（2026-11-04 六主题对照渲染发现，深色主题表格数据行
        # 全黑压深底）。所以规则改为按底色选极值：浅底用纯黑、深底用纯白，
        # 两者都保留"把墨/底差拉到最大"的原意，只是方向跟着底色走。
        fg = colors["table_header_text"] if header else _table_body_fg(bg)
        for cell in row.cells:
            try:
                cell.fill.solid()
                cell.fill.fore_color.rgb = _RGB(bg)
                changed["fills_set"] += 1
            except Exception as e:  # noqa: BLE001
                changed["warnings"].append(f"table fill: {type(e).__name__}")
            # 2.0：显式关掉原稿带来的竖线与外框，只保留有语义的横线。
            for side in ("L", "R", "T"):
                _set_cell_edge(cell, side, None, 0, changed)
            if header:
                other = "T" if rules["accent_edge"] == "B" else "B"
                _set_cell_edge(cell, other, None, 0, changed)
                _set_cell_edge(cell, rules["accent_edge"], rules["accent_hex"],
                               rules["accent_emu"], changed)
            else:
                hair_w = 6350 if rules["rule"] == "hairline" else 9525
                _set_cell_edge(cell, "B", rules["hair_hex"], hair_w, changed)
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


_BORDER_TAGS = ("L", "R", "T", "B")
_TAG_QN = {"L": "a:lnL", "R": "a:lnR", "T": "a:lnT", "B": "a:lnB"}


def _set_cell_edge(cell, which: str, hex_color: str | None,
                   width_emu: int, changed) -> None:
    """设置单元格**单条**边的线型。

    ``hex_color=None`` 写 ``a:noFill``（显式关掉原稿带来的竖线/外框）。
    2.0 的现代表格只保留横线：四边实线框是 Word 表格感的主要来源。
    """
    from pptx.oxml.ns import qn
    from lxml import etree

    tag = _TAG_QN[which]
    try:
        tcPr = cell._tc.get_or_add_tcPr()
    except Exception as e:  # noqa: BLE001
        changed["warnings"].append(f"tcPr: {type(e).__name__}")
        return
    order = ["a:lnL", "a:lnR", "a:lnT", "a:lnB", "a:lnTlToBr", "a:lnBlToTr", "a:cell3D",
             "a:noFill", "a:solidFill", "a:gradFill", "a:blipFill", "a:pattFill", "a:grpFill",
             "a:headers", "a:extLst"]
    ln = tcPr.find(qn(tag))
    if ln is None:
        ln = etree.SubElement(tcPr, qn(tag))
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
    if hex_color is None:
        etree.SubElement(ln, qn("a:noFill"))
    else:
        fill = etree.SubElement(ln, qn("a:solidFill"))
        clr = etree.SubElement(fill, qn("a:srgbClr"))
        clr.set("val", hex_color.upper())
        etree.SubElement(ln, qn("a:prstDash")).set("val", "solid")


def _set_cell_borders(cell, hex_color, changed, width_emu: int = 9525):
    """给单元格四边加细边框（保留 1.x 调用形状；2.0 的表格改走 _set_cell_edge）。"""
    for which in _BORDER_TAGS:
        _set_cell_edge(cell, which, hex_color, width_emu, changed)


#: 表格风格 -> (表头背景, 表头文字, 数据行背景策略, 强调边, 强调宽度 EMU, 行分隔线)
#: 2.0 的五种表格语言都遵守"只改样式"：不改数据、不改行列对应、不重排。
_TABLE_STYLES: dict[str, dict] = {
    # 公文：深墨表头 + 朱砂压线，数据行纯白、只留行线（无斑马、无竖线）
    "editorial": {"banded": False, "rule": "bottom",
                  "accent_edge": "B", "accent_color": "accent", "accent_pt": 2.0},
    # 商务：深松表头 + 黄铜顶线，极淡斑马作行节奏
    "ruled": {"banded": True, "rule": "top",
              "accent_edge": "T", "accent_color": "accent", "accent_pt": 2.0},
    # 仪表：深底数据卡，冰蓝表头压线，行间极淡横线
    "data": {"banded": True, "rule": "hairline",
             "accent_edge": "B", "accent_color": "primary", "accent_pt": 1.5},
    # 书卷：浅纸表头 + 墨色压线，零斑马，最轻的分隔
    "quiet": {"banded": False, "rule": "hairline",
              "accent_edge": "B", "accent_color": "primary", "accent_pt": 1.25},
    # 影院：深炭表头 + 香槟金顶线，行间细线
    "cinema": {"banded": True, "rule": "hairline",
               "accent_edge": "T", "accent_color": "primary", "accent_pt": 2.0},
}


def _table_style_rules(style: str | None, colors: dict) -> dict:
    """把风格令牌解算成具体颜色与线宽；未知风格失败关闭到 editorial。"""
    s = _TABLE_STYLES.get(style or "editorial") or _TABLE_STYLES["editorial"]
    return {
        "banded": s["banded"],
        "rule": s["rule"],
        "accent_edge": s["accent_edge"],
        "accent_hex": colors[s["accent_color"]],
        "accent_emu": int(round(s["accent_pt"] * 12700)),
        "hair_hex": colors["border"],
    }


_CHART_STYLES: dict[str, dict] = {
    # 2.0：外框与默认粗网格是"Office 默认图表"的观感来源，绝大多数风格关掉它们。
    # 只有 grid/data 保留极细水平网格，且颜色取自主题的低饱和 border 色。
    "minimal": {"frame": False, "grid": False},
    "grid":    {"frame": True,  "grid": True, "grid_color": "border", "grid_pt": 0.5},
    "data":    {"frame": False, "grid": True, "grid_color": "chart_grid", "grid_pt": 0.5},
    "quiet":   {"frame": False, "grid": False},
    "cinema":  {"frame": False, "grid": False},
}


def _style_chart(chart, colors, fonts, sizes, changed, floor: dict | None = None,
                 style: str | None = None):
    """图表：文字/图例/绘图区/序列配色；**不改数据、类别、系列对应与轴范围**。

    `floor`（材料任务）：chart_text 下限，默认主题 chart_text=13/14 时
    不低于 16pt（可读性基线）。
    `style`（2.0）：外框与网格线的有无，按主题装帧语言解算。
    """
    from pptx.util import Pt
    from pptx.oxml.ns import qn
    from lxml import etree

    cs_rule = _CHART_STYLES.get(style or "minimal") or _CHART_STYLES["minimal"]
    frame_hex = colors["border"] if cs_rule["frame"] else colors["chart_bg"]

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
        _fill_spPr(spPr, colors["chart_bg"], frame_hex)
        ok.append("chart_bg")
    except Exception as e:  # noqa: BLE001
        changed["warnings"].append(f"chart bg: {type(e).__name__}")

    # 绘图区（c:plotArea/c:spPr，schema 中位于末尾）
    try:
        pa = cs.chart.plotArea
        pspPr = pa.find(qn("c:spPr"))
        if pspPr is None:
            pspPr = etree.SubElement(pa, qn("c:spPr"))
        _fill_spPr(pspPr, colors["surface"], frame_hex)
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

    # 网格线：2.0 默认隐藏（Office 默认粗网格是"未设计"的观感来源），
    # 只有 grid/data 风格保留 0.5pt 低饱和水平线。
    try:
        grid_line = chart.value_axis.major_gridlines.format.line
        if cs_rule["grid"]:
            grid_line.color.rgb = _RGB(colors[cs_rule["grid_color"]])
            grid_line.width = Pt(cs_rule["grid_pt"])
            ok.append("grid")
        else:
            grid_line.fill.background()
            ok.append("grid_hidden")
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
