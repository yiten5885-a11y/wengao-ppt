"""wengao_ppt.research.llm

Qwen 本地模型调用封装：
- 严格调用本机 loopback 端点 (127.0.0.1:11434 / 127.0.0.1:18888)
- 环回绕过系统代理 (ProxyHandler({}))
- 接入 resource_lock("llm")
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 兼容 resource_lock：优先导入全局锁，不存在则回退至本地锁
_local_llm_lock = threading.Lock()


@contextlib.contextmanager
def acquire_llm_lock():
    try:
        from wengao_ppt.resource_lock import resource_lock  # type: ignore
        with resource_lock("llm"):
            yield
    except ImportError:
        with _local_llm_lock:
            yield


def get_local_endpoint() -> str:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    # 18088：本机加速推理框架（mlx-dspark，OpenAI 兼容 /v1/chat/completions）；
    # 18888：通用 llama.cpp 风格端点；11434：Ollama。按此优先级探测首个可用端口。
    for cand in ("http://127.0.0.1:18088", "http://127.0.0.1:18888", "http://127.0.0.1:11434"):
        try:
            req = urllib.request.Request(cand + "/api/tags" if ":11434" in cand else cand + "/health", method="GET")
            with opener.open(req, timeout=1.5) as r:
                if r.status in (200, 404):
                    return cand
        except Exception:
            continue
    return "http://127.0.0.1:18088"


def call_llm_json(
    prompt: str,
    system_prompt: str = "",
    model: str = "qwen3.8:27b",
    timeout_s: int = 45,
    num_predict: int = 2048,
) -> Optional[dict[str, Any]]:
    endpoint = get_local_endpoint()
    payload: dict[str, Any] = {
        "model": model,
        "stream": False,
        "prompt": prompt,
        "format": "json",
        "options": {
            "temperature": 0.1,
            "num_predict": num_predict,
        },
        "keep_alive": "15m",
    }
    if system_prompt:
        payload["system"] = system_prompt

    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(f"{endpoint}/api/generate", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    openai_payload: dict[str, Any] = {
        "model": model,
        "messages": ([{"role": "system", "content": system_prompt}] if system_prompt else [])
                    + [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": num_predict,
        "stream": False,
        "response_format": {"type": "json_object"},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    openai_req = urllib.request.Request(
        f"{endpoint}/v1/chat/completions",
        data=json.dumps(openai_payload, ensure_ascii=False).encode("utf-8"),
        method="POST")
    openai_req.add_header("Content-Type", "application/json")

    with acquire_llm_lock():
        try:
            with opener.open(req, timeout=timeout_s) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                response_str = result.get("response", "").strip()
                if not response_str:
                    return None
                return json.loads(response_str)
        except urllib.error.HTTPError as e:
            if e.code != 404:
                logger.warning("LLM JSON 调用失败: %s", e)
                return None
            # 既非 Ollama 路由：尝试 OpenAI 兼容 /v1/chat/completions
            # （本机加速推理框架 mlx-dspark 等采用此格式）。
            try:
                with opener.open(openai_req, timeout=timeout_s) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                    choices = result.get("choices") or []
                    message = (choices[0].get("message") or {}) if choices else {}
                    response_str = str(message.get("content") or "").strip()
                    if not response_str:
                        return None
                    return json.loads(response_str)
            except Exception as e2:
                logger.warning("LLM JSON 调用失败: %s", e2)
                return None
        except Exception as e:
            logger.warning("LLM JSON 调用失败: %s", e)
            return None
