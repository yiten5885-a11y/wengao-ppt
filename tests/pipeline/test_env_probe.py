"""tests/pipeline/test_env_probe.py —— 本机依赖探测测试（只探测不安装）。"""
from __future__ import annotations

from wengao_ppt import env_probe


def test_probe_all_shape():
    rep = env_probe.probe_all()
    assert rep["schema"] == "rs.env_probe.v1"
    keys = [r["key"] for r in rep["rows"]]
    for expect in ("ollama", "searxng", "libreoffice", "comfyui",
                   "vision_model", "fonts"):
        assert expect in keys, f"缺少探测项 {expect}"
    for r in rep["rows"]:
        assert isinstance(r["ok"], bool)
        assert isinstance(r["detail"], str) and r["detail"]


def test_probe_never_installs():
    """探测函数只读：不创建任何目录/文件（用不存在的主机也不会写盘）。"""
    before = set(env_probe.__dict__)
    env_probe.probe_searxng()
    env_probe.probe_comfyui()
    env_probe.probe_vision_model()
    env_probe.probe_fonts()
    assert set(env_probe.__dict__) == before


def test_fonts_probe_finds_pingfang():
    row = env_probe.probe_fonts()
    # 本机 macOS：PingFang 必然存在；探测为真
    assert row["ok"] is True
    assert row["found"]


def test_as_text_output():
    text = env_probe.as_text(env_probe.probe_all())
    assert "本机依赖探测" in text
    assert "✓" in text or "✗" in text
