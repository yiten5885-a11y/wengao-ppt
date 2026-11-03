import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wengao_ppt import cloud_model_config as cmc


class CloudVisionOnlyTests(unittest.TestCase):
    """仅云端视觉：图片复核走云端，文本规划与生图仍走本机。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        d = Path(self._tmp.name)
        for name, value in (("CONFIG_DIR", d), ("CONFIG_PATH", d / "cloud_model.json")):
            patcher = mock.patch.object(cmc, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _write(self, **cfg):
        base = {"enabled": False, "endpoint": "https://cloud.example/v1", "api_key": "k",
                "text_model": "t-model", "vision_model": "v-model",
                "image_gen_model": "g-model"}
        base.update(cfg)
        cmc.CONFIG_PATH.write_text(json.dumps(base), encoding="utf-8")

    def test_vision_only_routes_vision_to_cloud_and_keeps_text_local(self):
        self._write(vision_enabled=True)
        self.assertEqual(cmc.active_vision_target()["model"], "v-model")
        self.assertIsNone(cmc.active_text_target())
        self.assertIsNone(cmc.active_image_gen_target())

    def test_vision_only_falls_back_to_text_model_name(self):
        self._write(vision_enabled=True, vision_model="")
        self.assertEqual(cmc.active_vision_target()["model"], "t-model")

    def test_everything_off_keeps_vision_local(self):
        self._write()
        self.assertIsNone(cmc.active_vision_target())

    def test_old_config_without_field_defaults_off(self):
        self._write()
        raw = json.loads(cmc.CONFIG_PATH.read_text(encoding="utf-8"))
        raw.pop("vision_enabled", None)
        cmc.CONFIG_PATH.write_text(json.dumps(raw), encoding="utf-8")
        self.assertFalse(cmc.load()["vision_enabled"])
        self.assertIsNone(cmc.active_vision_target())

    def test_vision_only_still_needs_a_key(self):
        self._write(vision_enabled=True, api_key="")
        self.assertIsNone(cmc.active_vision_target())

    def test_save_keeps_vision_flag_when_not_passed(self):
        self._write(vision_enabled=True)
        cmc.save(enabled=False, endpoint="https://cloud.example/v1",
                 text_model="t-model", vision_model="v-model")
        self.assertTrue(cmc.load()["vision_enabled"])
        self.assertTrue(cmc.status()["vision_enabled"])
        self.assertFalse(cmc.status()["api_key_configured"] is False)


class CloudVisionQuotaTests(unittest.TestCase):
    def test_402_credits_depleted_is_quota_not_generic_http(self):
        import io
        import urllib.error
        from wengao_ppt.scene_model import SceneModelError, _call_cloud_vision_once
        body = (b'{"error":{"code":402,"message":"Your prepayment credits are depleted.",'
                b'"status":"RESOURCE_EXHAUSTED"}}')
        target = {"endpoint": "https://cloud.example/v1", "api_key": "k", "model": "m"}
        err = urllib.error.HTTPError(target["endpoint"], 402, "pay", {}, io.BytesIO(body))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(SceneModelError) as ctx:
                _call_cloud_vision_once(target, "p", "aGk=", 60)
        self.assertEqual(ctx.exception.code, "MODEL.QUOTA_EXCEEDED")


if __name__ == "__main__":
    unittest.main()
