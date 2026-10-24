"""W2 版式与成稿：layout 子包。

组成：
- tokens    设计系统 tokens（网格、字号阶梯、主题色、按文字分的字体栈）
- measure   文本测量（宽度/行数/高度估算，用于布局预判）
- engine    约束式版式引擎（网格布局、溢出检测与两栏再平衡）
- templates 12 页型 × ≥2 变体（含有图 / 无图两种版式）
- compose   compose_deck / recompose_slide / insert_slide
- checks    check_layout（从 PPTX 重算的版式硬检查）

边界（见 `00_总览与拼接.md` §2 / W2 指令 §2）：
- 不取图 / 不生图 / 不判图（W1）；不检索 / 不抽外部事实（W3）；
  不做工作台界面与任务编排（W4）。
- `charts.py`、`tables.py`、`source_fidelity.py`、`checks.py` 只读调用；
  构建器新建在 `wengao_ppt/builders/`，用前两者做回环校验。
"""
from __future__ import annotations

__all__ = ["tokens", "measure", "engine", "templates", "compose", "checks"]
