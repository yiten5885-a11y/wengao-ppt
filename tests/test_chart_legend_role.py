"""Chart legend text beside a bar column is chart_aux, not body (2026-10-29)."""
import importlib.util
import unittest
from pathlib import Path

from wengao_ppt.type_scale import is_chart_legend_label


def _text(left, top, text, width=1.2, height=0.18):
    return {"left": left, "top": top, "width": width, "height": height,
            "text": text, "solid": False}


def _bar(left, top, width, height=0.21):
    return {"left": left, "top": top, "width": width, "height": height,
            "text": "", "solid": True}


# job-3afdd5d88bd9 page 6: L5..L3 level bars, label right of each bar
LEVEL_CHART = [_bar(9.46, 4.78, 0.62), _text(10.19, 4.80, "L5 引领级"),
               _bar(9.46, 5.06, 0.85), _text(10.42, 5.08, "L4 卓越级"),
               _bar(9.46, 5.34, 1.08), _text(10.65, 5.36, "L3 先进级")]


class ChartLegendRoleTests(unittest.TestCase):
    def test_labels_beside_a_bar_chart_column(self):
        for i in (1, 3, 5):
            with self.subTest(text=LEVEL_CHART[i]["text"]):
                self.assertTrue(is_chart_legend_label(i, LEVEL_CHART))

    def test_equal_width_tag_chips_are_not_a_chart(self):
        boxes = [_bar(0.88, 4.91, 0.67, 0.23), _text(1.71, 4.92, "只申报一个能力域"),
                 _bar(0.88, 5.30, 0.67, 0.23), _text(1.71, 5.31, "覆盖全部能力域")]
        self.assertFalse(is_chart_legend_label(1, boxes))

    def test_hairlines_and_single_accent_bar_are_not_a_chart(self):
        boxes = [_bar(0.62, 1.76, 0.25, 0.02), _text(1.00, 1.68, "城市级 Token 超市"),
                 _bar(0.62, 6.16, 3.96, 0.01)]
        self.assertFalse(is_chart_legend_label(1, boxes))
        header = [_bar(0.60, 0.66, 0.30, 0.12), _text(0.62, 0.62, "01 战略转变")]
        self.assertFalse(is_chart_legend_label(1, header))

    def test_long_text_is_never_a_legend(self):
        boxes = LEVEL_CHART + [_text(10.19, 4.80, "这是一段很长很长很长的正文描述内容超过限制")]
        self.assertFalse(is_chart_legend_label(len(boxes) - 1, boxes))

    def test_gate_and_product_share_the_same_rule(self):
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("lg_for_test", root / "scripts" / "layout_gate.py")
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        self.assertIs(gate.is_chart_legend_label, is_chart_legend_label)
        from wengao_ppt import refine
        self.assertIn("is_chart_legend_label", refine.infer_text_roles.__code__.co_names
                      + refine.infer_text_roles.__code__.co_varnames)


class VisionOomBackoffTests(unittest.TestCase):
    def test_oom_is_retried_then_succeeds(self):
        from unittest import mock
        from wengao_ppt import scene_model as sm
        oom = sm.SceneModelError("MODEL.VISION_HTTP_ERROR",
                                 "Generation failed: [METAL] Command buffer execution failed: "
                                 "Insufficient Memory (00000008)", 500)
        with mock.patch.object(sm, "call_local_vision_model",
                               side_effect=[oom, {"response": "{}"}]) as call, \
                mock.patch.object(sm.time, "sleep") as sleep:
            self.assertEqual(sm._call_vision_with_oom_backoff("m", "p", b"x", 60),
                             {"response": "{}"})
        self.assertEqual(call.call_count, 2)
        sleep.assert_called_once_with(sm._VISION_OOM_BACKOFF_S[0])

    def test_other_errors_are_not_retried(self):
        from unittest import mock
        from wengao_ppt import scene_model as sm
        err = sm.SceneModelError("MODEL.VISION_HTTP_ERROR", "bad request", 400)
        with mock.patch.object(sm, "call_local_vision_model", side_effect=err) as call, \
                mock.patch.object(sm.time, "sleep"):
            with self.assertRaises(sm.SceneModelError):
                sm._call_vision_with_oom_backoff("m", "p", b"x", 60)
        self.assertEqual(call.call_count, 1)

    def test_persistent_oom_gives_up_after_backoffs(self):
        from unittest import mock
        from wengao_ppt import scene_model as sm
        oom = sm.SceneModelError("MODEL.VISION_HTTP_ERROR", "Insufficient Memory", 500)
        with mock.patch.object(sm, "call_local_vision_model", side_effect=oom) as call, \
                mock.patch.object(sm.time, "sleep"):
            with self.assertRaises(sm.SceneModelError):
                sm._call_vision_with_oom_backoff("m", "p", b"x", 60)
        self.assertEqual(call.call_count, len(sm._VISION_OOM_BACKOFF_S) + 1)


if __name__ == "__main__":
    unittest.main()
