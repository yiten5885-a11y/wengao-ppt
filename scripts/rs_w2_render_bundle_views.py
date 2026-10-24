#!/usr/bin/env python3
"""scripts/rs_w2_render_bundle_views.py —— P1-1 验收：
针对 ResearchBundle.views（对比/趋势/洞察）的真实样张验证（4 份标准 fixture）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pptx import Presentation
from wengao_ppt import render as R
from wengao_ppt.layout import checks, compose
from wengao_ppt.planner import deck_plan

FIXTURES = [
    {
        "id": "conflict",
        "bundle": ROOT / "fixtures/research/bundle_sample_conflict.json",
        "material": (
            "# Company A 经营概述\n\n"
            "Company A 在企业云基础设施领域的市场份额达到了35.0%，领先竞争对手。\n\n"
            "## 核心战略\n持续加大研发投入，推动产品智能化升级。"
        ),
        "options": {"language": "zh", "theme": "ink"},
    },
    {
        "id": "zh",
        "bundle": ROOT / "fixtures/research/bundle_sample_zh.json",
        "material": (
            "# 2024年新能源汽车市场复盘\n\n"
            "2024Q3国内新能源乘用车市场零售渗透率达到46.8%，较去年同期大幅提升，展现出强劲替代效应。\n\n"
            "## 市场格局\n自主品牌继续扩大领先优势，供应链协同效应显著。"
        ),
        "options": {"language": "zh", "theme": "ink"},
    },
    {
        "id": "en",
        "bundle": ROOT / "fixtures/research/bundle_sample_en.json",
        "material": (
            "# Global Cloud Infrastructure Review\n\n"
            "Company A achieved significant market presence with resilient hybrid cloud deployments.\n\n"
            "## Outlook\nExpanding AI inference platforms across core regions."
        ),
        "options": {"language": "en", "theme": "ink"},
    },
    {
        "id": "empty",
        "bundle": ROOT / "fixtures/research/bundle_sample_empty.json",
        "material": (
            "# 空检索兜底演示\n\n"
            "本材料不含有效联网检索数据，系统应平滑降级，仅呈现材料本身事实。"
        ),
        "options": {"language": "zh", "theme": "ink"},
    },
]


def main() -> int:
    out_dir = ROOT / "reports" / "rs-w2" / "research_views"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {"schema": "rs.research_views_verification.v1", "results": []}

    for item in FIXTURES:
        fid = item["id"]
        bundle = json.loads(item["bundle"].read_text(encoding="utf-8"))
        job_dir = out_dir / f"job_{fid}"
        job_dir.mkdir(parents=True, exist_ok=True)

        plan = deck_plan.plan_deck(item["material"], bundle, {}, item["options"], str(job_dir))
        pptx_path = out_dir / f"bundle_{fid}.pptx"
        comp_rep = compose.compose_deck(plan, [], item["options"].get("theme", "ink"), str(pptx_path))

        chk = checks.check_layout(str(pptx_path))

        # 渲染真实 PDF 与 PNG
        render_res = R.render_real(pptx_path, out_dir / fid, dpi=110, timeout=120)

        entry = {
            "fixture": fid,
            "bundle_id": bundle.get("bundle_id"),
            "pages_in_plan": len(plan.get("pages", [])),
            "views_total": len(bundle.get("views") or []),
            "deck_path": str(pptx_path.relative_to(ROOT)),
            "check_pass": chk["pass"],
            "render_ok": render_res.get("ok"),
            "pdf_path": str(Path(render_res["pdf"]).relative_to(ROOT)) if render_res.get("pdf") else None,
            "png_count": len(list((out_dir / fid / "png").glob("*.png"))) if (out_dir / fid / "png").exists() else 0,
        }
        report["results"].append(entry)
        print(f"[{fid}] plan_pages={entry['pages_in_plan']} views={entry['views_total']} "
              f"check_pass={entry['check_pass']} render_ok={entry['render_ok']} png={entry['png_count']}")

    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n全部 4 份 fixture 验证完成，报告已写至: {out_dir / 'report.json'}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
