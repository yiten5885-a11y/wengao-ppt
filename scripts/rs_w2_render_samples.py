"""W2 样张渲染：12 页型 × 2 变体 + 6 文字样张，全部走真实 LibreOffice 渲染。

产出（默认落在 <repo>/reports/rs-w2/）：
- page_types.pptx / page_types.pdf / page_types/png/page-*.png   24 页样张
- scripts_6.pptx / scripts_6.pdf / scripts_6/png/page-*.png      6 页多文字样张
- render_report.json                                           页数/字号/字体嵌入/检查结论

用法：
    .venv/bin/python scripts/rs_w2_render_samples.py [--out DIR] [--dpi 110]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pptx import Presentation  # noqa: E402

from wengao_ppt.layout import checks, compose, engine, templates, tokens  # noqa: E402
from wengao_ppt import render as R  # noqa: E402

# 12 页型（与 templates.ROLE_FN 的键一致）
ROLES = ["cover", "section", "content", "compare", "trend", "insight", "image",
         "process", "timeline", "table", "quote", "closing"]

#: 6 文字样张：每页同一结构、不同文字，用来验证字体栈与字形
SCRIPT_SAMPLES = [
    ("zh", "中文样张", "营业收入 3.2 亿元，同比增长 18%；覆盖 12 个行业。"),
    ("ja", "日本語サンプル", "売上高は 32 億円、前年比 18% 増。12 業界をカバー。"),
    ("latin", "Latin Sample", "Revenue 3.2B, up 18% YoY across 12 industries."),
    ("cyrillic", "Кириллица", "Выручка 3,2 млрд, рост 18% год к году, 12 отраслей."),
    ("arabic", "نموذج عربي", "الإيرادات 3.2 مليار، نمو 18% سنويًا في 12 قطاعًا."),
    ("devanagari", "देवनागरी", "राजस्व 3.2 अरब, वार्षिक वृद्धि 18%, 12 उद्योग।"),
]


def page_spec(role: str, variant: str, index: int, title: str) -> dict:
    """每种页型给一份最小但结构完整的 spec（含 chart/table/process/timeline 原生对象）。"""
    spec = {"role": role, "variant": variant, "page_id": f"{role}-{variant}",
            "index": index, "title": title}
    if role == "cover":
        spec["blocks"] = [{"kind": "text", "text": "稳稿 PPT 版式引擎样张"}]
    elif role == "section":
        spec["blocks"] = [{"kind": "text", "text": "本节说明文字。"}]
    elif role == "content":
        # b 变体给密料：超过单栏实测容量才会真正走双栏，两个变体结构才分得开
        items = ["营业收入 3.2 亿元，同比增长 18%",
                 "核心业务收入 2.1 亿元，占比 65%",
                 "新签订单 46 家客户，覆盖 12 个行业"]
        if variant == "b":
            items = items + [
                "存量客户续约率 92%，较上季度提升 3 个百分点",
                "研发投入 8000 万元，投向智能推荐与数据平台",
                "基础设施成本 3.1 亿元，占总收入的 24%",
                "运维自动化覆盖 78% 的常规巡检任务",
                "多区域部署扩展到 6 个可用区，延迟中位数 34 毫秒",
                "开发者社区注册开发者 27 万人，模板 5400 个",
                "企业版客户平均使用模块数 9.4 个，提升 1.6 个",
                "渠道伙伴 58 家，渠道收入 2.3 亿元占 18%",
                "单位请求成本从 0.021 元下降到 0.013 元",
            ]
        spec["blocks"] = [{"kind": "bullets", "items": items}]
    elif role == "compare":
        # a = 双卡对照；b = 原生条形图 + 右侧洞察栏（两种结构不同的版式）
        if variant == "a":
            spec["blocks"] = [
                {"kind": "bullets", "items": ["公司增速 18%"], "payload": {"side": "left"}},
                {"kind": "bullets", "items": ["行业均值 9%"], "payload": {"side": "right"}}]
        else:
            spec["blocks"] = [
                {"kind": "chart", "chart_kind": "bar",
                 "categories": ["公司", "行业均值"], "series": {"增速": [18, 9]},
                 "h_emu": int(3.6 * tokens.EMU_PER_IN), "data_labels": True},
                {"kind": "bullets", "items": ["高出行业 9 个百分点"]}]
    elif role == "trend":
        # a = 折线整幅；b = 柱图 + 洞察栏
        spec["blocks"] = [{"kind": "chart",
                           "chart_kind": "line" if variant == "a" else "column",
                           "categories": ["2025Q3", "2025Q4", "2026Q1"],
                           "series": {"营业收入": [2.4, 2.7, 3.2]},
                           "h_emu": int(3.2 * tokens.EMU_PER_IN), "legend": False}]
        if variant == "b":
            spec["blocks"].append({"kind": "bullets",
                                   "items": ["连续三个季度环比向上"]})
    elif role == "insight":
        spec["blocks"] = [{"kind": "bignumber", "value": "18%", "label": "同比增速",
                           "note": "高出行业均值 9 个百分点"}]
    elif role == "image":
        spec["blocks"] = [{"kind": "text", "text": "本页样张未配图（T4 文字版式）。"}]
    elif role == "process":
        spec["blocks"] = [{"kind": "process",
                           "steps": ["需求确认", "方案设计", "开发联调", "上线复盘"]}]
    elif role == "timeline":
        spec["blocks"] = [{"kind": "timeline",
                           "milestones": [{"time": "2026Q1", "label": "一期上线"},
                                          {"time": "2026Q2", "label": "二期扩容"},
                                          {"time": "2026Q3", "label": "三期智能化"}]}]
    elif role == "table":
        spec["blocks"] = [{"kind": "table", "headers": ["指标", "数值", "口径"],
                           "rows": [["营业收入", "3.2 亿元", "合并报表"],
                                    ["同比增速", "18%", "同比"],
                                    ["客户数", "46 家", "签约口径"]]}]
    elif role == "quote":
        spec["blocks"] = [{"kind": "quote", "text": "事实只有一种，就是把话说实。"}]
        spec["footnote"] = {"text": "来源：用户材料"}
    elif role == "closing":
        spec["blocks"] = [{"kind": "bullets",
                           "items": ["锁定交付节奏，周会跟踪", "补齐数据口径说明",
                                     "下季度复盘并更新目标"]}]
    return spec


def build_page_types_deck() -> Presentation:
    prs = Presentation()
    prs.slide_width, prs.slide_height = tokens.CANVAS_W_EMU, tokens.CANVAS_H_EMU
    ctx = templates.TemplateContext(tokens.THEMES["ink"], {},
                                    lambda *a, **k: None, None)
    i = 0
    for role in ROLES:
        for variant in ("a", "b"):
            i += 1
            slide = prs.slides.add_slide(prs.slide_layouts[6])
            templates.render_page(slide, page_spec(role, variant, i,
                                                   f"{role} · {variant}"), ctx)
    return prs


def build_scripts_deck() -> Presentation:
    prs = Presentation()
    prs.slide_width, prs.slide_height = tokens.CANVAS_W_EMU, tokens.CANVAS_H_EMU
    ctx = templates.TemplateContext(tokens.THEMES["ink"], {},
                                    lambda *a, **k: None, None)
    for i, (script, title, body) in enumerate(SCRIPT_SAMPLES, start=1):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        templates.render_page(slide, {
            "role": "content", "variant": "a", "page_id": f"script-{script}",
            "index": i, "title": title,
            "blocks": [{"kind": "bullets", "items": [body]}]}, ctx)
    return prs


def render_one(pptx: Path, out_dir: Path, dpi: int, timeout: int) -> dict:
    res = R.render_real(pptx, out_dir, dpi=dpi, timeout=timeout)
    pdf = res.get("pdf") or res.get("pdf_path")
    entry = {"pptx": pptx.name, "pptx_sha256": hashlib.sha256(pptx.read_bytes()).hexdigest(),
             "exit_code": res.get("exit_code"), "timed_out": res.get("timed_out"),
             "pdf": str(pdf) if pdf else None}
    if pdf and Path(pdf).exists():
        fams, err = R.pdf_font_families(pdf)
        entry.update({"pdf_sha256": hashlib.sha256(Path(pdf).read_bytes()).hexdigest(),
                      "font_families": fams, "font_error": err,
                      "pages": R.pdf_page_count(pdf), "cjk_stats": R.pdf_cjk_stats(pdf)})
    pngs = sorted((out_dir / "png").glob("*.png")) if (out_dir / "png").exists() else []
    entry["png_count"] = len(pngs)
    entry["png_sha256"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in pngs}
    return entry


E2E_MATERIAL = """2026 年一季度经营复盘
公司实现营业收入 3.2 亿元，同比增长 18%，环比提升 0.5 亿元。
其中核心业务收入 2.1 亿元，占比 65%，是本期增长的主要来源。
新签订单 46 家客户，覆盖 12 个行业，其中制造业客户 18 家。
存量客户续约率为 92%，较上季度提升 3 个百分点。

二季度重点工作
完成三大能力建设：交付提效、渠道拓展、组织升级。
研发投入计划 8000 万元，重点投向智能推荐与数据平台。
计划新增渠道伙伴 20 家，目标带来 1.2 亿元增量收入。

行业对比
行业平均增速为 9%，公司高出行业 9 个百分点。
主要竞品同期增速为 12%，公司在客户留存上仍具优势。
"""


def build_e2e_deck(out_dir: Path) -> tuple[Presentation, dict]:
    """材料 → plan_deck → compose_deck，走完整规划-成稿链。"""
    from wengao_ppt.planner import deck_plan
    plan = deck_plan.plan_deck(E2E_MATERIAL, None, {},
                               {"language": "zh", "images": {"enabled": False}},
                               str(out_dir / "e2e_job"))
    prs = compose.compose_deck(plan, {}, "ink", str(out_dir / "e2e_deck.pptx"),
                              place_image=None)
    return Presentation(str(out_dir / "e2e_deck.pptx")), plan


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "reports" / "rs-w2"))
    ap.add_argument("--dpi", type=int, default=110)
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    report = {"schema": "rs.w2_render_report.v1", "dpi": args.dpi, "artifacts": []}

    for name, builder in (("page_types", build_page_types_deck),
                          ("scripts_6", build_scripts_deck),
                          ("e2e_deck", lambda: build_e2e_deck(out)[0])):
        prs = builder()
        pptx = out / f"{name}.pptx"
        prs.save(str(pptx))
        chk = checks.check_layout(str(pptx))
        (out / f"{name}.check.json").write_text(
            json.dumps(chk, ensure_ascii=False, indent=1), encoding="utf-8")
        entry = render_one(pptx, out / name, args.dpi, args.timeout)
        entry["check"] = {"pass": chk["pass"], "page_count": chk["page_count"],
                          "findings": chk["findings"],
                          "size_rules_source": chk.get("size_rules_source")}
        report["artifacts"].append(entry)
        print(f"[{name}] pages={chk['page_count']} check_pass={chk['pass']} "
              f"pdf_pages={entry.get('pages')} png={entry['png_count']} "
              f"fonts={entry.get('font_families')}")
        for f in chk["findings"]:
            print("   -", f["code"], "p", f["page"], f["detail"][:100])

    (out / "render_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    def _pages(a):
        p = a.get("pages")
        return p[0] if isinstance(p, (tuple, list)) else p

    ok = all(a["check"]["pass"] and _pages(a) == a["check"]["page_count"]
             and a["png_count"] == a["check"]["page_count"]
             and not a.get("timed_out") for a in report["artifacts"])
    print("ALL_OK" if ok else "HAS_ISSUES")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
