"""wengao_ppt.research.extract

外部事实结构化抽取与来源分级：
- 抽取外部事实必填字段 (subject, metric, value, unit, period, definition, publisher, url, retrieved_at, quote_span)
- 来源分级 (A: 官方/财报/权威协会, B: 主流财经科技媒体, C: 其他)
- 内容农场与无出处聚合站排除
"""

from __future__ import annotations

import datetime
import logging
import re
import urllib.parse
from typing import Any, Optional

from wengao_ppt.research.llm import call_llm_json
from wengao_ppt.research.types import Fact

logger = logging.getLogger(__name__)

# 内容农场与垃圾源黑名单域名
EXCLUDED_DOMAINS = {
    "baijiahao.baidu.com",
    "zhidao.baidu.com",
    "tieba.baidu.com",
    "so.com",
    "bilibili.com",
    "weibo.com",
    "zhihu.com",  # 问答社区非一手数据
    "doc88.com",
    "wenku.baidu.com",
}

# 权威来源 A 级关键字
GRADE_A_KEYWORDS = [
    "统计局", "政府", "部委", "央行", "sec.gov", "证监会", "年报", "财报",
    "公告", "乘联会", "信通院", "gartner", "idc", "canalys", "counterpoint",
    "statista", "worldbank", "imf", "oecd", "官方", "快报", "招股书",
]

# 主流媒体 B 级关键字
GRADE_B_KEYWORDS = [
    "新华社", "人民网", "央视网", "路透社", "reuters", "bloomberg", "wsj",
    "彭博", "财新", "第一财经", "界面", "36氪", "虎嗅", "雪球", "东方财富",
    "cnbc", "forbes", "ft.com", "economist", "techcrunch", "theverge",
]


def classify_source_grade(publisher: str, url: str) -> str:
    """对发布者和 URL 进行来源评级。"""
    text_check = f"{publisher} {url}".lower()
    for kw in GRADE_A_KEYWORDS:
        if kw in text_check:
            return "A"
    for kw in GRADE_B_KEYWORDS:
        if kw in text_check:
            return "B"
    return "C"


def is_excluded_domain(url: str) -> bool:
    """检查是否属于内容农场或低质聚合域名。"""
    try:
        hostname = urllib.parse.urlparse(url).hostname or ""
        hostname = hostname.lower()
        for d in EXCLUDED_DOMAINS:
            if hostname == d or hostname.endswith("." + d):
                return True
    except Exception:
        pass
    return False


FACT_EXTRACT_SYSTEM_PROMPT = """你是一个极其严谨的数据事实抽取助手。你的任务是从抓取的网页正文纯文本中抽取【包含具体数字】的行业/商业关键事实。

【极其重要的纪律要求】：
1. 抽取的每个事实必须包含确切的数字指标（如销售额、市场份额、渗透率、增长率、单价、利润率等）。
2. quote_span 字段必须是从正文中【完全逐字连续复制】的一小段原文（40~120字），必须逐字包含该数字、单位和主体！绝对禁止概括或修改原文字符！
3. 数值 value 必须是纯浮点数（如 46.8, 1250000），单位 unit 必须是单独的字符串（如 "%", "亿元", "台"）。
4. 统计周期 period 必须明确（如 "2024Q3", "2024", "2023"）。

请输出 JSON 列表：
{
  "facts": [
    {
      "subject": "实体/公司名",
      "metric": "指标名",
      "value": 46.8,
      "unit": "%",
      "period": "2024Q3",
      "definition": "统计口径说明",
      "publisher": "发布机构或媒体名称",
      "quote_span": "正文中逐字连续出现且包含该数字的原文片段"
    }
  ]
}"""


def extract_facts_from_snapshot(
    text_content: str,
    source_url: str,
    publisher_hint: str = "",
    start_index: int = 1,
) -> list[Fact]:
    """从页面快照中提取结构化事实。"""
    if is_excluded_domain(source_url):
        logger.info("跳过排除域名来源: %s", source_url)
        return []

    facts: list[Fact] = []
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    grade = classify_source_grade(publisher_hint, source_url)

    # 1. 尝试 LLM 结构化抽取（限制长度并控制超时在 12 秒以内）
    prompt = f"来源URL: {source_url}\n媒体提示: {publisher_hint}\n\n网页正文:\n{text_content[:1000]}"
    res = call_llm_json(prompt, system_prompt=FACT_EXTRACT_SYSTEM_PROMPT, timeout_s=12, num_predict=1024)

    if res and isinstance(res, dict) and "facts" in res:
        raw_facts = res.get("facts", [])
        idx = start_index
        for rf in raw_facts:
            subj = str(rf.get("subject", "")).strip()
            metric = str(rf.get("metric", "")).strip()
            val_raw = rf.get("value")
            unit = str(rf.get("unit", "")).strip()
            period = str(rf.get("period", "")).strip() or "近期"
            definition = str(rf.get("definition", "")).strip() or metric
            pub = str(rf.get("publisher", "")).strip() or publisher_hint or "网络检索"
            quote = str(rf.get("quote_span", "")).strip()

            try:
                val = float(val_raw)
            except (ValueError, TypeError):
                continue

            # 校验必填字段
            if not subj or not metric or not quote:
                continue

            fid = f"f{idx:03d}"
            idx += 1
            facts.append(
                Fact(
                    fact_id=fid,
                    origin="WEB_RESEARCH",
                    subject=subj,
                    metric=metric,
                    value=val,
                    unit=unit,
                    period=period,
                    definition=definition,
                    publisher=pub,
                    source_grade=grade,
                    url=source_url,
                    retrieved_at=now_iso,
                    quote_span=quote,
                )
            )

    # 2. 规则后备抽取（当 LLM 未能返回或超时，确保至少有符合规则的候选事实）
    if not facts:
        # 正则扫描正文中包含数字与常见指标词的连续句子
        sentences = re.split(r"[。\n]+", text_content)
        idx = start_index
        for s in sentences:
            s = s.strip()
            if len(s) < 20 or len(s) > 200:
                continue
            # 寻找数字与单位
            m = re.search(r"([A-Za-z0-9\u4e00-\u9fa5]{2,10})[^\d]{0,10}?(\d+(?:\.\d+)?)\s*(%|亿元|万元|万辆|辆|万|亿美元|美元)?", s)
            if m:
                subj = m.group(1)
                val_str = m.group(2)
                unit = m.group(3) or ""
                try:
                    val = float(val_str)
                except ValueError:
                    continue

                if val >= 1990 and val <= 2035 and unit == "":
                    continue  # 跳过裸年份

                p_m = re.search(r"(20\d\d(?:年|Q[1-4]|季度)?|上半年|前三季度)", s)
                period = p_m.group(1) if p_m else "2024"

                fid = f"f{idx:03d}"
                idx += 1
                facts.append(
                    Fact(
                        fact_id=fid,
                        origin="WEB_RESEARCH",
                        subject=subj,
                        metric=subj,
                        value=val,
                        unit=unit,
                        period=period,
                        definition="公开报道数据",
                        publisher=publisher_hint or "公开网络资料",
                        source_grade=grade,
                        url=source_url,
                        retrieved_at=now_iso,
                        quote_span=s,
                    )
                )
                if len(facts) >= 5:
                    break

    return facts
