"""fontenv 绝对路径加固回归（2026-11-04 job-293501ae3698 v3/v4 事故）。

事故链：驱动传相对 project_root（如 `--project-root .`）→ fontenv 写出相对
FONTCONFIG_FILE/XDG_CACHE_HOME → macOS soffice wrapper 启动 soffice.bin 前
不保证保持 cwd → 相对路径失配 → fontconfig 静默回退默认配置 → 全部中文
画成方框、退出码仍 0。修复：build_env 对 data_root 先 resolve()。

本文件锁定契约：**无论调用方传什么形态的 root（绝对/相对/字符串/环境变量），
build_env 产出的 FONTCONFIG_FILE、XDG_CACHE_HOME 与 record.data_root 都必须是
绝对路径**，且不依赖调用方 cwd。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from wengao_ppt import fontenv

_PATH_KEYS = ("FONTCONFIG_FILE", "XDG_CACHE_HOME")


@pytest.fixture(autouse=True)
def _clean_fontenv_env(monkeypatch, tmp_path):
    """隔离外部环境：OVERRIDE/DISABLE/数据根环境变量一律清空，cwd 进 tmp。"""
    for var in (fontenv.OVERRIDE_ENV, fontenv.DISABLE_ENV, "WENGAO_DATA_ROOT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)


def test_relative_path_root_yields_absolute_env(monkeypatch, tmp_path):
    """v3/v4 事故的直接复现条件：调用方传 `Path(".")`。"""
    monkeypatch.chdir(tmp_path)
    env, rec = fontenv.build_env(Path("."))
    assert rec["ok"] is True
    assert rec["data_root"] == str(tmp_path)  # resolve 后与 cwd 无关的绝对形态
    for k in _PATH_KEYS:
        assert k in env, f"缺少 {k}"
        assert Path(env[k]).is_absolute(), f"{k} 必须是绝对路径，得到 {env[k]!r}"
        assert Path(env[k]).exists(), f"{k} 指向的文件/目录应已写出"
    # 落盘位置必须在解析后的数据根下，而不是 cwd 的某个歧义位置
    assert Path(env["FONTCONFIG_FILE"]).parent.parent == tmp_path


def test_relative_string_root_yields_absolute_env(monkeypatch, tmp_path):
    """字符串形态的相对 root（CLI 直传 `"."` 的真实写法）。"""
    monkeypatch.chdir(tmp_path)
    env, rec = fontenv.build_env(".")
    assert rec["ok"] is True
    for k in _PATH_KEYS:
        assert Path(env[k]).is_absolute(), f"{k} 必须是绝对路径，得到 {env[k]!r}"
    assert rec["data_root"] == str(tmp_path)


def test_relative_root_via_env_var_yields_absolute(monkeypatch, tmp_path):
    """WENGAO_DATA_ROOT 传相对值时同样必须解析为绝对路径。"""
    monkeypatch.setenv("WENGAO_DATA_ROOT", "some/relative/root")
    env, rec = fontenv.build_env(None)
    assert rec["ok"] is True
    expected = (tmp_path / "some/relative/root").resolve()
    assert rec["data_root"] == str(expected)
    for k in _PATH_KEYS:
        assert Path(env[k]).is_absolute(), f"{k} 必须是绝对路径，得到 {env[k]!r}"


def test_absolute_root_unchanged(monkeypatch, tmp_path):
    """既有行为不回归：绝对 root 下产出一致且仍为绝对路径。"""
    env, rec = fontenv.build_env(tmp_path)
    assert rec["ok"] is True
    assert rec["data_root"] == str(tmp_path)
    for k in _PATH_KEYS:
        assert Path(env[k]).is_absolute()
        assert Path(env[k]).exists()


def test_effective_conf_contains_platform_dirs(monkeypatch, tmp_path):
    """写出的有效配置必须注入平台目录（豆腐块事故的另一道防线）。"""
    monkeypatch.chdir(tmp_path)
    env, rec = fontenv.build_env(Path("."))
    text = Path(env["FONTCONFIG_FILE"]).read_text(encoding="utf-8")
    assert fontenv.PLATFORM_MARKER not in text
    assert any(f"<dir>{d}</dir>" in text for d in fontenv.platform_font_dirs())
