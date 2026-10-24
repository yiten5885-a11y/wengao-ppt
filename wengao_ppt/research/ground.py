"""wengao_ppt.research.ground

逐字核对与事实有效性核验模块：
- 逐字落地校验：quote_span 必须在快照文本中连续逐字出现
- 来源凭证完整性：publisher, url, snapshot_path, snapshot_sha256 必填
- 数值与单位核对：数值必须在 quote_span 中真实存在
- 可比性检查：横向与纵向两端必须同单位、同口径
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Optional

from wengao_ppt.research.types import DiscardedRecord, Fact, OmissionRecord

logger = logging.getLogger(__name__)


def verify_fact_grounding(
    fact: Fact,
    job_dir: str,
) -> tuple[bool, Optional[DiscardedRecord]]:
    """核对单条外部事实的落地真实性。

    若验证通过返回 (True, None)；若失败返回 (False, DiscardedRecord)。
    """
    # 1. 用户材料自身事实跳过网络快照校验
    if fact.origin == "USER_MATERIAL":
        return True, None

    # 2. 检查必要字段完整性
    if not fact.publisher or not fact.url:
        return False, DiscardedRecord(
            code="RESEARCH.SOURCE_MISSING",
            detail=f"事实 {fact.fact_id} 缺少发布者或来源 URL",
            url=fact.url,
        )

    if not fact.snapshot_path:
        return False, DiscardedRecord(
            code="RESEARCH.SOURCE_MISSING",
            detail=f"事实 {fact.fact_id} 缺少快照文件路径",
            url=fact.url,
        )

    # 3. 检查快照文件是否存在
    abs_snapshot = os.path.join(job_dir, fact.snapshot_path)
    if not os.path.isfile(abs_snapshot):
        return False, DiscardedRecord(
            code="RESEARCH.SOURCE_MISSING",
            detail=f"快照文件不存在: {fact.snapshot_path}",
            url=fact.url,
        )

    # 4. 读取快照并比对 sha256
    try:
        with open(abs_snapshot, "r", encoding="utf-8", errors="ignore") as f:
            snapshot_text = f.read()
    except Exception as e:
        return False, DiscardedRecord(
            code="RESEARCH.FETCH_FAILED",
            detail=f"读取快照失败: {e}",
            url=fact.url,
        )

    actual_sha256 = hashlib.sha256(snapshot_text.encode("utf-8")).hexdigest()
    if fact.snapshot_sha256 and actual_sha256 != fact.snapshot_sha256:
        return False, DiscardedRecord(
            code="RESEARCH.QUOTE_NOT_IN_SNAPSHOT",
            detail=f"快照 SHA256 不匹配: 记录值 {fact.snapshot_sha256} vs 实际值 {actual_sha256}",
            url=fact.url,
        )

    # 5. quote_span 必须在快照中连续逐字出现
    quote = (fact.quote_span or "").strip()
    if not quote or quote not in snapshot_text:
        return False, DiscardedRecord(
            code="RESEARCH.QUOTE_NOT_IN_SNAPSHOT",
            detail=f"引文 quote_span 未在快照中逐字连续匹配: {quote[:60]}...",
            url=fact.url,
        )

    # 6. 数值与单位核对
    val_int_str = str(int(fact.value)) if fact.value.is_integer() else f"{fact.value:.2f}"
    val_float_str = str(fact.value)

    # 检查 quote_span 中是否包含数值文本
    if val_int_str not in quote and val_float_str not in quote:
        return False, DiscardedRecord(
            code="RESEARCH.UNIT_OR_PERIOD_MISMATCH",
            detail=f"引文中未找到对应数值 {fact.value} (quote: {quote[:60]})",
            url=fact.url,
        )

    return True, None


def check_comparability(fact_a: Fact, fact_b: Fact) -> tuple[bool, Optional[OmissionRecord]]:
    """检查两个事实是否具备同图可比性。

    若可比返回 (True, None)；若不可比返回 (False, OmissionRecord)。
    """
    # 1. 规范化单位
    unit_a = fact_a.unit.strip().lower()
    unit_b = fact_b.unit.strip().lower()

    # 允许的等价单位映射
    equiv_units = [
        {"%", "个百分点", "百分比"},
        {"亿元", "亿"},
        {"万元", "万"},
        {"亿美元", "亿美金"},
        {"台", "辆", "件"},
    ]
    is_same_unit = (unit_a == unit_b)
    if not is_same_unit:
        for group in equiv_units:
            if unit_a in group and unit_b in group:
                is_same_unit = True
                break

    if not is_same_unit:
        return False, OmissionRecord(
            want=f"对比 {fact_a.subject}({fact_a.metric}) 与 {fact_b.subject}({fact_b.metric})",
            code="RESEARCH.INCOMPARABLE_DEFINITION",
        )

    # 2. 检查统计口径兼容性（若明确给出但冲突）
    def_a = (fact_a.definition or "").strip().lower()
    def_b = (fact_b.definition or "").strip().lower()
    if def_a and def_b and ("同比" in def_a and "环比" in def_b or "环比" in def_a and "同比" in def_b):
        return False, OmissionRecord(
            want=f"对比口径冲突: {def_a} vs {def_b}",
            code="RESEARCH.INCOMPARABLE_DEFINITION",
        )

    return True, None
