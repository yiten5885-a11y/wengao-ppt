"""W2 规划器与原生构建器测试（fact_ledger / deck_plan / builders.roundtrip）。"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = ROOT / ".pytest_tmp_w2p"


@pytest.fixture
def tmp_path():
    _TMP.mkdir(parents=True, exist_ok=True)
    yield _TMP
    shutil.rmtree(_TMP, ignore_errors=True)


MATERIAL = """一季度经营情况
公司实现营业收入 3.2 亿元，同比增长 18%。
其中核心业务收入 2.1 亿元，占比 65%。
新签订单 46 家客户，覆盖 12 个行业。

二季度计划
完成三大能力建设：交付提效、渠道拓展、组织升级。
研发投入计划 8000 万元，重点投向智能推荐与数据平台。

行业对比
行业平均增速为 9%，公司高出行业 9 个百分点。
"""


# ---------------------------------------------------------------- 台账
def test_plain_text_headings_become_section_subjects():
    from wengao_ppt.planner import fact_ledger as FL
    facts = FL.ledger_from_plain_text(MATERIAL)
    subjects = []
    for f in facts:
        if f["subject"] not in subjects:
            subjects.append(f["subject"])
    assert "一季度经营情况" in subjects
    assert "二季度计划" in subjects
    assert "行业对比" in subjects
    # 不得退化成"第 1 段"
    assert not any(s.startswith("第 ") and s.endswith(" 段") for s in subjects)


def test_ledger_records_every_number_with_source_sentence():
    from wengao_ppt.planner import fact_ledger as FL
    facts = FL.ledger_from_plain_text(MATERIAL)
    nums = [f["raw_value_text"] for f in facts if not f.get("qualitative")]
    for token in ("3.2 亿元", "18%", "2.1 亿元", "65%", "46 家", "12",
                  "8000 万元", "9%", "9 个百分点"):
        assert token in nums, (token, nums)
    for f in facts:
        assert f["source_ref"]["sentence"], "每条事实都必须能回到原句"


# ---------------------------------------------------------------- plan_deck
def _bundle():
    return {
        "schema": "rs.research_bundle.v1", "bundle_id": "rb-t", "material_language": "zh",
        "enabled": True,
        "facts": [{"fact_id": "f001", "origin": "WEB_RESEARCH", "subject": "行业",
                   "metric": "增速", "value": 9.0, "unit": "%", "period": "2026Q1",
                   "definition": "行业营收同比", "publisher": "示例统计",
                   "source_grade": "A", "url": "https://example.com/ind",
                   "retrieved_at": "2026-10-24", "quote_span": "",
                   "snapshot_path": "", "snapshot_sha256": ""}],
        "views": [{"view_id": "v01", "kind": "comparison", "title": "增速对比",
                   "metric": "增速", "unit": "%", "period": "2026Q1",
                   "series": [{"label": "增速对比", "origin": "WEB_RESEARCH",
                               "points": [{"x": "公司", "value": 18, "fact_ref": "f001"},
                                          {"x": "行业均值", "value": 9, "fact_ref": "f001"}]}],
                   "highlight": {"type": "max_gap", "text": "高出 9 个百分点",
                                 "fact_refs": ["f001"], "rule": "max_gap",
                                 "computed": {"gap": 9}},
                   "conflicts": [],
                   "footnote": {"text": "来源：示例统计（2026-10-24 检索）",
                                "fact_refs": ["f001"]}}],
    }


def test_plan_deck_uses_material_headings_as_titles():
    from wengao_ppt.planner import deck_plan
    plan = deck_plan.plan_deck(MATERIAL, _bundle(), {},
                               {"language": "zh", "images": {"enabled": False}},
                               str(_TMP))
    roles = [p["role"] for p in plan["pages"]]
    assert roles[0] == "cover" and roles[-1] == "closing"
    titles = [p.get("title") for p in plan["pages"]]
    assert titles[0] == "一季度经营情况", titles
    assert any(t == "二季度计划" for t in titles)
    assert any(t == "行业对比" for t in titles)
    # 视图页锚定在提到该指标的内容页之后
    assert "compare" in roles


def test_plan_deck_never_invents_numbers():
    """事实保护：页面出现的每个数字都必须来自台账或 bundle。"""
    from wengao_ppt.planner import deck_plan
    plan = deck_plan.plan_deck(MATERIAL, _bundle(), {},
                               {"language": "zh", "images": {"enabled": False}},
                               str(_TMP))
    facts = deck_plan.FL.ledger_from_plain_text(MATERIAL)
    allowed = deck_plan.collect_grounded_numbers(facts, _bundle())
    bad = deck_plan.numbers_in_plan_ungrounded(plan, allowed)
    assert not bad, bad


def test_content_variant_switches_to_two_columns_when_dense():
    from wengao_ppt.planner import deck_plan
    dense = "密集章节\n" + "\n".join(
        f"第 {i} 条要点：本季度该项指标完成度为 {i * 3}%，环比提升 {i} 个百分点，"
        f"对应的资源投入约 {i * 120} 万元。" for i in range(1, 15))
    plan = deck_plan.plan_deck(dense, None, {},
                               {"language": "zh", "images": {"enabled": False}},
                               str(_TMP))
    content = [p for p in plan["pages"] if p["role"] == "content"]
    assert content
    variants = {p["variant"] for p in content}
    assert "b" in variants, f"密集内容应走双栏变体，实得 {variants}"


def test_sparse_material_stays_single_column():
    from wengao_ppt.planner import deck_plan
    sparse = "简短章节\n只有一个要点：营收 3.2 亿元。"
    plan = deck_plan.plan_deck(sparse, None, {},
                               {"language": "zh", "images": {"enabled": False}},
                               str(_TMP))
    content = [p for p in plan["pages"] if p["role"] == "content"]
    assert all(p["variant"] == "a" for p in content)


def test_dense_section_splits_pages_without_continuation_marker():
    """一节放不下时必须切页，且标题用 (1/2) 而不是硬门禁禁止的"（续）"。"""
    from wengao_ppt.planner import deck_plan
    dense = "密集章节\n" + "\n".join(
        f"第 {i} 条要点：本季度该指标完成度为 {i * 3}%，环比提升 {i} 个百分点，"
        f"对应投入约 {i * 120} 万元。" for i in range(1, 13))
    plan = deck_plan.plan_deck(dense, None, {},
                               {"language": "zh", "images": {"enabled": False}},
                               str(_TMP))
    content = [p for p in plan["pages"] if p["role"] == "content"]
    assert len(content) >= 2, "12 条密料必须切页"
    for p in content:
        assert "（续）" not in p["title"], p["title"]
    assert all(p["variant"] == "b" for p in content)
    assert any("（1/2）" in p["title"] for p in content)


def test_view_without_data_is_dropped_not_fabricated():
    from wengao_ppt.planner import deck_plan
    b = _bundle()
    b["views"] = [{"view_id": "v9", "kind": "comparison", "title": "空视图",
                   "series": [{"label": "x", "points": []}]}]
    plan = deck_plan.plan_deck(MATERIAL, b, {},
                               {"language": "zh", "images": {"enabled": False}},
                               str(_TMP))
    codes = [w["code"] for w in plan["warnings"]]
    assert "RESEARCH.VIEW_NOT_RENDERABLE" in codes
    assert not [p for p in plan["pages"] if p.get("research_view_ref") == "v9"]


# ---------------------------------------------------------------- 原生构建器回环
def _blank_slide(tmp_path):
    from pptx import Presentation
    from wengao_ppt.layout import tokens
    prs = Presentation()
    prs.slide_width, prs.slide_height = tokens.CANVAS_W_EMU, tokens.CANVAS_H_EMU
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    return prs, slide


def test_chart_builder_roundtrip_matches_input(tmp_path):
    from pptx.util import Emu
    from wengao_ppt.builders import chart as cb, roundtrip as rt
    from wengao_ppt.layout import engine, tokens
    prs, slide = _blank_slide(tmp_path)
    rect = engine.Rect(914400, 914400, 6000000, 3000000)
    cb.add_native_chart(slide, rect, "column", ["2025Q4", "2026Q1"],
                        {"营业收入": [2.7, 3.2]}, tokens.THEMES["ink"],
                        title="营收", data_labels=True)
    out = tmp_path / "chart.pptx"
    prs.save(str(out))
    rep = rt.verify_chart(out, {"categories": ["2025Q4", "2026Q1"],
                                "series": {"营业收入": [2.7, 3.2]}})
    assert rep["ok"], rep


def test_chart_builder_rejects_unknown_highlight_series(tmp_path):
    from wengao_ppt.builders import chart as cb
    from wengao_ppt.layout import engine, tokens
    prs, slide = _blank_slide(tmp_path)
    with pytest.raises(ValueError):
        cb.add_native_chart(slide, engine.Rect(0, 0, 100, 100), "column",
                            ["a"], {"S": [1]}, tokens.THEMES["ink"],
                            highlight_series="不存在的系列")


def test_table_builder_roundtrip_matches_input(tmp_path):
    from wengao_ppt.builders import roundtrip as rt, table as tb
    from wengao_ppt.layout import engine, tokens
    prs, slide = _blank_slide(tmp_path)
    tb.add_native_table(slide, engine.Rect(914400, 914400, 6000000, 2000000),
                        ["指标", "数值"], [["营业收入", "3.2 亿元"], ["同比", "18%"]],
                        tokens.THEMES["ink"])
    out = tmp_path / "table.pptx"
    prs.save(str(out))
    rep = rt.verify_table(out, {"headers": ["指标", "数值"],
                                "rows": [["营业收入", "3.2 亿元"], ["同比", "18%"]]})
    assert rep["ok"], rep


def test_process_builder_requires_two_steps(tmp_path):
    from wengao_ppt.builders import process as pb
    from wengao_ppt.layout import engine, tokens
    prs, slide = _blank_slide(tmp_path)
    with pytest.raises(ValueError):
        pb.add_process(slide, engine.Rect(0, 0, 100, 100), ["只有一步"],
                       tokens.THEMES["ink"])


def test_timeline_builder_requires_two_milestones(tmp_path):
    from wengao_ppt.builders import timeline as tl
    from wengao_ppt.layout import engine, tokens
    prs, slide = _blank_slide(tmp_path)
    with pytest.raises(ValueError):
        tl.add_timeline(slide, engine.Rect(0, 0, 100, 100),
                        [{"time": "2026Q1", "label": "一期"}], tokens.THEMES["ink"])


# ---------------------------------------------------------------- compose 页级接口
def test_recompose_slide_leaves_other_pages_untouched(tmp_path):
    from wengao_ppt.layout import compose, templates, tokens
    from pptx import Presentation
    prs = Presentation()
    prs.slide_width, prs.slide_height = tokens.CANVAS_W_EMU, tokens.CANVAS_H_EMU
    ctx = templates.TemplateContext(tokens.THEMES["ink"], {}, lambda *a, **k: None, None)
    for i in range(3):
        s = prs.slides.add_slide(prs.slide_layouts[6])
        templates.render_page(s, {"role": "content", "variant": "a", "page_id": f"p{i}",
                                  "index": i + 1, "title": f"第 {i + 1} 页",
                                  "blocks": [{"kind": "bullets",
                                              "items": [f"要点 {i + 1}"]}]}, ctx)
    src = tmp_path / "three.pptx"
    prs.save(str(src))
    rep = compose.recompose_slide(src, 1, "b", "ink", str(tmp_path / "out.pptx"))
    assert rep["others_unchanged"] is True
    assert not rep["warnings"]


def test_insert_slide_keeps_page_count_and_others_unchanged(tmp_path):
    from wengao_ppt.layout import compose, templates, tokens
    from pptx import Presentation
    prs = Presentation()
    prs.slide_width, prs.slide_height = tokens.CANVAS_W_EMU, tokens.CANVAS_H_EMU
    ctx = templates.TemplateContext(tokens.THEMES["ink"], {}, lambda *a, **k: None, None)
    for i in range(2):
        s = prs.slides.add_slide(prs.slide_layouts[6])
        templates.render_page(s, {"role": "content", "variant": "a", "page_id": f"p{i}",
                                  "index": i + 1, "title": f"第 {i + 1} 页",
                                  "blocks": [{"kind": "bullets",
                                              "items": [f"要点 {i + 1}"]}]}, ctx)
    src = tmp_path / "two.pptx"
    prs.save(str(src))
    rep = compose.insert_slide(src, 0, {"role": "content", "variant": "a",
                                        "title": "插入页", "page_id": "ins",
                                        "blocks": [{"kind": "bullets",
                                                    "items": ["新要点"]}]},
                               "ink", str(tmp_path / "ins.pptx"))
    assert rep["page_count_after"] == rep["page_count_before"] + 1
    assert rep["others_unchanged"] is True


# ---------------------------------------------------------------- 回放材料标题识别
MD_MATERIAL = """# 合成业务材料：年度经营（合成样例）

> **本材料为合成样例**，所有数字均为虚构，不得作为真实业务结论引用。

## 一、经营概况

2024 年全年总收入达到 12.8 亿元，同比增长 38%。

## 二、产品进展

实时数据管道上线，日均处理消息量达到 42 亿条。
"""


def test_markdown_heading_separated_by_blank_line_becomes_subject():
    """回放材料是 .md：`## 标题` 与正文之间常空一行，必须仍被识别为节标题。"""
    from wengao_ppt.planner import fact_ledger as FL
    facts = FL.ledger_from_plain_text(MD_MATERIAL)
    subjects = []
    for f in facts:
        if f["subject"] not in subjects:
            subjects.append(f["subject"])
    assert "一、经营概况" in subjects, subjects
    assert "二、产品进展" in subjects, subjects
    # markdown 前缀不得漏进标题
    assert not any(s.startswith("#") for s in subjects), subjects


def test_meta_sentence_is_not_rendered_as_content():
    """合成样例声明等元信息句：可进台账，但不进正文页（规划器过滤）。"""
    from wengao_ppt.planner import deck_plan, fact_ledger as FL
    facts = FL.ledger_from_plain_text(MD_MATERIAL)
    sections = deck_plan._sections_from_facts(facts)
    rendered = []
    for sec in sections:
        for pg in deck_plan._content_pages_for_section(sec, {}):
            rendered += [u["sentence"] for u in pg["units"]]
    assert not any("合成样例" in s for s in rendered), rendered
    assert any("12.8 亿元" in s for s in rendered)


# ---------------------------------------------------------------- 字体三槽
def test_apply_font_slots_writes_ea_and_cs():
    """原生构建器（表格/流程/时间线）的 run 必须写 latin/ea/cs 三槽，
    否则渲染时回退到 Carlito（非 OFL、整稿字体不统一）。"""
    from pptx import Presentation
    from pptx.util import Pt
    from wengao_ppt.layout import tokens
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    tb = slide.shapes.add_textbox(0, 0, 100000, 100000)
    p = tb.text_frame.paragraphs[0]
    r = p.add_run()
    r.text = "营业收入"
    r.font.size = Pt(20)
    tokens.apply_font_slots(r.font, "cjk_zh")
    rPr = r._r.get_or_add_rPr()
    from pptx.oxml.ns import qn
    assert rPr.find(qn("a:latin")).get("typeface") == "Noto Sans SC"
    assert rPr.find(qn("a:ea")).get("typeface") == "Noto Sans SC"
    assert rPr.find(qn("a:cs")).get("typeface") == "Noto Sans"


def test_native_table_and_process_runs_carry_noto_family():
    """表格/流程构建器产出的 run 必须带 Noto 家族（三槽），不是裸字号。"""
    from pptx import Presentation
    from pptx.oxml.ns import qn
    from wengao_ppt.builders import process as P, table as T
    from wengao_ppt.layout import tokens
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    from wengao_ppt.layout import engine
    rect = engine.Grid().rect(0, 12, 0, 1000000)
    T.add_native_table(slide, rect, ["指标"], [["营业收入"]], tokens.THEMES["ink"])
    P.add_process(slide, rect, ["需求确认", "方案设计"], tokens.THEMES["ink"])
    xml = slide._element.xml
    assert 'a:ea typeface="Noto Sans SC"' in xml, "ea 槽缺失（会回退 Carlito）"
    assert "Carlito" not in xml


# ---------------------------------------------------------------- 有图成稿
def _png_bytes(path: Path) -> None:
    from PIL import Image
    Image.new("RGB", (320, 180), (40, 80, 140)).save(path)


def test_compose_deck_places_resolved_image_and_checks_clean(tmp_path):
    """有图半场：RESOLVED 槽位必须真的落到图片，check_layout 全过。"""
    from pptx import Presentation
    from wengao_ppt.layout import checks, compose, templates, tokens
    img = tmp_path / "t.png"
    _png_bytes(img)
    resolutions = {"s1": {"status": "RESOLVED_T2", "asset": {
        "path": str(img), "sha256": "0" * 64,
        "attribution_text": "W2 合成测试图", "provider": "w2_local_synthetic"}}}
    prs = Presentation()
    prs.slide_width, prs.slide_height = tokens.CANVAS_W_EMU, tokens.CANVAS_H_EMU
    ctx = templates.TemplateContext(tokens.THEMES["ink"], resolutions,
                                    compose._default_place_image(), None)
    s = prs.slides.add_slide(prs.slide_layouts[6])
    templates.render_page(s, {"role": "image", "variant": "a", "page_id": "p1",
                              "index": 1, "title": "有图页",
                              "blocks": [{"kind": "text", "text": "图注文字。"}],
                              "image_slots": [{"slot_id": "s1",
                                               "scene": {"scene_id": "s1"}}]}, ctx)
    out = tmp_path / "img.pptx"
    prs.save(str(out))
    slide = Presentation(str(out)).slides[0]
    pics = [sh for sh in slide.shapes
            if sh.shape_type is not None and "PICTURE" in str(sh.shape_type)]
    assert len(pics) == 1
    assert pics[0]._element.nvPicPr.cNvPr.get("descr"), "图片缺 alt/署名"
    rep = checks.check_layout(str(out))
    assert rep["pass"], rep["findings"]


def test_t4_fallback_keeps_footnote_trace_and_no_picture(tmp_path):
    """槽位未解析：不放占位图，页脚如实留痕，check_layout 记 INFO 不阻断。"""
    from pptx import Presentation
    from wengao_ppt.layout import checks, templates, tokens
    prs = Presentation()
    prs.slide_width, prs.slide_height = tokens.CANVAS_W_EMU, tokens.CANVAS_H_EMU
    ctx = templates.TemplateContext(tokens.THEMES["ink"], {},
                                    lambda *a, **k: None, None)
    s = prs.slides.add_slide(prs.slide_layouts[6])
    rep = templates.render_page(s, {"role": "content", "variant": "a",
                                    "page_id": "p1", "index": 1, "title": "降级页",
                                    "blocks": [{"kind": "bullets",
                                                "items": ["要点一"]}],
                                    "image_slots": [{"slot_id": "s9",
                                                     "scene": {"scene_id": "s9"}}]},
                                ctx)
    assert rep["mode"] == "content_single"
    out = tmp_path / "t4.pptx"
    prs.save(str(out))
    slide = Presentation(str(out)).slides[0]
    pics = [sh for sh in slide.shapes
            if sh.shape_type is not None and "PICTURE" in str(sh.shape_type)]
    assert not pics, "T4 降级不得放占位图"
    assert any("本页未配图" in (sh.text_frame.text if sh.has_text_frame else "")
               for sh in slide.shapes), "T4 降级必须页脚留痕"
    chk = checks.check_layout(str(out))
    assert chk["pass"], chk["findings"]
    assert any(f["code"] == "LAYOUT.IMAGE_SLOT_EMPTY" for f in chk["findings"])


def test_check_layout_flags_picture_without_alt(tmp_path):
    """无 alt 的图片必须被记 WARN（署名不可见）。"""
    from pptx import Presentation
    from wengao_ppt.layout import checks, tokens
    img = tmp_path / "noalt.png"
    _png_bytes(img)
    prs = Presentation()
    prs.slide_width, prs.slide_height = tokens.CANVAS_W_EMU, tokens.CANVAS_H_EMU
    s = prs.slides.add_slide(prs.slide_layouts[6])
    s.shapes.add_picture(str(img), 914400, 914400, 1828800, 1028700)
    out = tmp_path / "noalt.pptx"
    prs.save(str(out))
    chk = checks.check_layout(str(out))
    assert any(f["code"] == "LAYOUT.IMAGE_NO_ALT" for f in chk["findings"])
