"""tests/research/test_runner_and_verify.py

测试研究流水线端到端运行与 verify_bundle 独立重算校验器。
"""

import json
import os
import pytest
from unittest.mock import patch
from wengao_ppt.research.runner import run_research
from wengao_ppt.research.search.base import SearchResultItem
from wengao_ppt.research.search.mock import MockSearchBackend
from wengao_ppt.research.types import Fact, QueryRecord
from wengao_ppt.research.verify import verify_bundle


def test_run_research_disabled(tmp_path):
    job_dir = str(tmp_path)
    options = {"research": {"enabled": False}}
    emitted = []

    bundle = run_research("测试内容", options, job_dir, emit=lambda ev: emitted.append(ev))
    assert bundle.enabled is False
    assert len(bundle.facts) == 0
    assert len(bundle.queries) == 0
    # 验证磁盘生成的 bundle.json
    report = verify_bundle(job_dir)
    assert report["is_valid"] is True
    assert report["details"]["status"] == "RESEARCH_DISABLED"


def test_run_research_with_mock_data_and_verify(tmp_path):
    job_dir = str(tmp_path)
    mock_backend = MockSearchBackend()

    # 设置模拟网页响应
    page_html = (
        "<html><body>"
        "<p>国家统计局权威发布：2024年全国规模以上工业企业利润达到 75000 亿元，同比增长 3.5%。</p>"
        "<p>高技术制造业表现突出，继续发挥战略支撑作用。</p>"
        "</body></html>"
    )

    mock_item = SearchResultItem(
        title="国家统计局规模以上工业企业利润数据公报",
        url="https://www.stats.gov.cn/sj/zxfb/202501/t20250127.html",
        snippet="2024年全国规模以上工业企业利润达到 75000 亿元，同比增长 3.5%",
        engine="mock",
    )
    mock_backend.set_results("", [mock_item])

    import wengao_ppt.research.runner as runner_mod
    original_fetch = runner_mod.fetch_url_text
    runner_mod.fetch_url_text = lambda url, timeout_s=8: "国家统计局权威发布：2024年全国规模以上工业企业利润达到 75000 亿元，同比增长 3.5%。高技术制造业表现突出。"

    mock_queries = [QueryRecord(q="工业企业利润 2024 统计局", locale="zh", backend="mock", sent_at="2026-10-24T05:00:00Z")]
    mock_user_facts = [Fact(
        fact_id="uf001", origin="USER_MATERIAL", subject="中国工业", metric="利润",
        value=72000.0, unit="亿元", period="2023", definition="前值", publisher="用户材料",
    )]

    with patch("wengao_ppt.research.runner.plan_research_queries", return_value=(mock_queries, mock_user_facts)):
        try:
            emitted_events = []
            options = {
                "research": {"enabled": True, "max_queries": 2},
                "language": "zh",
            }
            material = "我们关注中国工业经济基本面，2024年工业企业总体表现稳健。"
            bundle = run_research(material, options, job_dir, emit=lambda ev: emitted_events.append(ev), search_backend=mock_backend)

            # 检查事件流按顺序触发
            stages = [e.get("stage") for e in emitted_events]
            assert "research" in stages
            assert "compare" in stages
            assert "trend" in stages

            # 检查证据落盘
            res_dir = os.path.join(job_dir, "evidence", "research")
            assert os.path.isfile(os.path.join(res_dir, "bundle.json"))
            assert os.path.isfile(os.path.join(res_dir, "queries.json"))
            assert os.path.isfile(os.path.join(res_dir, "report.md"))

            # 运行磁盘独立重算 verify_bundle
            report = verify_bundle(job_dir)
            assert report["is_valid"] is True, f"重算失败: {report['issues']}"
            assert report["passed_facts_count"] > 0
        finally:
            runner_mod.fetch_url_text = original_fetch


def test_verify_bundle_tampered_calculation(tmp_path):
    job_dir = str(tmp_path)
    res_dir = os.path.join(job_dir, "evidence", "research")
    os.makedirs(res_dir, exist_ok=True)

    # 构造一个有数学计算错误的 bundle
    malformed_bundle = {
        "schema": "rs.research_bundle.v1",
        "bundle_id": "test-bad-calc",
        "enabled": True,
        "facts": [],
        "views": [
            {
                "view_id": "v1",
                "kind": "comparison",
                "title": "错值测试",
                "metric": "份额",
                "unit": "%",
                "period": "2024",
                "series": [],
                "highlight": {
                    "type": "max_gap",
                    "text": "虚假高出",
                    "fact_refs": [],
                    "rule": "TEST",
                    "computed": {
                        "target_value": 30.0,
                        "benchmark_value": 10.0,
                        "diff": 999.0  # 篡改！实际应为 20.0
                    }
                }
            }
        ]
    }
    with open(os.path.join(res_dir, "bundle.json"), "w", encoding="utf-8") as f:
        json.dump(malformed_bundle, f)

    report = verify_bundle(job_dir)
    assert report["is_valid"] is False
    codes = [i["code"] for i in report["issues"]]
    assert "RESEARCH.CALCULATION_ERROR" in codes
