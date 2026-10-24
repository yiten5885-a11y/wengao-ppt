"""W2 版式引擎与设计系统测试（tokens / measure / engine / templates / compose / checks）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import shutil

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = ROOT / ".pytest_tmp_w2"


@pytest.fixture
def tmp_path():
    """仓库内临时目录：本环境的 pytest tmp_path 会被宿主 broker 拦截，无法建目录。"""
    _TMP.mkdir(parents=True, exist_ok=True)
    yield _TMP
    shutil.rmtree(_TMP, ignore_errors=True)

from wengao_ppt.layout import checks, compose, engine, measure, tokens  # noqa: E402


# ---------------------------------------------------------------- tokens
def test_three_themes_registered_and_contrast_safe():
    assert len(tokens.THEMES) >= 3
    for tid, th in tokens.THEMES.items():
        for row in tokens.theme_contrast_report(th):
            # accent_2 是装饰性副色，只用于非正文图形
            if row["role"] == "accent_2":
                continue
            assert row["ratio"] >= 4.5, (tid, row)


def test_font_ladder_matches_frozen_policy():
    """字号阶梯必须与冻结硬门禁一致（不得私自放宽正文下限）。"""
    assert tokens.BODY_HARD_MIN_PT == 24
    assert tokens.TYPE_ROLES["body"].pt >= tokens.BODY_HARD_MIN_PT
    assert tokens.TYPE_ROLES["footnote"].pt == 16
    assert tokens.TYPE_ROLES["cover_title"].pt >= 36


def test_script_font_stacks_cover_six_scripts():
    for script in ("cjk_zh", "cjk_ja", "arabic", "devanagari", "cyrillic", "latin"):
        stack = tokens.stack_for(script)
        assert stack, script
        assert isinstance(stack[0], str) and stack[0]


def test_grid_is_twelve_columns_and_content_within_margins():
    g = engine.Grid()
    assert tokens.GRID_COLS == 12
    assert g.content_w > 0
    r = g.rect(0, 12, 0, 0)
    assert r.x >= tokens.MARGINS_EMU["left"]
    assert r.x + r.w <= tokens.CANVAS_W_EMU - tokens.MARGINS_EMU["right"]


# ---------------------------------------------------------------- measure
def test_wrap_lines_never_splits_number_unit_token():
    lines = measure.wrap_lines("同比增长 18%，其中 2500亿元的专项资金", 400000, 26)
    joined = "".join(lines)
    for tok in ("18%", "2500亿", "18", "2500"):
        assert tok in joined
    # 数字+单位必须完整出现在某一行里
    assert any("18%" in ln for ln in lines)
    assert any("2500亿" in ln for ln in lines)


def test_wrap_lines_terminates_on_signed_numbers():
    """回归：'+/-' 开头的数字 token 曾造成死循环。"""
    for text in ("-10 至 +20 区间", "±5% 波动", "2026-10-24 检索"):
        lines = measure.wrap_lines(text, 300000, 20)
        assert "".join(lines).replace(" ", "") == text.replace(" ", "")


def test_wrap_lines_terminates_on_bignumber_percent_and_bullets(tmp_path):
    """P0-2 回归 (BUG-W2-01)：大数字 46.8% + 要点混排文本不得导致 wrap_lines 死循环。"""
    import time
    from pptx import Presentation
    from wengao_ppt.layout import checks, templates, tokens

    # 1. 直接单元测试 wrap_lines 在不同宽度下立即终止
    texts = [
        "46.8%",
        "复购率 46.8%，连续 8 个季度提升。门店扩展到 120 家，会员规模达到 85 万。",
        "• 46.8% 核心指标渗透率\n• 同比提升 18.2 个百分点\n• 行业均值 37.4%",
    ]
    t0 = time.perf_counter()
    for t in texts:
        for w in (0, 1000, 50000, 300000, 1200000):
            res = measure.wrap_lines(t, w, 24)
            assert isinstance(res, list) and len(res) >= 1
    assert time.perf_counter() - t0 < 1.0, "wrap_lines 执行超时（死循环回归）"

    # 2. 真实 PPTX 渲染 + check_layout 回归测试
    prs = Presentation()
    prs.slide_width, prs.slide_height = tokens.CANVAS_W_EMU, tokens.CANVAS_H_EMU
    ctx = templates.TemplateContext(tokens.THEMES["ink"], {}, lambda *a, **k: None, None)
    s = prs.slides.add_slide(prs.slide_layouts[6])
    page = {
        "role": "insight",
        "variant": "a",
        "page_id": "p_bug01",
        "index": 1,
        "title": "新能源汽车零售渗透率突破 46.8%",
        "blocks": [
            {"kind": "bignumber", "value": "46.8%", "label": "渗透率", "note": "同比大幅提升"},
            {"kind": "bullets", "items": ["2024Q3 国内新能源乘用车市场零售渗透率达到 46.8%",
                                          "较去年同期大幅提升，展现强劲替代效应",
                                          "全面确立在中国汽车消费市场的主导地位"]},
        ],
    }
    templates.render_page(s, page, ctx)
    deck_path = tmp_path / "bignumber_check.pptx"
    prs.save(str(deck_path))
    t1 = time.perf_counter()
    chk = checks.check_layout(str(deck_path))
    assert time.perf_counter() - t1 < 5.0, "check_layout 在含 46.8% 页面超时"
    assert isinstance(chk, dict)



def test_block_height_scales_with_font_and_wrap():
    short = measure.block_height_emu("短文本", 900000, 26)
    long = measure.block_height_emu("短文本" * 12, 900000, 26)
    assert long > short
    assert measure.block_height_emu("同长度文本", 900000, 26) < \
        measure.block_height_emu("同长度文本", 900000, 40)


# ---------------------------------------------------------------- engine
def test_page_regions_title_height_follows_role():
    a = engine.page_regions(title_role="title")
    b = engine.page_regions(title_role="cover_title")
    assert b.title.h > a.title.h, "封面 44pt 的标题带必须高于正文页 36pt"


def _dense_items(n: int = 6) -> list[str]:
    """单栏放不下、双栏放得下的密料（约 40 字/条）。"""
    return [f"第 {i} 条要点：本季度该指标完成度为 {i * 3}%，"
            f"对应资源投入约 {i * 120} 万元，需周会跟踪。" for i in range(1, n + 1)]


def test_two_columns_split_items_not_blocks():
    """回归：双栏此前按"块"切分，单个多条目 bullets 块会全塞进第 0 栏后报溢出。"""
    g = engine.Grid()
    region = engine.page_regions(has_footnote=True).content
    block = engine.Block(kind="bullets", text="", payload={"items": _dense_items()},
                         role="body")
    single = engine.flow_blocks([block], region, g, allow_two_col=False)
    assert single.get("overflow"), "前提不成立：单栏本该放不下"
    res = engine.flow_blocks([block], region, g, allow_two_col=True)
    cols = {}
    for p in res["placements"]:
        cols.setdefault(p.col_start, 0)
        cols[p.col_start] += 1
    assert len(cols) == 2, f"两栏必须都分到条目，实得 {cols}"
    assert all(c > 0 for c in cols.values())
    assert not res.get("overflow"), res.get("overflow")


def test_two_columns_keep_gutter_between_them():
    """两栏之间必须有栏距，不能贴在一起。"""
    g = engine.Grid()
    region = engine.page_regions(has_footnote=True).content
    block = engine.Block(kind="bullets", text="", payload={"items": _dense_items()},
                         role="body")
    res = engine.flow_blocks([block], region, g, allow_two_col=True)
    left = [p for p in res["placements"] if p.col_start == 0]
    right = [p for p in res["placements"] if p.col_start > 0]
    assert left and right
    gap = min(p.rect.x for p in right) - max(p.rect.x + p.rect.w for p in left)
    assert gap >= tokens.GUTTER_EMU - 1, f"栏距不足：{gap}"


def test_flow_blocks_reports_overflow_instead_of_clipping():
    g = engine.Grid()
    region = engine.page_regions(has_footnote=True).content
    blocks = [engine.Block(kind="bullets", text=f"第 {i} 条很长的要点文本" * 6,
                           payload={"items": [f"第 {i} 条很长的要点文本" * 6]},
                           role="body") for i in range(40)]
    res = engine.flow_blocks(blocks, region, g)
    assert res.get("overflow"), "放不下必须如实报 overflow，不得静默裁掉"


# ---------------------------------------------------------------- 12 页型 × 2 变体
ROLES = ["cover", "section", "content", "compare", "trend", "insight", "image",
         "process", "timeline", "table", "quote", "closing"]


def _page_spec(role: str, variant: str, index: int) -> dict:
    base = {"role": role, "variant": variant, "page_id": f"t-{role}-{variant}",
            "index": index, "title": f"{role} 页型 {variant} 变体"}
    if role == "cover":
        base["blocks"] = [{"kind": "text", "text": "副标题：稳稿 PPT 版式引擎"}]
    elif role == "content":
        base["blocks"] = [{"kind": "bullets",
                           "items": ["要点一：营业收入 3.2 亿元", "要点二：同比增长 18%"]}]
    elif role == "compare":
        base["blocks"] = [
            {"kind": "bullets", "items": ["左侧：公司增速 18%"], "payload": {"side": "left"}},
            {"kind": "bullets", "items": ["右侧：行业增速 9%"], "payload": {"side": "right"}}]
    elif role == "trend":
        base["blocks"] = [{"kind": "chart", "chart_kind": "line",
                           "categories": ["2025Q4", "2026Q1"],
                           "series": {"营业收入": [2.7, 3.2]}, "h_emu": 3200000}]
    elif role == "insight":
        base["blocks"] = [{"kind": "bignumber", "value": "18%", "label": "同比增速",
                           "note": "高出行业 9 个百分点"}]
    elif role == "image":
        base["blocks"] = [{"kind": "text", "text": "图注：场景图说明"}]
    elif role == "process":
        base["blocks"] = [{"kind": "process", "steps": ["立项", "研发", "上线"]}]
    elif role == "timeline":
        base["blocks"] = [{"kind": "timeline",
                           "milestones": [{"time": "2026Q1", "label": "一期"},
                                          {"time": "2026Q3", "label": "二期"}]}]
    elif role == "table":
        base["blocks"] = [{"kind": "table", "headers": ["指标", "数值"],
                           "rows": [["营业收入", "3.2 亿元"], ["同比", "18%"]]}]
    elif role == "quote":
        base["blocks"] = [{"kind": "quote", "text": "事实只有一种，就是把话说实。"}]
        base["footnote"] = {"text": "来源：用户材料"}
    elif role == "closing":
        base["blocks"] = [{"kind": "bullets", "items": ["行动一：锁定交付", "行动二：复盘"]}]
    else:  # section
        base["blocks"] = [{"kind": "text", "text": "本节导语"}]
    return base


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("variant", ["a", "b"])
def test_every_page_type_and_variant_composes_clean(tmp_path, role, variant):
    prs = compose.Presentation()
    prs.slide_width, prs.slide_height = tokens.CANVAS_W_EMU, tokens.CANVAS_H_EMU
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    ctx = tokens and __import__("wengao_ppt.layout.templates", fromlist=["TemplateContext"]).TemplateContext(
        tokens.THEMES["ink"], {}, lambda *a, **k: None, None)
    from wengao_ppt.layout import templates
    rep = templates.render_page(slide, _page_spec(role, variant, 1), ctx)
    out = tmp_path / f"{role}-{variant}.pptx"
    prs.save(str(out))
    chk = checks.check_layout(str(out))
    hard = [f for f in chk["findings"]
            if f["code"] in ("LAYOUT.FONT_TOO_SMALL", "LAYOUT.MARGIN_VIOLATION",
                             "LAYOUT.OVERLAP", "LAYOUT.EMPTY_PAGE")]
    assert not hard, (role, variant, hard)
    assert rep.get("overflow") in (None, [], ()), (role, variant, rep.get("overflow"))


# ---------------------------------------------------------------- check_layout
def test_check_layout_flags_undersized_body_font(tmp_path):
    from pptx import Presentation
    from pptx.util import Inches, Pt
    prs = Presentation()
    prs.slide_width, prs.slide_height = tokens.CANVAS_W_EMU, tokens.CANVAS_H_EMU
    sl = prs.slides.add_slide(prs.slide_layouts[6])
    tb = sl.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(2))
    tb.name = "w2:body"
    r = tb.text_frame.paragraphs[0].add_run()
    r.text = "正文被缩到 12pt"
    r.font.size = Pt(12)
    out = tmp_path / "small.pptx"
    prs.save(str(out))
    chk = checks.check_layout(str(out))
    assert any(f["code"] == "LAYOUT.FONT_TOO_SMALL" for f in chk["findings"])
    assert chk["pass"] is False


def test_check_layout_accepts_policy_roles_for_footnote_and_ordinal(tmp_path):
    """页脚 16pt / 序号 18pt 是策略允许区间，不得误报。"""
    from pptx import Presentation
    from pptx.util import Inches, Pt
    prs = Presentation()
    prs.slide_width, prs.slide_height = tokens.CANVAS_W_EMU, tokens.CANVAS_H_EMU
    sl = prs.slides.add_slide(prs.slide_layouts[6])
    for name, pt, text in (("w2:footnote", 16, "来源：示例统计"),
                           ("w2:ordinal", 18, "01"),
                           ("w2:chart_aux", 18, "轴标签")):
        tb = sl.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(2))
        tb.name = name
        r = tb.text_frame.paragraphs[0].add_run()
        r.text = text
        r.font.size = Pt(pt)
    out = tmp_path / "roles.pptx"
    prs.save(str(out))
    chk = checks.check_layout(str(out))
    assert not [f for f in chk["findings"] if f["code"] == "LAYOUT.FONT_TOO_SMALL"], chk["findings"]


def test_size_rules_come_from_frozen_policy_file():
    rules, source = checks.size_rules()
    assert source.startswith("policy:"), source
    assert rules["body_hard_min"] == 24
    assert rules["table"] == (20, 22)


def test_render_pages_outputs_two_png_tiers(tmp_path):
    """W2-G：render_pages 必须出缩略图 + 大图两档，且都来自同一份 PDF。"""
    from wengao_ppt import render as R
    from wengao_ppt.layout import templates, tokens
    from pptx import Presentation
    prs = Presentation()
    prs.slide_width, prs.slide_height = tokens.CANVAS_W_EMU, tokens.CANVAS_H_EMU
    ctx = templates.TemplateContext(tokens.THEMES["ink"], {},
                                    lambda *a, **k: None, None)
    for i in range(2):
        s = prs.slides.add_slide(prs.slide_layouts[6])
        templates.render_page(s, {"role": "content", "variant": "a",
                                  "page_id": f"p{i}", "index": i + 1,
                                  "title": f"第 {i + 1} 页",
                                  "blocks": [{"kind": "bullets",
                                              "items": ["营业收入 3.2 亿元"]}]}, ctx)
    src = tmp_path / "two_tiers.pptx"
    prs.save(str(src))
    res = R.render_pages(src, tmp_path / "out", dpi=100, timeout=300)
    if not res["ok"]:
        import pytest
        pytest.skip(f"本机无 LibreOffice：{res['code']} {res.get('detail')}")
    assert res["png_tiers"]["large"]["count"] == 2
    assert res["png_tiers"]["thumb"]["count"] == 2
    assert res["png_tiers"]["thumb"]["dpi"] == R.THUMB_DPI
    # 两档必须都栅格化同一份 PDF
    assert res["fonts_report"]["thumb"]["error"] is None
    assert res["fonts_report"]["pdf_sha256"]


def test_render_pages_outputs_two_png_tiers(tmp_path):
    """W2-G：render_pages 必须出缩略图 + 大图两档，且都来自同一份 PDF。"""
    from wengao_ppt import render as R
    from wengao_ppt.layout import templates, tokens
    from pptx import Presentation
    prs = Presentation()
    prs.slide_width, prs.slide_height = tokens.CANVAS_W_EMU, tokens.CANVAS_H_EMU
    ctx = templates.TemplateContext(tokens.THEMES["ink"], {},
                                    lambda *a, **k: None, None)
    for i in range(2):
        s = prs.slides.add_slide(prs.slide_layouts[6])
        templates.render_page(s, {"role": "content", "variant": "a",
                                  "page_id": f"p{i}", "index": i + 1,
                                  "title": f"第 {i + 1} 页",
                                  "blocks": [{"kind": "bullets",
                                              "items": ["营业收入 3.2 亿元"]}]}, ctx)
    src = tmp_path / "two_tiers.pptx"
    prs.save(str(src))
    res = R.render_pages(src, tmp_path / "out", dpi=100, timeout=300)
    if not res["ok"]:
        import pytest
        pytest.skip(f"本机无 LibreOffice：{res['code']} {res.get('detail')}")
    assert res["png_tiers"]["large"]["count"] == 2
    assert res["png_tiers"]["thumb"]["count"] == 2
    assert res["png_tiers"]["thumb"]["dpi"] == R.THUMB_DPI
    # 两档必须都栅格化同一份 PDF
    assert res["fonts_report"]["thumb"]["error"] is None
    assert res["fonts_report"]["pdf_sha256"]


def test_arabic_paragraph_is_rtl_and_right_aligned(tmp_path):
    """W2 验收项 2：阿拉伯语段落必须 rtl=1 且右对齐（否则行首在左、阅读顺序错）。"""
    from wengao_ppt.layout import templates, tokens
    from pptx import Presentation
    prs = Presentation()
    prs.slide_width, prs.slide_height = tokens.CANVAS_W_EMU, tokens.CANVAS_H_EMU
    ctx = templates.TemplateContext(tokens.THEMES["ink"], {},
                                    lambda *a, **k: None, None)
    s = prs.slides.add_slide(prs.slide_layouts[6])
    templates.render_page(s, {"role": "content", "variant": "a", "page_id": "ar",
                              "index": 1, "title": "نموذج عربي",
                              "blocks": [{"kind": "bullets",
                                          "items": ["الإيرادات 3.2 مليار."]}]}, ctx)
    from pptx.enum.text import PP_ALIGN
    from pptx.oxml.ns import qn
    rtl_seen = 0
    for shp in s.shapes:
        if not shp.has_text_frame:
            continue
        for p in shp.text_frame.paragraphs:
            if not p.text.strip():
                continue
            pPr = p._p.find(qn("a:pPr"))
            assert pPr is not None and pPr.get("rtl") == "1", \
                f"RTL 段落缺 rtl=1：{p.text[:20]!r}"
            assert p.alignment == PP_ALIGN.RIGHT, p.alignment
            rtl_seen += 1
    assert rtl_seen >= 2  # 标题 + 要点


def test_glyphcheck_warns_on_system_font_fallback():
    """P1-4：当页面实际渲染字体落到栈末位系统字体（如 Arial Unicode MS）时必须报 WARN。"""
    from wengao_ppt import glyphcheck
    mock_result = {
        "error": None,
        "pages": [
            {
                "page": 1,
                "status": "OK",
                "distinct_cjk": 10,
                "font_resources": [
                    {"resource": "F1", "base_font": "ArialUnicodeMS", "embedded_program": False}
                ],
            }
        ],
    }
    findings = glyphcheck.quality_findings(mock_result)
    assert any(
        f.get("code") == "GLYPH.FONT_FALLBACK_STACK_BOTTOM" and f.get("severity") == "WARN"
        for f in findings
    )

