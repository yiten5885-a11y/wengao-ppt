"""wengao_ppt.pipeline —— W4 管线编排层。

公开接口：
- GeneratePipeline：生成模式主管线（read → research → plan → images → compose
  → transition → render+checks），C2 事件流，C8 降级/阻断语义。
- modules：契约模块注册表（真实优先、诚实桩兜底）。
- options / decisions / metrics：JobOptions、冲突裁决、出稿率与场景覆盖率记账。
"""
from wengao_ppt.pipeline.generate import GeneratePipeline  # noqa: F401

__all__ = ["GeneratePipeline"]
