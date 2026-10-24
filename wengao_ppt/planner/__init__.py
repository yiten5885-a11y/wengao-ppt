"""W2-E　规划器：事实台账 · DeckPlan · plan_deck。

- `fact_ledger`：自样例脚本拆出的事实台账（确定性规则，语义不变）；
- `deck_plan`：DeckPlan/PageSpec 数据结构与 plan_deck（契约 C5/C6）。

事实保护语义（不得削弱）：数字只能来自台账或 ResearchBundle.facts；
规划器不生成、不估算、不改写任何数字。
"""
from __future__ import annotations

__all__ = ["fact_ledger", "deck_plan"]
