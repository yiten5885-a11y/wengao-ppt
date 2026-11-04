#!/usr/bin/env python3
"""HG13 零照片门禁：说明卡兜底必须与 run 阶段按同一口径结账。

背景（2026-11-04 job-624391145aa3）：用户显式勾选说明卡兜底，冻结场景计划
为 5 个规划场景 = 1 个 unplaced_scenes（容量不足，reason=NO_VERIFIED_PHOTO_
SKIPPED）+ 4 张 text_cards（每张带 fallback_reason），unresolved_scenes=0。
run 阶段 scene_stage_verdict 判 resolved（放行），validate 的 HG13 却因
capacity_only 排除 text_cards 判 SCENE.NO_SCENE_PICTURES BLOCKING——同一条
显式开关产出的候选必然过不了验收。

修复：把零照片出口判定收敛为纯函数 scene_no_photo_outcomes，供 HG13 复用。
本文件双向锁定：合法兜底必须放行；真失败、出口原因不合规、场景未被覆盖
仍必须阻断；且 run / validate 两侧口径对无照片计划必须一致。
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from wengao_ppt.runner import scene_no_photo_outcomes, scene_stage_verdict  # noqa: E402
from wengao_ppt.semantic_scenes import CAPACITY_UNRESOLVED_REASONS  # noqa: E402


CAPACITY_REASON = "SCENE.NO_VERIFIED_PHOTO_SKIPPED"


def _plan(scene_keys, *, unplaced=(), text_cards=None, images=None, unresolved=()):
    """构造与冻结场景计划同形状的最小 dict。"""
    return {
        "scenes": [{"scene_key": k} for k in scene_keys],
        "images": dict(images or {}),
        "unplaced_scenes": list(unplaced),
        "text_cards": {} if text_cards is None else text_cards,
        "unresolved_scenes": list(unresolved),
    }


def _real_job_plan():
    """复刻 job-624391145aa3 的出口形状（1 unplaced + 4 text_cards）。"""
    keys = ["player_tapping_generator", "player_receiving_reward",
            "player_spending_diamonds", "designer_typing_font_specs",
            "designer_reviewing_ui_mockup"]
    return _plan(
        keys,
        unplaced=[{"scene_key": "designer_typing_font_specs",
                   "reason": CAPACITY_REASON,
                   "original_reason": "SCENE.GEN_IMAGE_UNAVAILABLE"}],
        text_cards={
            "player_tapping_generator": {"fallback_reason": "SCENE.GEN_IMAGE_UNAVAILABLE"},
            "player_receiving_reward": {"fallback_reason": "SCENE.GEN_IMAGE_UNAVAILABLE"},
            "player_spending_diamonds": {"fallback_reason": "SCENE.GEN_IMAGE_UNAVAILABLE"},
            "designer_reviewing_ui_mockup": {"fallback_reason": "SCENE.IMAGE_CONTEXT_UNVERIFIED"},
        },
    )


class TestSceneNoPhotoOutcomes(unittest.TestCase):

    def test_real_job_shape_is_an_allowed_determined_result(self):
        """真实缺陷形状：1 个容量未放置 + 4 张说明卡兜底，必须 EXECUTED。"""
        plan = _real_job_plan()
        result = scene_no_photo_outcomes(plan)
        self.assertTrue(result["no_photo"])
        self.assertTrue(result["capacity_only"])
        self.assertTrue(result["all_scenes_accounted"])
        self.assertEqual(result["text_card_count"], 4)
        self.assertEqual(result["capacity_count"], 1)
        self.assertEqual(len(result["outcomes"]), 5)
        # run 阶段也必须放行，否则又是"一侧放行一侧拦"。
        self.assertFalse(scene_stage_verdict(plan)["blocks"])

    def test_capacity_only_shape_does_not_regress(self):
        """2026-10-27 修好的纯容量口径必须原样保留。"""
        plan = _plan(["a", "b"], unplaced=[
            {"scene_key": "a", "reason": "SCENE.NO_SAFE_PHOTO_AREA"},
            {"scene_key": "b", "reason": "SCENE.PAGE_SCENE_CAPACITY_EXCEEDED"},
        ])
        result = scene_no_photo_outcomes(plan)
        self.assertTrue(result["capacity_only"])
        self.assertTrue(result["all_scenes_accounted"])
        self.assertEqual(result["text_card_count"], 0)

    def test_unresolved_scenes_still_block(self):
        """真失败（找不到/不合规）绝不因为说明卡存在而放行。"""
        plan = _real_job_plan()
        plan["unresolved_scenes"] = [{"scene_key": "x", "reason": "SCENE.VISUAL_REVIEW_UNAVAILABLE"}]
        result = scene_no_photo_outcomes(plan)
        self.assertFalse(result["capacity_only"])
        self.assertFalse(result["all_scenes_accounted"])
        self.assertTrue(scene_stage_verdict(plan)["blocks"])

    def test_three_empty_exits_still_block(self):
        """没有任何有依据的具体场景，仍然阻断。"""
        result = scene_no_photo_outcomes(_plan([]))
        self.assertFalse(result["capacity_only"])
        self.assertFalse(result["all_scenes_accounted"])

    def test_undisclosed_text_card_is_not_silently_executed(self):
        """说明卡缺 fallback_reason：不得静默伪装成已核验结果。"""
        plan = _real_job_plan()
        plan["text_cards"]["player_tapping_generator"] = {}
        result = scene_no_photo_outcomes(plan)
        self.assertTrue(result["capacity_only"])          # 出口存在，进确定结果分支
        self.assertFalse(result["all_scenes_accounted"])  # 但只能 NOT_EVALUABLE

    def test_uncovered_scene_is_not_accounted(self):
        """有规划场景既无照片也无允许出口，不得判 EXECUTED。"""
        plan = _real_job_plan()
        plan["scenes"].append({"scene_key": "never_resolved"})
        result = scene_no_photo_outcomes(plan)
        self.assertFalse(result["all_scenes_accounted"])

    def test_invalid_capacity_reason_is_not_accounted(self):
        """非容量类 reason 不能冒充"版位放不下"。"""
        plan = _real_job_plan()
        plan["unplaced_scenes"][0]["reason"] = "SCENE.VISUAL_REVIEW_UNAVAILABLE"
        result = scene_no_photo_outcomes(plan)
        self.assertFalse(result["all_scenes_accounted"])
        self.assertIn(CAPACITY_REASON, CAPACITY_UNRESOLVED_REASONS)

    def test_images_present_never_enters_no_photo_branch(self):
        """有照片时走完整配对渲染验证，零照片分支不得介入。"""
        plan = _real_job_plan()
        plan["images"] = {"player_tapping_generator": {"scene_key": "player_tapping_generator"}}
        result = scene_no_photo_outcomes(plan)
        self.assertFalse(result["no_photo"])
        self.assertFalse(result["capacity_only"])

    def test_text_cards_as_list_is_tolerated(self):
        """text_cards 历史/外部形状为 list 时不崩，且按同一纪律校验。"""
        plan = _plan(["a"], text_cards=[
            {"scene_key": "a", "fallback_reason": "SCENE.GEN_IMAGE_UNAVAILABLE"}])
        self.assertTrue(scene_no_photo_outcomes(plan)["all_scenes_accounted"])

    def test_run_and_validate_agree_on_no_photo_matrix(self):
        """结构性不变量：无照片计划上，run 的 blocks 与 validate 的出口判定互补。"""
        matrix = [
            _real_job_plan(),
            _plan(["a"], unplaced=[{"scene_key": "a", "reason": CAPACITY_REASON}]),
            _plan(["a"], text_cards={"a": {"fallback_reason": "SCENE.GEN_IMAGE_UNAVAILABLE"}}),
            _plan(["a"]),
            _plan(["a"], text_cards={"a": {}}),
            _plan(["a"], unplaced=[{"scene_key": "a", "reason": CAPACITY_REASON}],
                  unresolved=[{"scene_key": "a"}]),
        ]
        for plan in matrix:
            with self.subTest(plan=plan):
                result = scene_no_photo_outcomes(plan)
                self.assertTrue(result["no_photo"])
                self.assertEqual(scene_stage_verdict(plan)["blocks"],
                                 not result["capacity_only"])


if __name__ == "__main__":
    unittest.main()
