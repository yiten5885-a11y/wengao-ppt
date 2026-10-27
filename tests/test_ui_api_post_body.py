"""FIX-W4-001 回归测试：新界面入口不得消费旧路由的请求体，也不得抢占旧路由。

放置位置：仓库 tests/ 目录下。修复前，test_legacy_post_body_untouched 应失败
（dispatch_post 先读光请求体，或在假 handler 上因 _root_of 报错）。
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ui_api = pytest.importorskip("workbench.ui_api")


class FakeHandler:
    """最小 handler：只提供 dispatch_post 可能触碰的属性；读取会移动 rfile 位置。"""

    def __init__(self, body: bytes):
        self.rfile = io.BytesIO(body)
        self.headers = {"Content-Length": str(len(body))}
        self.sent = None

    def _body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None

    def _send(self, code, obj):
        self.sent = (code, obj)
        return True

    def _err(self, code, err_code, msg):
        self.sent = (code, {"error": {"code": err_code, "message": msg}})
        return True


# server.py do_POST 中旧路由的代表路径（每种写法至少一条）
LEGACY_POST_PATHS = [
    "/api/upload",
    "/api/material/upload",
    "/api/jobs",
    "/api/make",
    "/api/from-material",
    "/api/material/tasks",
    "/api/assets",
    "/api/cleanup",
    "/api/make/job-abc123/cancel",
    "/api/jobs/job-abc123/run",
    "/api/jobs/job-abc123/validate",
    "/api/jobs/job-abc123/native-playback",
    "/api/jobs/job-abc123/edit",
    "/api/material/tasks/mt-0123abcd/cancel",
    "/api/material/tasks/mt-0123abcd/retry",
    "/api/jobs/job-abc123/edits/v0001/render",
    "/api/jobs/job-abc123/edits/v0001/restore",
    "/api/jobs/job-abc123/edits/v0001/adopt",
]

# 当前被新入口抢占的旧路由（README §4-C）。修复后必须交还旧路由，
# 或由新入口按调用方正确分流；若采用后者，请改写本测试并在报告中说明。
SHADOWED_LEGACY_PATHS = [
    "/api/jobs/job-abc123/export",
    "/api/jobs/job-abc123/cancel",
]

BODY = b'{"theme":"wengao-basic","pages":12}'


@pytest.mark.parametrize("path", LEGACY_POST_PATHS)
def test_legacy_post_body_untouched(path):
    h = FakeHandler(BODY)
    handled = ui_api.dispatch_post(h, path)
    assert handled is False, f"{path} 不应由新界面入口处理"
    assert h.rfile.tell() == 0, f"{path} 的请求体被新界面入口读取了 {h.rfile.tell()} 字节"
    assert h.sent is None


@pytest.mark.parametrize("path", SHADOWED_LEGACY_PATHS)
def test_legacy_routes_not_shadowed(path):
    h = FakeHandler(BODY)
    handled = ui_api.dispatch_post(h, path)
    assert handled is False, f"{path} 是旧路由，被新界面入口抢占"
    assert h.rfile.tell() == 0


def test_unknown_path_untouched():
    h = FakeHandler(BODY)
    assert ui_api.dispatch_post(h, "/api/__definitely_not_a_route__") is False
    assert h.rfile.tell() == 0


UI_POST_PATHS = [
    "/api/ui/jobs/job-abc123/export",
    "/api/ui/jobs/job-abc123/cancel",
    "/api/ui/jobs/job-abc123/retry",
    "/api/ui/jobs/job-abc123/page-ops",
    "/api/ui/jobs/job-abc123/decisions",
]


@pytest.mark.parametrize("path", UI_POST_PATHS)
def test_ui_post_routes_recognized(path):
    assert ui_api._owns_post(path) is True


def test_server_handler_body_cache_and_consumed_guard():
    """验证 server.py Handler 上的只读一次保护机制。"""
    from workbench import server

    class MockServerHandler(server.Handler):
        def __init__(self, raw_body: bytes, headers: dict | None = None):
            self.rfile = io.BytesIO(raw_body)
            self.headers = headers or {"Content-Length": str(len(raw_body))}
            self.sent_responses = []

        def _send(self, code, obj, ctype="application/json"):
            self.sent_responses.append((code, obj))

    # 1. 首次读取缓存，后续重复读取不重复移动游标
    h = MockServerHandler(b'{"key": "value"}')
    data1 = h._body_json()
    assert data1 == {"key": "value"}
    assert h.rfile.tell() == 16
    data2 = h._body_json()
    assert data2 == {"key": "value"}
    assert h.rfile.tell() == 16  # 没有再次读取

    # 2. 已被 _body_json 消费后，上传接口调用立即拒绝（500 WB.BODY_ALREADY_CONSUMED）而不是挂起
    h._upload_bytes([".md"], type_code="WB.BAD_MATERIAL", type_message="类型错误")
    assert len(h.sent_responses) == 1
    code, payload = h.sent_responses[0]
    assert code == 500
    assert payload["error"]["code"] == "WB.BODY_ALREADY_CONSUMED"

    # 3. 反之：先上传消费，后调用 _body_json 触发 BodyAlreadyConsumedError
    h2 = MockServerHandler(b'dummy-file-content', headers={"Content-Length": "18", "X-Filename": "test.md"})
    h2._body_consumed = True  # 模拟已流式消费
    with pytest.raises(server.BodyAlreadyConsumedError):
        h2._body_json()


def test_server_handler_body_cache_null_body_sentinel():
    """请求体恰为 JSON null 时，缓存不得把 None 误判为"尚未读取"。

    FIX-W4-001 哨兵加固：_body_json 用 _BODY_UNSET 区分"未读取"与"读到的 null"，
    否则二次读取会误报 BODY_ALREADY_CONSUMED。
    """
    from workbench import server

    class MockServerHandler(server.Handler):
        def __init__(self, raw_body: bytes, headers: dict | None = None):
            self.rfile = io.BytesIO(raw_body)
            self.headers = headers or {"Content-Length": str(len(raw_body))}

    h = MockServerHandler(b'null')
    assert h._body_json() is None
    # 二次读取必须仍返回 None（走缓存），而不是抛 BodyAlreadyConsumedError
    assert h._body_json() is None
    assert h.rfile.tell() == 4


