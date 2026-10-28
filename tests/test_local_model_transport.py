"""本机模型传输层：断连分类、协议缓存、健康门重试、开跑前预检。

2026-10-28 job-3afdd5d88bd9：semantic_scenes 跑了 5 分钟后失败，页面显示
``MODEL.TRANSPORT_ERROR · Remote end closed connection without response``，
provider 被写成 ``ollama-local``（实际是 mlx-dspark）。这里用一个可编排行为的
本机假服务，把"读响应阶段被对端断开"这条路径固定下来：必须是中文 detail、
带端点、provider 是 local:host:port；重试要有健康门；视觉端点缺失要在开跑前
就失败，而不是跑完文本规划才崩。
"""
from __future__ import annotations

import json
import os
import socket
import socketserver
import threading
import time
import unittest
import urllib.error
from unittest import mock

from pptx import Presentation
from pptx.util import Inches

from wengao_ppt import cloud_model_config, scene_model
from wengao_ppt.scene_model import (
    SceneModelError,
    _call_endpoint_once,
    _call_json_payload_with_transport_retry,
    preflight_scene_models,
    reset_endpoint_protocol_cache,
)

_OK_BODY = {
    "response": '{"scenes": []}',
    "content": '{"scenes": []}',
    "model": "fake-local-model",
    "done_reason": "stop",
    "prompt_eval_count": 11,
    "eval_count": 7,
    "timings": {"prompt_n": 11, "predicted_n": 7},
    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    "choices": [{"message": {"content": '{"scenes": []}'}, "finish_reason": "stop"}],
    "status": "ok",
}


class _FakeModelHandler(socketserver.StreamRequestHandler):
    """最小 HTTP/1.1 读取器：按服务端脚本决定回包、断开或挂住。"""

    def finish(self):
        # 主动断开连接时 wfile 已不可用，缓冲区 flush 失败不影响结论。
        try:
            super().finish()
        except Exception:  # noqa: BLE001
            pass

    def handle(self):
        server = self.server
        with server.lock:
            server.accepted += 1
            server.active += 1
        try:
            request_line = self.rfile.readline(65536)
            if not request_line:
                return
            parts = request_line.decode("latin-1").split()
            method, path = parts[0], parts[1]
            length = 0
            while True:
                header = self.rfile.readline(65536)
                if header in (b"\r\n", b"\n", b""):
                    break
                name, _, value = header.partition(b":")
                if name.strip().lower() == b"content-length":
                    try:
                        length = int(value.strip())
                    except ValueError:
                        length = 0
            if length:
                self.rfile.read(length)
            with server.lock:
                server.requests.append((method, path))
                if method == "GET":
                    action = server.get_action
                elif server.post_actions:
                    action = server.post_actions.pop(0)
                else:
                    action = server.default_post_action
                if (method == "POST" and action == "ok"
                        and server.ok_post_paths is not None
                        and path not in server.ok_post_paths):
                    action = "not_found"
            if action == "close":
                # 不回任何字节就断开：客户端在"读响应头"阶段收到 RemoteDisconnected。
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return
            if action == "hang":
                time.sleep(server.hang_seconds)
                return
            if action == "not_found":
                self._send(404, {"error": "not found"})
                return
            self._send(200, dict(_OK_BODY))
        finally:
            with server.lock:
                server.active -= 1

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        head = ("HTTP/1.1 %d %s\r\nContent-Type: application/json\r\n"
                "Content-Length: %d\r\nConnection: close\r\n\r\n"
                % (status, "OK" if status == 200 else "Error", len(body))).encode("latin-1")
        try:
            self.wfile.write(head + body)
            self.wfile.flush()
        except OSError:
            pass


class _FakeModelServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _FakeModelHandler)
        self.lock = threading.Lock()
        self.requests: list[tuple[str, str]] = []
        self.accepted = 0
        self.active = 0
        self.post_actions: list[str] = []
        self.default_post_action = "ok"
        self.get_action = "ok"
        self.ok_post_paths = None
        self.hang_seconds = 2.0

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self.port

    def posts(self) -> list[str]:
        return [path for method, path in self.requests if method == "POST"]


class _LocalTransportTest(unittest.TestCase):
    def setUp(self):
        reset_endpoint_protocol_cache()
        self.addCleanup(reset_endpoint_protocol_cache)
        # 不依赖本机真实配置：云端一律视为关闭，除非某个用例自己打开。
        for name in ("active_text_target", "active_vision_target"):
            patcher = mock.patch.object(cloud_model_config, name, return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)
        for key in ("WENGAO_LOCAL_MODEL_ENDPOINT", "WENGAO_LOCAL_VISION_MODEL_ENDPOINT",
                    "WENGAO_FALLBACK_MODEL_ENDPOINT"):
            if key in os.environ:
                saved = os.environ.pop(key)
                self.addCleanup(os.environ.__setitem__, key, saved)

    def start_server(self, **attributes) -> _FakeModelServer:
        server = _FakeModelServer()
        for key, value in attributes.items():
            setattr(server, key, value)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def fast_retry(self):
        """把退避与健康等待压到毫秒级：验证逻辑，不验证真实等待时长。"""
        return mock.patch.multiple(
            scene_model,
            _LOCAL_TRANSPORT_BACKOFF_S=(0.0, 0.0),
            _LOCAL_HEALTH_PROBE_INTERVAL_S=0.01,
            _LOCAL_HEALTH_WAIT_S=0.3)

    def wait_until_idle(self, server: _FakeModelServer, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if server.active == 0:
                return True
            time.sleep(0.01)
        return server.active == 0


class RemoteDisconnectTests(_LocalTransportTest):
    def test_closed_connection_is_chinese_transport_error_with_endpoint(self):
        server = self.start_server(default_post_action="close")
        with self.assertRaises(SceneModelError) as caught:
            _call_endpoint_once(server.url, "fake-model", "prompt", 8)

        err = caught.exception
        self.assertEqual(err.code, "MODEL.TRANSPORT_ERROR")
        self.assertIn("RemoteDisconnected", err.detail)
        self.assertIn("127.0.0.1:%d" % server.port, err.detail)
        self.assertIn("连接被中断", err.detail)
        record = err.as_record()
        self.assertEqual(record["provider"], "local:127.0.0.1:%d" % server.port)
        self.assertNotEqual(record["provider"], "ollama-local")
        self.assertEqual(record["diagnostics"]["endpoint"], "127.0.0.1:%d" % server.port)

    def test_two_disconnects_then_success_recovers_and_records_attempts(self):
        server = self.start_server(post_actions=["close", "close"])
        with self.fast_retry():
            payload, meta, _raw = _call_json_payload_with_transport_retry(
                "fake-model", "prompt",
                lambda model, prompt: _call_endpoint_once(server.url, model, prompt, 8),
                phase="规划")
        self.assertEqual(payload, {"scenes": []})
        retry = meta.get("transport_retry") or {}
        self.assertEqual(retry.get("attempts"), 3)
        self.assertEqual(len(retry.get("failed_attempts") or []), 2)
        self.assertEqual(
            [row["exception"] for row in retry["failed_attempts"]],
            ["RemoteDisconnected", "RemoteDisconnected"])
        self.assertEqual(server.posts(), ["/api/generate"] * 3)

    def test_persistent_disconnect_reports_retry_count(self):
        server = self.start_server(default_post_action="close")
        with self.fast_retry():
            with self.assertRaises(SceneModelError) as caught:
                _call_json_payload_with_transport_retry(
                    "fake-model", "prompt",
                    lambda model, prompt: _call_endpoint_once(server.url, model, prompt, 8),
                    phase="规划")
        err = caught.exception
        self.assertEqual(err.code, "MODEL.TRANSPORT_ERROR")
        self.assertIn("已重试 3 次", err.detail)
        self.assertEqual(len(err.diagnostics.get("attempts") or []), 3)
        self.assertEqual(len(server.posts()), 3)

    def test_timeout_is_not_retried(self):
        server = self.start_server(default_post_action="hang", hang_seconds=1.5)
        with self.assertRaises(SceneModelError) as caught:
            _call_endpoint_once(server.url, "fake-model", "prompt", 0.4)
        self.assertEqual(caught.exception.code, "MODEL.REQUEST_TIMEOUT")

        reset_endpoint_protocol_cache()
        with self.fast_retry():
            with self.assertRaises(SceneModelError) as retried:
                _call_json_payload_with_transport_retry(
                    "fake-model", "prompt",
                    lambda model, prompt: _call_endpoint_once(server.url, model, prompt, 0.4),
                    phase="规划")
        self.assertEqual(retried.exception.code, "MODEL.REQUEST_TIMEOUT")
        self.assertEqual(len(server.posts()), 2)

    def test_unrecovered_health_stops_retrying(self):
        server = self.start_server(default_post_action="close", get_action="close")
        with self.fast_retry():
            with self.assertRaises(SceneModelError) as caught:
                _call_json_payload_with_transport_retry(
                    "fake-model", "prompt",
                    lambda model, prompt: _call_endpoint_once(server.url, model, prompt, 8),
                    phase="规划")
        err = caught.exception
        self.assertEqual(err.code, "MODEL.TRANSPORT_ERROR")
        self.assertIn("未恢复", err.detail)
        self.assertEqual(len(server.posts()), 1)


class ProtocolCacheTests(_LocalTransportTest):
    def test_second_call_reuses_the_successful_protocol(self):
        server = self.start_server(ok_post_paths={"/v1/chat/completions"})
        first = _call_endpoint_once(server.url, "fake-model", "p", 8)
        self.assertEqual(first["response"], '{"scenes": []}')
        self.assertEqual(server.posts(), ["/api/generate", "/completion",
                                          "/v1/chat/completions"])

        before = len(server.requests)
        _call_endpoint_once(server.url, "fake-model", "p", 8)
        second_round = [path for method, path in server.requests[before:]
                        if method == "POST"]
        self.assertEqual(second_round, ["/v1/chat/completions"])

    def test_probe_responses_are_closed(self):
        server = self.start_server(ok_post_paths={"/v1/chat/completions"})
        closed: list[int] = []
        original_close = urllib.error.HTTPError.close

        def _spy(http_error):
            closed.append(int(http_error.code))
            return original_close(http_error)

        with mock.patch.object(urllib.error.HTTPError, "close", _spy):
            _call_endpoint_once(server.url, "fake-model", "p", 8)
        self.assertEqual(closed, [404, 404])
        self.assertTrue(self.wait_until_idle(server))
        self.assertEqual(server.accepted, 3)


class PreflightTests(_LocalTransportTest):
    def _dead_port(self) -> int:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        return port

    def test_vision_endpoint_missing_fails_before_any_text_planning(self):
        text_server = self.start_server()
        dead_port = self._dead_port()
        calls: list[str] = []

        def counting_call(model, prompt):
            calls.append(prompt)
            return {"response": "{}"}

        with mock.patch.dict(os.environ, {
                "WENGAO_LOCAL_MODEL_ENDPOINT": text_server.url,
                "WENGAO_LOCAL_VISION_MODEL_ENDPOINT":
                    "http://127.0.0.1:%d" % dead_port}):
            started = time.monotonic()
            with self.assertRaises(SceneModelError) as caught:
                preflight_scene_models(needs_vision=True)
            elapsed = time.monotonic() - started

        err = caught.exception
        self.assertEqual(err.code, "MODEL.VISION_RUNTIME_UNAVAILABLE")
        self.assertIn("8080", err.detail)
        self.assertIn("云端视觉", err.detail)
        self.assertLess(elapsed, 1.0)
        self.assertEqual(err.diagnostics.get("next_actions"),
                         list(scene_model._VISION_UNAVAILABLE_NEXT_ACTIONS))
        # 预检没有发任何文本规划请求（也没有调用任何模型）。
        self.assertEqual(text_server.posts(), [])
        self.assertEqual(calls, [])

    def test_text_endpoint_missing_fails_fast_with_runtime_unavailable(self):
        dead_port = self._dead_port()
        with mock.patch.dict(os.environ, {
                "WENGAO_LOCAL_MODEL_ENDPOINT": "http://127.0.0.1:%d" % dead_port}):
            started = time.monotonic()
            with self.assertRaises(SceneModelError) as caught:
                preflight_scene_models(needs_vision=False)
            elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.code, "MODEL.RUNTIME_UNAVAILABLE")
        self.assertLess(elapsed, 4.0)

    def test_memory_pressure_is_recorded_but_not_blocking(self):
        pressure = {"status": "ok", "model": "fake-local-model",
                    "warnings": [{"code": "memory_pressure", "level": "attention",
                                  "message": "macOS memory pressure is WARN"}]}
        server = self.start_server()
        with mock.patch.dict(os.environ,
                             {"WENGAO_LOCAL_MODEL_ENDPOINT": server.url}), \
                mock.patch.object(scene_model, "_probe_endpoint_health",
                                  return_value=(True, pressure)):
            record = preflight_scene_models(needs_vision=False)
        self.assertEqual(record["model_health"]["memory_pressure"], "attention")
        self.assertEqual(record["text"]["available"], True)


class PlanPreflightOrderingTests(_LocalTransportTest):
    """真实入口：视觉端点缺失时，semantic_scenes 在模型调用之前就失败。"""

    def _tiny_pptx(self, path) -> None:
        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        box = slide.shapes.add_textbox(Inches(0.6), Inches(0.6), Inches(6), Inches(1.2))
        box.text_frame.text = "客户支持团队的响应流程"
        prs.save(str(path))

    def test_build_plan_fails_before_planning_when_vision_is_missing(self):
        import tempfile
        from pathlib import Path

        from wengao_ppt import semantic_scenes

        # 文本端点指向假服务：如果预检没拦住，规划会真的发出 POST，测试就能
        # 用"服务端一个 POST 都没收到"证明失败发生在任何模型调用之前。
        text_server = self.start_server()

        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        dead_port = dead.getsockname()[1]
        dead.close()

        with tempfile.TemporaryDirectory() as tmp:
            pptx = Path(tmp) / "tiny.pptx"
            self._tiny_pptx(pptx)
            with mock.patch.dict(os.environ, {
                    "WENGAO_LOCAL_MODEL_ENDPOINT": text_server.url,
                    "WENGAO_LOCAL_VISION_MODEL_ENDPOINT":
                        "http://127.0.0.1:%d" % dead_port}):
                started = time.monotonic()
                with self.assertRaises(SceneModelError) as caught:
                    semantic_scenes.build_plan(pptx, cache_dir=Path(tmp) / "assets")
                elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.code, "MODEL.VISION_RUNTIME_UNAVAILABLE")
        self.assertLess(elapsed, 1.0)
        self.assertEqual(text_server.posts(), [])


if __name__ == "__main__":
    unittest.main()
