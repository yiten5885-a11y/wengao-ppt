"""tests/test_workbench_vision_badge.py —— 视觉主题的可用性要在**选择之前**可见。

背景（2026-11-04）：用户要求「视觉主题在本安装不可用，那就重新安装或者让安装
变得可用」。处理后本安装云端视觉已实测打通（vision_model=gemini-3.8-flash，
生图 gemini-2.5-flash-image；见 reports/vision-enable-20261104/）。

这三条守卫保证工作台不再回退成「选了主题、运行阶段才第一次报错」的旧体验：
1. /api/bootstrap 必须带 vision 摘要，且与 probe_vision_endpoint 的真实判定一致；
   视觉升级主题必须带 visual_upgrade 标记（前端徽标与分组的判断依据）。
2. 前端静态接线：徽标样式类、动态 hint 容器、S.boot.vision 引用缺一不可。
3. _vision_summary 探测抛任何异常时都不得把 bootstrap 炸成 500。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wengao_ppt import scene_model  # noqa: E402

HTML = (ROOT / "workbench" / "index.html").read_text(encoding="utf-8")


def _expected_available() -> bool:
    """测试进程自己 probe 一次，作为 bootstrap 必须复现的真值。"""
    return bool(scene_model.probe_vision_endpoint(timeout_s=1.5).get("available"))


def test_bootstrap_reports_vision_and_visual_upgrade_flag(tmp_path, monkeypatch):
    """直接驱动 _bootstrap 组装逻辑：vision 与 probe 一致；visual-dark 带标记。

    不走 socket：本会话沙箱代理（HTTP_PROXY=127.0.0.1:…）下 pytest 进程对
    回环 HTTP 不可靠（同样请求 curl 2s 即回、pytest 内反复超时；传输层由
    scripts/browser_experience.py 的真 Chrome 走查兜底）。_bootstrap 的组装
    只依赖 self._send / self._vision_summary，用 stub 捕获即可测到真实组装。
    """
    monkeypatch.setenv("WENGAO_DATA_ROOT", str(tmp_path))
    monkeypatch.syspath_prepend(str(ROOT / "workbench"))   # make_flow 与 server 同目录
    spec = importlib.util.spec_from_file_location(
        "wb_server_for_badge_http", ROOT / "workbench" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    captured = {}

    class _StubHandler:
        _vision_summary = mod.Handler._vision_summary

        def _send(self, code, payload):
            captured["code"] = code
            captured["payload"] = payload

    mod.Handler._bootstrap(_StubHandler())   # 真实组装逻辑，只 stub 掉响应写出

    assert captured["code"] == 200
    data = captured["payload"]
    vis = data.get("vision")
    assert isinstance(vis, dict), "bootstrap 缺少 vision 摘要"
    assert isinstance(vis.get("available"), bool), "vision.available 必须是布尔"
    assert vis["available"] is _expected_available(), \
        "bootstrap.vision.available 与 probe_vision_endpoint 的判定不一致"
    assert isinstance(vis.get("mode"), str) and isinstance(vis.get("provider"), str)

    dark = next(t for t in data["themes"] if t["id"] == "wengao-visual-dark")
    assert dark["visual_upgrade"] is True, \
        "前端徽标/分组依赖 visual_upgrade 标记，visual-dark 必须带它"


def test_frontend_badge_wiring_present():
    """前端三处接线：徽标样式类、动态 hint 容器、S.boot.vision 引用。"""
    assert ".needvision" in HTML, "缺少视觉主题不可用徽标样式"
    assert "themes-visual-upgrade-hint" in HTML, "缺少视觉升级分组动态 hint 容器"
    assert "S.boot.vision" in HTML, "renderThemes/themeCard 未读取 bootstrap.vision"


def test_vision_summary_never_raises(tmp_path, monkeypatch):
    """探测抛异常时 _vision_summary 必须降级为 available=false，不得炸 bootstrap。"""
    monkeypatch.setenv("WENGAO_DATA_ROOT", str(tmp_path))
    monkeypatch.syspath_prepend(str(ROOT / "workbench"))   # make_flow 与 server 同目录
    spec = importlib.util.spec_from_file_location(
        "wb_server_for_badge_test", ROOT / "workbench" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    from wengao_ppt import scene_model as sm
    monkeypatch.setattr(sm, "probe_vision_endpoint",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = mod.Handler._vision_summary(object())   # 不用 self，直接以裸对象调用
    assert out == {"available": False, "mode": "unknown", "provider": ""}
