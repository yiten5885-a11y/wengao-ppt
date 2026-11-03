import json
import unittest

from wengao_ppt.scene_model import (
    SceneModelError, _apply_grounded_scene_gap_repairs,
    _merge_scene_conflict_patch, _scene_merge_conflict_components,
    _validate_scene_coverage_audit, _validate_scene_merge_replan_scope,
    _validate_scene_gap_repair_scope, scene_coverage_audit_prompt, scene_gap_repair_prompt,
    scene_merge_replan_prompt, scene_prompt, resolve_scene_semantics,
)
from wengao_ppt.language_support import detect_language_hint


class SceneCoverageGapDiagnosticsTests(unittest.TestCase):
    def _valid_common(self):
        pages = [
            {"page": 1, "text": "消费者在商超选购商品。"},
            {"page": 2, "text": "生产线工人检查设备。"},
        ]
        plan = {
            "page_coverage": [
                {"page": 1, "scene_keys": ["supermarket_shopping"],
                 "evidence": ["消费者在商超选购商品。"]},
                {"page": 2, "scene_keys": [],
                 "no_scene_reason": "该页只描述运行指标，不含具体可视过程。",
                 "evidence": ["生产线工人检查设备。"]},
            ],
            "scenes": [{"scene_key": "supermarket_shopping"}],
        }
        page_reviews = [
            {"page": 1, "coverage_status": "SCENE_COVERED",
             "scene_keys": ["supermarket_shopping"], "reason": "已有对应场景。",
             "evidence": ["消费者在商超选购商品。"]},
            {"page": 2, "coverage_status": "NO_SCENE", "scene_keys": [],
             "reason": "该页只描述运行指标，没有适合的具体场景。",
             "evidence": ["生产线工人检查设备。"]},
        ]
        return pages, plan, page_reviews

    def test_gap_error_reports_audited_pages_without_copying_source_text(self):
        pages, plan, page_reviews = self._valid_common()
        audit = {
            "schema_version": "1.0.0",
            "review_status": "GAPS_FOUND",
            "page_reviews": page_reviews,
            "missing_scenes": [{"page": 2, "canonical_scene_id": "factory_line_worker"}],
            "merged_distinct_scenes": [],
        }
        with self.assertRaises(SceneModelError) as caught:
            _validate_scene_coverage_audit(audit, pages, plan)
        self.assertEqual(caught.exception.code, "SCENE.COVERAGE_AUDIT_GAPS")
        self.assertIn("missing_scenes=1 pages=[2]", caught.exception.detail)
        self.assertNotIn("消费者在商超", caught.exception.detail)

    def test_unexplained_gaps_are_visible_in_error_summary(self):
        pages, plan, page_reviews = self._valid_common()
        audit = {
            "schema_version": "1.0.0",
            "review_status": "GAPS_FOUND",
            "page_reviews": page_reviews,
            "missing_scenes": [],
            "merged_distinct_scenes": [],
        }
        with self.assertRaises(SceneModelError) as caught:
            _validate_scene_coverage_audit(audit, pages, plan)
        self.assertIn("review_status='GAPS_FOUND'", caught.exception.detail)
        self.assertIn("missing_scenes=0 pages=[]", caught.exception.detail)
        self.assertIn("merged_distinct_scenes=0 pages=[]", caught.exception.detail)

    def test_compact_independent_audit_passes_against_frozen_page_ledger(self):
        pages, plan, _ = self._valid_common()
        audit = {
            "schema_version": "1.1.0",
            "review_status": "NO_GAPS",
            "page_reviews": [
                {"page": 1, "coverage_status": "SCENE_COVERED",
                 "scene_keys": ["supermarket_shopping"]},
                {"page": 2, "coverage_status": "NO_SCENE", "scene_keys": []},
            ],
            "missing_scenes": [],
            "merged_distinct_scenes": [],
        }
        result = _validate_scene_coverage_audit(audit, pages, plan)
        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(result["pages_checked"], 2)

    def test_post_repair_audit_prompt_binds_exact_page_keys_without_waiving_semantic_review(self):
        pages, plan, _ = self._valid_common()
        prompt = scene_coverage_audit_prompt(pages, plan, phase="post_repair")
        self.assertIn('"expected_page_ledger"', prompt)
        self.assertIn('"scene_keys":["supermarket_shopping"]', prompt)
        self.assertIn("copy each page, coverage_status, and scene_keys exactly", prompt)
        self.assertIn("does not establish semantic coverage", prompt)
        self.assertIn("report every real gap or distinct-scene merge", prompt)

    def test_empty_plan_reconsideration_prompt_is_bounded_and_does_not_force_scenes(self):
        pages = [{"page": 1, "text": "本季度收入增长，消费者通过商超购物并在收银台结算。"}]
        prompt = scene_prompt(pages, reconsider_empty_plan=True)
        self.assertIn("single bounded same-model reconsideration", prompt)
        self.assertIn("the only empty-plan reconsideration", prompt)
        self.assertIn("the product gate will block it", prompt)
        self.assertIn("不得为避免空结果强贴无关场景", prompt)

    def test_empty_plan_gets_one_grounded_replan_and_third_full_deck_audit(self):
        quote = "本季度收入增长，消费者通过商超购物并在收银台结算。"
        pages = [{"page": 1, "title": "消费", "text": quote}]
        scene_key = "consumer_checkout_payment"
        initial_plan = {
            "schema_version": "2.2.0", "scenes": [],
            "page_coverage": [{"page": 1, "scene_keys": [],
                               "no_scene_reason": "初轮未识别到适合的代表场景，仍需复核。",
                               "evidence": [quote]}],
        }
        recovery_scene = {
            "scene_key": scene_key,
            "canonical_scene_id": scene_key,
            "topic": "消费",
            "depiction_type": "illustrative",
            "illustration_rationale": "收银台结算的代表画面能帮助理解本页消费主题，不表示原文中的具体事件。",
            "subject": "消费者",
            "action": "选购商品",
            "setting": "商超",
            "label": "商超结算",
            "lighting": "bright balanced indoor lighting",
            "composition_camera": "documentary medium shot",
            "style": "realistic documentary photography",
            "quality": "clear natural detail",
            "photo_search_terms": {"subject": "consumer", "action": "shopping products",
                                   "setting": "supermarket"},
            "visual_query": "consumer shopping products supermarket",
            "visual_query_alternatives": [],
            "page_candidates": [{"page": 1, "relevance": 0.9,
                                 "evidence": [quote]}],
        }
        recovered_plan = {
            "schema_version": "2.2.0", "scenes": [recovery_scene],
            "page_coverage": [{"page": 1, "scene_keys": [scene_key],
                               "no_scene_reason": None, "evidence": [quote]}],
        }
        calls = []

        def model_call(model, prompt):
            calls.append((model, prompt))
            if len(calls) == 1:
                payload = initial_plan
            elif "independent second-pass semantic coverage auditor" in prompt:
                bound = json.loads(prompt.split("Audit every page in this payload:\n", 1)[1])
                payload = {
                    "schema_version": "1.3.0", "review_status": "NO_GAPS",
                    "page_reviews": bound["expected_page_ledger"],
                    "missing_scenes": [], "merged_distinct_scenes": [],
                }
            elif "single bounded same-model reconsideration" in prompt:
                payload = recovered_plan
            elif "post-repair semantic coverage verifier" in prompt:
                bound = json.loads(prompt.split("Audit every page in this payload:\n", 1)[1])
                payload = {
                    "schema_version": "1.3.0", "review_status": "NO_GAPS",
                    "page_reviews": bound["expected_page_ledger"],
                    "missing_scenes": [], "merged_distinct_scenes": [],
                }
            else:
                self.fail("unexpected model call")
            return {"response": json.dumps(payload, ensure_ascii=False),
                    "model": model, "elapsed_seconds": 0.01, "done_reason": "stop"}

        resolved, meta = resolve_scene_semantics(
            pages, model="fixture-local-model", model_call=model_call)

        self.assertEqual(len(calls), 4)
        self.assertTrue(all(model == "fixture-local-model" for model, _ in calls))
        self.assertIn("single bounded same-model reconsideration", calls[2][1])
        self.assertIn("post-repair semantic coverage verifier", calls[3][1])
        self.assertEqual(len(resolved["scenes"]), 1)
        self.assertEqual(resolved["page_coverage"][0]["scene_keys"], [scene_key])
        self.assertEqual(meta["coverage_audit"]["status"], "PASSED")
        self.assertEqual(meta["coverage_audit"]["phase"], "post_empty_plan_reconsideration")
        recovery = meta["empty_scene_recovery"]
        self.assertEqual(recovery["attempts"], 1)
        self.assertEqual(recovery["status"], "APPLIED_AND_REVERIFIED")
        self.assertEqual(recovery["verification_status"], "PASSED")
        self.assertEqual(recovery["reconsideration_model"], "fixture-local-model")
        self.assertNotIn("evidence", recovery)
        for key in ("original_plan_sha256", "reconsidered_plan_sha256",
                    "verification_prompt_sha256", "verification_response_sha256",
                    "verification_result_sha256"):
            self.assertRegex(recovery[key], r"^[0-9a-f]{64}$")

    def test_empty_plan_that_remains_empty_is_recorded_and_stays_blockable(self):
        quote = "报告仅列出经核验的年度总量指标，没有具体交易事件。"
        pages = [{"page": 1, "title": "总量指标", "text": quote}]
        empty_plan = {
            "schema_version": "2.2.0", "scenes": [],
            "page_coverage": [{"page": 1, "scene_keys": [],
                               "no_scene_reason": "初轮未识别适合解释本页的代表场景，需重新核对。",
                               "evidence": [quote]}],
        }
        calls = []

        def model_call(model, prompt):
            calls.append(prompt)
            if "independent second-pass semantic coverage auditor" in prompt:
                bound = json.loads(prompt.split("Audit every page in this payload:\n", 1)[1])
                payload = {"schema_version": "1.3.0", "review_status": "NO_GAPS",
                           "page_reviews": bound["expected_page_ledger"],
                           "missing_scenes": [], "merged_distinct_scenes": []}
            else:
                payload = empty_plan
            return {"response": json.dumps(payload, ensure_ascii=False),
                    "model": model, "elapsed_seconds": 0.01, "done_reason": "stop"}

        resolved, meta = resolve_scene_semantics(
            pages, model="fixture-local-model", model_call=model_call)

        self.assertEqual(len(calls), 3)
        self.assertIn("single bounded same-model reconsideration", calls[2])
        self.assertEqual(resolved["scenes"], [])
        recovery = meta["empty_scene_recovery"]
        self.assertEqual(recovery["attempts"], 1)
        self.assertEqual(recovery["status"], "BLOCKED_STILL_EMPTY")
        self.assertEqual(recovery["reconsidered_scene_count"], 0)
        self.assertEqual(meta["coverage_audit"]["status"], "PASSED")
        self.assertEqual(meta["coverage_audit"]["empty_scene_recovery"]["status"],
                         "BLOCKED_STILL_EMPTY")

    def test_empty_plan_replan_with_ungrounded_scene_fails_closed(self):
        quote = "季度经营保持稳定，月度收入数据已复核。"
        pages = [{"page": 1, "title": "经营情况", "text": quote}]
        empty_plan = {
            "schema_version": "2.2.0", "scenes": [],
            "page_coverage": [{"page": 1, "scene_keys": [],
                               "no_scene_reason": "初轮未识别适合解释本页的代表场景，需重新核对。",
                               "evidence": [quote]}],
        }
        invented_plan = {
            "schema_version": "2.2.0",
            "scenes": [{
                "scene_key": "worker_celebrating_growth",
                "canonical_scene_id": "worker_celebrating_growth",
                "topic": "经营情况", "depiction_type": "illustrative",
                "illustration_rationale": "工人庆祝增长可代表经营情况，但不是具体事件。",
                "subject": "工人", "action": "庆祝增长", "setting": "办公室",
                "label": "经营成果", "lighting": "bright office light",
                "composition_camera": "documentary medium shot",
                "style": "realistic documentary photography", "quality": "clear natural detail",
                "photo_search_terms": {"subject": "worker", "action": "celebrating growth",
                                       "setting": "office"},
                "visual_query": "worker celebrating growth office",
                "visual_query_alternatives": [],
                "page_candidates": [{"page": 1, "relevance": 0.9,
                                     "evidence": ["工人在办公室庆祝增长"]}],
            }],
            "page_coverage": [{"page": 1,
                               "scene_keys": ["worker_celebrating_growth"],
                               "no_scene_reason": None, "evidence": [quote]}],
        }
        calls = []

        def model_call(model, prompt):
            calls.append(prompt)
            if len(calls) == 1:
                payload = empty_plan
            elif "independent second-pass semantic coverage auditor" in prompt:
                bound = json.loads(prompt.split("Audit every page in this payload:\n", 1)[1])
                payload = {"schema_version": "1.3.0", "review_status": "NO_GAPS",
                           "page_reviews": bound["expected_page_ledger"],
                           "missing_scenes": [], "merged_distinct_scenes": []}
            else:
                payload = invented_plan
            return {"response": json.dumps(payload, ensure_ascii=False),
                    "model": model, "elapsed_seconds": 0.01, "done_reason": "stop"}

        with self.assertRaises(SceneModelError) as caught:
            resolve_scene_semantics(pages, model="fixture-local-model", model_call=model_call)
        self.assertEqual(caught.exception.code, "SCENE.EVIDENCE_NOT_GROUNDED")
        self.assertEqual(caught.exception.diagnostics["empty_scene_recovery_attempts"], 1)
        self.assertRegex(
            caught.exception.diagnostics["empty_scene_recovery_original_plan_sha256"],
            r"^[0-9a-f]{64}$")
        # 1 (empty plan) + 1 (NO_GAPS audit) + 2 x [1 reconsidered-plan call +
        # 1 bounded same-model evidence-repair attempt] before failing closed:
        # the reconsidered plan's evidence is never grounded, so the repair
        # correctly refuses to invent it and the whole batch gets one
        # corrective retry before SCENE.EVIDENCE_NOT_GROUNDED propagates.
        self.assertEqual(len(calls), 6)

    def test_page_key_mismatch_diagnostic_reports_counts_without_disclosing_keys(self):
        pages, plan, _ = self._valid_common()
        audit = {
            "schema_version": "1.3.0",
            "review_status": "NO_GAPS",
            "page_reviews": [
                {"page": 1, "coverage_status": "SCENE_COVERED", "scene_keys": []},
                {"page": 2, "coverage_status": "NO_SCENE", "scene_keys": []},
            ],
            "missing_scenes": [],
            "merged_distinct_scenes": [],
        }
        with self.assertRaises(SceneModelError) as caught:
            _validate_scene_coverage_audit(audit, pages, plan)
        self.assertEqual(caught.exception.code, "SCENE.COVERAGE_AUDIT_MISMATCH")
        self.assertIn("expected=1,audited=0,missing=1,extra=0,unknown=0", caught.exception.detail)
        self.assertIn("expected_ids=", caught.exception.detail)
        self.assertNotIn("supermarket_shopping", caught.exception.detail)

    def test_compact_audit_requires_source_grounded_missing_scene_evidence(self):
        pages, plan, _ = self._valid_common()
        audit = {
            "schema_version": "1.1.0",
            "review_status": "GAPS_FOUND",
            "page_reviews": [
                {"page": 1, "coverage_status": "SCENE_COVERED",
                 "scene_keys": ["supermarket_shopping"]},
                {"page": 2, "coverage_status": "NO_SCENE", "scene_keys": []},
            ],
            "missing_scenes": [{"page": 2, "canonical_scene_id": "factory_line_worker",
                                "evidence": "not in the source"}],
            "merged_distinct_scenes": [],
        }
        with self.assertRaises(SceneModelError) as caught:
            _validate_scene_coverage_audit(audit, pages, plan)
        self.assertEqual(caught.exception.code, "SCENE.COVERAGE_AUDIT_UNGROUNDED")

    def test_compact_audit_blocks_an_evidenced_missing_scene(self):
        pages, plan, _ = self._valid_common()
        audit = {
            "schema_version": "1.1.0",
            "review_status": "GAPS_FOUND",
            "page_reviews": [
                {"page": 1, "coverage_status": "SCENE_COVERED",
                 "scene_keys": ["supermarket_shopping"]},
                {"page": 2, "coverage_status": "NO_SCENE", "scene_keys": []},
            ],
            "missing_scenes": [{"page": 2, "canonical_scene_id": "factory_line_worker",
                                "evidence": "生产线工人检查设备。"}],
            "merged_distinct_scenes": [],
        }
        with self.assertRaises(SceneModelError) as caught:
            _validate_scene_coverage_audit(audit, pages, plan)
        self.assertEqual(caught.exception.code, "SCENE.COVERAGE_AUDIT_GAPS")
        self.assertIn("missing_scenes=1 pages=[2]", caught.exception.detail)

    def _complete_gap_audit(self, *, scene_key="factory_line_worker",
                            canonical="factory_line_worker_machine_check"):
        pages, plan, page_reviews = self._valid_common()
        audit = {
            "schema_version": "1.3.0",
            "review_status": "GAPS_FOUND",
            "page_reviews": page_reviews,
            "missing_scenes": [{
                "page": 2,
                "scene_key": scene_key,
                "canonical_scene_id": canonical,
                "topic": "制造生产",
                "depiction_type": "illustrative",
                "illustration_rationale": "工人检查生产设备能代表制造生产巡检主题，不表示来源中某个特定事件真实发生。",
                "subject": "factory worker",
                "action": "inspecting production equipment",
                "setting": "factory production line",
                "label": "Worker inspecting a factory line",
                "visual_query": "factory worker inspecting production line",
                "visual_query_alternatives": ["industrial worker checking machinery"],
                "lighting": "bright practical factory lighting",
                "composition_camera": "medium-wide documentary view",
                "style": "realistic industrial documentary photography",
                "quality": "sharp natural detail",
                "photo_search_terms": {
                    "subject": "worker",
                    "action": "inspecting equipment",
                    "setting": "factory line",
                },
                "evidence": "生产线工人检查设备。",
            }],
            "merged_distinct_scenes": [],
        }
        return pages, plan, audit

    def _valid_merge_conflict(self):
        pages = [
            {"page": 1, "text": "消费者在商超挑选商品。"},
            {"page": 2, "text": "消费者在收银台结账。"},
            {"page": 3, "text": "员工在办公室核对发票。"},
        ]
        shared = {
            "scene_key": "consumer_activity",
            "canonical_scene_id": "consumer_activity",
            "topic": "消费", "subject": "消费者",
            "depiction_type": "illustrative",
            "illustration_rationale": "购物和付款说明消费主题，不表示来源中某个特定人或事件真实发生。",
            "action": "购物和付款", "setting": "商业场所",
            "label": "消费活动", "visual_query": "consumer shopping paying store",
            "photo_search_terms": {"subject": "consumer", "action": "shopping goods",
                                   "setting": "store"},
            "visual_query_alternatives": [],
            "lighting": "bright commercial lighting",
            "composition_camera": "documentary medium view",
            "style": "realistic documentary photography",
            "quality": "sharp natural detail",
            "page_candidates": [
                {"page": 1, "relevance": .9, "evidence": [pages[0]["text"]]},
                {"page": 2, "relevance": .9, "evidence": [pages[1]["text"]]},
            ],
        }
        invoice = {
            "scene_key": "office_invoice_review",
            "canonical_scene_id": "office_invoice_review",
            "topic": "财务", "subject": "员工", "action": "核对发票",
            "depiction_type": "illustrative",
            "illustration_rationale": "核对发票代表财务审核工作，不表示来源中某个特定人或事件真实发生。",
            "setting": "办公室", "label": "办公室核对发票",
            "visual_query": "employee checking invoice office",
            "photo_search_terms": {"subject": "employee", "action": "checking invoice",
                                   "setting": "office"},
            "visual_query_alternatives": [],
            "lighting": "soft office lighting",
            "composition_camera": "documentary medium view",
            "style": "realistic documentary photography",
            "quality": "sharp natural detail",
            "page_candidates": [
                {"page": 3, "relevance": .9, "evidence": [pages[2]["text"]]},
            ],
        }
        plan = {
            "schema_version": "2.2.0",
            "page_coverage": [
                {"page": 1, "scene_keys": ["consumer_activity"],
                 "no_scene_reason": None, "evidence": [pages[0]["text"]]},
                {"page": 2, "scene_keys": ["consumer_activity"],
                 "no_scene_reason": None, "evidence": [pages[1]["text"]]},
                {"page": 3, "scene_keys": ["office_invoice_review"],
                 "no_scene_reason": None, "evidence": [pages[2]["text"]]},
            ],
            "scenes": [shared, invoice],
        }
        audit = {
            "schema_version": "1.3.0", "review_status": "GAPS_FOUND",
            "page_reviews": [
                {"page": 1, "coverage_status": "SCENE_COVERED",
                 "scene_keys": ["consumer_activity"]},
                {"page": 2, "coverage_status": "SCENE_COVERED",
                 "scene_keys": ["consumer_activity"]},
                {"page": 3, "coverage_status": "SCENE_COVERED",
                 "scene_keys": ["office_invoice_review"]},
            ],
            "missing_scenes": [],
            "merged_distinct_scenes": [{
                "pages": [1, 2], "scene_keys": ["consumer_activity"],
                "reason": "商超挑选商品与收银台付款是不同具体场景。",
            }],
        }
        replanned = {
            "schema_version": "2.2.0",
            "page_coverage": [
                {"page": 1, "scene_keys": ["consumer_supermarket_shopping"],
                 "no_scene_reason": None, "evidence": [pages[0]["text"]]},
                {"page": 2, "scene_keys": ["consumer_checkout_payment"],
                 "no_scene_reason": None, "evidence": [pages[1]["text"]]},
                {"page": 3, "scene_keys": ["office_invoice_review"],
                 "no_scene_reason": None, "evidence": [pages[2]["text"]]},
            ],
            "scenes": [
                {**shared, "scene_key": "consumer_supermarket_shopping",
                 "canonical_scene_id": "consumer_supermarket_shopping",
                 "action": "挑选商品", "setting": "商超", "label": "商超购物",
                 "visual_query": "consumer choosing goods supermarket",
                 "photo_search_terms": {"subject": "consumer", "action": "choosing goods",
                                        "setting": "supermarket"},
                 "page_candidates": [{"page": 1, "relevance": .9,
                                      "evidence": [pages[0]["text"]]}]},
                {**shared, "scene_key": "consumer_checkout_payment",
                 "canonical_scene_id": "consumer_checkout_payment",
                 "action": "结账付款", "setting": "收银台", "label": "收银台结账",
                 "visual_query": "consumer paying at checkout counter",
                 "photo_search_terms": {"subject": "consumer", "action": "paying bill",
                                        "setting": "checkout counter"},
                 "page_candidates": [{"page": 2, "relevance": .9,
                                      "evidence": [pages[1]["text"]]}]},
                invoice,
            ],
        }
        return pages, plan, audit, replanned

    def test_complete_source_grounded_gap_is_repaired_in_ledger_and_still_reported(self):
        pages, plan, audit = self._complete_gap_audit()
        gap = _validate_scene_coverage_audit(audit, pages, plan, allow_gaps=True)
        self.assertEqual(gap["status"], "GAPS_FOUND")
        self.assertEqual(gap["missing_scenes"], 1)

        repaired = _apply_grounded_scene_gap_repairs(audit, pages, plan)
        result = repaired["plan"]
        self.assertEqual(repaired["repaired_scene_keys"], ["factory_line_worker"])
        self.assertEqual(result["page_coverage"][1]["scene_keys"], ["factory_line_worker"])
        self.assertIsNone(result["page_coverage"][1]["no_scene_reason"])
        self.assertIn("生产线工人检查设备。", result["page_coverage"][1]["evidence"])
        scene = next(row for row in result["scenes"]
                     if row["scene_key"] == "factory_line_worker")
        self.assertEqual(scene["canonical_scene_id"], "factory_line_worker_machine_check")
        self.assertEqual(scene["page_candidates"][0]["evidence"], ["生产线工人检查设备。"])
        # A repair remains a reported gap until a later full-deck verification passes.
        with self.assertRaises(SceneModelError):
            _validate_scene_coverage_audit(audit, pages, plan)

    def test_gap_repair_rejects_missing_image_prompt_fields_and_merges(self):
        pages, plan, audit = self._complete_gap_audit()
        audit["missing_scenes"][0].pop("lighting")
        with self.assertRaises(SceneModelError) as caught:
            _apply_grounded_scene_gap_repairs(audit, pages, plan)
        self.assertEqual(caught.exception.code, "SCENE.COVERAGE_AUDIT_GAPS")

        pages, plan, audit, _ = self._valid_merge_conflict()
        with self.assertRaises(SceneModelError) as caught:
            _apply_grounded_scene_gap_repairs(audit, pages, plan)
        self.assertEqual(caught.exception.code, "SCENE.COVERAGE_AUDIT_GAPS")
        self.assertIn("错误合并", caught.exception.detail)

    def test_merge_replan_splits_only_audited_scenes_and_preserves_other_pages(self):
        pages, plan, audit, replanned = self._valid_merge_conflict()
        summary = _validate_scene_coverage_audit(audit, pages, plan, allow_gaps=True)
        self.assertEqual(summary["merged_distinct_scenes"], 1)
        prompt = scene_merge_replan_prompt(pages, plan, audit)
        self.assertIn("affected_source_pages", prompt)
        self.assertIn("audited_findings", prompt)
        self.assertIn("third full-deck audit", prompt)
        payload = json.loads(prompt.split("Conflict patch payload:\n", 1)[1])
        self.assertEqual([row["page"] for row in payload["affected_source_pages"]], [1, 2])
        self.assertTrue(all(row["editable_for_this_patch"]
                            for row in payload["affected_source_pages"]))
        self.assertNotIn("prior_scene_plan", payload)
        self.assertEqual([row["page"] for row in payload["target_page_coverage"]], [1, 2])
        changed = _validate_scene_merge_replan_scope(plan, replanned, pages, audit)
        self.assertEqual(changed, ["consumer_activity", "consumer_checkout_payment",
                                   "consumer_supermarket_shopping"])

        invalid = json.loads(json.dumps(replanned, ensure_ascii=False))
        invalid["page_coverage"][2]["evidence"] = ["invented evidence"]
        with self.assertRaises(SceneModelError) as caught:
            _validate_scene_merge_replan_scope(plan, invalid, pages, audit)
        self.assertEqual(caught.exception.code, "SCENE.COVERAGE_AUDIT_REPAIR_BLOCKED")
        self.assertIn("未标记", caught.exception.detail)

    def _as_merge_patch(self, replanned):
        scenes = [scene for scene in replanned["scenes"]
                  if scene["scene_key"] != "office_invoice_review"]
        return {
            "schema_version": "1.0.0",
            "page_scene_replacements": [
                {"page": row["page"], "scene_keys": list(row["scene_keys"])}
                for row in replanned["page_coverage"] if row["page"] in (1, 2)
            ],
            "scenes": json.loads(json.dumps(scenes, ensure_ascii=False)),
        }

    def test_merge_patch_is_merged_locally_and_preserves_unaffected_full_ledger(self):
        pages, plan, audit, replanned = self._valid_merge_conflict()
        component = _scene_merge_conflict_components(audit)[0]
        merged = _merge_scene_conflict_patch(plan, self._as_merge_patch(replanned),
                                             pages, component)
        self.assertEqual(merged["page_coverage"][2], plan["page_coverage"][2])
        self.assertEqual(next(scene for scene in merged["scenes"]
                              if scene["scene_key"] == "office_invoice_review"),
                         next(scene for scene in plan["scenes"]
                              if scene["scene_key"] == "office_invoice_review"))
        self.assertNotIn("consumer_activity", {row["scene_key"] for row in merged["scenes"]})
        self.assertEqual(merged["page_coverage"][0]["scene_keys"],
                         ["consumer_supermarket_shopping"])
        self.assertEqual(merged["page_coverage"][1]["scene_keys"],
                         ["consumer_checkout_payment"])
        self.assertEqual(_validate_scene_merge_replan_scope(plan, merged, pages, audit),
                         ["consumer_activity", "consumer_checkout_payment",
                          "consumer_supermarket_shopping"])

    def test_merge_patch_can_split_affected_uses_and_keep_locked_original_scene(self):
        pages, plan, audit, replanned = self._valid_merge_conflict()
        pages.append({"page": 4, "text": "消费者在商超挑选商品。"})
        plan["page_coverage"].append({"page": 4, "scene_keys": ["consumer_activity"],
                                      "no_scene_reason": None,
                                      "evidence": [pages[3]["text"]]})
        shared = next(scene for scene in plan["scenes"]
                      if scene["scene_key"] == "consumer_activity")
        shared["page_candidates"].append({"page": 4, "relevance": .9,
                                           "evidence": [pages[3]["text"]]})
        audit["page_reviews"].append({"page": 4, "coverage_status": "SCENE_COVERED",
                                      "scene_keys": ["consumer_activity"]})
        replanned["page_coverage"].append({"page": 4, "scene_keys": ["consumer_activity"],
                                           "no_scene_reason": None,
                                           "evidence": [pages[3]["text"]]})
        component = _scene_merge_conflict_components(audit)[0]
        merged = _merge_scene_conflict_patch(plan, self._as_merge_patch(replanned),
                                             pages, component)
        retained = next(scene for scene in merged["scenes"]
                        if scene["scene_key"] == "consumer_activity")
        self.assertEqual([row["page"] for row in retained["page_candidates"]], [4])
        self.assertEqual(merged["page_coverage"][3]["scene_keys"], ["consumer_activity"])
        self.assertEqual(_validate_scene_merge_replan_scope(plan, merged, pages, audit),
                         ["consumer_activity", "consumer_checkout_payment",
                          "consumer_supermarket_shopping"])

    def test_merge_patch_rejects_ungrounded_evidence_and_out_of_scope_pages(self):
        pages, plan, audit, replanned = self._valid_merge_conflict()
        component = _scene_merge_conflict_components(audit)[0]
        patch = self._as_merge_patch(replanned)
        patch["scenes"][0]["page_candidates"][0]["evidence"] = ["编造的画面"]
        with self.assertRaises(SceneModelError) as caught:
            _merge_scene_conflict_patch(plan, patch, pages, component)
        self.assertEqual(caught.exception.code, "SCENE.COVERAGE_AUDIT_UNGROUNDED")

        patch = self._as_merge_patch(replanned)
        patch["page_scene_replacements"].append({"page": 3,
                                                 "scene_keys": ["consumer_activity"]})
        with self.assertRaises(SceneModelError) as caught:
            _merge_scene_conflict_patch(plan, patch, pages, component)
        self.assertEqual(caught.exception.code, "SCENE.COVERAGE_AUDIT_REPAIR_BLOCKED")

        patch = self._as_merge_patch(replanned)
        duplicate = json.loads(json.dumps(next(scene for scene in plan["scenes"]
                                               if scene["scene_key"] == "office_invoice_review")))
        duplicate.update({"scene_key": "invoice_review_duplicate",
                          "canonical_scene_id": "invoice_review_duplicate",
                          "page_candidates": [{"page": 1, "relevance": .9,
                                                "evidence": [pages[0]["text"]]}]})
        patch["page_scene_replacements"][0]["scene_keys"].append("invoice_review_duplicate")
        patch["scenes"].append(duplicate)
        with self.assertRaises(SceneModelError) as caught:
            _merge_scene_conflict_patch(plan, patch, pages, component)
        self.assertEqual(caught.exception.code, "SCENE.CANONICAL_SCENE_CONFLICT")

    def test_resolve_scene_semantics_uses_scoped_patch_then_third_full_deck_audit(self):
        pages, plan, audit, replanned = self._valid_merge_conflict()
        patch = self._as_merge_patch(replanned)
        calls = []

        def model_call(model, prompt):
            calls.append(prompt)
            if len(calls) == 1:
                payload = plan
            elif len(calls) == 2:
                payload = audit
            elif len(calls) == 3:
                self.assertIn("page_scene_replacements", prompt)
                self.assertNotIn("Return a complete replacement plan", prompt)
                payload = patch
            elif len(calls) == 4:
                self.assertIn("post-repair", prompt)
                payload = {
                    "schema_version": "1.2.0", "review_status": "NO_GAPS",
                    "page_reviews": [
                        {"page": 1, "coverage_status": "SCENE_COVERED",
                         "scene_keys": ["consumer_supermarket_shopping"]},
                        {"page": 2, "coverage_status": "SCENE_COVERED",
                         "scene_keys": ["consumer_checkout_payment"]},
                        {"page": 3, "coverage_status": "SCENE_COVERED",
                         "scene_keys": ["office_invoice_review"]},
                    ],
                    "missing_scenes": [], "merged_distinct_scenes": [],
                }
            else:
                self.fail("unexpected extra model call")
            return {"response": json.dumps(payload, ensure_ascii=False),
                    "model": "fake-local", "elapsed_seconds": 0.01,
                    "done_reason": "stop"}

        resolved, meta = resolve_scene_semantics(pages, model="fake-local", model_call=model_call)
        self.assertEqual(len(calls), 4)
        self.assertEqual(meta["coverage_audit"]["status"], "PASSED")
        self.assertEqual(meta["coverage_audit"]["gap_repair"]["kind"],
                         "merge_replan_patches")
        self.assertEqual(meta["coverage_audit"]["gap_repair"]["replan"]["component_count"], 1)
        self.assertEqual(resolved["page_coverage"][0]["scene_keys"],
                         ["consumer_supermarket_shopping"])

    def test_resolve_scene_semantics_fails_closed_when_scoped_patch_is_truncated(self):
        pages, plan, audit, _ = self._valid_merge_conflict()
        calls = []

        def model_call(model, prompt):
            calls.append(prompt)
            if len(calls) == 1:
                payload = plan
                return {"response": json.dumps(payload, ensure_ascii=False),
                        "model": "fake-local", "done_reason": "stop"}
            if len(calls) == 2:
                return {"response": json.dumps(audit, ensure_ascii=False),
                        "model": "fake-local", "done_reason": "stop"}
            return {"response": "{\"schema_version\":", "model": "fake-local",
                    "done_reason": "length", "eval_count": 4096}

        with self.assertRaises(SceneModelError) as caught:
            resolve_scene_semantics(pages, model="fake-local", model_call=model_call)
        self.assertEqual(caught.exception.code, "SCENE.MODEL_OUTPUT_TRUNCATED")
        self.assertIn("场景冲突补丁", caught.exception.detail)
        self.assertEqual(len(calls), 3)

    def test_new_repair_scene_requires_structured_photo_search_anchors(self):
        pages, plan, audit = self._complete_gap_audit()
        audit["missing_scenes"][0].pop("photo_search_terms")
        with self.assertRaises(SceneModelError) as caught:
            _apply_grounded_scene_gap_repairs(audit, pages, plan)
        self.assertEqual(caught.exception.code, "SCENE.COVERAGE_AUDIT_REPAIR_BLOCKED")
        self.assertIn("主体/动作/地点", caught.exception.detail)

    def test_gap_repair_accepts_strictly_valid_flat_anchor_fields(self):
        pages, plan, audit = self._complete_gap_audit()
        item = audit["missing_scenes"][0]
        item.pop("photo_search_terms")
        item.update({"subject": "worker", "action": "inspecting equipment",
                     "setting": "factory",
                     "visual_query": "worker inspecting equipment factory",
                     "visual_query_alternatives": ["employee checking machinery workshop"]})
        repaired = _apply_grounded_scene_gap_repairs(audit, pages, plan)
        scene = next(row for row in repaired["plan"]["scenes"]
                     if row["scene_key"] == item["scene_key"])
        self.assertEqual(scene["photo_search_terms"], {
            "subject": "worker", "action": "inspecting equipment", "setting": "factory"})

    def test_gap_anchor_repair_is_page_scoped_and_preserves_audited_evidence(self):
        pages, plan, audit = self._complete_gap_audit()
        prompt = scene_gap_repair_prompt(pages, plan, audit)
        self.assertIn("constrained local semantic-scene gap-record repairer", prompt)
        payload = json.loads(prompt.split("Repair payload:\n", 1)[1])
        self.assertEqual([row["page"] for row in payload["affected_source_pages"]], [2])
        repaired = {"schema_version": "1.0.0", "missing_scenes": audit["missing_scenes"]}
        self.assertEqual(_validate_scene_gap_repair_scope(audit, repaired), repaired)

        changed = json.loads(json.dumps(repaired, ensure_ascii=False))
        changed["missing_scenes"][0]["evidence"] = "编造的原文证据"
        with self.assertRaises(SceneModelError) as caught:
            _validate_scene_gap_repair_scope(audit, changed)
        self.assertEqual(caught.exception.code, "SCENE.COVERAGE_AUDIT_REPAIR_BLOCKED")

    def test_gap_repair_declined_returns_empty_missing_scenes_without_raising(self):
        pages, plan, audit = self._complete_gap_audit()
        declined_response = {"status": "BLOCKED", "reason_code": "insufficient_source_evidence"}
        result = _validate_scene_gap_repair_scope(audit, declined_response)
        self.assertEqual(result["schema_version"], "1.0.0")
        self.assertEqual(result["missing_scenes"], [])
        self.assertEqual(result["declined_reason"], "insufficient_source_evidence")

    def test_same_canonical_scene_on_a_new_page_reuses_existing_scene_record(self):
        pages = [
            {"page": 1, "text": "消费者在商超选购商品。"},
            {"page": 2, "text": "消费者在商超再次选购商品。"},
        ]
        plan = {
            "page_coverage": [
                {"page": 1, "scene_keys": ["supermarket_shopping"],
                 "evidence": ["消费者在商超选购商品。"]},
                {"page": 2, "scene_keys": [],
                 "no_scene_reason": "该页暂未登记具体消费场景，待独立复核确认。",
                 "evidence": ["消费者在商超再次选购商品。"]},
            ],
            "scenes": [{
                "scene_key": "supermarket_shopping",
                "canonical_scene_id": "consumer_supermarket_shopping",
                "topic": "消费", "subject": "consumer", "action": "choosing products",
                "depiction_type": "illustrative",
                "illustration_rationale": "超市选购是消费主题的说明性代表画面，不表示来源中的具体事件真实发生。",
                "setting": "supermarket", "label": "Consumer shopping at supermarket",
                "visual_query": "consumer choosing products in supermarket",
                "lighting": "bright store lighting",
                "composition_camera": "wide documentary view",
                "style": "realistic commercial documentary photography",
                "quality": "sharp natural detail",
                "page_candidates": [{"page": 1, "relevance": .9,
                                     "evidence": ["消费者在商超选购商品。"]}],
            }],
        }
        audit = {
            "schema_version": "1.3.0", "review_status": "GAPS_FOUND",
            "page_reviews": [
                {"page": 1, "coverage_status": "SCENE_COVERED",
                 "scene_keys": ["supermarket_shopping"]},
                {"page": 2, "coverage_status": "NO_SCENE", "scene_keys": []},
            ],
            "missing_scenes": [{
                "page": 2, "scene_key": "supermarket_shopping",
                "canonical_scene_id": "consumer_supermarket_shopping",
                "topic": "消费", "subject": "consumer", "action": "choosing products",
                "depiction_type": "illustrative",
                "illustration_rationale": "超市选购是消费主题的说明性代表画面，不表示来源中的具体事件真实发生。",
                "setting": "supermarket", "label": "Consumer shopping at supermarket",
                "visual_query": "consumer choosing products in supermarket",
                "visual_query_alternatives": [],
                "lighting": "bright store lighting",
                "composition_camera": "wide documentary view",
                "style": "realistic commercial documentary photography",
                "quality": "sharp natural detail",
                "photo_search_terms": {
                    "subject": "consumer",
                    "action": "choosing products",
                    "setting": "supermarket",
                },
                "evidence": "消费者在商超再次选购商品。",
            }],
            "merged_distinct_scenes": [],
        }
        repaired = _apply_grounded_scene_gap_repairs(audit, pages, plan)
        result = repaired["plan"]
        self.assertEqual(len(result["scenes"]), 1)
        self.assertEqual([row["page"] for row in result["scenes"][0]["page_candidates"]], [1, 2])
        self.assertEqual(result["page_coverage"][1]["scene_keys"], ["supermarket_shopping"])

    def test_same_canonical_scene_can_be_reused_across_confident_source_languages(self):
        en_text = "The consumer shops for fresh products in the supermarket store."
        zh_text = "消费者在超市货架间挑选商品。"
        pages = [
            {"page": 1, "text": en_text, "language_hint": detect_language_hint(en_text)},
            {"page": 2, "text": zh_text, "language_hint": detect_language_hint(zh_text)},
        ]
        self.assertEqual(pages[0]["language_hint"]["primary"], "en")
        self.assertEqual(pages[1]["language_hint"]["primary"], "zh")
        plan = {
            "page_coverage": [
                {"page": 1, "scene_keys": ["supermarket_shopping"], "evidence": [en_text]},
                {"page": 2, "scene_keys": [],
                 "no_scene_reason": "原始场景账本漏登记了跨语言重复场景。",
                 "evidence": [zh_text]},
            ],
            "scenes": [{
                "scene_key": "supermarket_shopping",
                "canonical_scene_id": "consumer_supermarket_shopping",
                "topic": "Consumer retail", "subject": "consumer",
                "action": "shopping for fresh products", "setting": "supermarket aisles",
                "label": "Consumer shopping in a supermarket",
                "visual_query": "consumer shopping supermarket",
                "lighting": "bright store lighting", "composition_camera": "wide documentary view",
                "style": "realistic documentary photography", "quality": "sharp natural detail",
                "page_candidates": [{"page": 1, "relevance": .9, "evidence": [en_text]}],
            }],
        }
        audit = {
            "schema_version": "1.3.0", "review_status": "GAPS_FOUND",
            "page_reviews": [
                {"page": 1, "coverage_status": "SCENE_COVERED", "scene_keys": ["supermarket_shopping"]},
                {"page": 2, "coverage_status": "NO_SCENE", "scene_keys": []},
            ],
            "missing_scenes": [{
                "page": 2, "scene_key": "supermarket_shopping",
                "canonical_scene_id": "consumer_supermarket_shopping",
                "topic": "消费零售", "subject": "消费者",
                "depiction_type": "illustrative",
                "illustration_rationale": "超市购物代表消费零售主题，不表示来源中的具体事件真实发生。",
                "action": "在超市货架间挑选商品", "setting": "超市货架通道",
                "label": "消费者在超市购物",
                "visual_query": "consumer shopping supermarket",
                "photo_search_terms": {
                    "subject": "consumer",
                    "action": "shopping products",
                    "setting": "supermarket",
                },
                "lighting": "bright store lighting", "composition_camera": "wide documentary view",
                "style": "realistic documentary photography", "quality": "sharp natural detail",
                "evidence": zh_text,
            }],
            "merged_distinct_scenes": [],
        }
        repaired = _apply_grounded_scene_gap_repairs(audit, pages, plan)["plan"]
        self.assertEqual(len(repaired["scenes"]), 1)
        self.assertEqual([row["page"] for row in repaired["scenes"][0]["page_candidates"]], [1, 2])
        self.assertEqual(repaired["page_coverage"][1]["scene_keys"], ["supermarket_shopping"])

    def test_same_language_conflicting_scene_identity_is_not_repaired(self):
        pages, plan, audit = self._complete_gap_audit()
        pages = [{**page, "language_hint": detect_language_hint(page["text"])} for page in pages]
        plan["page_coverage"][0]["scene_keys"] = ["factory_line_worker"]
        audit["page_reviews"][0]["scene_keys"] = ["factory_line_worker"]
        plan["scenes"][0].update({
            "scene_key": "factory_line_worker",
            "canonical_scene_id": "factory_line_worker_machine_check",
            "subject": "production worker", "action": "inspecting equipment",
            "setting": "factory production line",
            "page_candidates": [{"page": 1, "relevance": .9,
                                 "evidence": ["消费者在商超选购商品。"]}],
        })
        audit["missing_scenes"][0].update({
            "scene_key": "factory_line_worker",
            "canonical_scene_id": "factory_line_worker_machine_check",
            "subject": "consumer", "action": "shopping for groceries",
            "setting": "supermarket aisle", "visual_query": "consumer shopping supermarket",
        })
        with self.assertRaises(SceneModelError) as caught:
            _apply_grounded_scene_gap_repairs(audit, pages, plan)
        self.assertEqual(caught.exception.code, "SCENE.CANONICAL_SCENE_CONFLICT")


if __name__ == "__main__":
    unittest.main()
