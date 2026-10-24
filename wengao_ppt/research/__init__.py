"""wengao_ppt.research

联网研究模块（W3 开发窗口独占）：
- run_research: 执行联网研究、抓取、抽取、核对与视图组装
- verify_bundle: 只读磁盘独立重算校验器，供门禁脚本复核
- 类型定义与数据契约完全对齐 rs-contracts-v1
"""

from __future__ import annotations

from wengao_ppt.research.runner import run_research
from wengao_ppt.research.types import Fact, Highlight, OmissionRecord, ResearchBundle, Series, SeriesPoint, View
from wengao_ppt.research.verify import verify_bundle

__all__ = [
    "run_research",
    "verify_bundle",
    "ResearchBundle",
    "Fact",
    "View",
    "Series",
    "SeriesPoint",
    "Highlight",
    "OmissionRecord",
]
