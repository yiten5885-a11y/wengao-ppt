"""tests/research/test_privacy_and_plan.py

验证规划模块隐私红线：
- 检索词只含中性实体词/指标词
- 严禁外发原文段落
- 提取用户自身事实
"""

from unittest.mock import patch
import pytest
from wengao_ppt.research.plan import extract_user_material_facts, plan_research_queries, sanitize_neutral_query
from wengao_ppt.research.types import Fact


def test_sanitize_neutral_query():
    raw = "我们公司在2024年实现了巨大的商业成功！请看详细报告：利润暴涨了50%。"
    sanitized = sanitize_neutral_query(raw)
    assert "！" not in sanitized
    assert "。" not in sanitized
    assert len(sanitized.split()) <= 8


def test_privacy_no_user_paragraph_leaked_with_mock():
    sensitive_material = (
        "内部机密：保密项目X在华东区域的销售额突破了12.5亿元，同比增长45%。"
        "我们的CEO张总在内部会议上强调要严守代码与财务秘密，绝不可对外披露研发代号Alpha2024。"
    )

    mock_llm_output = {
        "topic": "华东区域商业销售",
        "entities": ["保密项目X"],
        "metrics": ["销售额", "增长率"],
        "queries_zh": ["华东区域 商业零售 市场规模 2024", "行业均值 销售额 增长率 统计局"],
        "queries_en": ["retail market share East China 2024"],
    }

    with patch("wengao_ppt.research.plan.call_llm_json", return_value=mock_llm_output):
        queries, user_facts = plan_research_queries(sensitive_material, {"language": "zh"}, max_queries=6)

        assert len(queries) > 0
        for q in queries:
            assert "内部机密" not in q.q
            assert "张总在内部会议上强调" not in q.q
            assert len(q.q) < 80

        # 用户事实中应捕获 12.5 亿元
        val_found = any(abs(f.value - 12.5) < 0.01 for f in user_facts)
        assert val_found, "未从用户材料中提取出数值事实 12.5 亿元"
        for uf in user_facts:
            assert uf.origin == "USER_MATERIAL"


def test_fallback_planning_privacy_rule():
    sensitive_material = (
        "内部机密段落：绝密财务数据公布。公司A在2024年实现纯利润3.8亿元。"
    )
    with patch("wengao_ppt.research.plan.call_llm_json", return_value=None):
        queries, user_facts = plan_research_queries(sensitive_material, {"language": "zh"}, max_queries=4)
        for q in queries:
            assert "内部机密段落" not in q.q
            assert "绝密财务数据公布" not in q.q
