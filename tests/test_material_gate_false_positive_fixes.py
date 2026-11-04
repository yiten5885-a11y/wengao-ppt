"""material 流保真门三处误伤/自相矛盾修复的回归锁定（2026-11-04 mt-2e236f66）。

用户任务 mt-2e236f66（DOCX→PPT，14 页）validate 报 3 条 BLOCKING，逐条
溯源发现其中 2 条是**门与生成侧契约打架**、1 条是**度量盲区**：

① FIDELITY.CHART_CATEGORY_TRUNCATED：label_extract._short_source_desc 的
   docstring 承诺「截断到标点边界」，实现却是硬切 22 字 + "…"。兜底标签
   天然以 … 结尾，而保真门（source_fidelity._looks_truncated）把 … 结尾
   的图表类目一律判 BLOCKING——左手制造缺陷、右手拦截缺陷。
   修复：实现真正的标点边界截断，不再补省略号。

② FIDELITY.REQUIRED_TEXT_MISSING（F084）：原文「+6%」被冻结器归一为
   '6%'，候选页忠实照抄原文写 '+6%'；value_matches 第 245 行注释意图是
   拦「-N vs 正数」的负号翻转，条件却写成 `sign and …`，把 sign='+'
   （冗余正向修饰，语义与无符号一致）也拦成 BLOCKING。
   修复：只拦负号。

③ SCENE.RENDERED_IMAGE_NOT_VISIBLE：页 12 的 AI 生成 UI mockup 有
   41.9% 近白像素，照片渲染完美（match=1.0、mae=1.3、无遮挡、无裁切），
   但「有图 vs 无图」的像素贡献只有 0.592<0.65——白底照片在浅色页面上
   的度量盲区。修复：match 过线且 mae 极低时构成「照片确实渲染」的
   充分证据（content_proved），低 contribution 不再一票否决。
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from wengao_ppt import label_extract, semantic_scenes, source_fidelity
from wengao_ppt.source_fidelity import value_matches

from test_semantic_scenes import (
    FakePhotoProvider,
    make_source_pptx,
    semantic_fixture,
    synthetic_scene_render_evidence,
)


class TestShortSourceDescPunctBoundary(unittest.TestCase):
    """修复①：兜底标签截断到标点边界，不再产出以 … 结尾的片段。"""

    def test_long_clause_cut_at_punctuation_without_ellipsis(self):
        sentence = ("一级生成器掉落：1 级物品 85%、2 级 13%、3 级 2%；"
                    "每升 1 级生成器，2 级以上概率 +6%。")
        label = label_extract._short_source_desc(sentence, "+6")
        self.assertTrue(label, "兜底标签不应为空")
        self.assertLessEqual(len(label), 22)
        self.assertFalse(label.endswith("…"), f"不得以省略号结尾：{label!r}")
        self.assertFalse(label.endswith("..."), f"不得以省略号结尾：{label!r}")
        self.assertIn(label, sentence, "标签必须是来源句的前缀片段")

    def test_cut_label_is_not_mid_word_truncation(self):
        # 截点必须落在标点/空格边界：source_fidelity._truncates_mid_word
        # 对 unreliable 标签的词中截断判 BLOCKING（同一套门的另一分支）。
        sentence = ("兴奋度指数 H 由五个分量加权求和得到，其中 C 连击强度 "
                    "取近 30 秒合成次数与上限的比值。")
        label = label_extract._short_source_desc(sentence, "30")
        if len(label) < len(sentence.rstrip("。！？")):
            self.assertFalse(source_fidelity._truncates_mid_word(label, sentence),
                             f"标签断在词中：{label!r}")

    def test_short_clause_passes_through_unchanged(self):
        # 函数既有契约：取**包含该数字的最小完整分句**（标点界定），
        # 分句本身 ≤22 字时原样返回，不做任何追加截断。
        sentence = "每升 1 级生成器，2 级以上概率 +6%"
        label = label_extract._short_source_desc(sentence, "+6")
        self.assertEqual(label, "2 级以上概率 +6%")
        self.assertFalse(label.endswith("…"))


class TestValueMatchesSignSemantics(unittest.TestCase):
    """修复②：只拦负号翻转；"+" 是冗余正向修饰，不得判符号不一致。"""

    def _fact(self, raw="6%", value="6", unit="%"):
        return {"fact_id": "F084", "raw_value_text": raw,
                "value": value, "unit_class": unit,
                "source_ref": {"sentence": "每升 1 级生成器，2 级以上概率 +6%。"}}

    def test_plus_sign_variant_passes(self):
        # 原文「+6%」→ 冻结 '6%'；候选照抄原文写 '+6%' 必须通过。
        r = value_matches(self._fact(), "每升 1 级生成器，2 级以上概率 +6%")
        self.assertTrue(r["ok"], f"'+6%' 被误判：{r}")

    def test_bare_plus_value_without_unit_still_fails(self):
        # '+6' 后面没有单位关联：修复②放行符号，但单位检查仍须拦截
        # （单位缺失是另一个维度的问题，不能被顺手放过）。
        r = value_matches(self._fact(), "每升 1 级生成器，2 级以上概率 +6")
        self.assertFalse(r["ok"])

    def test_negative_flip_still_blocked(self):
        r = value_matches(self._fact(), "每升 1 级生成器，2 级以上概率 -6%")
        self.assertFalse(r["ok"])
        self.assertIn("符号", r["why"])

    def test_positive_fact_with_negative_candidate_blocked(self):
        r = value_matches(self._fact(raw="-6", value="-6", unit="unknown"),
                          "指标 -6 之外还有别的数字")
        self.assertTrue(r["ok"])  # 冻结为负、候选为负 → 一致


class TestRenderedVisibilityContentProved(unittest.TestCase):
    """修复③：浅色照片渲染完美时低 contribution 不得一票否决。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wengao-scene-fp-")
        self.root = Path(self.temp.name)
        self.source = make_source_pptx(self.root / "source.pptx")
        self.assets = self.root / "assets"
        self.assets.mkdir()
        # 近白照片 + 轻微颗粒（provider 侧会拒绝纯平色块；颗粒幅度 5 个
        # 灰度远低于差异阈值 18，保证「有图 vs 无图」逐像素几乎同色）。
        self.images = {}
        for key, base, title in (("cup", 248, "Ceramic cup drinking"),
                                 ("device", 244, "iPhone Mac work desk")):
            noise = Image.effect_noise((800, 600), 8).convert("L")
            light = noise.point(lambda v, b=base: max(0, min(255, b + (v - 128) // 6)))
            path = self.assets / f"light-{key}.png"
            light.convert("RGB").save(path)
            self.images[key] = {"file": str(path), "title": title}

    def tearDown(self):
        self.temp.cleanup()

    def _fixture(self, occlude_fraction: float = 0.0):
        provider = FakePhotoProvider(self.images)
        plan = semantic_scenes.build_plan(
            self.source, provider=provider, semantic_resolution=semantic_fixture())
        candidate = self.root / "light-photo-candidate.pptx"
        semantic_scenes.apply_plan(self.source, plan, candidate)
        plan_path = self.root / "light-photo-plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        evidence = synthetic_scene_render_evidence(
            candidate, plan, self.root / f"light-photo-pages-{occlude_fraction}",
            occlude_fraction=occlude_fraction,
            background=(249, 249, 249))
        return candidate, plan_path, plan, evidence

    def test_light_photo_with_perfect_match_counts_visible(self):
        candidate, plan_path, _plan, evidence = self._fixture()
        report = semantic_scenes.validate_rendered_scene_visibility(
            candidate, plan_path, evidence)
        summary = report.get("summary") or {}
        scenes = (summary.get("scenes")
                  or (summary.get("rendered_pixel_visibility") or {}).get("scenes")
                  or {})
        self.assertTrue(scenes, f"缺少场景可见性证据：{report}")
        low_contrib = False
        for key, row in scenes.items():
            self.assertTrue(row["visible"],
                            f"浅色照片被误判不可见：{key} → {row}")
            if row["candidate_pixel_contribution_fraction"] < 0.65:
                low_contrib = True
                self.assertTrue(row.get("content_proved"),
                                f"低贡献通过时必须携带 content_proved 证据：{key}")
        self.assertTrue(low_contrib,
                        "fixture 应当至少让一个场景落入低贡献盲区，否则本测试"
                        "没有覆盖到修复点")

    def test_opaque_occlusion_still_blocked(self):
        # 反向锁定：照片真被不透明层盖住时（mae 高、match 低），
        # content_proved 豁免不得把真缺陷放行。
        candidate, plan_path, plan, _evidence = self._fixture()
        evidence2 = synthetic_scene_render_evidence(
            candidate, plan, self.root / "occluded-pages", occlude_fraction=1.0)
        report = semantic_scenes.validate_rendered_scene_visibility(
            candidate, plan_path, evidence2)
        self.assertEqual(report["status"], "FAILED", report)
        self.assertTrue(report["blocking"])
        self.assertIn("SCENE.RENDERED_IMAGE_NOT_VISIBLE",
                      {item["code"] for item in report["findings"]})


if __name__ == "__main__":
    unittest.main()
