#!/usr/bin/env python3
"""云端视觉失败时回退本机 VLM（2026-11-05 mt-e5f3bcbf）。

事故：cloud_model.json 指向 Gemini，本机 8080 的 Qwen3-VL-8B 健康在线，
但云端 402（预付额度耗尽）让整个材料任务 run rc=4 失败。原因是
call_local_vision_model 在云端目标存在时直接 return，没有任何失败回退。

修复契约：
- 云端成功 → 行为完全不变，绝不碰本机；
- 额度/鉴权/限流/网络/响应格式类失败 + 本机端点健康 → 回退本机，
  结果里如实写 vision_fallback，绝不冒充云端判定；
- 本机也不健康 → 保留原始云端错误（诊断信息更准），不静默吞掉；
- 语义类错误不在回退集合 → 直接抛；
- 云端预检仍不探测本机端点（既有契约），只在 note 里说明运行时回退。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from wengao_ppt import scene_model as sm  # noqa: E402
from wengao_ppt.scene_model import SceneModelError  # noqa: E402


CLOUD_TARGET = {"endpoint": "https://vision.example.invalid/v1beta/openai",
                "api_key": "test-key", "model": "gemini-test-flash"}
LOCAL_RESULT = {
    "response": '{"scene_match": true}',
    "model": "/Volumes/macmini/models/Qwen3-VL-8B-4bit",
    "response_sha256": "local-sha",
    "elapsed_seconds": 2.6,
}
LOCAL_ENDPOINT = "http://127.0.0.1:8080"


class CloudVisionFallbackTests(unittest.TestCase):

    def setUp(self):
        patcher = mock.patch.object(sm, "_scene_review_image_bytes", return_value=b"jpeg-bytes")
        self.addCleanup(patcher.stop)
        patcher.start()

    def _cloud_error(self, code="MODEL.QUOTA_EXCEEDED", status=402):
        return SceneModelError(code, f"cloud failed: {code}", status,
                               provider="cloud:vision.example.invalid")

    def test_cloud_success_never_touches_local(self):
        cloud_result = dict(LOCAL_RESULT, model="gemini-test-flash")
        with mock.patch.object(sm.cloud_model_config, "active_vision_target",
                               return_value=CLOUD_TARGET), \
             mock.patch.object(sm, "_call_cloud_vision_once",
                               return_value=cloud_result), \
             mock.patch.object(sm, "_call_local_vision_only") as local:
            out = sm.call_local_vision_model("qwen3.8:27b", "prompt", b"img", timeout_s=120)
        self.assertEqual(out["model"], "gemini-test-flash")
        self.assertNotIn("vision_fallback", out)
        local.assert_not_called()

    def test_quota_exceeded_falls_back_to_healthy_local_and_discloses_it(self):
        with mock.patch.object(sm.cloud_model_config, "active_vision_target",
                               return_value=CLOUD_TARGET), \
             mock.patch.object(sm, "_call_cloud_vision_once",
                               side_effect=self._cloud_error()), \
             mock.patch.object(sm, "_local_vision_fallback_ready",
                               return_value=LOCAL_ENDPOINT), \
             mock.patch.object(sm, "_call_local_vision_only",
                               return_value=dict(LOCAL_RESULT)) as local:
            out = sm.call_local_vision_model("qwen3.8:27b", "prompt", b"img", timeout_s=120)
        self.assertEqual(out["model"], "/Volumes/macmini/models/Qwen3-VL-8B-4bit")
        fb = out["vision_fallback"]
        self.assertEqual(fb["from"], "cloud")
        self.assertEqual(fb["to"], "local")
        self.assertEqual(fb["reason_code"], "MODEL.QUOTA_EXCEEDED")
        self.assertEqual(fb["status_code"], 402)
        self.assertEqual(fb["cloud_provider"], "cloud:vision.example.invalid")
        self.assertEqual(fb["local_endpoint"], "127.0.0.1:8080")
        self.assertIn("未向任何第三方外发数据", fb["detail"])
        local.assert_called_once()

    def test_local_unavailable_preserves_original_cloud_error(self):
        with mock.patch.object(sm.cloud_model_config, "active_vision_target",
                               return_value=CLOUD_TARGET), \
             mock.patch.object(sm, "_call_cloud_vision_once",
                               side_effect=self._cloud_error()), \
             mock.patch.object(sm, "_local_vision_fallback_ready", return_value=None), \
             mock.patch.object(sm, "_call_local_vision_only") as local:
            with self.assertRaises(SceneModelError) as ctx:
                sm.call_local_vision_model("m", "p", b"img", timeout_s=120)
        self.assertEqual(ctx.exception.code, "MODEL.QUOTA_EXCEEDED")
        self.assertEqual(ctx.exception.status_code, 402)
        self.assertEqual(ctx.exception.provider, "cloud:vision.example.invalid")
        local.assert_not_called()

    def test_semantic_error_is_not_swallowed(self):
        with mock.patch.object(sm.cloud_model_config, "active_vision_target",
                               return_value=CLOUD_TARGET), \
             mock.patch.object(sm, "_call_cloud_vision_once",
                               side_effect=self._cloud_error("SCENE.VISION_REVIEW_INVALID")), \
             mock.patch.object(sm, "_local_vision_fallback_ready",
                               return_value=LOCAL_ENDPOINT), \
             mock.patch.object(sm, "_call_local_vision_only") as local:
            with self.assertRaises(SceneModelError) as ctx:
                sm.call_local_vision_model("m", "p", b"img", timeout_s=120)
        self.assertEqual(ctx.exception.code, "SCENE.VISION_REVIEW_INVALID")
        local.assert_not_called()

    def test_all_declared_fallback_codes_actually_fall_back(self):
        for code in sorted(sm.CLOUD_VISION_FALLBACK_CODES):
            with self.subTest(code=code):
                with mock.patch.object(sm.cloud_model_config, "active_vision_target",
                                       return_value=CLOUD_TARGET), \
                     mock.patch.object(sm, "_call_cloud_vision_once",
                                       side_effect=self._cloud_error(code)), \
                     mock.patch.object(sm, "_local_vision_fallback_ready",
                                       return_value=LOCAL_ENDPOINT), \
                     mock.patch.object(sm, "_call_local_vision_only",
                                       return_value=dict(LOCAL_RESULT)):
                    out = sm.call_local_vision_model("m", "p", b"img", timeout_s=120)
                self.assertEqual(out["vision_fallback"]["reason_code"], code)

    def test_no_cloud_config_goes_straight_to_local(self):
        with mock.patch.object(sm.cloud_model_config, "active_vision_target",
                               return_value=None), \
             mock.patch.object(sm, "_call_local_vision_only",
                               return_value=dict(LOCAL_RESULT)) as local, \
             mock.patch.object(sm, "_call_cloud_vision_once") as cloud:
            out = sm.call_local_vision_model("m", "p", b"img", timeout_s=120)
        local.assert_called_once()
        cloud.assert_not_called()
        self.assertNotIn("vision_fallback", out)

    def test_cloud_probe_still_never_probes_local(self):
        """预检既有契约：云端分支不得探测本机端点（不产生费用/外发）。"""
        with mock.patch.object(sm.cloud_model_config, "active_vision_target",
                               return_value=CLOUD_TARGET), \
             mock.patch.object(sm, "_probe_endpoint_health",
                               side_effect=AssertionError("云端分支不得探测本机端点")):
            rec = sm.probe_vision_endpoint()
        self.assertTrue(rec["available"])
        self.assertEqual(rec["mode"], "cloud")
        self.assertIn("回退", rec["note"])


if __name__ == "__main__":
    unittest.main()
