"""生图失败码必须被"没图可用"政策分类，否则整单会被单个场景拦下。

2026-11-05 job-08c2fe26cd1d：用户已勾选说明卡兜底，5 个场景里 4 个已正常
兜底，仅剩 1 个场景因 ``SCENE.GEN_IMAGE_PERMANENT_FAILURE``（云端生图端点
本次任务永久失败）留在 unresolved，run 阶段被判
``VISUAL.SCENE_UNRESOLVED`` 整单 FAILED。

该码本质是"生图不可用、没有插入任何图"，与已在白名单里的
``GEN_IMAGE_UNAVAILABLE`` / ``GEN_IMAGE_FAILED`` 同类；漏登记等于让一个
基础设施故障冒充内容真实性问题。

这里同时守住结构性不变量：``wengao_ppt`` 里出现的每一个
``SCENE.GEN_IMAGE_*`` 码都必须被 ``NO_PHOTO_FOUND_REASONS`` 覆盖——新增
生图失败码时若忘了分类，测试直接失败，而不是等到线上整单被拦才发现。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from wengao_ppt import semantic_scenes


PKG_DIR = Path(semantic_scenes.__file__).parent
_CODE_RE = re.compile(r'"SCENE\.GEN_IMAGE_[A-Z_]+"')

PERMANENT = "SCENE.GEN_IMAGE_PERMANENT_FAILURE"


def _codes_used_in_package() -> set[str]:
    found: set[str] = set()
    for path in PKG_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        found.update(m.strip('"') for m in _CODE_RE.findall(text))
    return found


class GenImageCodesAreClassifiedTests(unittest.TestCase):
    def test_every_gen_image_code_is_classified(self):
        codes = _codes_used_in_package()
        self.assertTrue(codes, "没有扫描到任何 SCENE.GEN_IMAGE_* 码，扫描逻辑失效")
        unclassified = sorted(c for c in codes
                              if c not in semantic_scenes.NO_PHOTO_FOUND_REASONS)
        self.assertEqual(
            unclassified, [],
            f"这些生图失败码未归入『没图可用』政策，单个场景即可拦下整单：{unclassified}")

    def test_permanent_failure_is_in_the_policy(self):
        self.assertIn(PERMANENT, semantic_scenes.NO_PHOTO_FOUND_REASONS)


class DowngradeNoPhotoUnresolvedTests(unittest.TestCase):
    def _item(self, reason=PERMANENT):
        return {"scene_key": "designer_typing_font_specs", "reason": reason,
                "detail": "云端生图服务本次任务内已判定为永久失败。",
                "query_attempts": [{"query": "ai_image_generation",
                                    "code": PERMANENT}]}

    def test_permanent_failure_becomes_reported_unplaced(self):
        item = self._item()
        out = semantic_scenes.downgrade_no_photo_unresolved([item])
        self.assertEqual(len(out), 1)
        row = out[0]
        self.assertEqual(row["reason"], semantic_scenes.NO_VERIFIED_PHOTO_SKIPPED)
        # 原始失败码必须留痕，跳过不等于没记录。
        self.assertEqual(row["original_reason"], PERMANENT)
        self.assertEqual(row["scene_key"], item["scene_key"])
        self.assertEqual(row["query_attempts"], item["query_attempts"])
        self.assertTrue(row["detail"])

    def test_downgraded_scene_is_capacity_not_blocking(self):
        out = semantic_scenes.downgrade_no_photo_unresolved([self._item()])
        blocking, capacity = semantic_scenes.split_unresolved_scenes(out)
        self.assertEqual(blocking, [])
        self.assertEqual([r["scene_key"] for r in capacity],
                         ["designer_typing_font_specs"])

    def test_integrity_reasons_pass_through_unchanged(self):
        for reason in ("SCENE.LICENSE_METADATA_MISSING",
                       "SCENE.PHOTO_REUSED_OR_UNVERIFIED",
                       "SCENE.NEAR_DUPLICATE_PHOTO",
                       "SCENE.PRIVACY_REVIEW_REQUIRED",
                       "SCENE.SCENE_BLOCKED_PRIVACY",
                       "SCENE.PHOTO_IDENTITY_UNVERIFIED",
                       "SCENE.VISUAL_REVIEW_UNAVAILABLE",
                       "SCENE.NO_SCENES_PLANNED",
                       "SCENE.SOMETHING_UNKNOWN"):
            with self.subTest(reason=reason):
                item = self._item(reason)
                out = semantic_scenes.downgrade_no_photo_unresolved([item])
                self.assertIs(out[0], item, "真实性问题不得被改写")
                blocking, capacity = semantic_scenes.split_unresolved_scenes(out)
                self.assertEqual(len(blocking), 1)
                self.assertEqual(capacity, [])

    def test_candidate_rejected_on_safety_metadata_is_downgradable(self):
        """候选被安全元数据否决＝一张都没插入，属于"没图可用"。

        这与"隐私判定要求人工复核"（PRIVACY_REVIEW_REQUIRED）不同：后者
        是判断失灵，必须阻断。此处显式区分，避免以后把两者混为一谈。
        """
        for reason in ("SCENE.CONTENT_SAFETY_METADATA_BLOCK",
                       "SCENE.VISUAL_CONTENT_MISMATCH",
                       "SCENE.PHOTO_STYLE_MISMATCH",
                       "SCENE.IMAGE_CONTEXT_UNVERIFIED"):
            with self.subTest(reason=reason):
                out = semantic_scenes.downgrade_no_photo_unresolved([self._item(reason)])
                self.assertEqual(out[0]["reason"],
                                 semantic_scenes.NO_VERIFIED_PHOTO_SKIPPED)
                self.assertEqual(out[0]["original_reason"], reason)

    def test_mixed_batch_keeps_only_integrity_blocking(self):
        rows = [self._item(), self._item("SCENE.LICENSE_METADATA_MISSING")]
        blocking, capacity = semantic_scenes.split_unresolved_scenes(
            semantic_scenes.downgrade_no_photo_unresolved(rows))
        self.assertEqual([r["reason"] for r in blocking],
                         ["SCENE.LICENSE_METADATA_MISSING"])
        self.assertEqual([r["reason"] for r in capacity],
                         [semantic_scenes.NO_VERIFIED_PHOTO_SKIPPED])

    def test_empty_and_none_inputs(self):
        self.assertEqual(semantic_scenes.downgrade_no_photo_unresolved(None), [])
        self.assertEqual(semantic_scenes.downgrade_no_photo_unresolved([]), [])


if __name__ == "__main__":
    unittest.main()
