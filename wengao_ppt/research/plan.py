"""wengao_ppt.research.plan

研究问题规划模块：
- 从用户输入材料中提取主题、核心实体、数字主张与时间范围
- 生成横向对比（同行/行业均值）与纵向趋势（历史走势）检索问题
- 隐私硬规则：检索词只允许为中性实体词与指标词，绝对严禁外发用户原文段落！
- 生成中英双语检索词
"""

from __future__ import annotations

import datetime
import logging
import re
from typing import Any

from wengao_ppt.research.llm import call_llm_json
from wengao_ppt.research.types import Fact, QueryRecord

logger = logging.getLogger(__name__)


def sanitize_neutral_query(query: str) -> str:
    """清理并确保检索词中性化，移除长句、语气词及潜在段落。"""
    q = re.sub(r"[\r\n\t]+", " ", query).strip()
    # 去除句号、感叹号、问号等段落特征
    q = re.sub(r"[。！？!?]+", " ", q)
    # 限制单条检索词长度，中性实体词/指标词一般在 60 字符以内
    tokens = q.split()
    if len(tokens) > 8:
        tokens = tokens[:8]
    return " ".join(tokens)


def extract_user_material_facts(material_text: str, material_language: str = "zh") -> list[Fact]:
    """从用户材料中抽取数字事实，标记为 origin='USER_MATERIAL'。"""
    facts: list[Fact] = []
    # 规则抽取：识别包含百分比、数值的句子
    sentences = re.split(r"[。！？\n]+", material_text)
    fact_counter = 1

    for s in sentences:
        s = s.strip()
        if not s:
            continue
        # 查找数字及百分比或单位
        matches = list(re.finditer(r"([A-Za-z0-9\u4e00-\u9fa5]{2,15})[^\d]{0,8}?(\d+(?:\.\d+)?)\s*(%|个百分点|亿元|万元|万|亿|美元|台|件|名|倍)?", s))
        for m in matches:
            subj_candidate = m.group(1)
            val_str = m.group(2)
            unit_str = m.group(3) or ""
            val = float(val_str)
            # 过滤年份如 2024, 2025 作为纯数值的情况
            if val >= 1990 and val <= 2035 and unit_str == "":
                continue

            # 抽取时间标签
            period_match = re.search(r"(20\d\d(?:年|Q[1-4]|季度)?|去年|今年|同期)", s)
            period = period_match.group(1) if period_match else "当前"

            fid = f"uf{fact_counter:03d}"
            fact_counter += 1
            facts.append(
                Fact(
                    fact_id=fid,
                    origin="USER_MATERIAL",
                    subject=subj_candidate,
                    metric=subj_candidate,
                    value=val,
                    unit=unit_str,
                    period=period,
                    definition="用户材料陈述",
                    publisher="用户材料",
                    source_grade="A",
                    url="",
                    retrieved_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    quote_span=s[:100],
                    snapshot_path="",
                    snapshot_sha256="",
                )
            )
            if len(facts) >= 10:
                break
        if len(facts) >= 10:
            break

    return facts


PLAN_SYSTEM_PROMPT = """你是一个严谨的行业研究助手。你的任务是从用户的商业/科技/行业材料中抽取：
1. 核心主题 (topic)
2. 核心实体或公司 (entities)
3. 关键指标 (metrics)
4. 横向研究检索词 (horizontal_queries): 针对行业均值、竞品、同行对比的中性中英文短语
5. 纵向研究检索词 (vertical_queries): 针对历史数据、近年趋势的中性中英文短语

【极其重要的安全与隐私红线】：
- 外发的检索词只能包含【中性实体词】和【行业/财务指标词】（如 "Tesla gross margin 2024 2025", "比亚迪 新能源汽车 渗透率 行业均值 2024"）
- 严禁包含用户原文长句、隐私段落、内部机密！
- 检索词必须言简意赅，只含中性词。

请以 JSON 格式输出：
{
  "topic": "...",
  "entities": ["..."],
  "metrics": ["..."],
  "queries_zh": ["..."],
  "queries_en": ["..."]
}"""


def plan_research_queries(
    material_text: str,
    options: dict[str, Any],
    max_queries: int = 8,
) -> tuple[list[QueryRecord], list[Fact]]:
    """生成合规的研究查询词列表与初始用户事实。"""
    user_facts = extract_user_material_facts(material_text)

    # 尝试使用本地 LLM 解析规划
    queries: list[QueryRecord] = []
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

    prompt = f"请针对以下用户材料，抽取核心实体并规划中性检索词：\n\n{material_text[:800]}"
    llm_res = call_llm_json(prompt, system_prompt=PLAN_SYSTEM_PROMPT, timeout_s=12, num_predict=512)

    if llm_res and isinstance(llm_res, dict):
        zh_qs = llm_res.get("queries_zh", [])
        en_qs = llm_res.get("queries_en", [])
        for q in zh_qs:
            sq = sanitize_neutral_query(str(q))
            if sq and len(queries) < max_queries:
                queries.append(QueryRecord(q=sq, locale="zh", backend="searxng", sent_at=now_iso))
        for q in en_qs:
            sq = sanitize_neutral_query(str(q))
            if sq and len(queries) < max_queries:
                queries.append(QueryRecord(q=sq, locale="en", backend="searxng", sent_at=now_iso))

    # 兜底生成（如 LLM 未响应或为空）：从用户事实中提炼中性词
    if not queries:
        subjects = list(dict.fromkeys(f.subject for f in user_facts))[:3]
        if not subjects:
            # 从首句中抽取可能的主题词
            words = [w for w in re.split(r"\W+", material_text[:200]) if len(w) >= 2][:4]
            subjects = [" ".join(words[:2])] if words else ["行业市场"]

        for s in subjects:
            queries.append(QueryRecord(q=sanitize_neutral_query(f"{s} 行业均值 市场规模"), locale="zh", backend="searxng", sent_at=now_iso))
            queries.append(QueryRecord(q=sanitize_neutral_query(f"{s} market share industry benchmark"), locale="en", backend="searxng", sent_at=now_iso))
            queries.append(QueryRecord(q=sanitize_neutral_query(f"{s} 历史走势 增长率 统计"), locale="zh", backend="searxng", sent_at=now_iso))

    return queries[:max_queries], user_facts
