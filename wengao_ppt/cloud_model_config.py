"""wengao_ppt.cloud_model_config —— 可选的云端模型配置（用户显式开启，默认关闭）。

背景：本地模型（mlx-dspark/Ollama 等）受限于本机算力与可用性，在某些场景下
（配图语义复核、长稿一致性核对等）不够稳定。用户可以按自己的需要配置一个
OpenAI 兼容的云端端点（自带 API key），作为语义规划/视觉复核的备选项。

纪律：
- 默认关闭（enabled=false）。只有用户在界面上显式启用并填写端点+key 后才会
  生效；绝不因为检测到本机模型不可用就自动静默切到云端——那是用户自己的钱
  和自己的数据出境决定，必须显式同意。
- 启用云端后，原稿内容（场景原文摘录、图片语义复核的图像字节）会发送给该
  云端服务商；这一点必须在界面上明确提示，不能只在代码注释里写。
- 配置只存在本机用户目录下的 JSON 文件里（不进任务记录、不进版本库）；
  API key 只回显"已配置"，从不回显明文。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

CONFIG_DIR = Path.home() / ".wengao_ppt"
CONFIG_PATH = CONFIG_DIR / "cloud_model.json"

_DEFAULTS = {
    "enabled": False,
    "endpoint": "",       # OpenAI 兼容 base，例如 https://api.openai.com/v1
    "api_key": "",
    "text_model": "",     # 文本/语义规划用的模型名
    "vision_model": "",   # 图片语义复核用的模型名（留空则复用 text_model）
    "image_gen_model": "",  # 云端生图模型名（留空则不启用云端生图，走本机 ComfyUI）
}


def load() -> dict:
    """读取云端模型配置；文件不存在或损坏时返回全关闭的默认值，不抛异常。"""
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(_DEFAULTS)
    if not isinstance(raw, dict):
        return dict(_DEFAULTS)
    cfg = dict(_DEFAULTS)
    for k in _DEFAULTS:
        if k in raw:
            cfg[k] = raw[k]
    cfg["enabled"] = bool(cfg["enabled"])
    for k in ("endpoint", "api_key", "text_model", "vision_model", "image_gen_model"):
        cfg[k] = str(cfg.get(k) or "").strip()
    return cfg


def save(*, enabled: bool, endpoint: str, api_key: Optional[str] = None,
         text_model: str = "", vision_model: str = "", image_gen_model: str = "") -> dict:
    """写入配置。api_key=None 表示"保留已存的 key 不变"（界面重新提交其他字段时
    不强制用户重新粘贴 key）；传空字符串才是真正清空。"""
    current = load()
    cfg = {
        "enabled": bool(enabled),
        "endpoint": (endpoint or "").strip(),
        "api_key": current["api_key"] if api_key is None else str(api_key).strip(),
        "text_model": (text_model or "").strip(),
        "vision_model": (vision_model or "").strip(),
        "image_gen_model": (image_gen_model or "").strip(),
    }
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass
    return status(cfg)


def status(cfg: Optional[dict] = None) -> dict:
    """返回不含明文 key 的状态视图，供界面展示。"""
    cfg = cfg or load()
    return {
        "enabled": cfg["enabled"],
        "endpoint": cfg["endpoint"],
        "api_key_configured": bool(cfg["api_key"]),
        "text_model": cfg["text_model"],
        "vision_model": cfg["vision_model"],
        "image_gen_model": cfg["image_gen_model"],
    }


def active_text_target() -> Optional[dict]:
    """若云端已启用且配置齐全，返回 {endpoint, api_key, model}；否则 None（走本机）。"""
    cfg = load()
    if not cfg["enabled"] or not cfg["endpoint"] or not cfg["api_key"] or not cfg["text_model"]:
        return None
    return {"endpoint": cfg["endpoint"].rstrip("/"), "api_key": cfg["api_key"],
            "model": cfg["text_model"]}


def active_vision_target() -> Optional[dict]:
    cfg = load()
    if not cfg["enabled"] or not cfg["endpoint"] or not cfg["api_key"]:
        return None
    model = cfg["vision_model"] or cfg["text_model"]
    if not model:
        return None
    return {"endpoint": cfg["endpoint"].rstrip("/"), "api_key": cfg["api_key"], "model": model}


def active_image_gen_target() -> Optional[dict]:
    """若云端生图已启用且配置齐全，返回 {endpoint, api_key, model, provider}；
    否则 None（走本机 ComfyUI）。

    云端生图没有统一的 OpenAI 兼容形状（OpenAI 走 /images/generations，Gemini
    走原生 generateContent 返回 inlineData），provider 字段标出要用哪种协议：
    - endpoint 域名含 generativelanguage.googleapis.com → "gemini-native"
      （把配置的 OpenAI 兼容 base 的 /openai 后缀去掉，还原成原生 API base）。
    - 其余一律按 "openai-images" 处理（标准 /images/generations 形状）。
    """
    cfg = load()
    if (not cfg["enabled"] or not cfg["endpoint"] or not cfg["api_key"]
            or not cfg["image_gen_model"]):
        return None
    endpoint = cfg["endpoint"].rstrip("/")
    if "generativelanguage.googleapis.com" in endpoint:
        native_base = endpoint
        if native_base.endswith("/openai"):
            native_base = native_base[: -len("/openai")]
        return {"endpoint": native_base, "api_key": cfg["api_key"],
                "model": cfg["image_gen_model"], "provider": "gemini-native"}
    return {"endpoint": endpoint, "api_key": cfg["api_key"],
            "model": cfg["image_gen_model"], "provider": "openai-images"}
