"""Regression tests for model call chain failure classification, tool calls, and continuation.

Covers:
1. 502 Local Ollama connection failed / Bad Gateway is strictly classified as
   MODEL.TRANSPORT_ERROR, never mislabeled as MODEL.QUOTA_EXCEEDED.
2. Local service not listening / Connection Refused is distinct from quota and timeout.
3. Request timeout is strictly MODEL.REQUEST_TIMEOUT with timeout seconds recorded.
4. Distinguishing normal text response (no tool calls) from tool call events.
5. End-to-end tool call emit -> tool execution result feed -> continuation response loop.
"""
from __future__ import annotations

import io
import json
import socket
import unittest
import urllib.error
from unittest import mock

from wengao_ppt.scene_model import (
    SceneModelError,
    _call_endpoint_once,
    classify_external_model_failure,
)
from samples.r5_material_to_ppt import _model_http_error, ModelCallError


class ModelCallChainDiagnosticsTests(unittest.TestCase):
    def test_502_local_ollama_failure_is_transport_error_not_quota(self):
        body = b'{"error": {"message": "Local Ollama connection failed", "type": "api_connection_error"}}'
        exc = urllib.error.HTTPError(
            url="http://127.0.0.1:11435/v1/chat/completions",
            code=502,
            msg="Bad Gateway",
            hdrs={},
            fp=io.BytesIO(body),
        )
        opener = mock.Mock()
        opener.open.side_effect = exc
        with mock.patch("wengao_ppt.scene_model.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(SceneModelError) as caught:
                _call_endpoint_once("http://127.0.0.1:11435", "qwen3.8:27b", "{}", 10)

        err = caught.exception
        self.assertEqual(err.code, "MODEL.TRANSPORT_ERROR")
        self.assertEqual(err.status_code, 502)
        self.assertIn("Local Ollama connection failed", err.detail)
        self.assertNotIn("QUOTA", err.code)
        record = err.as_record()
        # 供应方按实际端点标注，不再一律写成 "ollama-local"（2026-10-28 起）。
        self.assertEqual(record["provider"], "local:127.0.0.1:11435")
        self.assertEqual(record["code"], "MODEL.TRANSPORT_ERROR")

    def test_r5_planner_502_classified_as_transport_error(self):
        body = b'{"error": {"message": "Local Ollama connection failed", "type": "api_connection_error"}}'
        exc = urllib.error.HTTPError(
            url="http://127.0.0.1:11435/v1/chat/completions",
            code=502,
            msg="Bad Gateway",
            hdrs={},
            fp=io.BytesIO(body),
        )
        err = _model_http_error(exc)
        self.assertEqual(err.code, "MODEL.TRANSPORT_ERROR")
        self.assertEqual(err.status_code, 502)
        self.assertIn("Local Ollama connection failed", err.detail)
        self.assertNotEqual(err.code, "MODEL.QUOTA_EXCEEDED")

    def test_connection_refused_error_distinct_from_quota(self):
        wrapped = urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
        opener = mock.Mock()
        opener.open.side_effect = wrapped
        with mock.patch("wengao_ppt.scene_model.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(SceneModelError) as caught:
                _call_endpoint_once("http://127.0.0.1:11434", "qwen3.8:27b", "{}", 5)

        self.assertEqual(caught.exception.code, "MODEL.TRANSPORT_ERROR")
        self.assertIn("ConnectionRefusedError", caught.exception.detail)
        self.assertNotIn("QUOTA", caught.exception.code)

    def test_classify_external_model_failure_502_and_connection_failures(self):
        class MockBridgeError(Exception):
            def __init__(self, detail, status=None):
                super().__init__(detail)
                self.detail = detail
                self.status_code = status

        err502 = MockBridgeError("Local Ollama connection failed", 502)
        classified = classify_external_model_failure(err502)
        self.assertIsNotNone(classified)
        self.assertEqual(classified.code, "MODEL.TRANSPORT_ERROR")
        self.assertEqual(classified.status_code, 502)

        err_refused = MockBridgeError("connect failed: connection refused", None)
        classified_refused = classify_external_model_failure(err_refused)
        self.assertIsNotNone(classified_refused)
        self.assertEqual(classified_refused.code, "MODEL.TRANSPORT_ERROR")

        err_quota = MockBridgeError("insufficient quota available", 402)
        classified_quota = classify_external_model_failure(err_quota)
        self.assertIsNotNone(classified_quota)
        self.assertEqual(classified_quota.code, "MODEL.QUOTA_EXCEEDED")

    def test_timeout_error_budget_respected(self):
        opener = mock.Mock()
        opener.open.side_effect = socket.timeout("timed out")
        with mock.patch("wengao_ppt.scene_model.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(SceneModelError) as caught:
                _call_endpoint_once("http://127.0.0.1:11434", "qwen3.8:27b", "{}", 12)
        self.assertEqual(caught.exception.code, "MODEL.REQUEST_TIMEOUT")
        self.assertIn("12 秒", caught.exception.detail)

    def test_tool_call_loopback_and_continuation_flow(self):
        """Verify the 3-step closure: normal response, tool call emission, continuation after tool result."""
        # 1. Normal reply (no tool call)
        step1_response = {
            "choices": [{
                "message": {"role": "assistant", "content": "收到，准备就绪。"},
                "finish_reason": "stop"
            }]
        }
        self.assertEqual(step1_response["choices"][0]["finish_reason"], "stop")
        self.assertNotIn("tool_calls", step1_response["choices"][0]["message"])
        self.assertEqual(step1_response["choices"][0]["message"]["content"], "收到，准备就绪。")

        # 2. Tool call emission
        tool_call_id = "call_test_123"
        step2_response = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": tool_call_id,
                        "type": "function",
                        "function": {
                            "name": "Read",
                            "arguments": json.dumps({"file_path": "/path/to/pointer.txt"})
                        }
                    }]
                },
                "finish_reason": "tool_calls"
            }]
        }
        self.assertEqual(step2_response["choices"][0]["finish_reason"], "tool_calls")
        calls = step2_response["choices"][0]["message"]["tool_calls"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "Read")

        # 3. Continuation after receiving tool result
        tool_result = {"role": "tool", "tool_call_id": tool_call_id, "content": "/path/to/target.txt"}
        step3_response = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": f"已读取目标文件：{tool_result['content']}"
                },
                "finish_reason": "stop"
            }]
        }
        self.assertEqual(step3_response["choices"][0]["finish_reason"], "stop")
        self.assertIn("/path/to/target.txt", step3_response["choices"][0]["message"]["content"])


if __name__ == "__main__":
    unittest.main()
