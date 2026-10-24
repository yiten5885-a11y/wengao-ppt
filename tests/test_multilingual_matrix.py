"""Unit tests for the multilingual demographic support matrix, 5-layer classification,
cross-script recognition, and cross-language canonical scene identity.

Requirements tested:
1. Public demographic data source: Ethnologue (2024, 27th edition) / UNESCO (> 20M speakers).
2. 5-Layer classification: text_recognition, semantic_planning, visual_query_generation,
   typography_and_layout, generation_and_gate.
3. Explicit support status: FULL_SUPPORT, PARTIAL_SUPPORT, PENDING_VERIFICATION.
   Never claim all 28 hints are fully supported.
4. Script and RTL/CJK helpers (Arabic/Urdu/Hebrew RTL, Chinese/Japanese/Korean CJK).
5. Cross-language canonical scene identity:
   - UE01 (zh shopping) vs UE36 (en shopping) -> same canonical identity
   - UE01 (shopping) vs UE02 (checkout) vs UE03 (receipt) -> 3 distinct sub-scenes
   - Healthcare and industrial sub-scenes remain distinct.
"""
import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from pptx import Presentation
from pptx.util import Inches

from wengao_ppt import visual_plan
from wengao_ppt.language_support import (
    CURRENT_LANGUAGE_ACCEPTANCE_CODES,
    CURRENT_LANGUAGE_ACCEPTANCE_SCOPE_ID,
    OUT_OF_SCOPE_LANGUAGE_HINT_CODES,
    LANGUAGE_SUPPORT_MATRIX,
    POPULATION_DATA_SOURCE,
    SUPPORTED_MAJOR_LANGUAGES,
    canonical_scene_identity,
    detect_language_hint,
    get_language_support_level,
    get_support_matrix,
    is_cjk_language,
    is_rtl_language,
    language_support_record,
)
from wengao_ppt.scene_model import (
    scene_coverage_audit_prompt,
    scene_gap_repair_prompt,
    scene_merge_replan_prompt,
    scene_prompt,
)


class TestMultilingualMatrix(unittest.TestCase):

    def test_demographic_source_and_threshold(self):
        matrix = get_support_matrix()
        self.assertIn("Ethnologue (2024, 27th edition)", matrix["source"])
        self.assertEqual(matrix["population_threshold_millions"], 20)
        self.assertEqual(len(matrix["languages"]), 28)
        scope = matrix["version_acceptance_scope"]
        self.assertEqual(scope["scope_id"], "existing-matrix-26-v1")
        self.assertEqual(scope["language_count"], 26)
        self.assertEqual(set(scope["language_codes"]), set(CURRENT_LANGUAGE_ACCEPTANCE_CODES))
        self.assertFalse(scope["all_layers_verified"])
        self.assertFalse(scope["worldwide_threshold_list_exhaustive"])
        self.assertEqual(scope["out_of_scope_language_hints"], list(OUT_OF_SCOPE_LANGUAGE_HINT_CODES))

        # Count distribution
        summary = matrix["summary"]
        self.assertEqual(summary["FULL_SUPPORT"], 2)  # en, zh
        self.assertEqual(summary["PARTIAL_SUPPORT"], 22)
        self.assertEqual(summary["PENDING_VERIFICATION"], 4)  # te, ta, el, he

    def test_five_layer_schema_and_status(self):
        expected_layers = [
            "text_recognition",
            "semantic_planning",
            "visual_query_generation",
            "typography_and_layout",
            "generation_and_gate",
        ]
        valid_layer_values = {"VERIFIED", "PARTIAL", "PENDING"}

        for code, meta in LANGUAGE_SUPPORT_MATRIX.items():
            layers = meta.get("layers", {})
            for layer in expected_layers:
                self.assertIn(layer, layers, f"Language '{code}' missing layer '{layer}'")
                self.assertIn(layers[layer], valid_layer_values,
                              f"Language '{code}' layer '{layer}' has invalid value '{layers[layer]}'")

            status = meta.get("support_status")
            self.assertIn(status, {"FULL_SUPPORT", "PARTIAL_SUPPORT", "PENDING_VERIFICATION"},
                          f"Language '{code}' has invalid support_status '{status}'")

            # Population threshold verification
            if status != "PENDING_VERIFICATION":
                self.assertGreaterEqual(meta["speakers_millions"], 20,
                                        f"Language '{code}' has status '{status}' but speakers < 20M")

    def test_full_support_restricted_to_verified_tier(self):
        # Only zh and en are marked as FULL_SUPPORT
        full_langs = [code for code, meta in LANGUAGE_SUPPORT_MATRIX.items()
                      if meta["support_status"] == "FULL_SUPPORT"]
        self.assertEqual(set(full_langs), {"zh", "en"})

        zh_info = get_language_support_level("zh")
        self.assertEqual(zh_info["support_status"], "FULL_SUPPORT")
        self.assertTrue(all(v == "VERIFIED" for v in zh_info["layers"].values()))

        en_info = get_language_support_level("en")
        self.assertEqual(en_info["support_status"], "FULL_SUPPORT")
        self.assertTrue(all(v == "VERIFIED" for v in en_info["layers"].values()))

    def test_partial_support_transparency(self):
        # Major languages with > 20M speakers that are partially supported
        for code in ("ar", "hi", "bn", "es", "id", "de", "fr", "ja", "ru"):
            info = get_language_support_level(code)
            self.assertEqual(info["support_status"], "PARTIAL_SUPPORT",
                             f"Language {code} should be PARTIAL_SUPPORT")
            self.assertTrue(info["supported"])
            # Should have at least one layer NOT yet fully VERIFIED (e.g. typography or gate)
            layers = info["layers"]
            has_partial_or_pending = any(v in ("PARTIAL", "PENDING") for v in layers.values())
            self.assertTrue(has_partial_or_pending,
                            f"Language {code} claims all layers VERIFIED but is marked PARTIAL_SUPPORT")

    def test_pending_verification_languages(self):
        for code in ("te", "ta", "el", "he"):
            info = get_language_support_level(code)
            self.assertEqual(info["support_status"], "PENDING_VERIFICATION")
            self.assertFalse(info["supported"])

    def test_script_helpers(self):
        # RTL helpers
        self.assertTrue(is_rtl_language("ar"))
        self.assertTrue(is_rtl_language("ur"))
        self.assertTrue(is_rtl_language("fa"))
        self.assertTrue(is_rtl_language("he"))
        self.assertFalse(is_rtl_language("en"))
        self.assertFalse(is_rtl_language("zh"))
        self.assertFalse(is_rtl_language("es"))

        # CJK helpers
        self.assertTrue(is_cjk_language("zh"))
        self.assertTrue(is_cjk_language("ja"))
        self.assertTrue(is_cjk_language("ko"))
        self.assertFalse(is_cjk_language("en"))
        self.assertFalse(is_cjk_language("hi"))
        self.assertFalse(is_cjk_language("ar"))

    def test_cross_script_detection(self):
        cases = [
            ("中国空间站核心舱机械臂展开试验", "zh"),
            ("Supermarket shoppers browsing fresh grocery products in retail store", "en"),
            ("भारतीय अंतरिक्ष अनुसंधान संगठन का आगामी मिशन", "hi"),
            ("সুপারমার্কেটে কেনাকাটা করছেন ক্রেতা ও সাধারণ মানুষ", "bn"),
            ("أطباء وممرضون يراجعون الأشعة في غرفة الطوارئ بالمستشفى", "ar"),
            ("Los consumidores compran alimentos frescos en los pasillos del supermercado", "es"),
            ("Pasien mengunjungi klinik untuk konsultasi dengan dokter spesialis", "id"),
        ]
        for text, expected_lang in cases:
            hint = detect_language_hint(text)
            self.assertIn(expected_lang, hint["languages"],
                          f"Failed to detect {expected_lang} in '{text}', got {hint}")

    def test_shared_indic_danda_punctuation_does_not_create_false_mixed_language(self):
        bengali = "কাল্পনিক সেবা উন্নয়ন। নাগরিকেরা অনলাইনে আবেদন করেন। কর্মীরা আবেদন যাচাই করেন।"
        hindi = "काल्पनिक सेवा सुधार। नागरिक ऑनलाइन आवेदन करते हैं। कर्मचारी आवेदन जाँचते हैं।"
        bn_hint = detect_language_hint(bengali)
        hi_hint = detect_language_hint(hindi)
        self.assertEqual(bn_hint["primary"], "bn", bn_hint)
        self.assertEqual(bn_hint["languages"], ["bn"], bn_hint)
        self.assertEqual(bn_hint["scripts"], ["Bengali"], bn_hint)
        self.assertEqual(hi_hint["primary"], "hi", hi_hint)
        self.assertEqual(hi_hint["languages"], ["hi"], hi_hint)
        self.assertEqual(hi_hint["scripts"], ["Devanagari"], hi_hint)

    def test_recognition_samples_cover_every_over_20m_matrix_language(self):
        # This exercises the advisory recognizer for the frozen v1 language
        # scope only. It does not claim typography, generation, or end-to-end
        # support for a locale.
        fixture = Path(__file__).parent / "fixtures" / "major-language-recognition-samples.json"
        samples = json.loads(fixture.read_text(encoding="utf-8"))["languages"]
        required = set(CURRENT_LANGUAGE_ACCEPTANCE_CODES)
        self.assertEqual(set(samples), required)
        for code, text in samples.items():
            hint = detect_language_hint(text)
            self.assertIn(code, hint["languages"],
                          f"Failed to detect {code}: {hint}")

    def test_scene_prompt_explicitly_covers_frozen_v1_languages(self):
        prompt = scene_prompt([{"page": 1, "title": "", "text": "sample",
                                "language_hint": {"primary": "und"}}])
        for code in CURRENT_LANGUAGE_ACCEPTANCE_CODES:
            with self.subTest(language=code):
                self.assertIn(LANGUAGE_SUPPORT_MATRIX[code]["name"], prompt)
        self.assertIn("not a worldwide exhaustive language inventory", prompt)

    def test_scene_prompt_detects_all_26_from_original_text_before_keyword_extraction(self):
        fixture = Path(__file__).parent / "fixtures" / "major-language-recognition-samples.json"
        samples = json.loads(fixture.read_text(encoding="utf-8"))["languages"]
        pages = [{"page": index, "title": code, "text": text}
                 for index, (code, text) in enumerate(samples.items(), 1)]
        prompt = scene_prompt(pages)

        self.assertIn("Before extracting any scene keywords, identify", prompt)
        self.assertIn("不能假定输入是中文", prompt)
        for index, (code, text) in enumerate(samples.items(), 1):
            hint = detect_language_hint(text)
            expected_page_payload = json.dumps({
                "page": index,
                "title": code,
                "source_language_hint": code,
                "source_language_candidates": [code],
                "source_scripts": hint["scripts"],
                "source_language_confidence": hint["confidence"],
                "source_language_detector": hint["detector"],
                "text": text,
            }, ensure_ascii=False, separators=(",", ":"))
            with self.subTest(language=code):
                self.assertIn(expected_page_payload, prompt)

    def test_visual_keyword_matcher_detects_language_first_and_bypasses_zh_tags_for_other_languages(self):
        fixture = Path(__file__).parent / "fixtures" / "major-language-recognition-samples.json"
        samples = json.loads(fixture.read_text(encoding="utf-8"))["languages"]
        prs = Presentation()
        for code, text in samples.items():
            slide = prs.slides.add_slide(prs.slide_layouts[6])
            box = slide.shapes.add_textbox(Inches(0.5), Inches(0.6), Inches(5.2), Inches(1.2))
            box.text_frame.text = text

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "26-language-keyword-order.pptx"
            prs.save(source)
            plan = visual_plan.build_plan(str(source))

        self.assertEqual(len(plan["pages"]), 26)
        self.assertEqual(plan["language_support"]["detected_source_languages"],
                         sorted(samples))
        self.assertEqual(plan["keyword_scene_order"][1],
                         "detect_language_and_script_offline")
        for index, (code, _) in enumerate(samples.items()):
            row = plan["pages"][index]
            with self.subTest(language=code):
                self.assertEqual(row["source_language"]["primary"], code)
                if code == "zh":
                    self.assertEqual(row["legacy_keyword_matcher"], "ZH_ONLY_APPLIED")
                else:
                    self.assertEqual(row["legacy_keyword_matcher"],
                                     "BYPASSED_AFTER_LANGUAGE_DETECTION")
                    self.assertEqual(row["keywords"], [])
                    self.assertIsNone(row["image"])

        bound = {key: plan[key] for key in
                 ("pages", "images", "particles", "language_support", "keyword_scene_order")}
        expected_sha = hashlib.sha256(json.dumps(
            bound, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        self.assertEqual(plan["plan_sha256"], expected_sha)

    def test_scene_prompt_detected_language_overrides_conflicting_caller_hint(self):
        text = "Los consumidores compran alimentos en el supermercado y comparan los precios."
        prompt = scene_prompt([{
            "page": 1, "title": "", "text": text,
            "language_hint": {"primary": "zh", "languages": ["zh"],
                              "scripts": ["Han"], "confidence": "script"},
        }])
        self.assertIn('"source_language_hint":"es"', prompt)
        self.assertIn('"source_language_candidates":["es"]', prompt)
        self.assertIn(text, prompt)

    def test_scene_audits_and_repairs_keep_original_language_context(self):
        text = "Les consommateurs achètent des produits au supermarché et comparent les prix."
        pages = [{"page": 1, "title": "", "text": text}]
        expected = (
            '"source_language_hint":"fr","source_language_candidates":["fr"]'
        )
        prompts = [
            scene_coverage_audit_prompt(pages, {}),
            scene_gap_repair_prompt(pages, {}, {
                "missing_scenes": [{"page": 1, "evidence": text,
                                    "scene_key": "supermarket-shopping",
                                    "canonical_scene_id": "supermarket_shopping"}]
            }),
            scene_merge_replan_prompt(pages, {}, {
                "merged_distinct_scenes": [{"pages": [1]}]
            }),
        ]
        for prompt in prompts:
            with self.subTest(prompt=prompt[:60]):
                self.assertIn(expected, prompt)
                self.assertIn(text, prompt)

    def test_scope_contract_matches_runtime_and_defers_extra_hints(self):
        contract_path = Path(__file__).parents[1] / "contracts" / "language_acceptance_scope_v1.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        self.assertEqual(contract["scope_id"], CURRENT_LANGUAGE_ACCEPTANCE_SCOPE_ID)
        self.assertEqual(set(contract["acceptance_language_codes"]),
                         set(CURRENT_LANGUAGE_ACCEPTANCE_CODES))
        self.assertEqual(len(contract["acceptance_language_codes"]), 26)
        self.assertEqual(set(contract["deferred_existing_hint_codes"]),
                         set(OUT_OF_SCOPE_LANGUAGE_HINT_CODES))
        self.assertEqual(contract["required_keyword_scene_pipeline_order"][1],
                         "detect_source_language_and_script_offline")
        self.assertEqual(contract["required_keyword_scene_pipeline_order"][2],
                         "apply_language_scoped_legacy_keyword_matcher_only_after_detection")
        self.assertEqual(contract["required_keyword_scene_pipeline_order"][3],
                         "plan_source_grounded_scene_keywords_in_original_language")
        wiring = contract["language_to_scene_wiring"]
        self.assertTrue(wiring["detector_runs_before_scene_keyword_extraction"])
        self.assertEqual(wiring["legacy_keyword_matcher_language_scope"], "zh_only")
        self.assertTrue(wiring["non_chinese_unknown_or_mixed_pages_bypass_legacy_chinese_tag_library"])
        self.assertTrue(wiring["non_chinese_pages_continue_to_multilingual_open_vocabulary_scene_planner"])
        self.assertTrue(wiring["planner_receives_original_text_and_language_context"])
        self.assertEqual(wiring["semantic_model_quality_acceptance"], "NOT_FULLY_ACCEPTED")
        self.assertEqual(wiring["real_photo_retrieval_and_insertion_acceptance"],
                         "NOT_FULLY_ACCEPTED")
        self.assertEqual(contract["fixture_status"]["recognition_samples"],
                         "PASS_26_OFFLINE_RECOGNITION_UNIT_TESTS")
        self.assertEqual(contract["fixture_status"]["scene_planner_payload_samples"],
                         "PASS_26_ORIGINAL_TEXT_AND_LANGUAGE_CONTEXT_FIXTURES")
        self.assertEqual(contract["fixture_status"]["real_50_deck_prepublication_gate"],
                         "SEPARATE_GATE_IN_PROGRESS_0_OF_50_ACTUAL_DECKS")

    def test_canonical_scene_cross_language_deduplication(self):
        # UE01 (Chinese supermarket shopping) vs UE36 (English supermarket shopping)
        id_zh = canonical_scene_identity(
            topic="消费与零售",
            subject="消费者",
            action="推车选购生鲜食品",
            setting="连锁超市货架通道",
        )
        id_en = canonical_scene_identity(
            topic="Consumer & Retail",
            subject="Shoppers",
            action="browsing fresh grocery products",
            setting="supermarket aisles",
        )
        self.assertEqual(id_zh, id_en)
        self.assertIn("supermarket-shopping", id_zh)

    def test_distinct_subscenes_retained_within_same_topic(self):
        # 1. Supermarket shopping
        id_shopping = canonical_scene_identity(
            topic="消费零售",
            subject="消费者",
            action="推车在货架通道选购生鲜商品",
            setting="超市货架",
        )
        # 2. Checkout payment
        id_checkout = canonical_scene_identity(
            topic="消费零售",
            subject="顾客与收银员",
            action="在收银台结算扫码支付",
            setting="超市收银台",
        )
        # 3. Receipt still life
        id_receipt = canonical_scene_identity(
            topic="消费零售",
            subject="购物发票小票与硬币",
            action="静物特写展示明细与印章",
            setting="桌面静物",
        )

        # Crucial hard gate: distinct sub-scenes must NOT collapse to same scene
        self.assertNotEqual(id_shopping, id_checkout)
        self.assertNotEqual(id_shopping, id_receipt)
        self.assertNotEqual(id_checkout, id_receipt)

        self.assertIn("supermarket-shopping", id_shopping)
        self.assertIn("checkout-payment", id_checkout)
        self.assertIn("receipt-still-life", id_receipt)

    def test_unregistered_non_ascii_scenes_do_not_collapse_to_general(self):
        # Open-vocabulary scripts must not all become the same ``scene.general``
        # identity merely because they have no ASCII transliteration.
        wind_sensor = canonical_scene_identity(
            topic="海上风电运维",
            subject="风机检修人员",
            action="更换叶片传感器",
            setting="海上风电机组顶部",
        )
        subsea_cable = canonical_scene_identity(
            topic="海上风电运维",
            subject="海缆检修人员",
            action="检查海底电缆接头",
            setting="海上升压站",
        )
        wind_sensor_repeat = canonical_scene_identity(
            topic="海上风电运维！",
            subject="风机检修人员",
            action="更换叶片传感器。",
            setting="海上风电机组顶部",
        )

        self.assertEqual(wind_sensor, wind_sensor_repeat)
        self.assertNotEqual(wind_sensor, subsea_cable)
        self.assertNotEqual(wind_sensor, "scene.general")
        self.assertNotEqual(subsea_cable, "scene.general")

        bengali = canonical_scene_identity(
            topic="নগর সেবা",
            subject="কর্মী",
            action="বিদ্যুৎ মিটার পরীক্ষা",
            setting="আবাসিক ভবন",
        )
        another_bengali_scene = canonical_scene_identity(
            topic="নগর সেবা",
            subject="শিক্ষার্থী",
            action="শ্রেণিকক্ষে পড়াশোনা",
            setting="বিদ্যালয়",
        )
        self.assertNotEqual(bengali, another_bengali_scene)

    def test_healthcare_and_industry_distinct_subscenes(self):
        # Radiologist CT scan vs Doctor consultation vs Hospital care
        id_ct = canonical_scene_identity(
            topic="医疗健康",
            subject="放射科医生",
            action="阅片分析CT影像",
            setting="放射科阅片室",
        )
        id_consult = canonical_scene_identity(
            topic="医疗健康",
            subject="医生与患者",
            action="诊室面对面沟通病情与问诊",
            setting="门诊诊室",
        )
        id_hospital = canonical_scene_identity(
            topic="医疗健康",
            subject="护士巡视病房",
            action="检查监护仪器与病床",
            setting="医院病房",
        )
        self.assertNotEqual(id_ct, id_consult)
        self.assertNotEqual(id_ct, id_hospital)
        self.assertNotEqual(id_consult, id_hospital)

        # Factory line vs Factory worker
        id_line = canonical_scene_identity(
            topic="工业制造",
            subject="自动化流水线",
            action="机械臂装配作业",
            setting="汽车总装车间流水线",
        )
        id_worker = canonical_scene_identity(
            topic="工业制造",
            subject="车间装配工人",
            action="手工校准精密零部件",
            setting="装配工位",
        )
        self.assertNotEqual(id_line, id_worker)


if __name__ == "__main__":
    unittest.main()
