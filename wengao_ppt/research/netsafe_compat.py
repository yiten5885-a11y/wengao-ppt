"""wengao_ppt.research.netsafe_compat —— 兼容垫片（Step 0.4）。

网络请求安全层已于 2026-10-24 搬移到 `wengao_ppt/netsafe.py`（规范模块，W4 在
Step 0 抽取）。本文件只做 re-export，保证 W3 窗口及历史 import 路径不破；
**新代码请直接 import wengao_ppt.netsafe**。
"""
from __future__ import annotations

from wengao_ppt.netsafe import (  # noqa: F401
    ALLOWED_LOOPBACK_PORTS,
    DEFAULT_TIMEOUT_S,
    FORBIDDEN_PORTS,
    MAX_FETCH_SIZE,
    NetworkSecurityError,
    UNTRUSTED_HOST_SUFFIXES,
    is_loopback_host,
    safe_urlopen,
    validate_target_url,
)
