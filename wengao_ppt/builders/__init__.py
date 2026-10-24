"""W2-D　原生构建器：图表 / 表格 / 流程 / 时间线。

边界与依据（W2 指令 §3、§4-D；总览 §7-1）：
- `wengao_ppt/charts.py`、`tables.py` 是**抽取与校验**工具，不是构建器；
  本包新建真正的构建器，写完必须用它们做**回环校验**：建 → 抽 → 核对数据绑定。
- 全部用 python-pptx 原生对象（chart part 带工作簿、graphicFrame 表格、
  AutoShape 流程/时间线），禁止图片冒充。
- 字号用 tokens 硬门禁档（chart_auxiliary 18pt、table 20pt）。
- 图表数据只来自调用方传入的台账 / ResearchBundle 数字；构建器不做任何计算或估算。
"""
from __future__ import annotations

__all__ = ["chart", "table", "process", "timeline"]
