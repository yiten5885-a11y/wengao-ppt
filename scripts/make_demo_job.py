#!/usr/bin/env python3
"""scripts/make_demo_job.py —— 生成界面走查用的演示任务（证据完整、明确标注演示）。

用途：RS-9 验收要"真实浏览器逐状态点击验证并留截图"。本脚本用真实管线跑一份
演示任务（事件流/计划/账本/检查全部真实落盘），其中：
- 研究用离线替身（含 1 个待确认冲突、1 个对比视图）；
- 规划器注入 3 个图片槽（演示 T4 降级与配图标签）；
- 其余全部走 pipeline 真实代码路径。

任务 ID 带 `-demo` 标记，走查报告里如实说明"示例数据，非真实交付"。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wengao_ppt.pipeline import modules as mod  # noqa: E402
from wengao_ppt.pipeline.generate import GeneratePipeline  # noqa: E402
from wengao_ppt.pipeline.options import normalize  # noqa: E402

JOB_ID = "job-demo-walkthrough"


def fake_research(material_text, options, job_dir, emit):
    emit({"stage": "research", "status": "running", "summary": "正在规划行业检索词"})
    return {
        "schema": "rs.research_bundle.v1", "bundle_id": "rb-demo",
        "created_at": "2026-10-24T05:00:00+08:00", "material_language": "zh",
        "enabled": True,
        "queries": [{"q": "retail industry repurchase rate 2026", "locale": "en",
                     "backend": "searxng", "sent_at": "2026-10-24T04:59:00+08:00"}],
        "facts": [
            {"fact_id": "f001", "origin": "WEB_RESEARCH", "subject": "零售行业",
             "metric": "行业平均复购率", "value": 37.4, "unit": "%", "period": "2026Q3",
             "definition": "当季二次购买会员占活跃会员比例", "publisher": "行业协会（示例）",
             "source_grade": "A", "url": "https://example.org/report-2026q3",
             "retrieved_at": "2026-10-24T04:59:30+08:00",
             "quote_span": "行业平均复购率为 37.4%，较上年同期提高 0.4 个百分点",
             "snapshot_path": "fixtures/research/snapshot_f001.txt",
             "snapshot_sha256": "0" * 64},
            {"fact_id": "f000", "origin": "USER_MATERIAL", "subject": "云帆零售",
             "metric": "复购率", "value": 46.8, "unit": "%", "period": "2026Q3",
             "definition": "会员复购率", "publisher": "用户材料",
             "url": "", "retrieved_at": "2026-10-24T04:58:00+08:00",
             "quote_span": "复购率 46.8%，连续 8 个季度提升",
             "snapshot_path": "evidence/research/snapshots/user_material.txt",
             "snapshot_sha256": "1" * 64},
        ],
        "views": [{
            "view_id": "v01", "kind": "comparison", "title": "本季度复购率横向对比",
            "metric": "复购率", "unit": "%", "period": "2026Q3",
            "series": [
                {"label": "云帆零售", "origin": "USER_MATERIAL",
                 "points": [{"x": "2026Q3", "value": 46.8, "fact_ref": "f000"}]},
                {"label": "行业均值", "origin": "WEB_RESEARCH",
                 "points": [{"x": "2026Q3", "value": 37.4, "fact_ref": "f001"}]},
            ],
            "highlight": {"type": "max_gap", "text": "高于行业均值 9.4 个百分点",
                          "fact_refs": ["f000", "f001"], "rule": "max_gap",
                          "computed": {"gap": 9.4}},
            "conflicts": [{"conflict_id": "c01", "user_fact": "f000", "web_fact": "f001"}],
            "footnote": {"text": "来源：行业协会 2026 Q3 季度报告，检索于 2026-10-24",
                         "fact_refs": ["f001"]},
        }],
        "discarded": [{"code": "RESEARCH.QUOTE_NOT_IN_SNAPSHOT",
                       "detail": "引用在快照中未逐字出现", "url": "https://example.org/x"}],
        "omissions": [], "report_path": "evidence/research/report.md",
    }


def demo_plan(material, bundle, decisions, options, job_dir, emit):
    """带 3 个图片槽的演示计划（演示 T4 降级）。"""
    plan = {
        "schema": "rs.deck_plan.v1", "deck_id": "deck-demo", "language": "zh",
        "mode": "generate",
        "pages": [
            {"page_id": "p01", "index": 1, "role": "cover", "page_type": "cover",
             "variant": "a", "density": "standard", "title": "云帆零售 · Q3 复盘",
             "blocks": [{"kind": "text", "text": "2026 年第三季度经营复盘"}],
             "image_slots": [], "footnote": None, "research_view_ref": None},
            {"page_id": "p03", "index": 3, "role": "image", "page_type": "image",
             "variant": "a", "density": "standard", "title": "门店场景",
             "blocks": [{"kind": "text", "text": "门店收银台与顾客选购"}],
             "image_slots": [{"slot_id": "p03-img1", "aspect": "16:9",
                              "scene": {"schema": "rs.scene_request.v1",
                                        "scene_id": "p03-s1", "page_id": "p03",
                                        "slot_id": "p03-img1",
                                        "visual_query": "retail store checkout counter",
                                        "anchors": {"subject": "checkout counter",
                                                    "action": "customer paying",
                                                    "setting": "retail store"},
                                        "language": "zh", "aspect": "16:9",
                                        "orientation": "landscape",
                                        "requires_real_photo": False,
                                        "user_image": {"path": None, "sha256": None},
                                        "exclude_sha256": []}}],
             "footnote": None, "research_view_ref": None},
            {"page_id": "p06", "index": 6, "role": "insight", "page_type": "insight",
             "variant": "a", "density": "standard", "title": "复购率连续 8 个季度领先行业均值",
             "blocks": [{"kind": "bignumber", "value": 46.8, "unit": "%"},
                        {"kind": "chart", "view_ref": "v01"}],
             "image_slots": [], "footnote": {"text": "来源：行业协会 2026 Q3 季度报告，检索于 2026-10-24",
                                            "fact_refs": ["f001"]},
             "research_view_ref": "v01"},
        ],
    }
    emit({"stage": "layout", "status": "running", "summary": "正在规划页面"})
    return plan


def main() -> int:
    shutil.rmtree(ROOT / "jobs" / JOB_ID, ignore_errors=True)
    mod._cache["run_research"] = (fake_research, "demo-fake")
    mod._cache["verify_bundle"] = (lambda jd: {"status": "NOT_EVALUABLE",
                                               "reason": "演示任务不跑真实校验"}, "demo-fake")
    mod._cache["plan_deck"] = (demo_plan, "demo-fake")
    pipe = GeneratePipeline(ROOT, normalize({"mode": "generate"}))
    res = pipe.run(
        "云帆零售 Q3 经营复盘\n\n复购率 46.8%，连续 8 个季度提升。门店扩展到 120 家，"
        "会员规模达到 85 万。本季度上线新的社群运营体系，覆盖 60 个城市。",
        job_id=JOB_ID)
    print("status:", res["status"])
    print("deck:", res.get("deck_pptx"))
    print("job_dir:", res["job_dir"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
