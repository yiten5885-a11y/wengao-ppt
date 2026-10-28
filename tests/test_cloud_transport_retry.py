import io
import json
import socket
import ssl
import urllib.error
import unittest
from unittest import mock

from wengao_ppt import scene_model
from wengao_ppt.scene_model import (
    SceneModelError,
    _call_cloud_text_once,
    _call_cloud_vision_once,
)

TARGET = {"endpoint": "https://cloud.example/v1", "api_key": "k", "model": "m-flash"}
OK_BODY = {"choices": [{"message": {"content": "{\"ok\": true}"}, "finish_reason": "stop"}],
           "usage": {}}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ok():
    return _Resp(json.dumps(OK_BODY).encode("utf-8"))


def _ssleof():
    return urllib.error.URLError(ssl.SSLEOFError(8, "EOF occurred in violation of protocol"))


class CloudTransportRetryTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(scene_model.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_vision_review_recovers_from_one_ssleof(self):
        with mock.patch("urllib.request.urlopen", side_effect=[_ssleof(), _ok()]) as op:
            result = _call_cloud_vision_once(TARGET, "p", "aGk=", 60)
        self.assertEqual(op.call_count, 2)
        self.assertEqual(result["response"], "{\"ok\": true}")

    def test_text_call_recovers_from_raw_ssl_error_during_read(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=[ssl.SSLEOFError(8, "EOF"), _ok()]) as op:
            result = _call_cloud_text_once(TARGET, "p", 60, num_predict=64)
        self.assertEqual(op.call_count, 2)
        self.assertEqual(result["done_reason"], "stop")

    def test_persistent_ssleof_fails_after_bounded_attempts(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=[_ssleof() for _ in range(5)]) as op:
            with self.assertRaises(SceneModelError) as ctx:
                _call_cloud_vision_once(TARGET, "p", "aGk=", 60)
        self.assertEqual(op.call_count, scene_model._CLOUD_TRANSIENT_ATTEMPTS)
        self.assertEqual(ctx.exception.code, "MODEL.TRANSPORT_ERROR")
        self.assertIn("SSLEOFError", ctx.exception.detail)

    def test_timeout_is_not_retried(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError(socket.timeout("t"))) as op:
            with self.assertRaises(SceneModelError) as ctx:
                _call_cloud_vision_once(TARGET, "p", "aGk=", 60)
        self.assertEqual(op.call_count, 1)
        self.assertEqual(ctx.exception.code, "MODEL.REQUEST_TIMEOUT")

    def test_http_error_is_not_retried(self):
        err = urllib.error.HTTPError(TARGET["endpoint"], 401, "no", {}, io.BytesIO(b"bad key"))
        with mock.patch("urllib.request.urlopen", side_effect=err) as op:
            with self.assertRaises(SceneModelError) as ctx:
                _call_cloud_vision_once(TARGET, "p", "aGk=", 60)
        self.assertEqual(op.call_count, 1)
        self.assertEqual(ctx.exception.code, "MODEL.AUTH_FAILED")


if __name__ == "__main__":
    unittest.main()
