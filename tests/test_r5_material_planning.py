from __future__ import annotations

import hashlib
import json
import socket
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from samples import r5_material_to_ppt as material


def _skeleton_plan():
    return {
        "deck_title": "合成规划测试",
        "slides": [
            {"page": 1, "page_type": "cover", "title": "合成规划测试",
             "must_state": []},
            {"page": 2, "page_type": "agenda", "title": "目录",
             "must_state": []},
            {"page": 3, "page_type": "single_column", "title": "产能",
             "must_state": []},
            {"page": 4, "page_type": "single_column", "title": "消费",
             "must_state": []},
        ],
        "omitted_facts": [],
    }


class MaterialModelTimeoutTests(unittest.TestCase):
    def test_bare_socket_timeout_is_normalized_and_does_not_retry_fallback(self):
        with patch.dict("os.environ", {
                "WENGAO_LOCAL_MODEL_ENDPOINT": "http://127.0.0.1:11434",
                "WENGAO_FALLBACK_MODEL_ENDPOINT": "http://127.0.0.1:18888"}), \
                patch.object(material, "_call_model_single",
                             side_effect=TimeoutError("timed out")) as call:
            with self.assertRaises(material.ModelCallError) as caught:
                material.call_model("qwen-test", "synthetic", 8)

        self.assertEqual(caught.exception.code, "MODEL.REQUEST_TIMEOUT")
        self.assertIn("900s", caught.exception.detail)
        call.assert_called_once()

    def test_urlerror_wrapping_socket_timeout_is_not_mislabeled_transport(self):
        timeout = urllib.error.URLError(socket.timeout("timed out"))
        with patch.dict("os.environ", {
                "WENGAO_LOCAL_MODEL_ENDPOINT": "http://127.0.0.1:11434"}), \
                patch.object(material.urllib.request, "urlopen", side_effect=timeout):
            with self.assertRaises(material.ModelCallError) as caught:
                material.call_model("qwen-test", "synthetic", 8)

        self.assertEqual(caught.exception.code, "MODEL.REQUEST_TIMEOUT")
        self.assertIsNone(caught.exception.status_code)


class DeterministicMaterialAssignmentTests(unittest.TestCase):
    def test_source_sentences_are_assigned_once_without_per_section_model_calls(self):
        sentence = "产能提升至20%，新增10条生产线。"
        consumer_sentence = "消费者在门店核销优惠券。"
        facts = [
            {"fact_id": "F001", "subject": "产能", "raw_value_text": "20%",
             "qualitative": False, "source_ref": {"sentence": sentence}},
            {"fact_id": "F002", "subject": "产能", "raw_value_text": "10条",
             "qualitative": False, "source_ref": {"sentence": sentence}},
            {"fact_id": "F003", "subject": "消费", "raw_value_text": "门店",
             "qualitative": True, "source_ref": {"sentence": consumer_sentence}},
        ]

        with patch.object(material, "make_global_plan",
                          return_value=(_skeleton_plan(), [])), \
                patch.object(material, "fill_chunk",
                             side_effect=AssertionError("unexpected extra model call")):
            plan, trace = material.make_plan_chunked("qwen-test", facts, 4)

        self.assertFalse(plan.get("_failed"), plan.get("error"))
        content = [slide for slide in plan["slides"]
                   if slide.get("page_type") not in material.STRUCTURAL_TYPES]
        statements = [item for slide in content for item in slide["must_state"]]
        self.assertEqual(statements.count(sentence), 1)
        self.assertEqual(statements.count(consumer_sentence), 1)
        self.assertIn("20%", "\n".join(statements))
        self.assertIn("10条", "\n".join(statements))
        self.assertEqual(material.validate_plan(plan, 4, facts), [])
        assignments = [row for row in trace if row.get("phase") == "source_assignment"]
        self.assertEqual(len(assignments), 2)
        self.assertTrue(all(row["model_calls"] == 0 for row in assignments))

    def test_fact_without_source_sentence_or_value_fails_closed(self):
        facts = [{"fact_id": "F001", "subject": "产能", "source_ref": {}}]
        with patch.object(material, "make_global_plan",
                          return_value=(_skeleton_plan(), [])), \
                patch.object(material, "fill_chunk",
                             side_effect=AssertionError("unexpected extra model call")):
            plan, _trace = material.make_plan_chunked("qwen-test", facts, 4)

        self.assertTrue(plan.get("_failed"))
        self.assertIn("F001", plan.get("error", ""))


class CompletedPlanRevalidationTests(unittest.TestCase):
    @staticmethod
    def _plan(*, long_title=False):
        slides = [
            {"page": 1, "page_type": "cover", "title": "合成复验",
             "must_state": []},
            {"page": 2, "page_type": "agenda", "title": "目录",
             "must_state": ["章节一", "章节二"]},
        ]
        for page in range(3, 11):
            title = "X" * 31 if long_title and page == 4 else f"内容{page}"
            items = [f"唯一内容{page}"]
            if page in (3, 4):
                items.append("৯")
            slides.append({"page": page, "page_type": "single_column",
                           "title": title, "must_state": items})
        return {"deck_title": "合成重复项复验", "slides": slides,
                "omitted_facts": []}

    def test_source_completion_clears_only_repaired_duplicate_failure(self):
        plan = self._plan()
        before = material.validate_plan(plan, 10)
        self.assertTrue(any("完全重复条目" in problem for problem in before))
        plan["_failed"] = True
        plan["error"] = "; ".join(before)

        completion = material.complete_source_sentences(plan, [])
        self.assertEqual(len(completion["dropped_duplicates"]), 1)
        self.assertEqual(material.validate_plan(plan, 10), [])

        problems = material.revalidate_completed_plan(plan, 10)

        self.assertEqual(problems, [])
        self.assertNotIn("_failed", plan)
        self.assertNotIn("error", plan)

    def test_final_validation_preserves_unrepaired_failure(self):
        plan = self._plan(long_title=True)
        plan["_failed"] = True
        plan["error"] = "stale duplicate error"
        material.complete_source_sentences(plan, [])

        problems = material.revalidate_completed_plan(plan, 10)

        self.assertTrue(any("slide3.title 过长" in problem
                            for problem in problems))
        self.assertTrue(plan.get("_failed"))
        self.assertIn("slide3.title 过长", plan.get("error", ""))


class MultilingualTitleLengthTests(unittest.TestCase):
    def test_bengali_combining_marks_do_not_consume_title_units(self):
        title = "নীলপথ সমবায়ের ২০২৬ সালের প্রথমার্ধের কা"
        self.assertEqual(len(title), 40)
        self.assertEqual(material._title_visible_units(title), 29)

        plan = {
            "deck_title": "বাংলা প্রতিবেদন",
            "slides": [{"page": 1, "page_type": "cover", "title": title,
                        "must_state": []}],
            "omitted_facts": [],
        }
        self.assertEqual(material.validate_plan(plan, 1), [])

    def test_latin_title_limit_remains_hard(self):
        plan = {
            "deck_title": "Synthetic report",
            "slides": [{"page": 1, "page_type": "cover",
                        "title": "A" * 31, "must_state": []}],
            "omitted_facts": [],
        }
        self.assertIn("slide0.title 过长", material.validate_plan(plan, 1))

    def test_continuation_title_uses_neutral_marker_and_visible_limit(self):
        continued = material._continued_title(
            "নীলপথ সমবায়ের ২০২৬ সালের প্রথমার্ধের কার্যক্রম " * 2)
        self.assertTrue(continued.endswith("…"))
        self.assertNotIn("续", continued)
        self.assertLessEqual(material._title_visible_units(continued),
                             material.MAX_TITLE_VISIBLE_UNITS)

    def test_bengali_plan_titles_and_agenda_use_grapheme_safe_visible_limit(self):
        # 16 visible units are followed by a Bengali combining vowel sign.
        # Raw [:16] drops the sign and changes the rendered word.
        subject = "ক" * 15 + "বি" + "শ্রেণি"
        deck_title = "R" * 23 + "বি" + "শিরোনাম"
        sentence = "শ্রমিকেরা উৎপাদন লাইনে কাজ করেন।"
        skeleton = {
            "deck_title": deck_title,
            "slides": [{"page": 1, "page_type": "single_column",
                        "title": subject, "must_state": []}],
            "omitted_facts": [],
        }
        facts = [{"fact_id": "F-BN-1", "subject": subject,
                  "raw_value_text": "", "qualitative": True,
                  "source_ref": {"sentence": sentence}}]

        with patch.object(material, "make_global_plan",
                          return_value=(skeleton, [])):
            plan, _trace = material.make_plan_chunked("qwen-test", facts, 3)

        self.assertFalse(plan.get("_failed"), plan.get("error"))
        cover, agenda, content = plan["slides"]
        self.assertEqual(cover["title"],
                         material._truncate_title_visible_units(deck_title, 24))
        self.assertEqual(agenda["must_state"], [
            material._truncate_title_visible_units(subject, 16)])
        self.assertEqual(content["title"],
                         material._truncate_title_visible_units(subject, 16))
        self.assertTrue(agenda["must_state"][0].endswith("ি"))
        self.assertEqual(material._title_visible_units(content["title"]), 16)


class ChartFallbackPageCountTests(unittest.TestCase):
    def test_qualitative_chart_fallback_does_not_leave_empty_chart_slide(self):
        from pptx import Presentation
        from wengao_ppt import theme

        item = "消费者在商超结算台完成付款。"
        plan = {
            "deck_title": "合成场景规划",
            "slides": [{"page": 1, "page_type": "chart",
                        "title": "消费场景", "must_state": [item]}],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            out_path = Path(temp_dir) / "qualitative-chart-fallback.pptx"
            material.build_pptx(plan, [], out_path, theme)
            generated = Presentation(str(out_path))

        self.assertEqual(len(generated.slides), 1)
        slide_text = "\n".join(
            shape.text for shape in generated.slides[0].shapes
            if getattr(shape, "has_text_frame", False))
        self.assertIn(item, slide_text)


class BuilderPageManifestReconciliationTests(unittest.TestCase):
    def test_real_page_count_mismatch_fixture_reconciles_after_chart(self):
        from pptx import Presentation
        from wengao_ppt import theme

        fixture_dir = (Path(__file__).parent / "fixtures" /
                       "r5-page-count-mismatch")
        fixture = json.loads((fixture_dir / "fixture_manifest.json").read_text(
            encoding="utf-8"))
        for source in fixture["source_files"]:
            payload = (fixture_dir / source["path"]).read_bytes()
            self.assertEqual(len(payload), source["size"])
            self.assertEqual(hashlib.sha256(payload).hexdigest(), source["sha256"])

        plan = json.loads((fixture_dir / "content_plan.json").read_text(
            encoding="utf-8"))
        ledger = json.loads((fixture_dir / "fact_ledger.json").read_text(
            encoding="utf-8"))
        facts = ledger["facts"]
        planner_pages = material.expected_slide_count(plan)
        self.assertEqual(planner_pages, fixture["planner_pages"])
        chart_plan_index = next(i for i, slide in enumerate(plan["slides"])
                                if slide.get("page_type") == "chart")
        chart_facts = list(plan["slides"][chart_plan_index]["must_state"])

        with tempfile.TemporaryDirectory() as temp_dir:
            out_path = Path(temp_dir) / "reconciled.generated.pptx"
            generated = material.build_pptx(plan, facts, out_path, theme)
            reopened = Presentation(str(out_path))
            physical_count = len(reopened.slides)
            reconciled, pagination = material.reconcile_built_page_manifest(
                plan, plan["_pagination"],
                generated._rendered_page_manifest, physical_count)

        self.assertEqual(physical_count, fixture["actual_pptx_pages"])
        self.assertEqual(len(reconciled["slides"]), physical_count)
        self.assertEqual(material.expected_slide_count(reconciled), physical_count)
        self.assertTrue(pagination["builder_page_manifest_verified"])
        self.assertEqual(pagination["planner_pages_after"], planner_pages)
        self.assertEqual(pagination["pages_after"], physical_count)
        self.assertEqual(pagination["builder_generated_continuations"], 1)
        self.assertEqual([slide["page"] for slide in reconciled["slides"]],
                         list(range(1, physical_count + 1)))

        chart_actual_index = next(i for i, slide in enumerate(reconciled["slides"])
                                  if slide.get("page_type") == "chart")
        chart_actual = reconciled["slides"][chart_actual_index]
        continuation = reconciled["slides"][chart_actual_index + 1]
        self.assertTrue(continuation.get("_builder_generated_continuation"))
        self.assertEqual(continuation["page"], chart_actual["page"] + 1)
        self.assertEqual(continuation["_continued_from"], chart_actual["page"])
        self.assertTrue(continuation["must_state"])
        self.assertEqual(reconciled["slides"][chart_actual_index + 2]["page"],
                         continuation["page"] + 1)
        self.assertEqual(len(generated._page_type_decisions), physical_count)

        rendered_parts = []
        for slide in reopened.slides:
            for shape in slide.shapes:
                if getattr(shape, "has_text_frame", False):
                    rendered_parts.append(shape.text)
                if getattr(shape, "has_table", False):
                    rendered_parts.extend(
                        cell.text for row in shape.table.rows for cell in row.cells)
        rendered_text = "\n".join(rendered_parts)
        for statement in chart_facts:
            self.assertIn(statement, rendered_text)

        with self.assertRaisesRegex(RuntimeError, "builder manifest"):
            material.reconcile_built_page_manifest(
                plan, plan["_pagination"],
                generated._rendered_page_manifest[:-1], physical_count)

    def test_table_overflow_continues_in_source_order_next_to_parent(self):
        from pptx import Presentation
        from wengao_ppt import theme

        first = "甲" * 210
        second = "乙" * 210
        third = "丙"
        plan = {
            "deck_title": "表格续页顺序复验",
            "slides": [
                {"page": 1, "page_type": "cover", "title": "表格续页顺序复验",
                 "must_state": []},
                {"page": 2, "page_type": "table", "title": "完整事实",
                 "must_state": [first, second, third]},
                {"page": 3, "page_type": "summary_action", "title": "结论",
                 "must_state": ["结论页应排在续表之后。"]},
            ],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            out_path = Path(temp_dir) / "ordered-continuations.pptx"
            generated = material.build_pptx(plan, [], out_path, theme)
            reopened = Presentation(str(out_path))
            reconciled, pagination = material.reconcile_built_page_manifest(
                plan, {"pages_after": 3},
                generated._rendered_page_manifest, len(reopened.slides))

        self.assertEqual(len(reopened.slides), 4)
        self.assertEqual(pagination["pages_after"], 4)
        self.assertEqual(reconciled["slides"][1]["must_state"], [first, second, third])
        continuation = reconciled["slides"][2]
        self.assertTrue(continuation.get("_builder_generated_continuation"))
        self.assertEqual(continuation["_continued_from"], 2)
        self.assertEqual(continuation["must_state"], [second, third])
        self.assertEqual(reconciled["slides"][3]["title"], "结论")

        continuation_cells = [
            cell.text for row in reopened.slides[2].shapes[1].table.rows
            for cell in row.cells]
        self.assertEqual(continuation_cells[1:], [second, third])

    def test_single_table_item_over_page_capacity_fails_closed(self):
        from wengao_ppt import theme

        plan = {
            "deck_title": "超长事实复验",
            "slides": [
                {"page": 1, "page_type": "cover", "title": "超长事实复验",
                 "must_state": []},
                {"page": 2, "page_type": "table", "title": "完整事实",
                 "must_state": ["完整原句" * 500]},
            ],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            out_path = Path(temp_dir) / "oversized-row.pptx"
            with self.assertRaisesRegex(RuntimeError, "TABLE_ROW_EXCEEDS_PAGE_CAPACITY"):
                material.build_pptx(plan, [], out_path, theme)


if __name__ == "__main__":
    unittest.main()
