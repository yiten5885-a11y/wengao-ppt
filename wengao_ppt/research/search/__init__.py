"""wengao_ppt.research.search

检索后端模块导出与统一入口。
"""

from __future__ import annotations

import logging
from typing import Optional

from wengao_ppt.research.search.base import SearchBackend, SearchResultItem
from wengao_ppt.research.search.direct import DirectSearchBackend
from wengao_ppt.research.search.mock import MockSearchBackend
from wengao_ppt.research.search.searxng import DEFAULT_SEARXNG_URL, SearXNGBackend

logger = logging.getLogger(__name__)


class ResilientSearchBackend(SearchBackend):
    """弹性检索器：优先请求本机 SearXNG (8890)，若未起服务或空则回退至 Direct 备用源。"""

    def __init__(self, searxng_url: str = DEFAULT_SEARXNG_URL):
        self.searxng = SearXNGBackend(searxng_url)
        self.direct = DirectSearchBackend()

    def search(
        self,
        query: str,
        locale: str = "zh",
        max_results: int = 5,
        timeout_s: int = 8,
    ) -> list[SearchResultItem]:
        # 1. 优先尝试本地 SearXNG
        results = self.searxng.search(query, locale=locale, max_results=max_results, timeout_s=min(timeout_s, 3))
        if results:
            return results

        # 2. 回退到直接安全检索
        return self.direct.search(query, locale=locale, max_results=max_results, timeout_s=timeout_s)


__all__ = [
    "SearchBackend",
    "SearchResultItem",
    "SearXNGBackend",
    "DirectSearchBackend",
    "MockSearchBackend",
    "ResilientSearchBackend",
]
