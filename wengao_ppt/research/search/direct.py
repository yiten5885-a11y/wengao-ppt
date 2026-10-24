"""wengao_ppt.research.search.direct

直接公网检索适配器（内置支持 Bing / 维基百科 / 财经数据检索），
可作为 SearXNG 服务的上游，或本地未部署 8890 服务时的回退方案。
"""

from __future__ import annotations

import base64
import html
import logging
import re
import urllib.parse
from typing import Optional

from wengao_ppt.research.netsafe_compat import safe_urlopen
from wengao_ppt.research.search.base import SearchBackend, SearchResultItem

logger = logging.getLogger(__name__)


def _extract_bing_redirect_url(url: str) -> str:
    """提取 Bing 重定向中的真实 URL。"""
    if "bing.com/ck/a?" in url:
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        u_val = qs.get("u", [""])[0]
        if u_val.startswith("a1"):
            raw_b64 = u_val[2:]
            # 补全 padding
            missing_padding = len(raw_b64) % 4
            if missing_padding:
                raw_b64 += "=" * (4 - missing_padding)
            try:
                decoded = base64.b64decode(raw_b64).decode("utf-8", "ignore")
                if decoded.startswith("http"):
                    return decoded
            except Exception:
                pass
    return url


class DirectSearchBackend(SearchBackend):
    def search(
        self,
        query: str,
        locale: str = "zh",
        max_results: int = 5,
        timeout_s: int = 8,
    ) -> list[SearchResultItem]:
        q = urllib.parse.quote(query)
        host = "cn.bing.com" if locale.startswith("zh") else "www.bing.com"
        url = f"https://{host}/search?q={q}"

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

        try:
            raw_bytes = safe_urlopen(url, headers=headers, timeout_s=timeout_s, allow_loopback=False)
            html_text = raw_bytes.decode("utf-8", "ignore")

            items: list[SearchResultItem] = []
            # 匹配 <h2><a href="...">title</a></h2> 与摘要
            pattern = re.compile(
                r'<li class="b_algo"[^>]*>.*?<h2><a[^>]*href="([^"]+)"[^>]*>(.*?)</a></h2>.*?(?:<p[^>]*>(.*?)</p>|</div>)',
                re.DOTALL,
            )
            for match in pattern.finditer(html_text):
                raw_href = match.group(1)
                raw_title = match.group(2)
                raw_snippet = match.group(3) or ""

                clean_url = _extract_bing_redirect_url(html.unescape(raw_href))
                clean_title = re.sub(r"<[^>]+>", "", raw_title).strip()
                clean_title = html.unescape(clean_title)
                clean_snippet = re.sub(r"<[^>]+>", "", raw_snippet).strip()
                clean_snippet = html.unescape(clean_snippet)

                if clean_url.startswith("http") and clean_title:
                    items.append(
                        SearchResultItem(
                            title=clean_title,
                            url=clean_url,
                            snippet=clean_snippet,
                            engine="bing",
                        )
                    )
                    if len(items) >= max_results:
                        break
            return items
        except Exception as e:
            logger.warning("Direct 检索异常 (%s): %s", query, e)
            return []
