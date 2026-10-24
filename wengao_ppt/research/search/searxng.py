"""wengao_ppt.research.search.searxng

SearXNG 本地自托管检索适配器。
默认连接 http://127.0.0.1:8890，必须绕过环境变量代理。
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from typing import Optional

from wengao_ppt.research.netsafe_compat import safe_urlopen
from wengao_ppt.research.search.base import SearchBackend, SearchResultItem

logger = logging.getLogger(__name__)

DEFAULT_SEARXNG_URL = "http://127.0.0.1:8890"


class SearXNGBackend(SearchBackend):
    def __init__(self, base_url: str = DEFAULT_SEARXNG_URL):
        self.base_url = base_url.rstrip("/")

    def search(
        self,
        query: str,
        locale: str = "zh",
        max_results: int = 5,
        timeout_s: int = 8,
    ) -> list[SearchResultItem]:
        lang_code = "zh-CN" if locale.startswith("zh") else "en"
        params = {
            "q": query,
            "format": "json",
            "language": lang_code,
            "categories": "general",
        }
        url = f"{self.base_url}/search?{urllib.parse.urlencode(params)}"

        try:
            raw_bytes = safe_urlopen(url, timeout_s=timeout_s, allow_loopback=True)
            data = json.loads(raw_bytes.decode("utf-8"))
            results_raw = data.get("results", [])
            items: list[SearchResultItem] = []
            for r in results_raw[:max_results]:
                u = r.get("url", "")
                t = r.get("title", "")
                s = r.get("content", "") or r.get("snippet", "")
                eng = r.get("engine", "searxng")
                if u and (t or s):
                    items.append(SearchResultItem(title=t, url=u, snippet=s, engine=eng))
            return items
        except Exception as e:
            logger.warning("SearXNG 检索失败 (%s): %s", url, e)
            return []
