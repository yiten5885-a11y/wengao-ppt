import socket
import urllib.error
import urllib.request
import unittest
from unittest import mock

from wengao_ppt.scene_model import (
    SceneModelError,
    _call_endpoint_once,
    _scene_timeout_budget,
    _response_payload,
    call_local_model,
)


class SceneModelTimeoutTests(unittest.TestCase):
    def test_scene_policy_error_is_not_mislabeled_as_local_model_failure(self):
        record = SceneModelError("SCENE.COVERAGE_AUDIT_GAPS", "audit gaps").as_record()
        self.assertEqual(record["provider"], "semantic-scene-gate")

    def test_model_timeout_remains_attributed_to_local_model(self):
        record = SceneModelError("MODEL.REQUEST_TIMEOUT", "slow").as_record()
        self.assertEqual(record["provider"], "ollama-local")

    def test_direct_socket_timeout_has_request_timeout_code(self):
        opener = mock.Mock()
        opener.open.side_effect = socket.timeout("timed out")
        with mock.patch("wengao_ppt.scene_model.urllib.request.build_opener",
                        return_value=opener) as build_opener:
            with self.assertRaises(SceneModelError) as caught:
                _call_endpoint_once("http://127.0.0.1:11434", "qwen", "{}", 7)
        build_opener.assert_called_once()
        proxy_handler = build_opener.call_args.args[0]
        self.assertIsInstance(proxy_handler, urllib.request.ProxyHandler)
        self.assertEqual(proxy_handler.proxies, {})
        opener.open.assert_called_once()
        self.assertEqual(caught.exception.code, "MODEL.REQUEST_TIMEOUT")
        self.assertIn("7 秒", caught.exception.detail)

    def test_urlerror_wrapped_socket_timeout_has_request_timeout_code(self):
        wrapped = urllib.error.URLError(socket.timeout("timed out"))
        opener = mock.Mock()
        opener.open.side_effect = wrapped
        with mock.patch("wengao_ppt.scene_model.urllib.request.build_opener",
                        return_value=opener) as build_opener:
            with self.assertRaises(SceneModelError) as caught:
                _call_endpoint_once("http://127.0.0.1:11434", "qwen", "{}", 9)
        build_opener.assert_called_once()
        proxy_handler = build_opener.call_args.args[0]
        self.assertIsInstance(proxy_handler, urllib.request.ProxyHandler)
        self.assertEqual(proxy_handler.proxies, {})
        opener.open.assert_called_once()
        self.assertEqual(caught.exception.code, "MODEL.REQUEST_TIMEOUT")
        self.assertIn("9 秒", caught.exception.detail)

    def test_timeout_does_not_trigger_a_second_model_call(self):
        with mock.patch("wengao_ppt.scene_model._loopback_endpoint",
                        return_value="http://127.0.0.1:11434"), \
                mock.patch("wengao_ppt.scene_model._call_endpoint_once",
                           side_effect=SceneModelError("MODEL.REQUEST_TIMEOUT", "slow")) as call:
            with self.assertRaises(SceneModelError) as caught:
                call_local_model("qwen", "{}", timeout_s=3)
        self.assertEqual(caught.exception.code, "MODEL.REQUEST_TIMEOUT")
        call.assert_called_once_with("http://127.0.0.1:11434", "qwen", "{}", 3,
                                     num_predict=4096, keep_alive="15m")

    def test_scene_timeout_defaults_to_material_planner_budget(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(_scene_timeout_budget(None), 900)

    def test_scene_timeout_budget_is_configurable_but_bounded(self):
        with mock.patch.dict("os.environ", {"WENGAO_SCENE_MODEL_TIMEOUT_S": "1200"}):
            self.assertEqual(_scene_timeout_budget(None), 1200)
        with mock.patch.dict("os.environ", {"WENGAO_SCENE_MODEL_TIMEOUT_S": "30"}):
            with self.assertRaises(SceneModelError) as caught:
                _scene_timeout_budget(None)
        self.assertEqual(caught.exception.code, "MODEL.CONFIG_INVALID")

    def test_audit_call_has_capacity_for_complete_repair_json(self):
        expected = {"response": "{}", "model": "qwen"}
        with mock.patch("wengao_ppt.scene_model._loopback_endpoint",
                        return_value="http://127.0.0.1:11434"), \
                mock.patch("wengao_ppt.scene_model._call_endpoint_once",
                           return_value=expected) as call:
            got = call_local_model(
                "qwen", "You are a strict second-pass semantic coverage auditor. review", timeout_s=3)
        self.assertEqual(got, expected)
        call.assert_called_once_with("http://127.0.0.1:11434", "qwen",
                                     "You are a strict second-pass semantic coverage auditor. review", 3,
                                     num_predict=4096, keep_alive="15m")

    def test_truncated_audit_reports_metadata_without_persisting_response(self):
        response = {
            "response": '{"schema_version":"1.2.0",',
            "model": "qwen3.8:27b",
            "elapsed_seconds": 42.0,
            "response_sha256": "a" * 64,
            "prompt_eval_count": 1800,
            "eval_count": 1024,
            "done_reason": "length",
        }
        with self.assertRaises(SceneModelError) as caught:
            _response_payload(response, phase="逐页复核")
        self.assertEqual(caught.exception.code, "SCENE.MODEL_OUTPUT_TRUNCATED")
        record = caught.exception.as_record()
        self.assertEqual(record["provider"], "semantic-scene-gate")
        self.assertEqual(record["diagnostics"]["done_reason"], "length")
        self.assertEqual(record["diagnostics"]["eval_count"], 1024)
        self.assertEqual(record["diagnostics"]["response_chars"], len(response["response"]))
        self.assertNotIn("response", record["diagnostics"])
        self.assertNotIn("prompt", record["diagnostics"])

    def test_unavailable_fallback_probe_preserves_primary_transport_error(self):
        primary = SceneModelError("MODEL.TRANSPORT_ERROR", "primary endpoint refused")
        with mock.patch("wengao_ppt.scene_model._loopback_endpoint",
                        return_value="http://127.0.0.1:11434"), \
                mock.patch("wengao_ppt.scene_model._call_endpoint_once",
                           side_effect=primary), \
                mock.patch("wengao_ppt.scene_model.urllib.request.urlopen",
                           side_effect=urllib.error.URLError(ConnectionRefusedError())):
            with self.assertRaises(SceneModelError) as caught:
                call_local_model("qwen", "{}", timeout_s=3)
        self.assertEqual(caught.exception.code, "MODEL.TRANSPORT_ERROR")
        self.assertEqual(caught.exception.detail, "primary endpoint refused")


if __name__ == "__main__":
    unittest.main()
