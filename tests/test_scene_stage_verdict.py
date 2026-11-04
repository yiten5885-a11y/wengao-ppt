"""tests/test_scene_stage_verdict.py —— HG13 场景阶段放行判定的口径守卫。

背景（2026-11-04）：视觉链路打通后（reports/vision-enable-20261104/），干净
安装根首任务（3 页中性稿）第一次真正跑完规划：规划器判定三页全部 NO_SCENE，
build_plan 按**硬规则**放 unresolved=SCENE.NO_SCENES_PLANNED（"场景图硬规则
不允许纯文字通过"，防伪造纪律，tests/test_semantic_scenes.py 有专门断言）。
runner.scene_stage_verdict 把口径抽成纯函数；这些测试锁住它——特别记住：
**零场景也必须阻断**（r6 曾短暂改成放行，是错的，已回退）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wengao_ppt.runner import scene_stage_verdict  # noqa: E402


def _plan(**over):
    base = {
        "scenes": [], "images": {}, "text_cards": {},
        "unplaced_scenes": [], "unresolved_scenes": [],
    }
    base.update(over)
    return base


def test_zero_scenes_with_hard_rule_blocker_blocks():
    """整稿无物理场景：build_plan 放 NO_SCENES_PLANNED unresolved → 阻断。

    这是产品硬规则（不允许纯文字通过），r6 曾误改成放行——不许再犯。
    """
    v = scene_stage_verdict(_plan(
        unresolved_scenes=[{"scene_key": None, "reason": "SCENE.NO_SCENES_PLANNED",
                            "detail": "整套稿件没有一个有原文依据的具体场景；"
                                      "场景图硬规则不允许纯文字通过。"}]))
    assert v["blocks"] is True
    assert v["blockers"][0]["reason"] == "SCENE.NO_SCENES_PLANNED"


def test_unresolved_scenes_block():
    """复核不过/找不到合规图：阻断，blockers 如实透传。"""
    blockers = [{"scene_key": "s1", "reason": "VISION_REJECTED"}]
    v = scene_stage_verdict(_plan(scenes=[{"scene_key": "s1"}],
                                  unresolved_scenes=blockers))
    assert v["blocks"] is True
    assert v["blockers"] is blockers


def test_scenes_but_no_images_blocks():
    """有场景但一张图都没有、也没有版位记录：阻断（不出假货）。"""
    v = scene_stage_verdict(_plan(scenes=[{"scene_key": "s1"}]))
    assert v["blocks"] is True
    assert v["blockers"][0]["reason"] == "SCENE.NO_SCENE_IMAGES"


def test_unplaced_only_passes():
    """版位容量不足：如实登记在 unplaced_scenes，不阻断。"""
    v = scene_stage_verdict(_plan(scenes=[{"scene_key": "s1"}],
                                  unplaced_scenes=[{"scene_key": "s1",
                                                    "reason": "CAPACITY"}]))
    assert v["blocks"] is False


def test_resolved_passes():
    """正常放行：有场景、有图。"""
    v = scene_stage_verdict(_plan(scenes=[{"scene_key": "s1"}],
                                  images={"s1": {"page": 1}}))
    assert v["blocks"] is False
    assert v["detail_kind"] == "resolved"


def test_text_cards_alone_passes():
    """兜底说明卡片也是合法交付物（开了 --allow-text-card-fallback 之后）。"""
    v = scene_stage_verdict(_plan(scenes=[{"scene_key": "s1"}],
                                  text_cards={"s1": {"page": 1}}))
    assert v["blocks"] is False
