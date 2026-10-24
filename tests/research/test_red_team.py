"""tests/research/test_red_team.py

红队用例自动化验证（任务书 §3-RS-3 与公共契约硬约束）：
1. 伪造快照中不存在的数字/引文 -> 被阻断拒绝
2. 快照 SHA-256 篡改 -> 校验器拒绝
3. 来源信息缺失 -> 阻断
4. 口径/单位不可比 -> 拒绝同图并记录 omission
5. 用户材料与网络冲突 -> 并列呈现标待确认，不覆盖用户事实
6. 检索空结果 -> 对应视图省略，记录 omission，永远不出异常
"""

import hashlib
import json
import os
import pytest
from wengao_ppt.research.compare import assemble_views, detect_conflicts
from wengao_ppt.research.ground import check_comparability, verify_fact_grounding
from wengao_ppt.research.runner import run_research
from wengao_ppt.research.search.mock import MockSearchBackend
from wengao_ppt.research.types import Fact, ResearchBundle
from wengao_ppt.research.verify import verify_bundle


def test_redteam_fake_quote_not_in_snapshot(tmp_path):
    job_dir = str(tmp_path)
    snap_rel = "evidence/research/snapshots/f999.txt"
    abs_snap = os.path.join(job_dir, snap_rel)
    os.makedirs(os.path.dirname(abs_snap), exist_ok=True)

    true_content = "权威公告：本季度实现实际销售额为 5000 万元人民币。"
    with open(abs_snap, "w", encoding="utf-8") as f:
        f.write(true_content)
    true_sha = hashlib.sha256(true_content.encode("utf-8")).hexdigest()

    # 试图伪造数字 9999 万元
    fake_fact = Fact(
        fact_id="f999",
        origin="WEB_RESEARCH",
        subject="测试主体",
        metric="销售额",
        value=9999.0,
        unit="万元",
        period="2024Q1",
        publisher="权威机构",
        url="https://example.com/sec",
        quote_span="本季度实现实际销售额为 9999 万元人民币。",
        snapshot_path=snap_rel,
        snapshot_sha256=true_sha,
    )

    passed, disc = verify_fact_grounding(fake_fact, job_dir)
    assert passed is False
    assert disc is not None
    assert disc.code == "RESEARCH.QUOTE_NOT_IN_SNAPSHOT"


def test_redteam_snapshot_tampered(tmp_path):
    job_dir = str(tmp_path)
    snap_rel = "evidence/research/snapshots/f888.txt"
    abs_snap = os.path.join(job_dir, snap_rel)
    os.makedirs(os.path.dirname(abs_snap), exist_ok=True)

    original_text = "乘用车销量达到 100 万辆。"
    with open(abs_snap, "w", encoding="utf-8") as f:
        f.write("恶意篡改后的文件内容！！！")

    true_sha = hashlib.sha256(original_text.encode("utf-8")).hexdigest()

    fact = Fact(
        fact_id="f888",
        origin="WEB_RESEARCH",
        subject="乘用车",
        metric="销量",
        value=100.0,
        unit="万辆",
        period="2024",
        publisher="统计机构",
        url="https://example.com/stats",
        quote_span="乘用车销量达到 100 万辆。",
        snapshot_path=snap_rel,
        snapshot_sha256=true_sha,  # 哈希对不上
    )

    passed, disc = verify_fact_grounding(fact, job_dir)
    assert passed is False
    assert disc is not None
    assert disc.code == "RESEARCH.QUOTE_NOT_IN_SNAPSHOT"


def test_redteam_source_missing(tmp_path):
    job_dir = str(tmp_path)
    fact = Fact(
        fact_id="f777",
        origin="WEB_RESEARCH",
        subject="测试",
        metric="指标",
        value=12.0,
        unit="%",
        period="2024",
        publisher="",  # 缺来源机构
        url="",        # 缺 URL
        quote_span="指标为 12.0%",
        snapshot_path="",
    )
    passed, disc = verify_fact_grounding(fact, job_dir)
    assert passed is False
    assert disc.code == "RESEARCH.SOURCE_MISSING"


def test_redteam_incomparable_metrics():
    # 同比 vs 环比 口径不可比
    fa = Fact(fact_id="fa", origin="WEB_RESEARCH", subject="A", metric="增速", value=10.0, unit="%", period="2024Q1", definition="同比增速")
    fb = Fact(fact_id="fb", origin="WEB_RESEARCH", subject="B", metric="增速", value=12.0, unit="%", period="2024Q1", definition="环比增速")
    is_comp, omission = check_comparability(fa, fb)
    assert is_comp is False
    assert omission.code == "RESEARCH.INCOMPARABLE_DEFINITION"

    # 单位不同（台 vs 亿元）不可比
    fc = Fact(fact_id="fc", origin="WEB_RESEARCH", subject="C", metric="产量", value=500.0, unit="台", period="2024")
    fd = Fact(fact_id="fd", origin="WEB_RESEARCH", subject="D", metric="产值", value=500.0, unit="亿元", period="2024")
    is_comp2, omission2 = check_comparability(fc, fd)
    assert is_comp2 is False
    assert omission2.code == "RESEARCH.INCOMPARABLE_DEFINITION"


def test_redteam_conflict_handling_parallel_presentation():
    # 用户事实 50% vs 网络事实 32%
    uf = Fact(fact_id="uf1", origin="USER_MATERIAL", subject="某企业", metric="市场份额", value=50.0, unit="%", period="2024")
    wf = Fact(fact_id="wf1", origin="WEB_RESEARCH", subject="某企业", metric="市场份额", value=32.0, unit="%", period="2024")

    conflicts = detect_conflicts([uf], [wf])
    assert len(conflicts) == 1
    assert conflicts[0].user_fact == "uf1"
    assert conflicts[0].web_fact == "wf1"

    views, omissions, conf_list = assemble_views([uf, wf], {})
    # 必须并列呈现，两端都在 series 中，绝不覆盖用户事实
    assert len(views) > 0
    v = views[0]
    series_origins = [s.origin for s in v.series]
    assert "USER_MATERIAL" in series_origins
    assert "WEB_RESEARCH" in series_origins
    assert len(v.conflicts) > 0


def test_redteam_empty_search_results_graceful_degradation(tmp_path):
    job_dir = str(tmp_path)
    mock_backend = MockSearchBackend()  # 空结果

    options = {
        "research": {"enabled": True, "max_queries": 4},
        "language": "zh",
    }
    from unittest.mock import patch
    from wengao_ppt.research.types import QueryRecord
    mock_q = [QueryRecord(q="生僻领域规模 2024", locale="zh", backend="mock", sent_at="2026-10-24T05:00:00Z")]

    with patch("wengao_ppt.research.runner.plan_research_queries", return_value=(mock_q, [])):
        bundle = run_research("测试完全查不到的生僻内容", options, job_dir, search_backend=mock_backend)

        # 必须返回合法 bundle，不抛出异常
        assert isinstance(bundle, ResearchBundle)
        assert bundle.enabled is True
        # 外部事实应为0
        web_facts = [f for f in bundle.facts if f.origin == "WEB_RESEARCH"]
        assert len(web_facts) == 0
        # 应该如实记录 omissions
        assert len(bundle.omissions) > 0
        # report.md 和 queries.json 必须正常生成
        assert os.path.isfile(os.path.join(job_dir, "evidence", "research", "report.md"))
        assert os.path.isfile(os.path.join(job_dir, "evidence", "research", "queries.json"))
